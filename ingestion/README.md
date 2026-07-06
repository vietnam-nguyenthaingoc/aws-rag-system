# ingestion — RAG 取り込みパイプライン

日本語の企業 PDF を Step Functions で取り込み、検索用の埋め込みまで生成する。
2 ステップ（**ProcessDocument → EmbedDocument**）。1 コンテナイメージを共有し、
Lambda ごとに CMD を上書きして役割を切り替える。

> このリポジトリの差別化点は **ハイブリッド検索 + 定量評価** であり、取り込みは
> その土台。ここでは検索しやすい Markdown と、本文＋メタデータ付きチャンクを作る
> （BM25 / kNN 双方に効く形）。検索・評価は後段（別フォルダ）。

## ステップ（各 = 1 Lambda = 1 SF ステート）

```
Map[ documents ]            # 1 PDF = 1 アイテム（ドキュメント単位の並列）
  └─ ProcessDocument  →  EmbedDocument
       PDF → ページMarkdown    Markdown → チャンク + 埋め込み
       parse.handler.handler   chunk_embed.handler.handler
```

並列はドキュメント単位（ページ単位の Map は持たない）。Step Functions の
[Map ステート](https://docs.aws.amazon.com/step-functions/latest/dg/amazon-states-language-map-state.html)
で 1 PDF=1 アイテムに展開する。

### 1. ProcessDocument (`parse.handler.handler`) — 1 PDF を丸ごと Markdown 化

ページごとに [RapidLayout](https://github.com/RapidAI/RapidLayout)（モデル
[PP-DocLayout v3](https://arxiv.org/abs/2503.17213)、onnxruntime/CPU）で要素を
読み順に検出し、クラスで振り分ける:

| 要素クラス | 処理 | 備考 |
|---|---|---|
| `doc_title` / `title` | `# 見出し` | PyMuPDF `get_text(clip)`（無料） |
| `paragraph_title` | `## 見出し` | 同上 |
| `text` ほか本文 | テキスト抽出 | 同上 |
| `table` | [PyMuPDF `find_tables` → `to_markdown`](https://pymupdf.readthedocs.io/en/latest/page.html#Page.find_tables) | 信頼性判定に通らなければ VLM へ（下記） |
| `chart` / `image` | 領域を PNG 化 → VLM | sha256 でキャッシュ |
| `header`/`footer`/`seal`/`number`/`page_number` 等 | 破棄 | |

- 要素を読み順に連結。**1 要素も検出されないページのみ**
  [pymupdf4llm](https://pymupdf.readthedocs.io/en/latest/pymupdf4llm/) で全ページ
  フォールバック。
- 出力 `md/{domain}/{連番}_{title}/p{n}.md`、参照一覧（`pages[]`）を返す。
- **ページ画像**: ページ全体を `layout/{domain}/{連番}_{title}/p{n}.png` として保存
  （1 ページ = 1 画像。目視レビュー・将来のマルチモーダル検索用）。画像キーは
  `pages[].image_key` に記録。

**表のルーティング（罫線抽出 vs VLM）** — 「行・列が揃った整然とした表」だけ
PyMuPDF をそのまま採用し、崩れている表は VLM に回す。`_table_is_reliable()` が
不合格（→ VLM）にする条件は最小限の 2 つ:

1. 行 < 2 または 列 < 2（退化）
2. 充填率（非空セル / 全セル）< `TABLE_MIN_FILL_RATIO`（行罫線欠け＝空セル多で行列が対応しない）

**VLM（図・グラフ・崩れた表）** — 領域を余白付き・長辺 `MAX_IMAGE_LONG_EDGE` で
PNG 化し、Bedrock の Claude（既定 Haiku 4.5 / `jp.` プロファイル＝日本国内処理）へ
Converse で送る。
[Claude vision のベストプラクティス](https://platform.claude.com/docs/en/build-with-claude/vision)
に従い **画像を先・テキスト（プロンプト）を後**（`common.converse_image`）。
プロンプトは `prompts/vlm_transcribe.txt`（図・グラフ・表を日本語 Markdown に
書き起こす指示）。結果は PNG の sha256 でキャッシュ（再実行・重複領域で無料化）。
長辺 1568px は Claude vision の標準解像度ティア（〜1.15MP）の上限に合わせた既定。

### 2. EmbedDocument (`chunk_embed.handler.handler`) — Markdown → チャンク + 埋め込み

- **節分割**: `#`（ページ見出し）/ `##`（節見出し）で分解（ページ主タイトルが
  `##` と誤判定された場合はページ見出しへ昇格）。
- **チャンク化（基本方針）**: 図・表も含め本文はすべて同じ再帰分割でチャンク化する。
  図/表を 1 chunk に保つ atomic 化やタイトル/脚注の併合は行わない（将来の改善課題）。
  段落→行→文（`。．！？`）→語→文字の順に再帰分割し
  `MAX_TOKENS_PER_CHUNK` に収める。考え方は LangChain の
  [RecursiveCharacterTextSplitter](https://python.langchain.com/docs/how_to/recursive_text_splitter/)
  と同じ（区切りは日本語の句読点を含むよう調整）。`CHUNK_OVERLAP_RATIO` 分を
  末尾から次チャンクへ繰り越す。トークン数は `estimate_tokens_ja`（CJK≒1字1token、
  その他≒4字1token）で近似。
- **コンテキスト前置（breadcrumb）**: 各チャンク本文の先頭に
  `文書 > ページ見出し > 節` を付ける。チャンク単体で文脈を失わないための工夫で、
  Anthropic の[Contextual Retrieval](https://www.anthropic.com/news/contextual-retrieval)
  の考え方（チャンクに説明的文脈を前置する）に沿う簡易版。
- **parent-child**: `parent_id`（=節）を持たせ、検索後に親（節）へ辿れるようにする。
- **埋め込み**: [Cohere Embed v4](https://docs.aws.amazon.com/bedrock/latest/userguide/model-parameters-embed-v4.html)
  （`input_type="search_document"`、`output_dimension=EMBED_DIMENSION`、96 件バッチ、
  `truncate="RIGHT"`）。query 側は `input_type="search_query"` を使うこと。
  多言語・多次元（256/512/1024/1536）対応は
  [AWS の解説記事](https://aws.amazon.com/blogs/machine-learning/powering-enterprise-search-with-the-cohere-embed-4-multimodal-embeddings-model-in-amazon-bedrock/)参照。
  日本語の検索品質を優先して Cohere を既定採用（他モデルは eval 数値で比較してから検討）。
- 出力 `embeddings/{domain}/{連番}_{title}/p{n}.json`（md と同じく 1 ページ = 1 ファイル。
  そのページのチャンク配列。各要素 = チャンク本文 + メタデータ + ベクトル）。
  チャンクはページ・節をまたがない。

## I/O（ステート間は S3 ポインタのみ。本文は渡さない）

| Step | Input | Output |
|---|---|---|
| ProcessDocument | `{bucket, source_key}`（`doc_id`/`domain` は source_key から導出） | `{bucket, doc_id, domain, source_file, page_count, pages[]}` |
| EmbedDocument | `{bucket, doc_id, domain, source_file, pages[], doc_title?}` | `{bucket, doc_id, embeddings_prefix, chunk_count, page_count, embedding_model, embedding_dim}` |

型は `models.py`（`ProcessInput/Output`, `EmbedInput/Output`, `Chunk` 等）で固定。
各境界で `model_validate` / `model_dump`。

## S3 レイアウト

```
source/{domain}/{連番}_{title}.pdf            # 入力 PDF（Step Functions が source/ を列挙）
md/{domain}/{連番}_{title}/p{n}.md             # ProcessDocument の Markdown（ページ単位）
layout/{domain}/{連番}_{title}/p{n}.png        # ページ全体のレンダリング画像（1 ページ = 1 枚）
vlm-cache/{sha256}.json                        # VLM 結果キャッシュ（画像ハッシュ＝ドメイン非依存）
embeddings/{domain}/{連番}_{title}/p{n}.json   # EmbedDocument の最終成果物（ページ単位のチャンク配列）
```

## 設定（環境変数）

[pydantic-settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/) で
管理（`config.py`）。env 変数名はフィールド名の大文字（大小無視）。
プロンプト本文と `*_CLASSES` は `prompts/` とカンマ区切り文字列に分離。

| 変数 | 既定 | 対象 | 備考 |
|---|---|---|---|
| `VLM_MODEL_ID` | `jp.anthropic.claude-haiku-4-5-20251001-v1:0` | parse | **要 region 確認**。接頭辞 `jp.`=日本国内 / `apac./global.` も可 |
| `VLM_MAX_TOKENS` | `4096` | parse | VLM 出力上限 |
| `VLM_PROMPT_FILE` | `vlm_transcribe.txt` | parse | `prompts/` 配下 |
| `MAX_IMAGE_LONG_EDGE` | `1568` | parse | VLM 送信 PNG の長辺（標準ティア上限） |
| `FIGURE_PAD_PT` | `14.0` | parse | 図領域クロップの余白 |
| `LAYOUT_MODEL_TYPE` | `pp_doc_layoutv3` | parse | RapidLayout モデル |
| `LAYOUT_DPI` | `150` | parse | レイアウト検出のレンダ解像度 |
| `LAYOUT_CONF` | `0.4` | parse | 検出スコア閾値 |
| `TABLE_FALLBACK` | `vlm` | parse | 罫線抽出不可時: `vlm`（正確/有料） / `text`（無料/崩れる） |
| `TABLE_MIN_FILL_RATIO` | `0.7` | parse | 非空セル率の下限 |
| `TABLE_MAX_CELL_NEWLINES` | `3` | parse | 1 セル内改行数の上限 |
| `EMBED_MODEL_ID` | `cohere.embed-v4` | chunk_embed | 日本語の検索品質を重視し Cohere 多言語を既定とする |
| `EMBED_DIMENSION` | `1024` | chunk_embed | Cohere v4: 256/512/1024/1536 |
| `MAX_TOKENS_PER_CHUNK` | `512` | chunk_embed | |
| `CHUNK_OVERLAP_RATIO` | `0.12` | chunk_embed | ~12% |
| `MD_PREFIX`/`VLM_CACHE_PREFIX`/`EMBED_PREFIX` | `md`/`vlm-cache`/`embeddings` | | S3 接頭辞 |
| `LOG_LEVEL` | `INFO` | all | |

要素クラスの振り分けも上書き可（カンマ区切り）:
`DOCTITLE_CLASSES` / `SECTION_CLASSES` / `TABLE_CLASSES` / `FIGURE_CLASSES` / `DROP_CLASSES`。

## IAM（実行ロール 最小権限）

- **ProcessDocument**: `s3:GetObject`(source, `vlm-cache/*`), `s3:PutObject`(`md/*`, `vlm-cache/*`),
  `bedrock:InvokeModel`(Claude vision)
- **EmbedDocument**: `s3:GetObject`(`md/*`), `s3:PutObject`(`embeddings/*`),
  `bedrock:InvokeModel`(埋め込みモデル)
- クロスリージョン推論プロファイル利用時は `inference-profile/*` と各 region の
  `foundation-model/*` への `bedrock:InvokeModel` も付与。

## エラー処理（Step Functions 連携）

- 一時障害（スロットリング/5xx/タイムアウト）→ `common.TransientError` → ASL の **Retry**。
- 恒久障害（入力不正・PDF 破損・未対応モデル）→ `common.PermanentError` → **Catch** で失敗。
- 冪等性: VLM は sha256 キャッシュ、各成果物は `{domain}/{連番}_{title}` 固定キーで上書き → 再実行安全。

ASL: [`../infra/ingest.asl.json`](../infra/ingest.asl.json)
（`${ProcessFunctionArn}` / `${EmbedFunctionArn}` を差し替え。外側 Map で 1 PDF=1 アイテム）。

## ビルド / デプロイ（概略）

[Lambda コンテナイメージ](https://docs.aws.amazon.com/lambda/latest/dg/python-image.html)
を 1 つ作り、2 つの Lambda で CMD を上書きする:

```bash
docker build -t rag-ingest ingestion/
# ECR push 後、2 つの Lambda(コンテナ) を同一イメージで作成し CMD を上書き:
#   parse.handler.handler  /  chunk_embed.handler.handler
```

- `Dockerfile` 内で RapidLayout の `opencv-python`(GUI 版/libGL 必須) を
  `opencv-python-headless` へ差し替え済み（Lambda に libGL が無いため）。
- PP-DocLayout v3 モデル(~124MB)はビルド時にイメージへ焼き込み（実行時 DL なし）。
- 推奨: メモリ ≥2048MB（PNG レンダリング）、タイムアウトは ProcessDocument を
  長め（VLM 呼び出し）に。Map の `MaxConcurrency` と Bedrock スロットリングを見て調整。

## ローカル実行

`common` の S3/Bedrock をローカル FS・モックに差し替えて試す
（[`../scripts/ingest_local.py`](../scripts/ingest_local.py)）:

```bash
python scripts/ingest_local.py --step all     --pdf "datasets/finance/01_….pdf" --doc-id seimei-2021 --domain finance
python scripts/ingest_local.py --step process --pdf "datasets/finance/01_….pdf"
python scripts/ingest_local.py --step embed
python scripts/ingest_local.py --step all     --real-bedrock   # 本物の Bedrock（要 creds）
```

成果物は `local_work/`（`.gitignore` 済み）に出力。`--real-bedrock` で
プロンプト変更を反映するには `local_work/vlm-cache/` を削除する。
