# aws-rag-assistant インフラ（小規模なのでモジュール化せず resource は本ファイルに集約）。
# 構成: ECR(1) + S3(1) + Lambda x3（parse / chunk_embed / index, 同一イメージを CMD で切替）
#       + Step Functions(ingest) + OpenSearch(検索ドメイン)。
# Bedrock 呼び出し権限は各 Lambda 実行ロールに直接付与（モデル登録は不要＝既定で有効）。
#   変数 → variables.tf / 出力 → outputs.tf / locals・data → locals.tf

# ---------------------------------------------------------------------------
# ECR（parse / chunk_embed が共有する単一イメージ）
# ---------------------------------------------------------------------------
resource "aws_ecr_repository" "ingest" {
  name                 = "${local.prefix}-repo"
  image_tag_mutability = "MUTABLE"
  force_delete         = true # ポートフォリオ用途: destroy 時にイメージごと削除可

  image_scanning_configuration {
    scan_on_push = true
  }
}

# ---------------------------------------------------------------------------
# S3（ソース PDF + 中間 md + vlm-cache + embeddings を格納）
# バケット名は local.bucket_name = <project_name>-data（IAM ポリシーと一致）。
# 各 Lambda は invoke イベントの bucket フィールドでこのバケットを参照する。
# ---------------------------------------------------------------------------
resource "aws_s3_bucket" "data" {
  bucket        = local.bucket_name
  force_destroy = true # ポートフォリオ用途: オブジェクトごと destroy 可
}

