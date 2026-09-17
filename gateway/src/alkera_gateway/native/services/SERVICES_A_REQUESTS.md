# SERVICES-A: requests for files we do not own

Written 2026-09-16 against `gateway/NATIVE_SURFACES.md`. SERVICES-A owns
`native/services/{jira,slack,confluence}.py`, their test and `HUMAN_PLANE.md`
and has edited nothing else. Each item below names the owner and what we did in
the meantime, so nothing here blocks anybody.

**Status.** Items 4, 6 and 7 were closed by the coordinator's rulings, recorded
in place below. **Items 1, 2 and 3 were closed on 2026-09-16 by
INTEGRATE-GATEWAY** — `native/context.py` is the shared home, `Observation`
gained `child()`/`seal()`, and `issues_list`/`chat_channels` now serve `project`
and `workspace`; what changed and what it was measured with is in
`native/INTEGRATION_GATEWAY.md`. Item 5 is still COMPOSE-DISCOVERY's, and the
three environment variables it names are unchanged (they moved file, not name).
The text below is left as it was written, as the record of what was asked.

## 1. A shared home for the world, the clock and the audited block — COMPOSE-DISCOVERY

`world()` (config + virtual clock, per request) and the `audited()` context
manager (one 9.2.2 row per request, failure path included) are **duplicated
verbatim in all three human-plane modules**. `native/services/_ops.py` carries
the same two pieces a third time for the ops plane, and says in its own
docstring that it should collapse into a shared `native/context.py` if one
lands. Three copies of the writer that must not drift is exactly the risk the
single-row-writer rule exists to remove.

Request: a module both planes can import — `native/context.py`, or the same
functions exported from `services/__init__.py`. We will delete our copies.

Meanwhile: the block is marked with this item number in each of the three files.

## 2. A composed read that an `Observation` can express — AUDIT (`records.py`)

`_ops.served` obtains its row's `op_class`/`targets` by handing the handler the
same `records.Observation` the MCP gateway hands the same `Operations` method.
The human plane cannot: several of its endpoints run **more than one** workflow
call per request (`jira.search` is `issues_list` + `issues_search` + one
`issues_get` per row; every Confluence handler is `kb_list` plus a `kb_get`), and
one `Observation` would end up carrying whatever the last sub-call set.

Request: a way for an `Observation` to be *sealed* after the operation that
defines the action (or a nested/child observation whose targets do not overwrite
the parent's). Then the human plane's classification stops being a literal in a
handler and becomes the same object the MCP row's comes from.

Meanwhile: the literals are guarded by
`test_one_action_two_transports_one_classification`, which diffs a native row
against an MCP row for 16 pairs. This is the weakest thing about the plane and
`HUMAN_PLANE.md` says so.

## 3. Two headers `workflow.py` does not serve — owner of `workflow.py` / `operations.py`

A Jira issue must name a project and a Slack `auth.test` must name a workspace.
`issues_list` serves `statuses` and `users` but **not** `project`;
`chat_channels` serves the channels and users but not `workspace`. So
`jira.headers()` and `slack.auth_test()` each call `IssueTracker.load()` /
`Chat.load()` for that one header — the only place either module touches a raw
document, and never for a record, which is what carries the author-side
`authoring` block.

Request: add `project` to the `issues_list` result and `workspace` to the
`chat_channels` result. Both are already in the documents and neither is
author-side; `test_workflow.py` would need one more assertion.

## 4. Port 8080 — CLOSED

Ruled: the ports stand (jira 8080, slack 8081, confluence 8090). Native mode
never runs the MCP gateway, `compose.native.yml` gives each service its own
container, and host topology maps endpoints explicitly through
`--native-endpoints`, so there is no collision to resolve. No change on our side.

## 5. What `serve.py` must set in the environment — COMPOSE-DISCOVERY

Our three apps resolve the world **per request**, not at import, so a test or a
reload can repoint them:

- `ALKERA_GATEWAY_CONFIG` — the gateway config JSON (the file `docker/start.py`
  already writes). Unset falls back to `GatewayConfig()` defaults.
- `ALKERA_NATIVE_AUDIT` (or `ALKERA_AUDIT_DIR`) — where `native.audit` writes.
- `ALKERA_SCHEDULER` — `native.audit` stamps the row's `vts` from this, not from
  our config; unset gives every row the `virtual_time` default rather than the
  world's `vnow`.

The ops plane instead uses `_ops.set_context(app, context)`. If `serve.py` wants
one mechanism for all nine services, that is item 1.

## 6. Tool names in the log — CLOSED, and it changed more than the names

Ruled: `<service>.<mcp_tool>` wherever an MCP equivalent exists, so one
suffix-match rule serves graders and `rollout.py --reclassify` across both
planes; the vendor endpoint stays visible in `args.method` / `args.path`; and a
composed endpoint emits one row per MCP-equivalent sub-call, with that sub-call's
name, exactly as the MCP agent would have paid.

Done: every row renamed (`jira.issues_get`, `slack.chat_post`,
`confluence.kb_edit`, ...); `args` now carries `method` and `path` on every row;
and the composed read paths were restructured around a per-request `Ledger` that
opens one span per sub-call. A JQL search with a text clause is now
`issues_list` + `issues_search` + one `issues_get` per returned row, and an
expanded wiki listing is `kb_list` + one `kb_get` per page.

The ruling needed one boundary drawn that it did not state, so we drew it and
wrote it into `Ledger`'s docstring in all three modules and into
`HUMAN_PLANE.md`: **a write pays one row and header/resolution reads pay none.**
A write endpoint's precondition reads (the current labels a `fields.labels`
replacement has to diff against, the current version a `PUT` has to check) are
part of the write, or a native `PUT` would cost two rows where `issues_update`
costs one. Vendor-shape header reads (project key, status vocabulary, user
directory, workspace name, the world channel behind a `C…` id, the space header)
pay nothing, or `GET /issue/KEY` would cost two rows where `issues_get` costs
one - and that parity is what the equivalence test checks. Say so if either
boundary should sit elsewhere; both are one-line changes.

Four endpoints keep a vendor-shaped name because no MCP tool asks their question
at all: `jira.server_info`, `jira.fields`, `jira.myself`, `slack.auth_test` -
plus `<name>.health`, which follows `_ops.service_health`.

## 7. `native/audit.record` has no `no_change`-style helper — CLOSED

Resolved, not requested: the `flags` parameter landed while this work was in
flight and we now pass `no_change` and `overwrite` exactly where
`operations.py` sets them, so the two transports' `flags` are diffed as equal.
Recorded here only because the first version of these modules was written
against the interface stub that had no `flags`, and anyone reading that stub's
call sites should know it is no longer the current shape.
