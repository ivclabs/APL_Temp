variable "region" {
  type    = string
  default = "eu-north-1" # Stockholm - closest to Sweden
}

variable "instance_type" {
  type    = string
  default = "t3.micro" # free-tier eligible, enough for a small Flask app
}

variable "my_ip_cidr" {
  type        = string
  description = "Your home IP, e.g. 84.246.94.47/32. Only this address may use SSH."
}

variable "ssh_public_key_path" {
  type    = string
  default = "~/.ssh/apl_temp_ed25519.pub"
}

variable "api_key" {
  type        = string
  sensitive   = true
  description = "Secret key the ESP32 sends in the X-API-Key header."
}

variable "repo_url" {
  type    = string
  default = "https://github.com/ivclabs/APL_Temp.git"
}
