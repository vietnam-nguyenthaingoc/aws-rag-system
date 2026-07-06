variable "project_name" {
  description = "リソース名のプレフィックス（全リソースが <project_name>-... で命名される）"
  type        = string
  default     = "aws-rag-assistant"
}

variable "region" {
  description = "デプロイ先 AWS リージョン（apac. 推論プロファイル + 日本語データに合わせ東京）"
  type        = string
  default     = "ap-northeast-1"
}

variable "vlm_inference_profile_id" {
  description = "VLM（Claude Haiku 4.5）のクロスリージョン推論プロファイル ID（jp.=データを日本国内に保持）"
  type        = string
  default     = "jp.anthropic.claude-haiku-4-5-20251001-v1:0"
}

variable "vlm_foundation_model_id" {
  description = "推論プロファイルがルーティングする基盤モデル ID"
  type        = string
  default     = "anthropic.claude-haiku-4-5-20251001-v1:0"
}

variable "embed_model_id" {
  description = "埋め込みモデル ID（リージョン内で直接呼び出し。バージョン修飾子 :0 まで必須）"
  type        = string
  default     = "amazon.titan-embed-text-v2:0"
}

# --- OpenSearch（デモ最小・最安構成）---
variable "opensearch_instance_type" {
  description = "OpenSearch データノードのインスタンスタイプ（t3.small.search=実用最安。t2 は暗号化非対応）"
  type        = string
  default     = "t3.small.search"
}

variable "opensearch_engine_version" {
  description = "OpenSearch エンジンバージョン（knn_vector + kuromoji + hybrid 対応の 2.x）"
  type        = string
  default     = "OpenSearch_2.17"
}

variable "opensearch_volume_size" {
  description = "データノードの EBS(gp3) サイズ(GiB)。データ少量のため最小の 10"
  type        = number
  default     = 10
}

