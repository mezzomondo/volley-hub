variable "region" {
  type    = string
  default = "eu-west-2"
}

variable "aws_profile" {
  description = "AWS CLI profile used by Terraform."
  type        = string
  default     = "volley-hub"
}

variable "instance_type" {
  description = "x86_64 type (the R/datavolley ingest image uses amd64 binaries)."
  type        = string
  default     = "t3a.medium"
}

variable "data_volume_gb" {
  description = "EBS volume for videos, clips, database and certificates (kept if the VM is replaced)."
  type        = number
  default     = 200
}

variable "domain" {
  description = "Public host name. Empty: <ip-with-dashes>.sslip.io, which works with Let's Encrypt without DNS setup."
  type        = string
  default     = ""
}

variable "repo_url" {
  type    = string
  default = "https://github.com/mezzomondo/volley-hub.git"
}

variable "repo_ref" {
  description = "Branch or tag deployed on the server."
  type        = string
  default     = "main"
}
