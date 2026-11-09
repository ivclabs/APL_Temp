terraform {
  required_version = ">= 1.10"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

provider "aws" {
  region = var.region

  # Every resource gets these tags, so it's easy to see what belongs to this project.
  default_tags {
    tags = {
      Project   = "APL_Temp"
      ManagedBy = "terraform"
    }
  }
}
