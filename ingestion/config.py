"""環境変数ベースの設定（pydantic-settings）と外部プロンプトの読み込み。

各 Lambda は config.process / config.embed を import して参照する。
env 変数名はフィールド名の大文字（VLM_MODEL_ID, LAYOUT_DPI 等、大小無視）。
カンマ区切りの集合（*_CLASSES）と \\uXXXX を含むプロンプト本文は prompts/ に分離。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROMPTS_DIR = Path(__file__).parent / "prompts"


@lru_cache(maxsize=None)
def load_prompt(name: str) -> str:
    """prompts/ 配下のプロンプト本文を読み込む（コード本体と分離）。"""
    return (PROMPTS_DIR / name).read_text(encoding="utf-8").strip()


class ProcessSettings(BaseSettings):
    """ProcessDocument の設定。"""

    # enable_decoding=False: *_CLASSES をカンマ区切り文字列として受ける（JSON 解釈しない）
    model_config = SettingsConfigDict(extra="ignore", enable_decoding=False)

    # VLM（図・グラフ・表の書き起こし）。jp. プロファイルでデータを日本国内に保持
    vlm_model_id: str = "jp.anthropic.claude-haiku-4-5-20251001-v1:0"
    vlm_max_tokens: int = 4096
    vlm_prompt_file: str = "vlm_transcribe.txt"
    max_image_long_edge: int = 1568
    figure_pad_pt: float = 14.0

    # RapidLayout（レイアウト検出）
    layout_model_type: str = "pp_doc_layoutv3"
    layout_dpi: int = 150
    layout_conf: float = 0.4
    table_fallback: str = "vlm"  # find_tables 不可時: "vlm"=正確/有料 / "text"=無料/崩れる
    # find_tables が有効な格子を返したかの最小判定（未満＝大半が空＝崩れ → VLM）
    table_min_fill_ratio: float = 0.7  # 非空セルの割合の下限

    # 要素クラスのルーティング（カンマ区切り。未分類は本文扱い）
    doctitle_classes: set[str] = {"doc_title", "title"}  # → #（ページ見出し）
    section_classes: set[str] = {"paragraph_title"}      # → ##（節見出し）
    table_classes: set[str] = {"table"}
    figure_classes: set[str] = {"chart", "image", "display_formula"}  # → VLM
    drop_classes: set[str] = {
        "header", "footer", "header_image", "footer_image",
        "number", "page_number", "seal",
    }

    # S3 プレフィックス
    md_prefix: str = "md"               # md/{domain}/{連番}_{title}/p{n}.md
    vlm_cache_prefix: str = "vlm-cache" # vlm-cache/{sha256}.json（ドメイン非依存）
    # ページ全体のレンダリング画像（md/embeddings と同じく 1 ページ = 1 ファイル）
    layout_prefix: str = "layout"       # layout/{domain}/{連番}_{title}/p{n}.png

    @field_validator(
        "doctitle_classes", "section_classes", "table_classes",
        "figure_classes", "drop_classes", mode="before",
    )
    @classmethod
    def _split_csv(cls, v: object) -> object:
        if isinstance(v, str):
            return {c.strip() for c in v.split(",") if c.strip()}
        return v

    @field_validator("md_prefix", "vlm_cache_prefix", "layout_prefix", mode="after")
    @classmethod
    def _strip_slash(cls, v: str) -> str:
        return v.strip("/")

    @property
    def vlm_prompt(self) -> str:
        return load_prompt(self.vlm_prompt_file)


class EmbedSettings(BaseSettings):
    """EmbedDocument の設定。"""

    model_config = SettingsConfigDict(extra="ignore")

    embed_model_id: str = "amazon.titan-embed-text-v2:0"
    embed_dimension: int = 1024
    max_tokens_per_chunk: int = 512
    chunk_overlap_ratio: float = 0.12
    embed_prefix: str = "embeddings"  # embeddings/{domain}/{連番}_{title}/p{n}.json（ページ単位）

    @field_validator("embed_prefix", mode="after")
    @classmethod
    def _strip_slash(cls, v: str) -> str:
        return v.strip("/")


class IndexSettings(BaseSettings):
    """IndexDocument の設定。"""

    model_config = SettingsConfigDict(extra="ignore")

    # OpenSearch ドメインエンドポイント（scheme 不要。例 xxxx.ap-northeast-1.es.amazonaws.com）
    opensearch_endpoint: str = ""
    opensearch_index: str = "chunks"
    bulk_batch_size: int = 500   # helpers.bulk の 1 リクエストあたり件数


process = ProcessSettings()
embed = EmbedSettings()
index = IndexSettings()
