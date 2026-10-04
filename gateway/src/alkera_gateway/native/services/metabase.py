"""Metabase-shaped BI service over `alkera_gateway.bi` (gateway/NATIVE.md).

The MCP agent reads a dashboard with `bi_get`, runs its panels with `bi_render`,
re-materialises the cached tiles with `bi_refresh` and reads those tiles back
with `bi_cached`. A data engineer does all four through Metabase, and Metabase
spells them as one endpoint with a cache flag:

    POST /api/card/{id}/query                    -> bi_render   (run the SQL now)
    POST /api/card/{id}/query {"ignore_cache":1} -> bi_refresh  (re-materialise)
    POST /api/card/{id}/query {"cache_only":1}   -> bi_cached   (serve the tile)

`cache_only` is this service's own flag, not Metabase's: Metabase serves a
cached result transparently and has no "cache or nothing" request, while
`bi_cached` is a distinct MCP tool with a distinct failure (`not_refreshed`).
Spelled as a flag rather than hidden, so the difference is visible in the row.

IDS. Metabase keys cards and dashboards by integer database id; this world keys
dashboards by name and panels by name inside them. Dashboard ids are therefore
accepted both ways - `/api/dashboard/revenue_daily` and `/api/dashboard/1` - and
card ids are positional over `bi_list`'s ordering (dashboards in directory
order, panels in definition order). Positional ids are NOT stable across a
`bi_update` that adds a dashboard; nothing in the world stores a card id, so
there is nothing to be stable against. Resolving one costs a real `bi_list`
read, and that read is audited rather than cached away.
"""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ...records import json_value
from ..context import DEV_TOKEN, ServiceError, audit_unrouted, note, served, service_health
from ..http import JSONBody

NAME = "metabase"
HOST = "metabase.internal"
PORT = 3000

# The one warehouse every card runs against. Metabase clients send and echo a
# database id; this world has one, and it is 1.
DATABASE_ID = 1
SESSION_ID = "alkera-local-dev-session"

app = FastAPI(title="Alkera Metabase (fake)", version="0.1.0")
# One row per request, including the ones no handler ran for (context.py).
audit_unrouted(app, NAME)


@app.exception_handler(ServiceError)
async def _service_error(request: Request, exc: ServiceError) -> JSONResponse:
    """Metabase answers a refused request with a bare `message` object."""
    return JSONResponse(
        status_code=exc.status, content={"message": exc.message, "error_code": exc.code}
    )


def _dashboards(request: Request) -> list[dict]:
    """Every dashboard definition, through `bi_list` - one audited read."""
    return served(request, NAME, "bi_list", lambda ops, obs: ops.bi_list(obs))["dashboards"]


def _catalogue(specs: list[dict]) -> list[tuple[int, dict, dict]]:
    """(card id, dashboard spec, panel) for every panel in the world."""
    return [
        (index, spec, panel)
        for index, (spec, panel) in enumerate(
            ((spec, panel) for spec in specs for panel in spec["panels"]), start=1
        )
    ]


def _sql(panel: dict) -> str:
    """The SQL the panel will actually run, including the filter `bi_render`
    wraps around it - a card whose `dataset_query` is prettier than the query
    that runs is a card that cannot be reproduced by hand."""
    if panel["row_filter"]:
        return f"SELECT * FROM ({panel['sql']}) AS tile WHERE {panel['row_filter']}"
    return panel["sql"]


def _card(card_id: int, panel: dict) -> dict:
    return {
        "id": card_id,
        "name": panel["name"],
        "description": panel["title"],
        "display": "table",
        "query_type": "native",
        "database_id": DATABASE_ID,
        "table_id": None,
        "collection_id": None,
        "collection_position": None,
        "archived": False,
        "dataset_query": {
            "type": "native",
            "database": DATABASE_ID,
            "native": {"query": _sql(panel), "template-tags": {}},
        },
    }


def _dashboard(dashboard_id: int, spec: dict, cards: list[tuple[int, dict, dict]]) -> dict:
    dashcards = [
        {
            "id": card_id,
            "card_id": card_id,
            "dashboard_id": dashboard_id,
            "row": position,
            "col": 0,
            "size_x": 12,
            "size_y": 6,
            "parameter_mappings": [],
            "card": _card(card_id, panel),
        }
        for position, (card_id, owner, panel) in enumerate(cards)
        if owner["dashboard"] == spec["dashboard"]
    ]
    return {
        "id": dashboard_id,
        "name": spec["dashboard"],
        "slug": spec["dashboard"],
        "description": spec.get("title"),
        "collection_id": None,
        "archived": False,
        "parameters": [],
        "dashcards": dashcards,
    }


