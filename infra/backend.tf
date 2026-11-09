# Terraform's memory ("state") is stored in S3, not on one PC.
# The bucket was created once by hand (Terraform can't create the bucket it stores itself in).
terraform {
  backend "s3" {
    bucket       = "apl-temp-tfstate-446476244232"
    key          = "apl-temp/terraform.tfstate"
    region       = "eu-north-1"
    encrypt      = true
    use_lockfile = true # stops two people running terraform at the same time
  }
}
