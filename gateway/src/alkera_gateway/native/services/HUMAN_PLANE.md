# The human plane: jira, slack, confluence

**Status: executed, 2026-09-16.** Every claim below is asserted by
`gateway/tests/test_native_services_human.py` (41 tests, all passing) unless the
line says *designed*. Owner: SERVICES-A. Spec: `gateway/NATIVE.md`.
Amended by INTEGRATE-GATEWAY, same day: items 1, 2 and 3 of
`SERVICES_A_REQUESTS.md` landed, so the classification below is now *obtained*
rather than asserted and the weakest finding has moved. See
`native/INTEGRATION_GATEWAY.md` for what changed and what it was measured with.

The three surfaces a data engineer's day starts in, in their vendors' shapes over
the documents the `issues_*` / `chat_*` / `kb_*` tools serve. Nothing here holds
state: every read and write runs `alkera_gateway.operations.Operations` - the
very methods the MCP tools are - so `var/workflow/*` stays the single source of
truth, timestamps come from the virtual clock, ids stay pure functions of the
document, and `authoring` blocks are stripped by the code that already strips
them (proved for bodies *and* rows). Each row's `op_class` and `targets` are
read off the `records.Observation` that method filled in; no handler names a
target.

| | NAME | HOST | PORT | credential |
|---|---|---|---|---|
| Jira Cloud REST v3 | `jira` | `jira.internal` | 8080 | Basic: any email + `alkera-local-dev-token` |
| Slack Web API | `slack` | `slack.internal` | 8081 | Bearer `xoxb-alkera-local-dev-token` (or `token=`) |
| Confluence REST | `confluence` | `confluence.internal` | 8090 | Basic: any user + `alkera-local-dev-token` |

## What costs a row

Rows are named `<service>.<mcp_tool>` wherever an MCP equivalent exists, so one
suffix-match rule serves graders and `rollout.py --reclassify` across both native
planes; the vendor endpoint lives in `args.method` / `args.path`, never in the
name. Three rules, in `Ledger` in each module, asserted by
`test_a_composed_read_costs_what_an_mcp_agent_would_have_paid`:

1. **A read pays one row per MCP-equivalent read it answers with.** A JQL search
   with a text clause is `issues_list` + `issues_search` + one `issues_get` per
   row returned — what an agent holding the tool list would have run to read the
   same tickets. An expanded wiki listing is `kb_list` + a `kb_get` per page.
   `Span.run` is the one call that opens a row and it may be made once; a second
   one raises rather than re-pointing the row (`records.Observation.seal`).
2. **A write pays exactly one row, the write's.** The precondition reads it does
   to turn a vendor payload into `issues_update`'s arguments (current labels,
   current version) are part of the write. A transition that also carries
   `update.comment` is two *actions*, so two rows.
3. **Header and resolution reads pay nothing** — project key, status vocabulary,
   user directory, workspace name, the world channel behind a `C…` id, space
   header. A row for them would make `GET /issue/KEY` cost two rows where
   `issues_get` costs one. They go through `Span.aside`, which runs them against
   `observation.child()`, so what they touch cannot reach the row.

## Endpoints and the rows they write

Starred rows are proved equal to MCP's by
`test_one_action_two_transports_one_classification`: 16 pairs, each run natively
and through an MCP `Gateway` over a pristine clone, diffing `targets`,
`read_targets`, `op_class`, `flags` and `vts`.

| endpoint | rows, in order | op_class · targets |
|---|---|---|
| `GET /rest/api/3/issue/{key}` | `jira.issues_get` * | read · `issue:KEY` |
| `GET /rest/api/3/search` (startAt/maxResults) and `/search/jql` (nextPageToken) | `jira.issues_list` *, [`jira.issues_search` *], `jira.issues_get` ×page | read · `issues`, `issues`, `issue:KEY` |
| `POST /rest/api/3/issue/{key}/comment` | `jira.issues_comment` * | write · `issue:KEY` |
| `GET /rest/api/3/issue/{key}/transitions` | `jira.issues_get` | read · `issue:KEY` |
| `POST /rest/api/3/issue/{key}/transitions` | `jira.issues_update` *, [`jira.issues_comment`] | write · `issue:KEY` |
| `PUT /rest/api/3/issue/{key}` | `jira.issues_update` * | write · `issue:KEY` |
| `GET /rest/api/3/myself`, `/serverInfo` | `jira.myself`, `jira.server_info` | read · `issues` (both run `issues_list`, for the user directory and the project title) |
| `GET /rest/api/3/field` | `jira.fields` | read · `issues`, through `context.note`: the one endpoint here that asks the world nothing, so no module classifies it |
| `conversations.list` *, `users.list` | `slack.chat_channels` | read · `chat` |
| `conversations.history` | `slack.chat_history` * | read · `chat:<channel>` |
| `conversations.replies` | `slack.chat_thread` * | read · `chat:<ch>`, `thread:<root>` |
| `search.messages` | `slack.chat_search` * | read · `chat:<ch>` or `chat` |
| `chat.postMessage` | `slack.chat_post` * | write · `chat:<channel>` |
| `auth.test` | `slack.auth_test` | read · `chat` |
| `GET /wiki/rest/api/content` | `confluence.kb_list` *, [`confluence.kb_get` ×page] | read · `kb`, `kb:<page>` |
| `GET /wiki/rest/api/content/{id}` *, and `/{id}/history` | `confluence.kb_get` | read · `kb:<page>` |
| `GET /wiki/rest/api/content/search` (CQL) | `confluence.kb_search` *, [`confluence.kb_get` ×page] | read · `kb`, `kb:<page>` |
| `PUT /wiki/rest/api/content/{id}` | `confluence.kb_edit` * | write · `kb:<page>` |
| `POST /wiki/rest/api/content` | `confluence.kb_create` * | write · `kb:<page>` |
| `GET /health` (all three) | `<name>.health` | read · `health:<name>`, really `issues`/`chat`/`kb` in `read_targets` (the `context.service_health` convention: a polling gate injects no episode targets) |

