output "bucket_name" {
  description = "Archive bucket name — injected into the batcher env as S3_BUCKET and into the Glue table location."
  value       = aws_s3_bucket.archive.bucket
}

output "bucket_arn" {
  value = aws_s3_bucket.archive.arn
}
