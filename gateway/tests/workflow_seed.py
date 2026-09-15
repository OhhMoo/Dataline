"""Deterministic seed content for the issue tracker, chat and wiki.

This is the reference corpus the `workflow_*` tools were built against, and the
shape a world theme has to emit (`gateway/WORKFLOW_CONTRACT.md`). It lives in
`gateway/tests/` rather than in `world/` because Track A owns `world/` and is
editing it right now; nothing here imports the gateway, so a theme generator can
port it verbatim without reversing the dependency arrow.

Everything is anchored on the incident the observability fixture already models,
so the four surfaces tell one story:

    2026-03-04 01:00  daily_load fails   - "connection reset by peer", dt=2026-03-03
    2026-03-05 01:00  daily_load backfills two days and goes green, in 5 minutes
    world clock       2026-03-05T09:00:00Z
    the data          03-03 and 03-04 at a quarter of normal volume,
                      every amount on 03-04 null

The honest cause is in `#data-eng`, in an @-mention nobody answered: the 03-03
drop was still uploading when the loader read it. Nothing says so directly. The
loudest voice in `#incidents` says something else, confidently, and is wrong.

`authoring` blocks are author-side. `workflow.py` strips them from every
response; `test_workflow.py` asserts the notes below never reach an agent.
"""

import json

USERS = [
    {"id": "mrivera", "name": "Maya Rivera", "role": "Finance analyst", "team": "Finance"},
    {"id": "dpatel", "name": "Divya Patel", "role": "Analytics engineer", "team": "Data"},
    {
        "id": "kliu",
        "name": "Kenji Liu",
        "role": "Data engineer",
        "team": "Data",
        "away_until": "2026-03-23",
    },
    {"id": "sokonkwo", "name": "Sam Okonkwo", "role": "Vendor operations", "team": "Ops"},
    {"id": "tokafor", "name": "Tolu Okafor", "role": "Data platform lead", "team": "Data"},
    {"id": "alertbot", "name": "Alert bot", "role": "Automation", "team": "Data"},
    {"id": "agent", "name": "On-call data engineer", "role": "On call", "team": "Data"},
]

