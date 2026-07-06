"""ProcessDocument Lambda — 1 PDF を丸ごと Markdown 化（split+pagemd 統合, 方式B）。

各ページ: RapidLayout(PP-DocLayout v3) で要素配列を取得し、クラスごとに振り分け:
- text/見出し → PyMuPDF `get_text(clip=bbox)`（無料）
- table      → PyMuPDF `find_tables(clip=bbox).to_markdown()`（失敗時は VLM）
- chart/image → crop(余白付き・高解像度) → VLM(Bedrock Claude), sha256 キャッシュ
- header/footer/seal 等 → 破棄
要素を読み順に並べて連結。要素ゼロ検出のページのみ pymupdf4llm 全ページにフォールバック。

S3 キー:
- 入力 : source/{domain}/{連番}_{title}.pdf （入力は bucket + source_key のみ）
- 出力 : md/{domain}/{連番}_{title}/p{n}.md （ページ単位）
- ページ画像: layout/{domain}/{連番}_{title}/p{n}.png （ページ全体のレンダリング）
- VLM キャッシュ: vlm-cache/{sha256}.json （画像内容ハッシュ＝ドメイン非依存で重複排除）
入出力モデルは models.ProcessInput / models.ProcessOutput を参照。
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Callable
from functools import partial

import cv2
import fitz  # PyMuPDF
import numpy as np
import pymupdf4llm
from pydantic import BaseModel, ConfigDict, ValidationError

import common
import config
import models

logger = common.get_logger(__name__)
cfg = config.process  # 環境変数ベースの設定（VLM・レイアウト・クラス振り分け）

_PIC_MARK_RE = re.compile(r"\*\*==> picture[^\n]*<==\*\*")
_PIC_TEXT_RE = re.compile(r"\*\*-+ Start of picture text -+\*\*.*?-+ End of picture text -+\*\*<br>", re.S)


# --- RapidLayout で要素検出 ---
_layout_engine = None


def _get_layout_engine():
    """RapidLayout を遅延生成（ウォームスタートで再利用）。"""
    global _layout_engine
    if _layout_engine is None:
        from rapid_layout import RapidLayout
        from rapid_layout.utils.typings import ModelType, RapidLayoutInput

        _layout_engine = RapidLayout(
            cfg=RapidLayoutInput(
                model_type=ModelType(cfg.layout_model_type), conf_thresh=cfg.layout_conf
            )
        )
        logger.info("RapidLayout loaded: model=%s", cfg.layout_model_type)
    return _layout_engine


def _page_to_bgr(page: "fitz.Page", dpi: int) -> "np.ndarray":
    pix = page.get_pixmap(dpi=dpi)
    arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
    return arr[:, :, :3][:, :, ::-1].copy()  # RGB -> BGR


class LayoutElement(BaseModel):
    """RapidLayout 1要素（rect は pt 単位、process 内部専用）。"""

    model_config = ConfigDict(arbitrary_types_allowed=True)
    cls: str
    rect: fitz.Rect
    score: float


def detect_elements(page: "fitz.Page") -> list[LayoutElement]:
    """読み順の要素配列を返す（bbox は px→pt 変換済み）。"""
    res = _get_layout_engine()(_page_to_bgr(page, cfg.layout_dpi))
    boxes = getattr(res, "boxes", None)
    classes = list(getattr(res, "class_names", []) or [])
    scores = getattr(res, "scores", None)
    if boxes is None or not classes:
        return []
    s = 72.0 / cfg.layout_dpi  # px -> pt
    return [
        LayoutElement(
            cls=cls,
            rect=fitz.Rect(b[0] * s, b[1] * s, b[2] * s, b[3] * s),
            score=float(scores[i]) if scores is not None else 0.0,
        )
        for i, (cls, b) in enumerate(zip(classes, boxes))
    ]


# --- 要素ごとの抽出 ---
def render_figure_png(page: "fitz.Page", rect: fitz.Rect) -> bytes:
    """領域を余白付き・長辺 MAX_IMAGE_LONG_EDGE で PNG 化。"""
    pad = cfg.figure_pad_pt
    r = fitz.Rect(rect.x0 - pad, rect.y0 - pad, rect.x1 + pad, rect.y1 + pad) & page.rect
    zoom = cfg.max_image_long_edge / max(r.width, r.height, 1.0)
    return page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=r).tobytes("png")


# レイアウト可視化のクラス別色（BGR）。未定義は灰色。
_LAYOUT_COLORS = {
    "table": (255, 120, 30), "chart": (40, 40, 230), "image": (40, 40, 230),
    "display_formula": (40, 40, 230), "figure_title": (60, 180, 60),
    "doc_title": (0, 140, 240), "paragraph_title": (0, 140, 240),
    "footnote": (200, 60, 200), "vision_footnote": (200, 60, 200),
}


def _layout_png(page: "fitz.Page", elements: list[LayoutElement]) -> bytes:
    """ページ画像にレイアウト検出枠（クラス別色＋ラベル）を描いて PNG 化。"""
    bgr = _page_to_bgr(page, cfg.layout_dpi)
    scale = cfg.layout_dpi / 72.0  # pt → px
    for el in elements:
        color = _LAYOUT_COLORS.get(el.cls, (130, 130, 130))
        p0 = (int(el.rect.x0 * scale), int(el.rect.y0 * scale))
        p1 = (int(el.rect.x1 * scale), int(el.rect.y1 * scale))
        cv2.rectangle(bgr, p0, p1, color, 2)
        cv2.putText(bgr, el.cls, (p0[0], max(12, p0[1] - 4)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1, cv2.LINE_AA)
    return cv2.imencode(".png", bgr)[1].tobytes()


def _save_page_image(bucket: str, doc_prefix: str, page_no: int, png: bytes) -> str:
    key = f"{cfg.layout_prefix}/{doc_prefix}/p{page_no}.png"
    common.put_bytes(bucket, key, png, "image/png")
    return key


def vlm_read_figure(image_png: bytes, bucket: str) -> str:
    digest = hashlib.sha256(image_png).hexdigest()
    cache_key = f"{cfg.vlm_cache_prefix}/{digest}.json"
    cached = common.get_json(bucket, cache_key)
    if cached:
        logger.info("VLM cache hit: %s", cache_key)
        return models.VlmCacheEntry.model_validate(cached).text
    text, in_tok, out_tok = common.converse_image(
        model_id=cfg.vlm_model_id, image_png=image_png,
        prompt=cfg.vlm_prompt, max_tokens=cfg.vlm_max_tokens,
    )
    entry = models.VlmCacheEntry(
        text=text, model_id=cfg.vlm_model_id, sha256=digest,
        input_tokens=in_tok, output_tokens=out_tok,
    )
    common.put_json(bucket, cache_key, entry.model_dump())
    return text


def _find_page_tables(page: "fitz.Page") -> list:
    """ページ全体で表検出（clip より信頼性が高い）。失敗時は空。"""
    try:
        return list(page.find_tables().tables)
    except Exception:  # noqa: BLE001
        return []


def _match_table(rect: fitz.Rect, page_tables: list):
    """table 要素の bbox に最も重なる検出表を返す。無ければ None。"""
    area = abs(rect.width * rect.height) or 1.0
    best, best_ov = None, 0.0
    for t in page_tables:
        try:  # PyMuPDF は壊れた表（空セル）で bbox 計算が ValueError になることがある
            tbox = fitz.Rect(t.bbox)
        except Exception:  # noqa: BLE001
            continue
        inter = rect & tbox
        if inter.is_empty:
            continue
        ov = abs(inter.width * inter.height) / area
        if ov > best_ov:
            best, best_ov = t, ov
    return best if best_ov >= 0.3 else None


def _table_is_reliable(table) -> bool:
    """find_tables が有効な格子を返したかの最小判定（失敗時は False で VLM に回す）。

    borderless 等で抽出が崩れると find_tables は格子を返せず、退化（行/列<2）か
    大半が空セルになる。その2点だけを見る。微妙な誤抽出（点線罫漏れ・セル潰れ）は
    検知せず素通しする（簡素化のため許容するリスク）。
    """
    rows = table.extract()
    if len(rows) < 2 or not rows[0] or len(rows[0]) < 2:  # 退化＝格子になっていない
        return False
    slots = sum(len(r) for r in rows)
    filled = sum(1 for row in rows for c in row if c and c.strip())
    return filled / slots >= cfg.table_min_fill_ratio  # 大半が空 → 崩れたとみなす


def _strip_picture_markers(md: str) -> str:
    return _PIC_MARK_RE.sub("", _PIC_TEXT_RE.sub("", md))


# 見出し先頭に紛れる箇条書き記号・矢印・私用領域グリフ（埋込フォントの ➡ 等）
_TITLE_LEAD_RE = re.compile(
    r"^[\s•■-◿←-⇿➠-➿-・◯○●◆■▶➡:：>＞\-]+"
)


def _clip_title(page: "fitz.Page", rect: fitz.Rect) -> str:
    """見出しを1行に畳み、先頭の箇条書き記号類を除去（## 行とメタを汚さない）。"""
    txt = " ".join(page.get_text("text", clip=rect, sort=True).split())
    return _TITLE_LEAD_RE.sub("", txt).strip()


