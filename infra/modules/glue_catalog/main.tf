# Glue Catalog database + table for the Parquet archive (ADR 0015
# §Decision 4). Schema declared HERE, in Terraform — never a Crawler
# (anti-pattern list: standing cost + it bypasses the exact
# schema-as-code rule this module exists for).
#
# Partition projection: Athena computes partition locations from the
# query predicate via the projection.* parameters below, so NOTHING
# registers partitions — no Crawler, no glue:CreatePartition from the
# batcher, no drift between the S3 layout and the catalog. Catalog
# object count stays at 2 (database + table), inside the 1 M
# Always-Free ceiling forever (verified 2026-06-04, ADR 0015).
#
# Columns mirror lambda_s3_batcher.handler.PARQUET_SCHEMA exactly —
# the reading-row fields (ADR 0010) plus pump_id; `ts` is the
# ISO-8601 string the DynamoDB sort key carried. A schema change
# there lands here in the same PR or Athena reads nulls.

resource "aws_glue_catalog_database" "archive" {
  name        = var.database_name
  description = "Cold-path Parquet archive of pump telemetry readings (ADR 0015)."
}

resource "aws_glue_catalog_table" "readings" {
  name          = var.table_name
  database_name = aws_glue_catalog_database.archive.name
  table_type    = "EXTERNAL_TABLE"

  parameters = {
    EXTERNAL       = "TRUE"
    classification = "parquet"

    # Partition projection (no Crawler, no CreatePartition — ever).
    # Out-of-range behavior (2026-06-04 cascade Q4): Athena queries
    # whose partition predicate falls outside the projected ranges
    # return EMPTY results, not errors; unfiltered queries enumerate
    # only projected partitions. Widen projection.year.range here if
    # the project outlives 2035.
    "projection.enabled"            = "true"
    "projection.year.type"          = "integer"
    "projection.year.range"         = "2025,2035"
    "projection.year.digits"        = "4"
    "projection.month.type"         = "integer"
    "projection.month.range"        = "1,12"
    "projection.month.digits"       = "2"
    "projection.day.type"           = "integer"
    "projection.day.range"          = "1,31"
    "projection.day.digits"         = "2"
    "projection.hour.type"          = "integer"
    "projection.hour.range"         = "0,23"
    "projection.hour.digits"        = "2"
    "storage.location.template"     = "s3://${var.bucket_name}/year=$${year}/month=$${month}/day=$${day}/hour=$${hour}/"
  }

  partition_keys {
    name = "year"
    type = "string"
  }
  partition_keys {
    name = "month"
    type = "string"
  }
  partition_keys {
    name = "day"
    type = "string"
  }
  partition_keys {
    name = "hour"
    type = "string"
  }

  storage_descriptor {
    location      = "s3://${var.bucket_name}/"
    input_format  = "org.apache.hadoop.hive.ql.io.parquet.MapredParquetInputFormat"
    output_format = "org.apache.hadoop.hive.ql.io.parquet.MapredParquetOutputFormat"

    ser_de_info {
      name                  = "parquet"
      serialization_library = "org.apache.hadoop.hive.ql.io.parquet.serde.ParquetHiveSerDe"
    }

    columns {
      name = "pump_id"
      type = "string"
    }
    columns {
      name = "ts"
      type = "string"
      comment = "ISO-8601 UTC ms — identical to the DynamoDB sort key it came from"
    }
    columns {
      name = "vibration_amp"
      type = "double"
    }
    columns {
      name = "bearing_temp"
      type = "double"
    }
    columns {
      name = "motor_current"
      type = "double"
    }
    columns {
      name = "rpm"
      type = "double"
    }
    columns {
      name = "score"
      type = "double"
      comment = "P(failure_48h) the scorer wrote with the reading (ADR 0010)"
    }
  }
}