ISSUES = {
    "version": 1,
    "project": {"key": "DATA", "name": "Data platform"},
    "statuses": ["open", "triage", "in_progress", "blocked", "done", "wontfix"],
    "users": USERS,
    "issues": [
        {
            "id": "DATA-118",
            "title": "Upgrade dbt to 1.9 before the December freeze",
            "body": (
                "We are two minor versions behind and the December change freeze "
                "starts on the 15th.\n\nPlan: bump in a branch, run the full build "
                "against a copy of the warehouse, merge on green."
            ),
            "status": "done",
            "reporter": "tokafor",
            "assignee": "dpatel",
            "labels": ["maintenance"],
            "created_at": "2025-11-20T11:03:00Z",
            "updated_at": "2025-12-03T16:41:00Z",
            "comments": [
                {
                    "id": "DATA-118#1",
                    "author": "dpatel",
                    "at": "2025-12-03T16:41:00Z",
                    "body": "Merged and deployed. No model changes needed.",
                }
            ],
            "history": [
                {
                    "at": "2025-12-03T16:41:00Z",
                    "actor": "dpatel",
                    "field": "status",
                    "from": "in_progress",
                    "to": "done",
                }
            ],
            "links": [],
            "authoring": {
                "kind": "noise",
                "note": "WF-A1 closed and irrelevant; exists so issues_list is not "
                "eight tickets that all matter.",
            },
        },
        {
            "id": "DATA-127",
            "title": "Write an SLA page for the vendor feed",
            "body": (
                "We have never written down when the vendor drop is supposed to "
                "land or what we do when it does not. Kenji has it in his head."
            ),
            "status": "open",
            "reporter": "tokafor",
            "assignee": "kliu",
            "labels": ["documentation"],
            "created_at": "2026-01-06T09:15:00Z",
            "updated_at": "2026-01-08T14:22:00Z",
            "comments": [
                {
                    "id": "DATA-127#1",
                    "author": "kliu",
                    "at": "2026-01-08T14:22:00Z",
                    "body": "Will do once the Acme migration is settled.",
                }
            ],
            "history": [],
            "links": [],
            "authoring": {
                "kind": "stale",
                "note": "WF-A2 open and untouched for 56 days, assigned to somebody on "
                "leave. The page it asks for now exists (de/runbooks/vendor-feed-sla) "
                "and nobody closed the ticket.",
            },
        },
        {
            "id": "DATA-132",
            "title": "Some orders rows have no amount",
            "body": (
                "Roughly 300 rows across November and December have a null amount.\n\n"
                "Cause: the vendor renamed amount_usd to amount on 2025-11-02 and our "
                "loader kept writing the old column for nine days. Backfilled on "
                "2026-01-23; leaving this open until the data test is added."
            ),
            "status": "open",
            "reporter": "dpatel",
            "assignee": None,
            "labels": ["data-quality"],
            "created_at": "2026-01-22T13:40:00Z",
            "updated_at": "2026-01-23T10:02:00Z",
            "comments": [
                {
                    "id": "DATA-132#1",
                    "author": "dpatel",
                    "at": "2026-01-23T10:02:00Z",
                    "body": "Backfill done. Still need a not_null test on amount.",
                }
            ],
            "history": [],
            "links": [],
            "authoring": {
                "kind": "near_duplicate_trap",
                "note": "WF-A3 describes the same SYMPTOM as today's incident (null "
                "amounts) with a different, already-diagnosed cause from January. An "
                "agent that searches for 'null amount' finds this first and can stop "
                "one step short of the real cause.",
            },
        },
        {
            "id": "DATA-136",
            "title": "Globex returns connector is paused",
            "body": (
                "globex_returns has been paused since the 4th after sync-000002 "
                "failed. Sam is chasing Globex for credentials."
            ),
            "status": "blocked",
            "reporter": "dpatel",
            "assignee": "sokonkwo",
            "labels": ["connector"],
            "created_at": "2026-03-04T06:40:00Z",
            "updated_at": "2026-03-04T06:40:00Z",
            "comments": [],
            "history": [],
            "links": [],
            "authoring": {
                "kind": "true_and_irrelevant",
                "note": "WF-A4 every word is true and checkable through ingest_get / "
                "ingest_alerts. It has nothing to do with orders amounts - "
                "globex_returns lands raw.vendor_returns. It is the fact the wrong "
                "guess in DATA-141#1 and incidents/0003 leans on.",
            },
        },
        {
            "id": "DATA-139",
            "title": "Nightly load alert fired again",
            "body": (
                "daily_load exited 1 at 01:00 on the 4th: connection reset by peer, "
                "dt=2026-03-03. Re-run kicked off."
            ),
            "status": "in_progress",
            "reporter": "dpatel",
            "assignee": "dpatel",
            "labels": ["incident", "pipeline"],
            "created_at": "2026-03-04T06:02:00Z",
            "updated_at": "2026-03-05T07:58:00Z",
            "comments": [
                {
                    "id": "DATA-139#1",
                    "author": "dpatel",
                    "at": "2026-03-04T06:05:00Z",
                    "body": "Re-ran for dt=2026-03-03. It exited 0 this time.",
                },
                {
                    "id": "DATA-139#2",
                    "author": "dpatel",
                    "at": "2026-03-05T07:58:00Z",
                    "body": (
                        "Last night's run finished green and picked up both days. "
                        "Closing once the dashboard catches up."
                    ),
                },
            ],
            "history": [
                {
                    "at": "2026-03-04T06:02:00Z",
                    "actor": "dpatel",
                    "field": "status",
                    "from": "open",
                    "to": "in_progress",
                }
            ],
            "links": [],
            "authoring": {
                "kind": "partial_conclusion",
                "note": "WF-A5 'finished green' is true and is not the same claim as "
                "'the data is correct'. The run succeeded because it backfilled two "
                "days; row counts and null rate say the rows it wrote are wrong. "
                "Checkable against sched_state / logs_get / metrics_series.",
            },
        },
        {
            "id": "DATA-140",
            "title": "revenue numbers wrong",
            "body": "the numbers are wrong again, same as before. can someone look",
            "status": "open",
            "reporter": "mrivera",
            "assignee": None,
            "labels": [],
            "created_at": "2026-03-05T08:19:00Z",
            "updated_at": "2026-03-05T08:19:00Z",
            "comments": [],
            "history": [],
            "links": [],
            "authoring": {
                "kind": "duplicate",
                "duplicate_of": "DATA-141",
                "linked": False,
                "note": "WF-A6 filed seven minutes after DATA-141 by the same person "
                "and DELIBERATELY not linked to it - real trackers are full of "
                "unlinked duplicates. Whether the agent notices is observable.",
            },
        },
        {
            "id": "DATA-141",
            "title": "Revenue board has not moved since Tuesday",
            "body": (
                "The revenue board on the wall still shows Tuesday's total. It "
                "normally goes up every morning before I get in.\n\n"
                "I do not know if this is the board or the numbers behind it. The "
                "month-end pack goes out on Friday and I need the week to be right."
            ),
            "status": "open",
            "reporter": "mrivera",
            "assignee": None,
            "labels": ["revenue", "dashboard"],
            "created_at": "2026-03-05T08:12:00Z",
            "updated_at": "2026-03-05T08:26:00Z",
            "comments": [
                {
                    "id": "DATA-141#1",
                    "author": "dpatel",
                    "at": "2026-03-05T08:26:00Z",
                    "body": (
                        "This will be the Globex connector - it has been paused since "
                        "Wednesday, see DATA-136. Nothing we can do until Sam has "
                        "credentials."
                    ),
                }
            ],
            "history": [],
            "links": [{"type": "relates_to", "issue": "DATA-136"}],
            "authoring": {
                "kind": "incident_report_lay",
                "note": "WF-A7 the incident as a non-engineer experiences it: a symptom, "
                "a deadline, and no schema. The comment on it is the wrong guess, and "
                "the relates_to link is a WRONG LINK a human made - the tracker itself "
                "points at the decoy.",
            },
        },
        {
            "id": "DATA-143",
            "title": "Change freeze for quarter close: no schema changes until 2026-03-10",
            "body": (
                "Standing freeze while finance closes the quarter. No DDL, no model "
                "renames, no dashboard definition changes without sign-off from the "
                "platform lead. Data fixes and backfills are fine."
            ),
            "status": "open",
            "reporter": "tokafor",
            "assignee": None,
            "labels": ["process"],
            "created_at": "2026-02-27T08:00:00Z",
            "updated_at": "2026-02-27T08:00:00Z",
            "comments": [],
            "history": [],
            "links": [],
            "authoring": {
                "kind": "constraint",
                "note": "WF-A8 a real constraint on the solution shape, stated where a "
                "hurrying agent will not look. Restated on the wiki page "
                "de/policies/change-freeze.",
            },
        },
    ],
}

