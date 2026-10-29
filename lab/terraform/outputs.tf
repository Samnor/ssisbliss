output "instance_id" {
  value = aws_instance.sql.id
}

output "ami_id" {
  value = aws_instance.sql.ami
}

output "bucket" {
  value = aws_s3_bucket.lab.bucket
}

output "parameter_path" {
  value = local.param_path
}
