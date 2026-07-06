"""Pydantic モデル — Lambda 間の入出力とドメインオブジェクトを型で固定する。

dict を直接やり取りせず、各境界で model_validate / model_dump して扱う。
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field

ElementType = Literal["paragraph", "figure"]

_DOC_ID_RE = re.compile(r"^(\d+)")  # ファイル名先頭の連番（例 "01_xxx.pdf" → "01"）


# --- ProcessDocument ---
class ProcessInput(BaseModel):
    """parse の入力。入力は bucket + source_key のみ。
    Step Functions が source/{domain}/{連番}_{title}.pdf を列挙して渡し、
    doc_id / domain は source_key（＝ファイルパス）から導出する。"""

    bucket: str
    source_key: str

    @property
    def domain(self) -> str:
        parts = self.source_key.split("/")
        return parts[-2] if len(parts) >= 2 else ""  # source/{domain}/file.pdf の {domain}

    @property
    def doc_id(self) -> str:
        stem = self.source_key.split("/")[-1].rsplit(".", 1)[0]
        m = _DOC_ID_RE.match(stem)
        return m.group(1) if m else stem  # 連番が無ければ拡張子抜きファイル名


class PageResult(BaseModel):
    """process_page の戻り（1ページ分の本文）。"""

    markdown: str
    parser: str
    element_type: ElementType
    figure_count: int
    image_key: str  # ページ全体のレンダリング画像（layout/.../p{n}.png）の S3 キー


class PageRef(BaseModel):
    """ページ Markdown への参照（process 出力 = embed 入力）。"""

    page_no: int
    md_key: str
    parser: str
    element_type: ElementType
    figure_count: int
    char_count: int
    image_key: str  # ページ全体のレンダリング画像（layout/.../p{n}.png）の S3 キー


class ProcessOutput(BaseModel):
    bucket: str
    doc_id: str
    domain: str
    source_file: str
    page_count: int
    pages: list[PageRef]


# --- EmbedDocument ---
class EmbedInput(BaseModel):
    bucket: str
    doc_id: str
    domain: str
    source_file: str
    pages: list[PageRef]
    doc_title: str | None = None


class ChunkMetadata(BaseModel):
    doc_id: str
    source_file: str
    domain: str
    page_no: int
    page_title: str | None
    section: str | None
    parent_id: str
    chunk_index: int
    token_count: int


class Chunk(BaseModel):
    chunk_id: str
    text: str
    metadata: ChunkMetadata
    embedding: list[float] = Field(default_factory=list)


class EmbedOutput(BaseModel):
    bucket: str
    doc_id: str
    embeddings_prefix: str  # embeddings/{domain}/{連番}_{title}（配下に p{n}.json）
    chunk_count: int
    page_count: int
    embedding_model: str
    embedding_dim: int


# --- IndexDocument ---
class IndexInput(BaseModel):
    """index の入力。EmbedOutput の部分集合（embeddings_prefix 配下を OpenSearch へ）。"""

    bucket: str
    doc_id: str
    embeddings_prefix: str
    page_count: int


class IndexOutput(BaseModel):
    doc_id: str
    index: str
    indexed: int  # OpenSearch に投入したレコード数


# --- VLM キャッシュ ---
class VlmCacheEntry(BaseModel):
    text: str
    model_id: str
    sha256: str
    input_tokens: int = 0   # VLM 呼出のトークン数（コスト集計用。キャッシュヒットは新規消費なし）
    output_tokens: int = 0