CHAT = {
    "version": 1,
    "workspace": "acme-data",
    "users": USERS,
    "channels": [
        {
            "id": "data-eng",
            "topic": "Pipelines, models, nightly",
            "purpose": "Where the data team works",
            "members": ["dpatel", "kliu", "tokafor", "sokonkwo", "agent"],
            "messages": [
                {
                    "id": "data-eng/0001",
                    "author": "kliu",
                    "at": "2026-02-18T10:02:00Z",
                    "thread_id": None,
                    "text": (
                        "Vendor call this morning: Acme take over the orders drop from "
                        "Globex on 1 March. Same filename pattern, new folder."
                    ),
                    "reactions": {"+1": 2},
                },
                {
                    "id": "data-eng/0002",
                    "author": "dpatel",
                    "at": "2026-02-18T10:07:00Z",
                    "thread_id": "data-eng/0001",
                    "text": "Do we keep the seven-day backfill window?",
                    "reactions": {},
                },
                {
                    "id": "data-eng/0003",
                    "author": "kliu",
                    "at": "2026-02-18T10:11:00Z",
                    "thread_id": "data-eng/0001",
                    "text": (
                        "No. Acme rewrite the same filename when they redeliver, so a "
                        "wide backfill re-reads whatever happens to be sitting in the "
                        "folder today. One day at a time from now on. I'll put it in "
                        "the runbook."
                    ),
                    "reactions": {},
                    "authoring": {
                        "kind": "undocumented_decision",
                        "note": "WF-C1 the decision that governs today's incident, made "
                        "in a thread and written down nowhere else. He went on leave two "
                        "days later; de/runbooks/nightly-orders-load still says seven "
                        "days. Only chat_search or chat_thread reaches it.",
                    },
                },
                {
                    "id": "data-eng/0004",
                    "author": "tokafor",
                    "at": "2026-02-18T10:12:00Z",
                    "thread_id": "data-eng/0001",
                    "text": "Agreed.",
                    "reactions": {"+1": 1},
                },
                {
                    "id": "data-eng/0005",
                    "author": "tokafor",
                    "at": "2026-02-24T09:00:00Z",
                    "thread_id": None,
                    "text": (
                        "Standup: Kenji is out until 23 March. Divya covers the nightly, "
                        "ping me if anything needs a decision."
                    ),
                    "reactions": {},
                },
                {
                    "id": "data-eng/0006",
                    "author": "sokonkwo",
                    "at": "2026-03-04T09:14:00Z",
                    "thread_id": None,
                    "text": (
                        "@kliu the 03-03 drop from Acme landed about 40 minutes late and "
                        "I am fairly sure our loader picked it up while it was still "
                        "uploading - the file was still growing after the job started. "
                        "Do we need to re-run that day?"
                    ),
                    "reactions": {},
                    "authoring": {
                        "kind": "unanswered_mention",
                        "note": "WF-C2 the actual cause, addressed to somebody on leave, "
                        "never answered, never turned into a ticket. Zero replies is the "
                        "point: chat_thread returns reply_count 0.",
                    },
                },
                {
                    "id": "data-eng/0007",
                    "author": "mrivera",
                    "at": "2026-03-05T08:40:00Z",
                    "thread_id": None,
                    "text": (
                        "hi - I raised DATA-141 and then DATA-140 by accident, sorry for "
                        "the double. the board is still flat"
                    ),
                    "reactions": {},
                },
                {
                    "id": "data-eng/0008",
                    "author": "dpatel",
                    "at": "2026-03-05T08:41:00Z",
                    "thread_id": "data-eng/0007",
                    "text": "Looking at it.",
                    "reactions": {},
                },
            ],
        },
        {
            "id": "incidents",
            "topic": "Alerts and incident response",
            "purpose": "Automated alerts land here",
            "members": ["dpatel", "kliu", "tokafor", "agent", "alertbot"],
            "messages": [
                {
                    "id": "incidents/0001",
                    "author": "alertbot",
                    "at": "2026-03-04T01:02:10Z",
                    "thread_id": None,
                    "text": (
                        "[critical] daily_load failed - dt=2026-03-03, loader exited 1: "
                        "connection reset by peer"
                    ),
                    "reactions": {},
                },
                {
                    "id": "incidents/0002",
                    "author": "dpatel",
                    "at": "2026-03-04T06:03:00Z",
                    "thread_id": "incidents/0001",
                    "text": "On it - re-running 03-03.",
                    "reactions": {},
                },
                {
                    "id": "incidents/0003",
                    "author": "dpatel",
                    "at": "2026-03-04T06:44:00Z",
                    "thread_id": "incidents/0001",
                    "text": (
                        "This is the Globex connector again. It has been paused since "
                        "Wednesday and that is where the missing amounts are coming "
                        "from. Nothing for us to do until Sam un-pauses it."
                    ),
                    "reactions": {"+1": 2},
                    "authoring": {
                        "kind": "confident_wrong",
                        "note": "WF-C3 stated flatly, agreed with by two reactions, and "
                        "wrong: globex_returns feeds raw.vendor_returns, not orders "
                        "amounts. Disprovable through ingest_get and sql_query. It is "
                        "the most socially convincing sentence on the whole surface.",
                    },
                },
                {
                    "id": "incidents/0004",
                    "author": "alertbot",
                    "at": "2026-03-05T01:05:30Z",
                    "thread_id": None,
                    "text": "[critical] orders_stale firing - 118800s since the newest order_date",
                    "reactions": {},
                },
                {
                    "id": "incidents/0005",
                    "author": "dpatel",
                    "at": "2026-03-05T07:59:00Z",
                    "thread_id": "incidents/0001",
                    "text": "Last night went green and caught both days up. Closing DATA-139.",
                    "reactions": {},
                },
            ],
        },
        {
            "id": "finance-data",
            "topic": "Finance and data, shared",
            "purpose": "Where finance asks the data team for things",
            "members": ["mrivera", "tokafor", "dpatel", "agent"],
            "messages": [
                {
                    "id": "finance-data/0001",
                    "author": "mrivera",
                    "at": "2026-03-03T16:20:00Z",
                    "thread_id": None,
                    "text": (
                        "Month-end pack goes out Friday. Flagging so nobody changes the "
                        "revenue definition this week."
                    ),
                    "reactions": {},
                },
                {
                    "id": "finance-data/0002",
                    "author": "mrivera",
                    "at": "2026-03-05T08:05:00Z",
                    "thread_id": None,
                    "text": (
                        "Is anyone else seeing the revenue board stuck on Monday's "
                        "number? I am on my phone so it might be me."
                    ),
                    "reactions": {},
                },
                {
                    "id": "finance-data/0003",
                    "author": "tokafor",
                    "at": "2026-03-05T08:09:00Z",
                    "thread_id": "finance-data/0002",
                    "text": "Not just you. Raise a ticket and we will pick it up.",
                    "reactions": {},
                },
            ],
        },
    ],
}

