data "aws_caller_identity" "current" {}

data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
  filter {
    name   = "default-for-az"
    values = ["true"]
  }
}

# Not every AZ offers every instance type (eu-west-2d has no t3a): pick a default subnet in one that does.
data "aws_ec2_instance_type_offerings" "here" {
  location_type = "availability-zone"
  filter {
    name   = "instance-type"
    values = [var.instance_type]
  }
}

data "aws_subnet" "candidate" {
  for_each = toset(data.aws_subnets.default.ids)
  id       = each.value
}

locals {
  usable_subnets = sort([
    for s in data.aws_subnet.candidate : s.id
    if contains(data.aws_ec2_instance_type_offerings.here.locations, s.availability_zone)
  ])
}

data "aws_subnet" "chosen" {
  id = local.usable_subnets[0]
}

data "aws_ssm_parameter" "ubuntu_ami" {
  name = "/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id"
}

locals {
  name   = "volley-hub"
  bucket = "volley-hub-${data.aws_caller_identity.current.account_id}-${var.region}"
  domain = var.domain != "" ? var.domain : "${replace(aws_eip.this.public_ip, ".", "-")}.sslip.io"
}

# ------------------------------------------------------------------ secrets

resource "random_password" "postgres" {
  length  = 32
  special = false
}

resource "random_password" "signing_key" {
  length  = 64
  special = false
}

resource "aws_ssm_parameter" "postgres_password" {
  name  = "/volley-hub/postgres_password"
  type  = "SecureString"
  value = random_password.postgres.result
}

resource "aws_ssm_parameter" "signing_key" {
  name  = "/volley-hub/signing_key"
  type  = "SecureString"
  value = random_password.signing_key.result
}

# ------------------------------------------------------------------ storage

resource "aws_s3_bucket" "this" {
  bucket = local.bucket
}

resource "aws_s3_bucket_public_access_block" "this" {
  bucket                  = aws_s3_bucket.this.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "this" {
  bucket = aws_s3_bucket.this.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "this" {
  bucket = aws_s3_bucket.this.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "this" {
  bucket = aws_s3_bucket.this.id

  rule {
    id     = "db-backups-90-days"
    status = "Enabled"
    filter {
      prefix = "backups/db/"
    }
    expiration {
      days = 90
    }
  }

  rule {
    id     = "logs-365-days"
    status = "Enabled"
    filter {
      prefix = "logs/"
    }
    expiration {
      days = 365
    }
  }

  rule {
    id     = "old-versions-30-days"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration {
      noncurrent_days = 30
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

resource "aws_ebs_volume" "data" {
  availability_zone = data.aws_subnet.chosen.availability_zone
  size              = var.data_volume_gb
  type              = "gp3"
  encrypted         = true
  tags = {
    Name = "${local.name}-data"
  }

  lifecycle {
    prevent_destroy = true
  }
}

# ------------------------------------------------------------------ network

resource "aws_security_group" "web" {
  name        = "${local.name}-web"
  description = "HTTP and HTTPS only; admin access through SSM"
  vpc_id      = data.aws_vpc.default.id

  ingress {
    description      = "HTTP (ACME challenge and redirect)"
    from_port        = 80
    to_port          = 80
    protocol         = "tcp"
    cidr_blocks      = ["0.0.0.0/0"]
    ipv6_cidr_blocks = ["::/0"]
  }

  ingress {
    description      = "HTTPS"
    from_port        = 443
    to_port          = 443
    protocol         = "tcp"
    cidr_blocks      = ["0.0.0.0/0"]
    ipv6_cidr_blocks = ["::/0"]
  }

  ingress {
    description      = "HTTP/3"
    from_port        = 443
    to_port          = 443
    protocol         = "udp"
    cidr_blocks      = ["0.0.0.0/0"]
    ipv6_cidr_blocks = ["::/0"]
  }

  egress {
    from_port        = 0
    to_port          = 0
    protocol         = "-1"
    cidr_blocks      = ["0.0.0.0/0"]
    ipv6_cidr_blocks = ["::/0"]
  }
}

resource "aws_eip" "this" {
  domain = "vpc"
  tags = {
    Name = local.name
  }
}

# ------------------------------------------------------------------ instance role

data "aws_iam_policy_document" "assume_ec2" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "instance" {
  name               = "${local.name}-instance"
  assume_role_policy = data.aws_iam_policy_document.assume_ec2.json
}

resource "aws_iam_role_policy_attachment" "ssm_core" {
  role       = aws_iam_role.instance.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

data "aws_iam_policy_document" "instance" {
  statement {
    sid       = "Bucket"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.this.arn]
  }
  statement {
    sid       = "Objects"
    actions   = ["s3:GetObject", "s3:PutObject"]
    resources = ["${aws_s3_bucket.this.arn}/*"]
  }
  statement {
    sid       = "Secrets"
    actions   = ["ssm:GetParameter", "ssm:GetParameters"]
    resources = ["arn:aws:ssm:${var.region}:${data.aws_caller_identity.current.account_id}:parameter/volley-hub/*"]
  }
}

resource "aws_iam_role_policy" "instance" {
  name   = "${local.name}-instance"
  role   = aws_iam_role.instance.id
  policy = data.aws_iam_policy_document.instance.json
}

resource "aws_iam_instance_profile" "instance" {
  provider = aws.untagged
  name     = "${local.name}-instance"
  role     = aws_iam_role.instance.name
}

# ------------------------------------------------------------------ server

resource "aws_instance" "this" {
  ami                    = data.aws_ssm_parameter.ubuntu_ami.value
  instance_type          = var.instance_type
  subnet_id              = data.aws_subnet.chosen.id
  vpc_security_group_ids = [aws_security_group.web.id]
  iam_instance_profile   = aws_iam_instance_profile.instance.name

  metadata_options {
    http_tokens = "required"
  }

  root_block_device {
    volume_size = 30
    volume_type = "gp3"
    encrypted   = true
  }

  user_data = templatefile("${path.module}/user_data.sh.tftpl", {
    region         = var.region
    bucket         = aws_s3_bucket.this.bucket
    domain         = local.domain
    repo_url       = var.repo_url
    repo_ref       = var.repo_ref
    data_volume_id = replace(aws_ebs_volume.data.id, "-", "")
  })

  tags = {
    Name = local.name
  }

  lifecycle {
    # A new Ubuntu image or bootstrap script must not silently replace the server.
    ignore_changes = [ami, user_data]
  }
}

resource "aws_volume_attachment" "data" {
  device_name = "/dev/sdf"
  volume_id   = aws_ebs_volume.data.id
  instance_id = aws_instance.this.id
}

resource "aws_eip_association" "this" {
  allocation_id = aws_eip.this.id
  instance_id   = aws_instance.this.id
}