# --- 要素クラス別の抽出 ---
# 同一シグネチャ (page, rect, bucket, page_tables) -> (追記する parts, figure 数)。
# doctitle/section/本文は _emit_text に集約（prefix=見出しレベル, title=1行化）。
def _reflow(txt: str) -> str:
    """段落内の折り返し改行（単独 \\n）を詰める。空行（段落区切り \\n\\n）は残す。
    日本語は語間スペースが無いため改行は空文字で連結する。"""
    return re.sub(r"[ \t]*\n(?!\n)[ \t]*", "", txt).strip()


def _emit_vlm(
    page: "fitz.Page", rect: fitz.Rect, bucket: str, page_tables: list | None = None
) -> tuple[list[str], int]:
    """図領域を VLM で書き起こし、本文にそのまま連結する（chart/image と表フォールバック共用）。"""
    desc = vlm_read_figure(render_figure_png(page, rect), bucket).strip()
    return ([desc], 1) if desc else ([], 0)


def _emit_table(page: "fitz.Page", rect: fitz.Rect, bucket: str, page_tables: list) -> tuple[list[str], int]:
    tbl = _match_table(rect, page_tables)
    if tbl is not None and _table_is_reliable(tbl):  # 罫線が揃った表 → markdown をそのまま
        md = tbl.to_markdown().strip()
        if md:
            return [md], 0
    # 罫線が不完全/抽出が不正 → VLM（正確・有料）か text（無料・崩れる）
    if cfg.table_fallback == "vlm":
        return _emit_vlm(page, rect, bucket)
    return _emit_text(page, rect, bucket, page_tables)


