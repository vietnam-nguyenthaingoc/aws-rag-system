#!/usr/bin/env bash
# フロントエンド（frontend/）を S3 へ同期し、CloudFront キャッシュを無効化。
# 事前に `terraform apply`（infra/frontend.tf）でバケットとディストリビューションを作成しておくこと。
#
# 使い方:
#   scripts/deploy-frontend.sh
#   PROJECT_NAME=foo REGION=us-east-1 scripts/deploy-frontend.sh

set -euo pipefail

PROJECT_NAME="${PROJECT_NAME:-aws-rag-assistant}"
REGION="${REGION:-${AWS_REGION:-ap-northeast-1}}"
BUCKET="${PROJECT_NAME}-frontend"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="${ROOT}/frontend"

# CloudFront ディストリビューション ID を Terraform 出力から取得
DIST_ID="$(terraform -chdir="${ROOT}/infra" output -raw frontend_distribution_id)"

echo "==> sync ${SRC} -> s3://${BUCKET}"
aws s3 sync "${SRC}" "s3://${BUCKET}" --region "${REGION}" --delete

echo "==> invalidate CloudFront ${DIST_ID}"
aws cloudfront create-invalidation --distribution-id "${DIST_ID}" --paths "/*" >/dev/null

echo "==> done: $(terraform -chdir="${ROOT}/infra" output -raw frontend_url)"
