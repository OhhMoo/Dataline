"""Exercise the shipped toolbelt against the live stack, from inside the episode.

`toolbelt_check.py` proves each binary runs. That is not the same as proving it is
*usable*: an `aws` that answers NoCredentialsError and a `psql` that cannot reach the
server both pass a `--version` probe. This script drives the tools against the real
Postgres, the real S3Mock and the real gateway, through the shell the agent gets.

    python gateway/docker/toolbelt_acceptance.py --project alkera-toolbelt

It brings its own stack up and tears it down, so use a dedicated project name. It
writes evidence/toolbelt.json. It does NOT check containment - containment.py does,
and it must be re-run after any toolbelt change, because curl and aws are exactly
the tools that would find a hole if one existed.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from containment import HERE, Stack

XLSX_PATH = "/tmp/toolbelt.xlsx"
MKXLSX_PATH = "/tmp/mkxlsx.py"

# (name, shell command, required substring of stdout). Every command runs under
# `sh -c` inside the episode container: the agent's own vantage, not the host's.
# An empty expectation means "any non-empty output" and is used where the payload
# belongs to another track's contract and this file must not freeze it.
CHECKS: tuple[tuple[str, str, str], ...] = (
    (
        "psql_reaches_postgres",
        'psql "$ALKERA_POSTGRES_DSN" -At -c "select current_database()"',
        "episode",
    ),
    (
        "psql_reads_the_warehouse_schema",
        'psql "$ALKERA_POSTGRES_DSN" -At -c '
        "\"select count(*) from information_schema.schemata where schema_name='warehouse'\"",
        "1",
    ),
    (
        "pg_dump_major_is_not_older_than_the_server",
        "pg_dump --version | grep -oE '[0-9]+' | head -1",
        "17",
    ),
    ("aws_lists_buckets", "aws s3 ls | awk '{print $NF}' | sort | tr '\\n' ' '", "landing"),
    (
        "aws_round_trips_an_object",
        "printf toolbelt-acceptance > /tmp/tb.txt "
        "&& aws s3 cp --quiet /tmp/tb.txt s3://landing/toolbelt.txt "
        "&& aws s3 cp --quiet s3://landing/toolbelt.txt - "
        "&& aws s3 rm --quiet s3://landing/toolbelt.txt",
        "toolbelt-acceptance",
    ),
    (
        "curl_and_jq_drive_a_tool_call",
        "curl -sS -X POST http://127.0.0.1:8080/call -H 'content-type: application/json' "
        '-d \'{"tool":"sql_query","args":{"sql":"select 1 as n"}}\' '
        "| jq -r '.result.rows[0][0] | if type==\"object\" then .value else . end'",
        "1",
    ),
    ("jq_reads_the_action_log", "jq -r .tool /audit/actions.jsonl | sort -u | tr '\\n' ' '", ""),
    (
        "sqlfluff_lints_sql",
        "printf 'select A from warehouse.items\\n' > /tmp/model.sql "
        "&& sqlfluff lint --dialect postgres --format json /tmp/model.sql "
        "| jq '[.[].violations[].code] | length > 0'",
        "true",
    ),
    (
        "csvkit_profiles_a_csv",
        "printf 'a,b\\n1,2\\n3,4\\n' > /tmp/t.csv && csvstat --count /tmp/t.csv",
        "2",
    ),
    (
        "in2csv_reads_an_xlsx",
        # excel_close writes .xlsx into the workspace and nothing in the gateway's own
        # environment can open one, so this is the agent's only route to that data.
        f"python {MKXLSX_PATH} {XLSX_PATH} && in2csv {XLSX_PATH} | tr '\\n' ' '",
        "h1,h2 v1,v2",
    ),
    ("unzip_opens_an_xlsx", f"unzip -l {XLSX_PATH} | grep -c xl/worksheets", "1"),
    ("file_identifies_an_xlsx", f"file -b {XLSX_PATH} | cut -c1-20", "Microsoft Excel 2007"),
    ("git_reads_the_workspace", "git -C /world/workspace rev-parse --is-inside-work-tree", "true"),
    ("tree_walks_the_world", "tree -L 2 /world | tail -1", "director"),
    ("ps_sees_the_gateway", "ps -eo args= | grep -c '[a]lkera-gateway'", "1"),
    ("nc_probes_a_backend_port", "nc -z -w 3 postgres 5432 && echo open", "open"),
    ("dig_resolves_a_service_name", "dig +short postgres | head -1 | grep -cE '^[0-9]+\\.'", "1"),
    ("less_pages_a_file", "less -F -X /world/workspace/docs/metrics.md", ""),
    (
        "make_runs_a_makefile",
        "mkdir -p /tmp/mk && printf 'x:\\n\\t@echo made\\n' > /tmp/mk/Makefile "
        "&& make -C /tmp/mk x",
        "made",
    ),
    ("openssl_hashes", "printf abc | openssl dgst -sha256 -r | cut -c1-8", "ba7816bf"),
    ("ssh_client_present", "ssh -V 2>&1 | cut -c1-8", "OpenSSH_"),
    ("vim_is_the_real_vim", "vim --version | head -1", "VIM - Vi IMproved"),
    ("nano_present", "nano --version | head -1", "GNU nano"),
)

# The toolbelt hands the agent far better egress tooling than `git` and `openssl` did.
# That is intended - the agent *should* be able to try - so these record the attempt
# with the tools rather than with a python socket. `containment.py` remains the proof
# (it probes from inside every service's namespace and carries a positive control);
# this is the same boundary asked the way an agent would ask it. A check passes when
# the command FAILS: non-zero exit, or no output.
EGRESS_ATTEMPTS: tuple[tuple[str, str], ...] = (
    ("curl_cannot_reach_pypi", "curl -sS --max-time 6 https://pypi.org/simple/"),
    ("curl_cannot_reach_a_bare_ip", "curl -sS --max-time 6 http://1.1.1.1/"),
    ("dig_cannot_resolve_a_public_name", "dig +short +time=2 +tries=1 example.com"),
    ("ping_cannot_leave", "ping -c 1 -W 2 8.8.8.8"),
    ("nc_cannot_open_a_public_port", "nc -z -w 3 1.1.1.1 443 && echo open"),
    ("aws_cannot_reach_real_s3", "aws s3 ls --endpoint-url https://s3.amazonaws.com"),
    (
        "ssh_cannot_leave",
        "ssh -o ConnectTimeout=4 -o StrictHostKeyChecking=no -o BatchMode=yes "
        "git@github.com true",
    ),
)

# A stdlib .xlsx writer. world/world/themes/excel_close/gen.py writes workbooks the
# same way (zipfile + inline-string XML); this makes one so the check opens a real
# workbook rather than a file named .xlsx.
MKXLSX = '''import sys, zipfile

ROWS = [["h1", "h2"], ["v1", "v2"]]
NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG = "http://schemas.openxmlformats.org/package/2006/relationships"
CT = "http://schemas.openxmlformats.org/package/2006/content-types"
DOC = "application/vnd.openxmlformats-officedocument.spreadsheetml"
RELCT = "application/vnd.openxmlformats-package.relationships+xml"

sheet = "".join(
    '<row r="%d">%s</row>'
    % (
        r + 1,
        "".join(
            '<c r="%s%d" t="inlineStr"><is><t>%s</t></is></c>' % (chr(65 + c), r + 1, v)
            for c, v in enumerate(cols)
        ),
    )
    for r, cols in enumerate(ROWS)
)
with zipfile.ZipFile(sys.argv[1], "w") as z:
    z.writestr(
        "[Content_Types].xml",
        '<?xml version="1.0"?><Types xmlns="%s">'
        '<Default Extension="rels" ContentType="%s"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="%s.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="%s.worksheet+xml"/>'
        "</Types>" % (CT, RELCT, DOC, DOC),
    )
    z.writestr(
        "_rels/.rels",
        '<?xml version="1.0"?><Relationships xmlns="%s">'
        '<Relationship Id="rId1" Type="%s/officeDocument" Target="xl/workbook.xml"/>'
        "</Relationships>" % (PKG, REL),
    )
    z.writestr(
        "xl/workbook.xml",
        '<?xml version="1.0"?><workbook xmlns="%s" xmlns:r="%s">'
        '<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>' % (NS, REL),
    )
    z.writestr(
        "xl/_rels/workbook.xml.rels",
        '<?xml version="1.0"?><Relationships xmlns="%s">'
        '<Relationship Id="rId1" Type="%s/worksheet" Target="worksheets/sheet1.xml"/>'
        "</Relationships>" % (PKG, REL),
    )
    z.writestr(
        "xl/worksheets/sheet1.xml",
        '<?xml version="1.0"?><worksheet xmlns="%s"><sheetData>%s</sheetData></worksheet>'
        % (NS, sheet),
    )
'''


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", default="alkera-toolbelt")
    parser.add_argument("--out", type=Path, default=HERE / "evidence" / "toolbelt.json")
    parser.add_argument("--keep-up", action="store_true")
    args = parser.parse_args()

    stack = Stack(args.project, [])
    rows, egress = [], []
    try:
        stack.compose("up", "-d", "--wait")
        stack.compose(
            "exec",
            "-T",
            "episode",
            "python",
            "-c",
            f"open({MKXLSX_PATH!r}, 'w', encoding='utf-8').write({MKXLSX!r})",
        )
        for name, command, expected in CHECKS:
            done = stack.compose("exec", "-T", "episode", "sh", "-c", command, check=False)
            output = (done.stdout or "").strip()
            ok = done.returncode == 0 and (expected in output if expected else bool(output))
            rows.append(
                {
                    "check": name,
                    "command": command,
                    "exit": done.returncode,
                    "stdout": output[:400],
                    "stderr": (done.stderr or "").strip()[:300],
                    "expected_substring": expected or "(any non-empty output)",
                    "ok": ok,
                }
            )
        for name, command in EGRESS_ATTEMPTS:
            done = stack.compose("exec", "-T", "episode", "sh", "-c", command, check=False)
            output = (done.stdout or "").strip()
            egress.append(
                {
                    "check": name,
                    "command": command,
                    "exit": done.returncode,
                    "stdout": output[:300],
                    "stderr": (done.stderr or "").strip()[:300],
                    "expectation": "command fails or returns nothing",
                    "ok": done.returncode != 0 or not output,
                }
            )
    finally:
        if not args.keep_up:
            stack.compose("down", "-v", "-t", "5", check=False)

    evidence = {
        "generated": datetime.now(UTC).isoformat(timespec="seconds"),
        "project": args.project,
        "vantage": "sh -c inside the episode container - the agent's own shell",
        "checks": rows,
        "egress_attempts": egress,
        "failed": [r["check"] for r in rows if not r["ok"]]
        + [r["check"] for r in egress if not r["ok"]],
        "passed": all(r["ok"] for r in rows) and all(r["ok"] for r in egress),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(evidence, indent=2, sort_keys=True), encoding="utf-8")
    print(
        json.dumps(
            {
                "artifact": str(args.out),
                "checks": len(rows),
                "egress_attempts_all_blocked": all(r["ok"] for r in egress),
                "failed": evidence["failed"],
                "passed": evidence["passed"],
            },
            indent=2,
        )
    )
    return 0 if evidence["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