`flags` carry the two the MCP tools set: `no_change` on an edit that changed
nothing, `overwrite` on a page edit; the caller travels as `actor:<principal>`
(9.2.2 has no `actor` key); targets use the world's channel id, `chat:data-eng`,
not `#data-eng`, because that is what `operations.py` writes. Four endpoints are
beyond the spec's list: three the real client cannot work without (`/serverInfo`
and `/field`, fetched by python `jira` before it can search;
`/content/{id}/history`, which `update_page` reads the version from) and one MCP
parity needs (`POST /wiki/rest/api/content`, without which native mode cannot do
`kb_create`, i.e. cannot write a postmortem).

## What of each vendor API is *not* imitated

- **Jira.** No issue creation, attachments, worklogs, watchers, votes, sprints,
  custom fields, changelog, editmeta, notifications or permissions. JQL is five
  clauses (`project =`, `status =`/`in`, `assignee =`, `labels =`, `text ~`) plus
  `ORDER BY updated|created|key|status`; anything else is **400, never silently
  ignored**. Bodies are ADF; a bare string is also accepted on input, because the
  python `jira` client posts one to the v3 comment endpoint even though real Jira
  Cloud rejects it. `deploymentType: Cloud` routes that client to `/search/jql`;
  `/search` is exercised by the same library with `get_server_info=False`.
- **Slack.** No events, sockets, blocks, files, agent-added reactions, channel
  joins, paging on `conversations.list` / `users.list`, `latest_reply`, or
  Slack's real search grammar — only `in:#channel` plus a literal needle, which
  is exactly `chat_search`. Ids are minted (`C…`, `U…`) from the world id, which
  is itself accepted everywhere, with or without a `#`. `ts` is `<virtual
  epoch>.<six-digit position>`, round-tripping to a world message id; cursors are
  decimal offsets, opaque to the client.
- **Confluence.** No blog posts, attachments, comments, labels API, permissions,
  drafts, rename, re-parenting or trash. `body.storage` carries the world's
  **markdown verbatim** rather than XHTML: converting would make a round trip
  lossy against the document the grader reads, a worse lie than the format label.
  Content ids are numbers derived from the page path. CQL is `text ~ "needle"`
  with optional `type = page` and `space = KEY`; anything else is 400. A
  `version.number` that is not current+1 is **409** and a rename or re-parent is
  **400** — two silent successes this app does not offer.

## Weakest finding

*Superseded 2026-09-16 by INTEGRATE-GATEWAY.* What this section used to say was
that the classification was **asserted, not obtained** — every span named its own
`op_class` and `targets` as literals, so adding an endpoint without adding a row
to `EQUIVALENT` failed nothing — and that the `world()`/`Ledger` block was
duplicated verbatim in all three modules. Both are closed: `records.Observation`
gained `child()`/`seal()`, the handlers hand their observation to `Operations`
and the row is written off it, and the duplicated block is one module,
`native/context.py`. `test_an_endpoint_that_ran_no_module_call_cannot_ship_a_row`
is the new guard: an endpoint that classifies nothing raises at the first
request instead of shipping a plausible row.

What is left, and it is the honest weakest thing now: **rule 1 makes a wide read
genuinely O(n) module calls**, each of which re-reads the whole workflow
document — correct, fine at fixture size (eight tickets, nine pages), unmeasured
above it. Second: `operations.py` serves `project`/`workspace` by loading the
document a second time, because `workflow.py` is not ours to change; the right
home for both headers is the `list_issues`/`list_channels` result itself.
