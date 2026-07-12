output "ecr_repository_url" {
  description = "ingest イメージの push 先（build 後にここへ push）"
  value       = aws_ecr_repository.ingest.repository_url
}

output "data_bucket_name" {
  description = "ソース PDF / md / embeddings を置く S3 バケット（invoke 時の bucket に指定）"
  value       = aws_s3_bucket.data.id
}

output "ingest_state_machine_arn" {
  description = "ingest パイプラインの Step Functions ARN（start-execution で起動）"
  value       = aws_sfn_state_machine.ingest.arn
}

output "parse_function_arn" {
  description = "Step Functions の ProcessDocument が呼ぶ Lambda ARN"
  value       = aws_lambda_function.parse.arn
}

output "embed_function_arn" {
  description = "Step Functions の EmbedDocument が呼ぶ Lambda ARN"
  value       = aws_lambda_function.embed.arn
}

output "index_function_arn" {
  description = "embeddings を OpenSearch に投入する IndexDocument Lambda ARN"
  value       = aws_lambda_function.index.arn
}

output "opensearch_endpoint" {
  description = "OpenSearch ドメインエンドポイント（index Lambda の OPENSEARCH_ENDPOINT）"
  value       = aws_opensearch_domain.search.endpoint
}

output "opensearch_dashboard_url" {
  description = "OpenSearch Dashboards の URL"
  value       = "https://${aws_opensearch_domain.search.dashboard_endpoint}"
}

output "frontend_bucket_name" {
  description = "UI をアップロードする S3 バケット（aws s3 sync 先）"
  value       = aws_s3_bucket.frontend.id
}

output "frontend_url" {
  description = "公開 UI の URL（CloudFront）"
  value       = "https://${aws_cloudfront_distribution.frontend.domain_name}"
}

output "frontend_distribution_id" {
  description = "デプロイ後のキャッシュ無効化に使う CloudFront ディストリビューション ID"
  value       = aws_cloudfront_distribution.frontend.id
}