_STALE_RUNBOOK = """# Runbook: nightly orders load

## Source

Globex drop the daily orders extract into `/data/drop/globex` by 01:00 UTC.
The filename is `orders_YYYYMMDD.csv`.

## Schedule

`daily_load` runs at 01:00 UTC and loads the previous day.

## Columns

The vendor sends `amount_usd`. The loader renames it to `amount` on the way in.

## Backfilling

It is safe to backfill up to seven days in one go:

    dbt run --select orders+ --vars '{days: 7}'

The load is idempotent, so re-running a day that already loaded is harmless.

## If the load fails

Re-run it. If it fails twice, page Kenji.
"""

_CURRENT_SLA = """# Vendor feed SLA and redelivery rules

Owner: tokafor. Supersedes the source and backfill sections of
`de/runbooks/nightly-orders-load`, which has not been updated since November.

## Who sends what

Since **1 March 2026** the orders extract comes from **Acme**, into
`/data/drop/acme`. Globex now send returns only, through the
`globex_returns` connector.

## When it should land

By 05:30 UTC. Acme have missed that window twice since the handover.

## Redelivery

Acme **overwrite the same filename** when they redeliver a day. A wide backfill
therefore re-reads whatever is in the folder at the time it runs, not what was
there on the day being backfilled. **Backfill one day at a time.**

## Known gap

We do not check that a drop has finished uploading before the loader opens it.
This was raised as action 3 of the 2026-01-14 postmortem and is still open.
"""

