terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}

provider "aws" {
  region  = var.region
  profile = var.aws_profile

  default_tags {
    tags = {
      project = "volley-hub"
    }
  }
}

# Same account without default tags: the deploy user may not tag IAM instance profiles.
provider "aws" {
  alias   = "untagged"
  region  = var.region
  profile = var.aws_profile
}
