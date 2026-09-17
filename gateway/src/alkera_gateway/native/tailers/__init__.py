"""Server-side logs turned into agent.md 9.2.2 rows.

In native mode the agent runs `psql`, `aws s3`, `airflow` - clients that leave
no row of their own. Each tailer here reads the log the *server* already keeps
and calls `native.audit.record`, so the action log is produced by the thing that
executed the action rather than by the thing that asked for it. A client that
lies about what it ran therefore cannot forge a row, and a client the harness
has never heard of is still recorded.

    postgres.py  `log_statement=all` stderr log -> `psql.statement` rows
    minio.py     the MinIO audit webhook          -> `minio.<S3Op>` rows
    airflow.py   the api-server access log        -> `airflow.<op>` rows

What each one cannot see is measured and listed in `native/PARITY.md`.
"""
