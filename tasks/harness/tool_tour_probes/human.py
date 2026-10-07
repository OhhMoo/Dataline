"""human: the three workflow services, each through its usual Python client -
jira (`jira`), confluence (`atlassian-python-api`), slack (`slack_sdk`).

Their truth is the world's own workflow state (`var/workflow/*.json`, which the services
serve and write), read by DuckDB on the host.
"""
from tool_tour import Probe

from tool_tour_probes import DA_LAPTOP_BI

PROFILE, PLUGS = DA_LAPTOP_BI

PROBES = [
    Probe(
        id="human.jira.ana238_status",
        profile=PROFILE,
        plugs=PLUGS,
        command="""cd /workspace
set -euo pipefail
python3 - <<'PY'
import json, os
from jira import JIRA

env = os.environ
jira = JIRA(server=env["JIRA_URL"], basic_auth=(env["JIRA_USER"], env["JIRA_API_TOKEN"]))
print(json.dumps({"value": str(jira.issue("ANA-238").fields.status)}))
PY
""",
        expect_tools=("jira.issues_get",),
        truth_sql=("SELECT i.status FROM (SELECT unnest(issues) AS i FROM "
                   "read_json('var/workflow/issues.json')) WHERE i.id = 'ANA-238'"),
        instruction=(
            "What status is Jira ticket ANA-238 in? Read it from the team's Jira with "
            "the credentials this box provides."),
    ),
    Probe(
        id="human.confluence.net_revenue_page_version",
        profile=PROFILE,
        plugs=PLUGS,
        command="""cd /workspace
set -euo pipefail
python3 - <<'PY'
import json, os
from atlassian import Confluence

env = os.environ
wiki = Confluence(url=env["CONFLUENCE_URL"].rstrip("/") + "/wiki",
                  username=env["CONFLUENCE_USER"], password=env["CONFLUENCE_API_TOKEN"])
page = wiki.get_page_by_id(wiki.get_page_id("AE", "Net Revenue and friends"),
                           expand="version")
print(json.dumps({"value": page["version"]["number"]}))
PY
""",
        expect_tools=("confluence.kb_get",),
        truth_sql=("SELECT p.version FROM (SELECT unnest(pages) AS p FROM "
                   "read_json('var/workflow/kb.json')) WHERE p.title = 'Net Revenue and friends'"),
        instruction=(
            "Which version is the wiki page 'Net Revenue and friends' (AE space) at? "
            "Read it from the team's Confluence."),
    ),
    Probe(
        id="human.slack.analytics_eng_threads",
        profile=PROFILE,
        plugs=PLUGS,
        command="""cd /workspace
set -euo pipefail
python3 - <<'PY'
import json, os
from slack_sdk import WebClient

env = os.environ
slack = WebClient(base_url=env["SLACK_BASE_URL"].rstrip("/") + "/api/",
                  token=env["SLACK_BOT_TOKEN"])
channel = next(c for c in slack.conversations_list()["channels"]
               if c["name"] == "analytics-eng")
messages, cursor = [], None
while True:
    page = slack.conversations_history(channel=channel["id"], limit=200, cursor=cursor)
    messages += page["messages"]
    cursor = (page.get("response_metadata") or {}).get("next_cursor")
    if not page.get("has_more") or not cursor:
        break
print(json.dumps({"value": len(messages)}))
PY
""",
        expect_tools=("slack.chat_history",),
        truth_sql=("SELECT count(*) FROM (SELECT unnest(c.messages) AS m FROM (SELECT "
                   "unnest(channels) AS c FROM read_json('var/workflow/chat.json')) "
                   "WHERE c.id = 'analytics-eng') WHERE m.thread_id IS NULL"),
        instruction=(
            "How many top-level messages are in the #analytics-eng Slack channel - every "
            "top-level message, i.e. not a reply in a thread? Read the channel history from "
            "the team's Slack."),
    ),
]
