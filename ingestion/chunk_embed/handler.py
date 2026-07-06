"""EmbedDocument Lambda — ページ Markdown を日本語チャンク化し Bedrock で埋め込む。

チャンクはページ境界・節境界をまたがず、本文に breadcrumb（文書 > ページ > 節）を
前置する。query 側は input_type="search_query" を使うこと。

S3 キー（md と同じ {domain}/{連番}_{title} フォルダ構成、1 ページ = 1 JSON）:
- 入力 : md/{domain}/{連番}_{title}/p{n}.md （parse の出力。PageRef.md_key 経由）
- 出力 : embeddings/{domain}/{連番}_{title}/p{n}.json
         （そのページのチャンク配列。各要素 = models.Chunk = {text, metadata, embedding[]}）
入出力は models.EmbedInput / models.EmbedOutput、チャンクは models.Chunk を参照。
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass

from pydantic import ValidationError

import common
import config
import models

logger = common.get_logger(__name__)
cfg = config.embed  # 環境変数ベースの設定（モデル・チャンク・プレフィックス）

_H1_RE = re.compile(r"^#\s+(.*)")  # ページ見出し
_H2_RE = re.compile(r"^##\s+(.*)")  # 節見出し
_NUM_PREFIX_RE = re.compile(r"^\d+[_\-\s]+")  # ファイル名先頭の "02_" 等


def doc_title_from(source_file: str) -> str:
    """ファイル名 → 文書タイトル（拡張子と先頭番号を除去）。"""
    stem = source_file.rsplit(".", 1)[0]
    return _NUM_PREFIX_RE.sub("", stem).strip()


def split_sections(markdown: str) -> tuple[str | None, list[tuple[str | None, str]]]:
    """Markdown を (page_title, [(section, body), ...]) に分解。

    # → page_title、## → 節見出し。PP-DocLayout がページ主タイトルを ## と誤判定する
    事があるため、# が無く先頭が本文を持たない見出しは page_title に昇格させる。
    """
    page_title: str | None = None
    sections: list[tuple[str | None, str]] = []
    cur_name: str | None = None
    cur_lines: list[str] = []

    def flush() -> None:
        body = "\n".join(cur_lines).strip()
        if body or cur_name is not None:  # 本文無しの見出しも一旦保持（後で昇格/除去）
            sections.append((cur_name, body))

    for line in markdown.splitlines():
        if _H2_RE.match(line):
            flush()
            cur_name, cur_lines = _H2_RE.match(line).group(1).strip(), []
            continue
        m1 = _H1_RE.match(line)
        if m1:
            page_title = m1.group(1).strip()
            continue
        cur_lines.append(line)
    flush()

    # # が無く、先頭が本文無しの見出し → page_title に昇格（## 誤判定対策）
    if page_title is None and sections and sections[0][0] and not sections[0][1]:
        page_title = sections[0][0]
        sections = sections[1:]
    # 本文を持たない見出しのみの節は捨てる（空 chunk 防止）
    sections = [(name, body) for name, body in sections if body]
    return page_title, sections


def _breadcrumb(*levels: str | None) -> str:
    return " > ".join(lv for lv in levels if lv)


# Recursive splitter の区切り（大→小）。日本語の句読点・空白を含む。
_RECURSIVE_SEPARATORS = ["\n\n", "\n", "。", "．", "！", "？", "!", "?", "、", " "]


def _split_keep_separator(text: str, sep: str) -> list[str]:
    """sep の直後で分割し、区切り文字は手前側へ残す（再結合で原文を保つ）。"""
    return [p for p in re.split(f"(?<={re.escape(sep)})", text) if p]


def _merge_splits(splits: list[str], max_tokens: int, overlap_tokens: int) -> list[str]:
    """小片を max_tokens まで貪欲に詰め、末尾を overlap として次へ繰り越す。"""
    chunks: list[str] = []
    cur: list[str] = []
    total = 0
    for piece in splits:
        plen = common.estimate_tokens_ja(piece)
        if cur and total + plen > max_tokens:
            chunks.append("".join(cur).strip())
            carry, carry_tokens = [], 0
            for prev in reversed(cur):
                pt = common.estimate_tokens_ja(prev)
                if carry_tokens + pt > overlap_tokens:
                    break
                carry.insert(0, prev)
                carry_tokens += pt
            cur, total = carry, carry_tokens
        cur.append(piece)
        total += plen
    if cur:
        chunks.append("".join(cur).strip())
    return [c for c in chunks if c]


def _split_recursive(
    text: str, separators: list[str], max_tokens: int, overlap_tokens: int
) -> list[str]:
    """区切りを大→小へ再帰適用。最大の意味単位を保ちつつ max_tokens に収める。"""
    if common.estimate_tokens_ja(text) <= max_tokens:
        return [text]
    sep, rest = "", []
    for i, s in enumerate(separators):
        if s in text:
            sep, rest = s, separators[i + 1:]
            break
    if not sep:  # これ以上区切れない → 文字単位で強制分割
        return _merge_splits(list(text), max_tokens, overlap_tokens)

    out: list[str] = []
    good: list[str] = []
    for piece in _split_keep_separator(text, sep):
        if common.estimate_tokens_ja(piece) <= max_tokens:
            good.append(piece)
        else:  # まだ大きい小片は次の区切りで再帰分割
            if good:
                out.extend(_merge_splits(good, max_tokens, overlap_tokens))
                good = []
            out.extend(_split_recursive(piece, rest, max_tokens, overlap_tokens))
    if good:
        out.extend(_merge_splits(good, max_tokens, overlap_tokens))
    return out


def chunk_text(
    text: str,
    *,
    max_tokens: int = cfg.max_tokens_per_chunk,
    overlap_ratio: float = cfg.chunk_overlap_ratio,
) -> list[str]:
    """Recursive splitter: 段落→行→文→語→文字の順に区切り、overlap を付与。"""
    text = text.strip()
    if not text:
        return []
    overlap_tokens = max(0, int(max_tokens * overlap_ratio))
    return _split_recursive(text, _RECURSIVE_SEPARATORS, max_tokens, overlap_tokens)


@dataclass(frozen=True)
class DocContext:
    """1 文書でページ間不変の属性。チャンク生成の共通引数をまとめる。"""

    doc_id: str
    source_file: str
    domain: str
    doc_title: str


def build_chunks_for_page(
    doc: DocContext, *, markdown: str, page_no: int,
) -> list[models.Chunk]:
    """1ページ分の Markdown → chunk（embedding は後で付与）。

    基本方針: 節ごとに分割し、各節本文を recursive splitter（max_tokens / overlap）で
    素直にチャンク化する。図/表も通常テキストと同じ規則で分割する（atomic 化や
    タイトル/脚注の特別扱いは行わない）。本文へ breadcrumb（文書 > ページ見出し > 節）
    を前置し、parent_id で親（節）へ辿れる。
    """
    page_title, sections = split_sections(markdown)
    records: list[models.Chunk] = []
    idx = 0
    for s_idx, (section, body) in enumerate(sections):
        crumb = _breadcrumb(doc.doc_title, page_title, section)
        parent_id = f"{doc.doc_id}::p{page_no}::s{s_idx}"  # 親=節（parent-child 検索用）
        for piece in chunk_text(body):
            text = f"{crumb}\n{piece}" if crumb else piece
            records.append(models.Chunk(
                chunk_id=f"{doc.doc_id}::p{page_no}::c{idx}",
                text=text,
                metadata=models.ChunkMetadata(
                    doc_id=doc.doc_id, source_file=doc.source_file, domain=doc.domain,
                    page_no=page_no, page_title=page_title, section=section,
                    parent_id=parent_id, chunk_index=idx,
                    token_count=common.estimate_tokens_ja(text),
                ),
            ))
            idx += 1
    return records


def handler(event: dict, context: object) -> dict:
    try:
        inp = models.EmbedInput.model_validate(event)
    except ValidationError as exc:
        raise common.PermanentError(f"invalid EmbedDocument input: {exc}") from exc

    doc_title = inp.doc_title or doc_title_from(inp.source_file)
    logger.info("EmbedDocument start: doc_id=%s pages=%d model=%s title=%s",
                inp.doc_id, len(inp.pages), cfg.embed_model_id, doc_title)

    doc = DocContext(
        doc_id=inp.doc_id, source_file=inp.source_file,
        domain=inp.domain, doc_title=doc_title,
    )
    chunks: list[models.Chunk] = []
    for page in sorted(inp.pages, key=lambda p: p.page_no):
        markdown = common.get_text(inp.bucket, page.md_key)
        chunks.extend(build_chunks_for_page(
            doc, markdown=markdown, page_no=page.page_no,
        ))

    doc_stem = inp.source_file.rsplit(".", 1)[0]  # {連番}_{title}
    # md と同じく文書フォルダ。配下に 1 ページ = 1 JSON（p{n}.json）を置く。
    embeddings_prefix = f"{cfg.embed_prefix}/{inp.domain}/{doc_stem}"
    out = models.EmbedOutput(
        bucket=inp.bucket, doc_id=inp.doc_id, embeddings_prefix=embeddings_prefix,
        chunk_count=len(chunks), page_count=len(inp.pages),
        embedding_model=cfg.embed_model_id, embedding_dim=cfg.embed_dimension,
    )

    if not chunks:  # 全ページ空 → ファイルは作らない
        logger.warning("doc_id=%s produced no chunks", inp.doc_id)
        return out.model_dump()

    vectors, dim = common.embed_texts(
        [c.text for c in chunks],
        model_id=cfg.embed_model_id, input_type="search_document",
        output_dimension=cfg.embed_dimension,
    )
    if len(vectors) != len(chunks):
        raise common.PermanentError(f"embedding count mismatch: {len(vectors)} != {len(chunks)}")
    for chunk, vec in zip(chunks, vectors):
        chunk.embedding = vec

    # ページごとに分けて書く（embeddings/{domain}/{連番}_{title}/p{n}.json）
    by_page: dict[int, list[models.Chunk]] = defaultdict(list)
    for chunk in chunks:
        by_page[chunk.metadata.page_no].append(chunk)
    for page_no, page_chunks in sorted(by_page.items()):
        common.put_json(
            inp.bucket, f"{embeddings_prefix}/p{page_no}.json",
            [c.model_dump() for c in page_chunks],
        )

    out.embedding_dim = dim
    logger.info("EmbedDocument done: %s", out.model_dump())
    return out.model_dump()
