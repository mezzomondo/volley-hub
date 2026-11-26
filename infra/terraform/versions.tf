terraform {
  required_version = ">= 1.10" # S3 native state locking

  # Remote state in a separate S3 bucket (see scripts/bootstrap-state.sh).
  # Bucket and profile come from backend.hcl (not committed): terraform init -backend-config=backend.hcl
  backend "s3" {
    key          = "volley-hub/terraform.tfstate"
    region       = "eu-west-2"
    encrypt      = true
    use_lockfile = true
  }
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