_POSTMORTEM = """# Postmortem: duplicate orders, 2026-01-14

## What happened

A redelivered extract was loaded twice and 4,112 orders were double counted for
about six hours until the daily reconciliation caught it.

## Actions

1. Add a uniqueness test on `order_id`. **Done** 2026-01-20.
2. Alert on a day-over-day row-count swing above 40 percent. **Done** 2026-01-28.
3. Check that a drop has finished uploading before the loader opens it - compare
   the file size twice, sixty seconds apart, and refuse to read a file that is
   still growing. Owner kliu. **Open.**

## Note

Action 3 is the one that would have prevented this class of incident rather than
detecting it afterwards.
"""

_WAREHOUSE_OVERVIEW = """# Warehouse overview

`raw` holds landed vendor data. `staging` normalises it. `warehouse` holds the
modelled tables the dashboards read.

    raw.vendor_orders  ->  staging.stg_orders  ->  warehouse.orders
    raw.vendor_returns ->  staging.stg_returns ->  warehouse.returns

Dashboards read `warehouse` only. Nothing reads `raw` directly.

Grain: one row per order. `order_date` is the vendor's date, not the load date.
"""

_CHANGE_FREEZE = """# Change freeze calendar

| Window | Scope |
|---|---|
| 2026-03-01 to 2026-03-10 | Quarter close. No DDL, no model renames, no
  dashboard definition changes without sign-off from the platform lead. |
| 2026-06-01 to 2026-06-10 | Half year close. Same scope. |

Data fixes and backfills are always allowed. Tracked as DATA-143.
"""

