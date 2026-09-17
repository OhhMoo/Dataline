"""Native surfaces: the agent works through real CLIs, SDKs and vendor-shaped
APIs instead of a bespoke tool list. Specification: gateway/NATIVE.md.

Layout
    audit.py      the single 9.2.2 row writer every service and shim calls
    context.py    the world, the run-and-record span and the ledger every
                  service shares (native/INTEGRATION_GATEWAY.md)
    services/     one FastAPI app per vendor shape (jira, slack, ...)
    tailers/      server-side logs -> 9.2.2 rows (postgres, minio, airflow, fakesnow)
    shims/        PATH-front wrappers for binaries that touch the world directly
    serve.py      mounts every registered service under its DNS alias
"""
