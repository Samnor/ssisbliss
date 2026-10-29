terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}

provider "aws" {
  profile = var.aws_profile != "" ? var.aws_profile : null
  region  = var.region

  default_tags {
    tags = {
      project = var.name
    }
  }
}

# Default to the latest Windows Server base image when no golden AMI is given.
data "aws_ssm_parameter" "base_ami" {
  name = var.base_ami_parameter
}

locals {
  ami_id     = var.ami_id != "" ? var.ami_id : nonsensitive(data.aws_ssm_parameter.base_ami.value)
  param_path = "/${var.name}"
}

# ---------------------------------------------------------------- network ---

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

# No inbound rules: all access goes through SSM Session Manager.
resource "aws_security_group" "lab" {
  name        = "${var.name}-sg"
  description = "Lab host, outbound only"
  vpc_id      = data.aws_vpc.default.id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# ---------------------------------------------------------------- storage ---

resource "random_id" "bucket" {
  byte_length = 4
}

# Installers, backups and SSIS project files.
resource "aws_s3_bucket" "lab" {
  bucket        = "${var.name}-${random_id.bucket.hex}"
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "lab" {
  bucket                  = aws_s3_bucket.lab.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# ---------------------------------------------------------------- secrets ---
# Passwords live in SSM Parameter Store under /<name>/ and are created by lab.py,
# not Terraform, so they survive `lab.py down` and keep matching saved images.

# -------------------------------------------------------------------- iam ---

data "aws_iam_policy_document" "assume_ec2" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "lab" {
  name               = "${var.name}-host"
  assume_role_policy = data.aws_iam_policy_document.assume_ec2.json
}

resource "aws_iam_role_policy_attachment" "ssm_core" {
  role       = aws_iam_role.lab.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

data "aws_iam_policy_document" "lab_access" {
  statement {
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.lab.arn]
  }
  statement {
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
    resources = ["${aws_s3_bucket.lab.arn}/*"]
  }
  statement {
    actions   = ["ssm:GetParameter", "ssm:GetParameters"]
    resources = ["arn:aws:ssm:${var.region}:*:parameter${local.param_path}/*"]
  }
}

resource "aws_iam_role_policy" "lab_access" {
  name   = "lab-access"
  role   = aws_iam_role.lab.id
  policy = data.aws_iam_policy_document.lab_access.json
}

resource "aws_iam_instance_profile" "lab" {
  name = "${var.name}-host"
  role = aws_iam_role.lab.name
}

# ---------------------------------------------------------------- compute ---

resource "aws_instance" "sql" {
  ami                         = local.ami_id
  instance_type               = var.instance_type
  subnet_id                   = sort(data.aws_subnets.default.ids)[0]
  vpc_security_group_ids      = [aws_security_group.lab.id]
  iam_instance_profile        = aws_iam_instance_profile.lab.name
  associate_public_ip_address = true

  metadata_options {
    http_tokens = "required"
  }

  root_block_device {
    volume_type = "gp3"
    volume_size = var.volume_size_gb
    encrypted   = true
  }

  tags = {
    Name        = "${var.name}-sql"
    lab_version = var.lab_version
  }
}

# A forgotten lab costs money: stop (not terminate) the host when it has been idle for a while.
# Builds and seeding keep the CPU busy; `lab.py start` brings it back.
resource "aws_cloudwatch_metric_alarm" "idle_stop" {
  count               = var.idle_stop_minutes > 0 ? 1 : 0
  alarm_name          = "${var.name}-idle-stop"
  alarm_description   = "Stop the lab host after ${var.idle_stop_minutes} idle minutes"
  namespace           = "AWS/EC2"
  metric_name         = "CPUUtilization"
  dimensions          = { InstanceId = aws_instance.sql.id }
  statistic           = "Average"
  period              = 300
  evaluation_periods  = ceil(var.idle_stop_minutes / 5)
  datapoints_to_alarm = ceil(var.idle_stop_minutes / 5)
  threshold           = 4
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = ["arn:aws:automate:${var.region}:ec2:stop"]
}
