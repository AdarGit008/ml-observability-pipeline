"""Cold-path archiver — drains recent reading rows to Parquet in S3.

Design locked by ADR 0015: watermark + per-pump Query read pattern,
pyarrow (no pandas) Parquet engine, 60 s EventBridge cadence. The
batcher moves rows; it computes nothing — no ``shared/`` import, by
the same outside-the-parity-set posture as ``dashboards_adapter``
(ADR 0014 §Decision 5).
"""
