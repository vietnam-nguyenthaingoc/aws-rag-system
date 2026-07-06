#!/usr/bin/env bash
# ingest（parse / chunk_embed / index）のコードを更新するデプロイ。
#   1) ECR リポジトリを用意（Terraform target apply）
#   2) コンテナイメージを build & push（:latest, linux/arm64）
#   3) parse / embed を Terraform target で作成/更新し、3 関数のコードを最新イメージへ反映
# index 関数の構成（env / OpenSearch 等）は通常の `terraform apply` で管理（要事前作成）。
#
# 使い方:
#   scripts/deploy-ingest-function.sh plan     # parse/embed の差分確認のみ
#   scripts/deploy-ingest-function.sh apply    # build → push → デプロイ
#   PROJECT_NAME=foo REGION=us-east-1 scripts/deploy-ingest-function.sh apply

set -euo pipefail

MODE="${1:-plan}"
PROJECT_NAME="${PROJECT_NAME:-aws-rag-assistant}"
REGION="${REGION:-${AWS_REGION:-ap-northeast-1}}"
REPO_NAME="${PROJECT_NAME}-ingest-repo"
PLATFORM="linux/arm64"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INFRA_DIR="${REPO_ROOT}/infra"
CONTEXT="${REPO_ROOT}/ingestion"

# ingest 2 関数に必要なリソース一式を target で指定。
# 注意: -target は「依存」しか辿らない（dependent は辿らない）。Lambda だけ指定すると
# role にアタッチする IAM policy/attachment が作られず権限不足になる → 明示的に含める。
# role と log group は policy/Lambda の依存として自動的に一緒に作成される。
TF_TARGETS=(
  -target=aws_lambda_function.parse
  -target=aws_lambda_function.embed
  -target=aws_iam_role_policy.parse
  -target=aws_iam_role_policy.embed
  -target=aws_iam_role_policy_attachment.parse_logs
  -target=aws_iam_role_policy_attachment.embed_logs
)

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
REGISTRY="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"
IMAGE_URI="${REGISTRY}/${REPO_NAME}:latest"
PARSE_FN="${PROJECT_NAME}-ingest-parse-function"
EMBED_FN="${PROJECT_NAME}-ingest-embed-function"
INDEX_FN="${PROJECT_NAME}-ingest-index-function"

echo ">> mode=${MODE}  account=${ACCOUNT_ID}  region=${REGION}"
echo ">> image=${IMAGE_URI}"

# --- plan: 2 関数ぶんの差分のみ表示して終了 ---
if [[ "${MODE}" == "plan" ]]; then
  terraform -chdir="${INFRA_DIR}" plan "${TF_TARGETS[@]}"
  exit 0
fi

if [[ "${MODE}" != "apply" ]]; then
  echo "ERROR: mode は plan か apply（指定: ${MODE}）" >&2
  exit 1
fi

# --- apply ---
# 1) イメージ push の前提として ECR リポジトリを先に作成
terraform -chdir="${INFRA_DIR}" apply -auto-approve -target=aws_ecr_repository.ingest

# 2) build & push（Lambda の arm64 に合わせる。Apple Silicon ならネイティブ）
# Lambda は Docker manifest schema2 のみ対応。buildx 既定の OCI mediatype + provenance
# attestation は拒否される（InvalidParameterValueException）ため Docker 形式に強制する。
aws ecr get-login-password --region "${REGION}" \
  | docker login --username AWS --password-stdin "${REGISTRY}"
docker buildx build \
  --platform "${PLATFORM}" \
  --provenance=false \
  --output "type=registry,oci-mediatypes=false" \
  -t "${IMAGE_URI}" \
  "${CONTEXT}"

# 3) 2 関数を作成/更新（未作成なら依存ごと作成。既存なら :latest で URI 不変＝no-op）
terraform -chdir="${INFRA_DIR}" apply -auto-approve "${TF_TARGETS[@]}"

# :latest は URI が変わらないため、既存関数にはイメージ更新を明示反映する
# index は terraform apply で作成済み前提。未作成ならスキップ（warn）。
for fn in "${PARSE_FN}" "${EMBED_FN}" "${INDEX_FN}"; do
  if ! aws lambda get-function --function-name "${fn}" --region "${REGION}" >/dev/null 2>&1; then
    echo ">> skip（未作成）: ${fn}（terraform apply で作成してください）"
    continue
  fi
  echo ">> update-function-code: ${fn}"
  aws lambda update-function-code \
    --function-name "${fn}" --image-uri "${IMAGE_URI}" --region "${REGION}" >/dev/null
  aws lambda wait function-updated --function-name "${fn}" --region "${REGION}"
done

echo ">> done: ${PARSE_FN}, ${EMBED_FN}, ${INDEX_FN}"