def _emit_text(
    page: "fitz.Page", rect: fitz.Rect, bucket: str, page_tables: list,
    *, prefix: str = "", title: bool = False,
) -> tuple[list[str], int]:
    """text 抽出。title=見出し(1行化＋記号除去)、prefix で # / ## を付与、本文は _reflow。"""
    txt = _clip_title(page, rect) if title else _reflow(page.get_text("text", clip=rect, sort=True))
    return ([f"{prefix}{txt}"], 0) if txt else ([], 0)


# 要素クラス → 抽出関数のディスパッチ表（先頭優先、未該当は本文 _emit_text）。
# 新クラス対応＝この表に1行加えるだけ（process_page は触らない）。
_ROUTES: list[tuple[set[str], Callable[..., tuple[list[str], int]]]] = [
    (cfg.figure_classes, _emit_vlm),
    (cfg.table_classes, _emit_table),
    (cfg.doctitle_classes, partial(_emit_text, prefix="# ", title=True)),
    (cfg.section_classes, partial(_emit_text, prefix="## ", title=True)),
]


def _route(cls: str) -> Callable[..., tuple[list[str], int]]:
    for classes, emit in _ROUTES:
        if cls in classes:
            return emit
    return _emit_text


# --- 1 ページ処理（要素ルーティング） ---
def process_page(
    doc: "fitz.Document", page_no: int, bucket: str, doc_prefix: str
) -> models.PageResult:
    page = doc[page_no - 1]
    elements = detect_elements(page)

    if not elements:  # 検出ゼロ → pymupdf4llm 全ページにフォールバック（画像は素のページ）
        image_key = _save_page_image(
            bucket, doc_prefix, page_no, page.get_pixmap(dpi=cfg.layout_dpi).tobytes("png")
        )
        body = _strip_picture_markers(
            pymupdf4llm.to_markdown(doc, pages=[page_no - 1], page_chunks=False)
        ).strip()
        return models.PageResult(
            markdown=body, parser="pymupdf4llm", element_type="paragraph",
            figure_count=0, image_key=image_key,
        )

    # レイアウト検出枠付きのページ画像を保存
    image_key = _save_page_image(bucket, doc_prefix, page_no, _layout_png(page, elements))

    page_tables = _find_page_tables(page)  # ページ全体で1回だけ表検出
    parts: list[str] = []
    figure_count = 0
    for el in elements:  # RapidLayout が予測した読み順をそのまま使い、要素を連結する
        if el.cls in cfg.drop_classes:
            continue
        new_parts, figures = _route(el.cls)(page, el.rect, bucket, page_tables)
        parts.extend(new_parts)
        figure_count += figures

    return models.PageResult(
        markdown="\n\n".join(parts).strip(),
        parser="rapidlayout+vlm" if figure_count else "rapidlayout",
        element_type="figure" if figure_count else "paragraph",
        figure_count=figure_count,
        image_key=image_key,
    )