def _dataset(result: dict, query: str) -> dict:
    """A warehouse result in Metabase's `{"data": {"rows", "cols"}}` envelope."""
    return {
        "status": "completed",
        "row_count": len(result["rows"]),
        "database_id": DATABASE_ID,
        "json_query": {"type": "native", "database": DATABASE_ID, "native": {"query": query}},
        "data": {
            "rows": json_value(result["rows"]),
            "cols": [
                {
                    "name": column["name"],
                    "display_name": column["name"],
                    "base_type": column["type"],
                    "source": "native",
                }
                for column in result["columns"]
            ],
            "native_form": {"query": query},
            "rows_truncated": bool(result.get("truncated")),
        },
    }


def _resolve(request: Request, card_id: int) -> tuple[dict, dict]:
    for index, spec, panel in _catalogue(_dashboards(request)):
        if index == card_id:
            return spec, panel
    raise ServiceError(404, "card_not_found", f"Not found: card {card_id}")


@app.get("/health")
def health(request: Request) -> object:
    """The dashboards this service can serve, counted through `bi_list`."""
    return service_health(
        request, NAME, lambda ops, obs: len(ops.bi_list(obs)["dashboards"])
    )


@app.post("/api/session")
def create_session(request: Request, body: JSONBody) -> dict:
    """Metabase's login. No MCP equivalent and no world state behind it, so the
    row it writes is a `read` against the service itself - see OPS_PLANE.md.

    Any username is accepted with the workspace's obviously-fake token as the
    password; a wrong password is refused, because a login that accepts anything
    teaches the agent that credentials are decoration.
    """
    if body.get("password") != DEV_TOKEN:
        raise ServiceError(401, "invalid_password", "did not match stored password")
    note(
        request, NAME, "session_create", op_class="read", targets=["metabase:session"],
        args={"username": body.get("username")}, result={"id": SESSION_ID},
    )
    return {"id": SESSION_ID}


@app.get("/api/dashboard")
def list_dashboards(request: Request) -> list[dict]:
    specs = _dashboards(request)
    cards = _catalogue(specs)
    return [_dashboard(index, spec, cards) for index, spec in enumerate(specs, start=1)]


@app.get("/api/dashboard/{dashboard_id}")
def get_dashboard(dashboard_id: str, request: Request) -> dict:
    """By name, one `bi_get` read. By integer id, one `bi_list` read first,
    because an integer is only meaningful against the whole ordering."""
    if dashboard_id.isdigit():
        specs = _dashboards(request)
        index = int(dashboard_id)
        if not 1 <= index <= len(specs):
            raise ServiceError(404, "dashboard_not_found", f"Not found: dashboard {index}")
        return _dashboard(index, specs[index - 1], _catalogue(specs))
    spec = served(
        request, NAME, "bi_get",
        lambda ops, obs: ops.bi_get(dashboard_id, obs),
        {"dashboard": dashboard_id},
    )
    return _dashboard(1, spec, _catalogue([spec]))


@app.get("/api/card")
def list_cards(request: Request) -> list[dict]:
    return [_card(index, panel) for index, _, panel in _catalogue(_dashboards(request))]


@app.get("/api/card/{card_id}")
def get_card(card_id: int, request: Request) -> dict:
    _, panel = _resolve(request, card_id)
    return _card(card_id, panel)


@app.post("/api/card/{card_id}/query")
def query_card(card_id: int, request: Request, body: JSONBody) -> dict:
    """Run one card. The MCP surface has no per-panel execution - `bi_render`
    and `bi_refresh` run a whole dashboard - so this runs the whole dashboard
    too and returns the requested panel. The row is therefore identical to the
    MCP row for the same dashboard, including the tables it names as read.
    """
    spec, panel = _resolve(request, card_id)
    dashboard = spec["dashboard"]
    args = {"dashboard": dashboard, "card": card_id}
    if body.get("cache_only"):
        rendered = served(request, NAME, "bi_cached",
                          lambda ops, obs: ops.bi_cached(dashboard, obs), args)
    elif body.get("ignore_cache"):
        rendered = served(request, NAME, "bi_refresh",
                          lambda ops, obs: ops.bi_refresh(dashboard, obs), args)
    else:
        rendered = served(request, NAME, "bi_render",
                          lambda ops, obs: ops.bi_render(dashboard, obs), args)
    if panel["name"] not in rendered["panels"]:
        raise ServiceError(404, "card_not_found", f"Card {card_id} has no result")
    return _dataset(rendered["panels"][panel["name"]], _sql(panel))
