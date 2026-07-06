terraform {
  required_version = ">= 1.9"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.52"
    }
  }

  # 個人プロジェクトのため state は local（terraform.tfstate）を使用。
}