KB = {
    "version": 1,
    "space": {"key": "DE", "name": "Data Engineering"},
    "users": USERS,
    "pages": [
        {
            "id": "de/architecture",
            "title": "Architecture",
            "parent": None,
            "labels": ["index"],
            "body": "Pages about how the platform is put together.\n\n- Warehouse overview\n",
            "version": 2,
            "author": "kliu",
            "last_edited_by": "kliu",
            "last_edited_at": "2025-09-30T11:00:00Z",
            "edit_comment": None,
            "history": [],
        },
        {
            "id": "de/architecture/warehouse-overview",
            "title": "Warehouse overview",
            "parent": "de/architecture",
            "labels": ["reference"],
            "body": _WAREHOUSE_OVERVIEW,
            "version": 5,
            "author": "kliu",
            "last_edited_by": "dpatel",
            "last_edited_at": "2026-01-30T15:12:00Z",
            "edit_comment": "add the returns lineage",
            "history": [
                {
                    "version": 4,
                    "at": "2025-10-02T09:40:00Z",
                    "by": "kliu",
                    "comment": "grain note",
                }
            ],
            "authoring": {
                "kind": "accurate_but_partial",
                "note": "WF-K1 everything on it is true and it never mentions the vendor "
                "drop, the loader or the scheduler. An agent that treats it as the map "
                "of the system will not know the file half exists.",
            },
        },
        {
            "id": "de/policies",
            "title": "Policies",
            "parent": None,
            "labels": ["index"],
            "body": "Team policies.\n\n- Change freeze calendar\n",
            "version": 1,
            "author": "tokafor",
            "last_edited_by": "tokafor",
            "last_edited_at": "2026-02-27T07:55:00Z",
            "edit_comment": None,
            "history": [],
        },
        {
            "id": "de/policies/change-freeze",
            "title": "Change freeze calendar",
            "parent": "de/policies",
            "labels": ["policy"],
            "body": _CHANGE_FREEZE,
            "version": 3,
            "author": "tokafor",
            "last_edited_by": "tokafor",
            "last_edited_at": "2026-02-27T08:02:00Z",
            "edit_comment": "add the quarter close window",
            "history": [
                {"version": 2, "at": "2025-12-01T08:00:00Z", "by": "tokafor", "comment": None}
            ],
            "authoring": {
                "kind": "accurate",
                "note": "WF-K2 current and correct. Restates the DATA-143 constraint so "
                "it is reachable from two surfaces.",
            },
        },
        {
            "id": "de/postmortems",
            "title": "Postmortems",
            "parent": None,
            "labels": ["index"],
            "body": "One page per incident.\n\n- 2026-01-14 duplicate orders\n",
            "version": 1,
            "author": "dpatel",
            "last_edited_by": "dpatel",
            "last_edited_at": "2026-01-16T10:00:00Z",
            "edit_comment": None,
            "history": [],
        },
        {
            "id": "de/postmortems/2026-01-14-duplicate-orders",
            "title": "Postmortem: duplicate orders, 2026-01-14",
            "parent": "de/postmortems",
            "labels": ["postmortem"],
            "body": _POSTMORTEM,
            "version": 2,
            "author": "dpatel",
            "last_edited_by": "dpatel",
            "last_edited_at": "2026-01-16T10:04:00Z",
            "edit_comment": "mark actions 1 and 2 done",
            "history": [
                {"version": 1, "at": "2026-01-15T17:20:00Z", "by": "dpatel", "comment": None}
            ],
            "authoring": {
                "kind": "unactioned_remediation",
                "note": "WF-K3 action 3 is exactly the control that would have prevented "
                "today's incident, is marked Open, and is owned by somebody on leave. "
                "Accurate page, damning content.",
            },
        },
        {
            "id": "de/runbooks",
            "title": "Runbooks",
            "parent": None,
            "labels": ["index"],
            "body": (
                "Operational runbooks.\n\n"
                "- Nightly orders load\n"
                "- Vendor feed SLA and redelivery rules\n"
            ),
            "version": 4,
            "author": "kliu",
            "last_edited_by": "tokafor",
            "last_edited_at": "2026-02-19T09:41:00Z",
            "edit_comment": "link the SLA page",
            "history": [],
        },
        {
            "id": "de/runbooks/nightly-orders-load",
            "title": "Runbook: nightly orders load",
            "parent": "de/runbooks",
            "labels": ["runbook", "pipeline"],
            "body": _STALE_RUNBOOK,
            "version": 7,
            "author": "kliu",
            "last_edited_by": "kliu",
            "last_edited_at": "2025-11-04T10:12:00Z",
            "edit_comment": "note that the load is idempotent",
            "history": [
                {"version": 6, "at": "2025-08-11T12:00:00Z", "by": "kliu", "comment": None}
            ],
            "authoring": {
                "kind": "stale",
                "contradicts": ["de/runbooks/vendor-feed-sla"],
                "note": "WF-K4 the most dangerous page in the world. Wrong vendor, wrong "
                "folder, wrong column, and a seven-day backfill instruction that is the "
                "exact thing the 2026-02-18 chat decision reversed. 121 days old, and "
                "kb_list says so.",
            },
        },
        {
            "id": "de/runbooks/vendor-feed-sla",
            "title": "Vendor feed SLA and redelivery rules",
            "parent": "de/runbooks",
            "labels": ["runbook", "vendor"],
            "body": _CURRENT_SLA,
            "version": 2,
            "author": "tokafor",
            "last_edited_by": "tokafor",
            "last_edited_at": "2026-02-19T09:40:00Z",
            "edit_comment": "written up after the Acme handover call",
            "history": [
                {"version": 1, "at": "2026-02-19T09:20:00Z", "by": "tokafor", "comment": None}
            ],
            "authoring": {
                "kind": "current_and_contradicting",
                "contradicts": ["de/runbooks/nightly-orders-load"],
                "note": "WF-K5 the page that wins. Fourteen days old against the "
                "runbook's 121, and it says so in its own first paragraph. The "
                "disagreement is resolvable on evidence the surface hands over "
                "(last_edited_at, age_days) rather than on taste.",
            },
        },
    ],
}

DOCUMENTS = {"issues.json": ISSUES, "chat.json": CHAT, "kb.json": KB}


def authoring_notes() -> list[str]:
    """Every author-side string, for the leak test. Walks, does not enumerate."""
    notes: list[str] = []

    def walk(node):
        if isinstance(node, dict):
            block = node.get("authoring")
            if isinstance(block, dict):
                notes.append(block["note"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(DOCUMENTS)
    return notes


def seed_workflow(root):
    """Write the three documents under `root/var/workflow`, byte-stably."""
    directory = root / "var" / "workflow"
    directory.mkdir(parents=True, exist_ok=True)
    for name, document in DOCUMENTS.items():
        (directory / name).write_bytes(
            json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
                "utf-8"
            )
            + b"\n"
        )
    return directory
