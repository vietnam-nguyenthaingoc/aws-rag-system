# CLAUDE.md

本番志向のRAG（AWS）。ポートフォリオ用途。
差別化点は **検索（ハイブリッド）＋ 評価（定量）** であり、「動くこと」ではない。

## データセット
- `allganize/RAG-Evaluation-Dataset-JA`（65 PDF / 約2,121ページ / 5ドメイン / MIT）

## ディレクトリ構成（5フォルダ・フラット）
- `backend/` - API層
- `frontend/` - 静的なシングルページUI
- `infra/` - IaC

## 作業ルール
- まず end-to-end の vertical slice を作り、その後で各部を深掘りする。
- 最新のAWS情報（API・料金）が要るときは AWS Documentation/Knowledge MCP を使い、記憶で推測しない。
- アーキ判断を提案するときはトレードオフを述べ、可能なら評価数値を根拠にする。

## デプロイ
- ingest（parse / chunk_embed）をデプロイするときは **`scripts/deploy-ingest-function.sh` を使う**（`docker build` や `terraform apply` を手動で叩かない）。
  - `scripts/deploy-ingest-function.sh plan` — 2 関数ぶんの差分確認のみ。
  - `scripts/deploy-ingest-function.sh apply` — ECR 準備 → イメージ build(buildx/arm64) & push → 2 関数を target apply → コード更新。
