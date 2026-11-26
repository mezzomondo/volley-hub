output "region" {
  value = var.region
}

output "instance_id" {
  value = aws_instance.this.id
}

output "public_ip" {
  value = aws_eip.this.public_ip
}

output "bucket" {
  value = aws_s3_bucket.this.bucket
}

output "url" {
  value = "https://${local.domain}"
}

output "mcp_url" {
  value = "https://${local.domain}/mcp"
}