# 機微データを含み得るため公開を完全遮断
resource "aws_s3_bucket_public_access_block" "data" {
  bucket                  = aws_s3_bucket.data.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# 保存時暗号化（SSE-S3。KMS 料金不要）
resource "aws_s3_bucket_server_side_encryption_configuration" "data" {
  bucket = aws_s3_bucket.data.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# ---------------------------------------------------------------------------
# IAM（Lambda 実行ロール）
# ---------------------------------------------------------------------------
data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

# S3: ソース PDF 読取 + md / vlm-cache / embeddings の読み書き
data "aws_iam_policy_document" "s3_access" {
  statement {
    sid       = "ReadWriteObjects"
    effect    = "Allow"
    actions   = ["s3:GetObject", "s3:PutObject"]
    resources = ["${local.bucket_arn}/*"]
  }
  statement {
    sid       = "ListBucket"
    effect    = "Allow"
    actions   = ["s3:ListBucket"]
    resources = [local.bucket_arn]
  }
}

# parse ロールの権限一式 = S3 + Bedrock(Claude)。モデルアクセスは既定で有効、
# 初回呼び出し時に Marketplace 自動サブスク（リソース指定不可のため "*"）。
data "aws_iam_policy_document" "parse" {
  source_policy_documents = [data.aws_iam_policy_document.s3_access.json]

  statement {
    sid     = "InvokeClaude"
    effect  = "Allow"
    actions = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
    resources = [
      "arn:aws:bedrock:${var.region}:${local.account_id}:inference-profile/${var.vlm_inference_profile_id}",
      "arn:aws:bedrock:*::foundation-model/${var.vlm_foundation_model_id}", # クロスリージョン推論で region をワイルドカード化
    ]
  }
  statement {
    sid       = "MarketplaceAutoSubscribe"
    effect    = "Allow"
    actions   = ["aws-marketplace:Subscribe", "aws-marketplace:ViewSubscriptions"]
    resources = ["*"]
  }
}

# embed ロールの権限一式 = S3 + Bedrock(Cohere)。
data "aws_iam_policy_document" "embed" {
  source_policy_documents = [data.aws_iam_policy_document.s3_access.json]

  statement {
    sid       = "InvokeCohere"
    effect    = "Allow"
    actions   = ["bedrock:InvokeModel"]
    resources = ["arn:aws:bedrock:*::foundation-model/${var.embed_model_id}"]
  }
  statement {
    sid       = "MarketplaceAutoSubscribe"
    effect    = "Allow"
    actions   = ["aws-marketplace:Subscribe", "aws-marketplace:ViewSubscriptions"]
    resources = ["*"]
  }
}

resource "aws_iam_role" "parse" {
  name               = "${local.prefix}-parse-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role" "embed" {
  name               = "${local.prefix}-embed-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

# CloudWatch Logs（基本実行権限）
resource "aws_iam_role_policy_attachment" "parse_logs" {
  role       = aws_iam_role.parse.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy_attachment" "embed_logs" {
  role       = aws_iam_role.embed.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "parse" {
  name   = "${local.prefix}-parse-policy"
  role   = aws_iam_role.parse.id
  policy = data.aws_iam_policy_document.parse.json
}

resource "aws_iam_role_policy" "embed" {
  name   = "${local.prefix}-embed-policy"
  role   = aws_iam_role.embed.id
  policy = data.aws_iam_policy_document.embed.json
}

# index ロールの権限一式 = S3 読み（embeddings 取得）+ OpenSearch への HTTP（bulk / index 作成）。
# OpenSearch ドメイン本体は末尾の検索セクションで定義（ARN は self-cycle 回避のため local 参照）。
data "aws_iam_policy_document" "index" {
  source_policy_documents = [data.aws_iam_policy_document.s3_access.json]

  statement {
    sid       = "OpenSearchHttp"
    effect    = "Allow"
    actions   = ["es:ESHttpGet", "es:ESHttpHead", "es:ESHttpPost", "es:ESHttpPut"]
    resources = ["${local.opensearch_domain_arn}/*"]
  }
}

resource "aws_iam_role" "index" {
  name               = "${local.prefix}-index-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy_attachment" "index_logs" {
  role       = aws_iam_role.index.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "index" {
  name   = "${local.prefix}-index-policy"
  role   = aws_iam_role.index.id
  policy = data.aws_iam_policy_document.index.json
}

# ---------------------------------------------------------------------------
# Lambda（同一コンテナイメージを CMD で parse / chunk_embed / index に切替）
# ---------------------------------------------------------------------------
resource "aws_cloudwatch_log_group" "parse" {
  name              = "/aws/lambda/${local.parse_function_name}"
  retention_in_days = 14
}

resource "aws_cloudwatch_log_group" "embed" {
  name              = "/aws/lambda/${local.embed_function_name}"
  retention_in_days = 14
}

resource "aws_cloudwatch_log_group" "index" {
  name              = "/aws/lambda/${local.index_function_name}"
  retention_in_days = 14
}

# parse: 1 PDF を丸ごと md 化。RapidLayout(ONNX) + ページ描画 + VLM のため重め。
resource "aws_lambda_function" "parse" {
  function_name = local.parse_function_name
  role          = aws_iam_role.parse.arn
  package_type  = "Image"
  image_uri     = "${aws_ecr_repository.ingest.repository_url}:latest"
  architectures = ["arm64"] # Docker のビルドプラットフォームと一致させる（Mac/Graviton=arm64）
  memory_size   = 2048      # ONNX + ページ描画 + 画像保存。1024 では OOM（実測）→ 倍増（RAM 増で CPU も増）
  timeout       = 900       # 大きな PDF + 逐次 VLM 呼び出しを考慮し最大値

  image_config {
    command = ["parse.handler.handler"]
  }

  depends_on = [aws_cloudwatch_log_group.parse]
}

# chunk_embed: md → チャンク → Cohere 埋め込み。ネットワーク主体で軽量。
resource "aws_lambda_function" "embed" {
  function_name = local.embed_function_name
  role          = aws_iam_role.embed.arn
  package_type  = "Image"
  image_uri     = "${aws_ecr_repository.ingest.repository_url}:latest"
  architectures = ["arm64"] # Docker のビルドプラットフォームと一致させる（Mac/Graviton=arm64）
  memory_size   = 512       # network 主体で軽量
  timeout       = 300

  image_config {
    command = ["chunk_embed.handler.handler"]
  }

  depends_on = [aws_cloudwatch_log_group.embed]
}

# index: embeddings JSON → OpenSearch bulk index。bulk 送信主体で軽量。
resource "aws_lambda_function" "index" {
  function_name = local.index_function_name
  role          = aws_iam_role.index.arn
  package_type  = "Image"
  image_uri     = "${aws_ecr_repository.ingest.repository_url}:latest"
  architectures = ["arm64"]
  memory_size   = 512
  timeout       = 300

  image_config {
    command = ["index.handler.handler"]
  }

  environment {
    variables = {
      OPENSEARCH_ENDPOINT = aws_opensearch_domain.search.endpoint
      OPENSEARCH_INDEX    = "chunks"
    }
  }

  depends_on = [aws_cloudwatch_log_group.index]
}

# ---------------------------------------------------------------------------
# Step Functions（source/ を listObjectsV2 → PDF ごとに parse → embed → index）
# ---------------------------------------------------------------------------
data "aws_iam_policy_document" "sfn_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["states.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "sfn" {
  # 3 つの ingest Lambda を呼び出す（:* で $LATEST/バージョン修飾子も許可）
  statement {
    sid     = "InvokeIngestLambdas"
    effect  = "Allow"
    actions = ["lambda:InvokeFunction"]
    resources = [
      aws_lambda_function.parse.arn, "${aws_lambda_function.parse.arn}:*",
      aws_lambda_function.embed.arn, "${aws_lambda_function.embed.arn}:*",
      aws_lambda_function.index.arn, "${aws_lambda_function.index.arn}:*",
    ]
  }
  # source/ を列挙するため
  statement {
    sid       = "ListSourceBucket"
    effect    = "Allow"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.data.arn]
  }
}

resource "aws_iam_role" "sfn" {
  name               = "${local.prefix}-sfn-role"
  assume_role_policy = data.aws_iam_policy_document.sfn_assume.json
}

resource "aws_iam_role_policy" "sfn" {
  name   = "${local.prefix}-sfn-policy"
  role   = aws_iam_role.sfn.id
  policy = data.aws_iam_policy_document.sfn.json
}

resource "aws_sfn_state_machine" "ingest" {
  name     = "${local.prefix}-sfn"
  role_arn = aws_iam_role.sfn.arn

  definition = templatefile("${path.module}/ingest.asl.json", {
    ProcessFunctionArn = aws_lambda_function.parse.arn
    EmbedFunctionArn   = aws_lambda_function.embed.arn
    IndexFunctionArn   = aws_lambda_function.index.arn
  })
}

# ---------------------------------------------------------------------------
# OpenSearch（検索ステージ）+ IndexDocument Lambda
# ---------------------------------------------------------------------------
# ポートフォリオ/デモ前提の「最小・最安」構成:
#   - 単一ノード t3.small.search（実用最安。t2 系は暗号化非対応で除外）
#   - 単一 AZ（zone_awareness 無効）/ 専用マスタ無し / スタンバイ無し
#   - gp3 10GiB（データ少量）/ パブリックエンドポイント（VPC/NAT 料金を回避）
#   データ量が少なく応答時間要件も無いため、冗長性・性能オプションは付けない。
# 認証は FGAC を使わず、ドメインアクセスポリシー（IAM）+ Lambda の SigV4 署名。
# ハイブリッド検索のマッピング（knn_vector + kuromoji）は index Lambda が初回に作成。
resource "aws_opensearch_domain" "search" {
  domain_name    = local.opensearch_domain_name
  engine_version = var.opensearch_engine_version

  cluster_config {
    instance_type  = var.opensearch_instance_type
    instance_count = 1 # 単一ノード（冗長性なし＝デモ用）
    # dedicated_master / zone_awareness は無効（既定）＝単一 AZ
  }

  ebs_options {
    ebs_enabled = true
    volume_type = "gp3"
    volume_size = var.opensearch_volume_size
    throughput  = 125 # gp3 最小スループット
  }

  # 暗号化は aws/es マネージドキー＝追加料金なし。t3.small.search は対応。
  encrypt_at_rest {
    enabled = true
  }
  node_to_node_encryption {
    enabled = true
  }
  domain_endpoint_options {
    enforce_https       = true
    tls_security_policy = "Policy-Min-TLS-1-2-2019-07"
  }

  access_policies = data.aws_iam_policy_document.opensearch_access.json

  tags = { Project = var.project_name }
}

# ドメインアクセスポリシー: index Lambda 実行ロールのみ HTTP アクセス許可（最小権限）。
# self-cycle を避けるためドメイン ARN は local（resource 参照しない）。
data "aws_iam_policy_document" "opensearch_access" {
  statement {
    sid       = "AllowIndexLambda"
    effect    = "Allow"
    actions   = ["es:ESHttpGet", "es:ESHttpHead", "es:ESHttpPost", "es:ESHttpPut"]
    resources = ["${local.opensearch_domain_arn}/*"]
    principals {
      type        = "AWS"
      identifiers = [aws_iam_role.index.arn]
    }
  }
}

# index Lambda（IAM・関数本体）は上の Lambda セクションに定義。

# ---------------------------------------------------------------------------
# フロントエンド配信: S3（非公開）+ CloudFront（OAC 経由でのみ読取）
#   バケット名 = <project_name>-frontend（= aws-rag-assistant-frontend）
#   S3 は完全非公開。CloudFront の Origin Access Control 経由のみアクセス可。
# ---------------------------------------------------------------------------
resource "aws_s3_bucket" "frontend" {
  bucket        = "${var.project_name}-frontend"
  force_destroy = true # ポートフォリオ用途: オブジェクトごと destroy 可
}

resource "aws_s3_bucket_public_access_block" "frontend" {
  bucket                  = aws_s3_bucket.frontend.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "frontend" {
  bucket = aws_s3_bucket.frontend.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# CloudFront → S3 の署名付きアクセス（OAI の後継。バケットは非公開のまま）
resource "aws_cloudfront_origin_access_control" "frontend" {
  name                              = "${var.project_name}-frontend-oac"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

resource "aws_cloudfront_distribution" "frontend" {
  enabled             = true
  default_root_object = "index.html"
  comment             = "${var.project_name} frontend"
  price_class         = "PriceClass_200" # 日本含むアジア/北米/欧州（全エッジより安価）

  origin {
    domain_name              = aws_s3_bucket.frontend.bucket_regional_domain_name
    origin_id                = "s3-frontend"
    origin_access_control_id = aws_cloudfront_origin_access_control.frontend.id
  }

  default_cache_behavior {
    target_origin_id       = "s3-frontend"
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD"]
    cached_methods         = ["GET", "HEAD"]
    # Managed-CachingOptimized（AWS 管理ポリシー・固定 ID）
    cache_policy_id = "658327ea-f89d-4fab-a63d-7e88639e58f6"
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    cloudfront_default_certificate = true # 独自ドメイン無し → *.cloudfront.net
  }
}

# CloudFront ディストリビューションのみ S3 読取を許可
data "aws_iam_policy_document" "frontend_bucket" {
  statement {
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.frontend.arn}/*"]
    principals {
      type        = "Service"
      identifiers = ["cloudfront.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "AWS:SourceArn"
      values   = [aws_cloudfront_distribution.frontend.arn]
    }
  }
}

resource "aws_s3_bucket_policy" "frontend" {
  bucket = aws_s3_bucket.frontend.id
  policy = data.aws_iam_policy_document.frontend_bucket.json
}