def handler(event: dict, context: object) -> dict:
    try:
        inp = models.ProcessInput.model_validate(event)
    except ValidationError as exc:
        raise common.PermanentError(f"invalid ProcessDocument input: {exc}") from exc

    source_file = os.path.basename(inp.source_key)
    doc_stem = source_file.rsplit(".", 1)[0]  # 拡張子抜きファイル名（= {連番}_{title}）
    doc_prefix = f"{inp.domain}/{doc_stem}"   # md / embeddings / layout 共通のフォルダ
    logger.info("ProcessDocument start: doc_id=%s source=s3://%s/%s",
                inp.doc_id, inp.bucket, inp.source_key)

    pdf_bytes = common.download_bytes(inp.bucket, inp.source_key)
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as exc:  # noqa: BLE001
        raise common.PermanentError(f"Cannot open PDF {inp.source_key}: {exc}") from exc

    pages: list[models.PageRef] = []
    with doc:
        if doc.page_count == 0:
            raise common.PermanentError(f"PDF has no pages (doc_id={inp.doc_id})")
        for page_no in range(1, doc.page_count + 1):
            res = process_page(doc, page_no, inp.bucket, doc_prefix)
            md_key = f"{cfg.md_prefix}/{doc_prefix}/p{page_no}.md"
            common.put_text(inp.bucket, md_key, res.markdown, "text/markdown; charset=utf-8")
            pages.append(models.PageRef(
                page_no=page_no, md_key=md_key, parser=res.parser,
                element_type=res.element_type, figure_count=res.figure_count,
                char_count=len(res.markdown), image_key=res.image_key,
            ))
        page_count = doc.page_count

    logger.info("ProcessDocument done: doc_id=%s pages=%d figures=%d",
                inp.doc_id, page_count, sum(p.figure_count for p in pages))
    return models.ProcessOutput(
        bucket=inp.bucket, doc_id=inp.doc_id, domain=inp.domain,
        source_file=source_file, page_count=page_count, pages=pages,
    ).model_dump()
