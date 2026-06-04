# Cold-path archive bucket (ADR 0015). Receives one Parquet file per
# batch from lambda_s3_batcher under year=/month=/day=/hour=/
# (_interfaces.md §S3 archive layout); the Glue table reads it.
#
# Cost posture (verified 2026-06-04, ADR 0015 §Context): 5 GB S3
# Standard is Always-Free; a demo writes ~1 MB + ~30 PUTs
# (~$0.0002/demo residue, recorded in the ADR).
#
# force_destroy = true is a recorded PO call (ADR 0015 §Decision 5):
# the archive is demo-ephemeral by design — apply → demo → teardown —
# so `terraform destroy` must not strand on a non-empty bucket.
# aws_teardown.sh verifies absence afterward.

data "aws_caller_identity" "current" {}

resource "aws_s3_bucket" "archive" {
  # Account-id suffix: bucket names are GLOBALLY unique; this keeps
  # the name deterministic (teardown can derive it) without a random
  # provider.
  bucket        = "${var.name_prefix}-pump-archive-${data.aws_caller_identity.current.account_id}"
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "archive" {
  bucket = aws_s3_bucket.archive.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "archive" {
  bucket = aws_s3_bucket.archive.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256" # SSE-S3: free, no KMS key to manage/tear down
    }
  }
}
