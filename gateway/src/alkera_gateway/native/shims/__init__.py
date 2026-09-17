"""PATH-front wrappers for binaries that touch the world without a server.

`psql`, `aws` and `airflow` talk to something that keeps a log, so a tailer can
reproduce their rows from the server side. `dbt`, `python` and `git` do not:
they open the warehouse file, the workspace and the repo directly, and if they
are not wrapped the world changes with no audited row - which is precisely the
blindness violation native mode has to keep detectable.

So the binary stays real and a wrapper goes in front of it on PATH. The wrapper
records argv, cwd, exit code, latency and, by diffing sha256 over the workspace
across the call, which files the binary actually changed. That last part is the
point: `dbt run` and `python fix.py` name no target on their command line, and
the changed-file set is the only honest answer to "what did this touch".

    install.py  materialises the wrappers into a bin dir to prepend to PATH
    run.py      the wrapper body; one process per invocation
"""
