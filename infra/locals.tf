data "aws_caller_identity" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id

  # 全リソース名は <project_name>-ingest-<役割>-<リソース種別> で統一
  prefix = "${var.project_name}-ingest"

  # log group は Lambda 関数名と一致させる必要があるため local で共有
  parse_function_name = "${local.prefix}-parse-function"
  embed_function_name = "${local.prefix}-embed-function"
  index_function_name = "${local.prefix}-index-function"

  bucket_name = "${var.project_name}-data"
  bucket_arn  = "arn:aws:s3:::${local.bucket_name}"

  # OpenSearch ドメイン名は小文字・先頭英字・≤28 文字（aws-rag-assistant-os = 20）
  opensearch_domain_name = "${var.project_name}-os"
  opensearch_domain_arn  = "arn:aws:es:${var.region}:${local.account_id}:domain/${local.opensearch_domain_name}"
}
