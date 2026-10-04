"""Sigma Computing: workbooks and data models as code, and the formula subset.

WHAT SIGMA OFFERS IN 2026 (researched 2026-10-03; every claim below cites the
page it came from - the OpenAPI document is the authority where pages differ)

* The Sigma REST API v2 is described by a public OpenAPI 3.1 document,
  "Sigma Computing Public REST API" 2.0.0+20260930.a3bda220:
  https://assets.sigmacomputing.com/openapi/public-rest-api/sigma-computing-public-rest-api.json
  Errors are `{"message", "code", "requestId"}` ("Sigma API Error"). Base URLs
  are per cloud (api.sigmacomputing.com, aws-api.sigmacomputing.com,
  api.us-a.aws..., api.eu.aws..., api.us.azure..., ...).
* Auth: `POST /v2/auth/token`, form-encoded `grant_type=client_credentials`
  with the client id and secret as HTTP Basic (securitySchemes basicAuth; the
  quickstart `curl --user "${CLIENT_ID}:${CLIENT_SECRET}"`), answering
  `{access_token, refresh_token, token_type: "bearer", expires_in}` - one hour
  (help.sigmacomputing.com/reference/post-token). `GET /v2/whoami` returns
  `{userId, organizationId}`.
* Lists page with `?limit=` (max 1000) and `?page=<nextPage>`, answering
  `{entries, nextPage: string|null, total, hasMore (deprecated)}`
  (reference/list-workbooks; the example pages by "50").
* WORKBOOKS AS CODE (Beta; docs/manage-workbooks-as-code): the representation
  is JSON or YAML (`?format=yaml` / `Accept: application/yaml`) and travels in
  `contents`:
    GET  /v2/workbooks/{workbookId}?includeContents=true   read (+documentVersion)
    PUT  /v2/workbooks/{workbookId}/contents               replace; "you can
         submit a GET response unchanged"; no partial updates; optional
         `documentVersion` for optimistic concurrency
    POST /v2/workbooks {name, folderId, contents, dryRun}  create / validate;
         a failed dry run is 400 `{dryRun, valid: false, errors, warnings}`
  Deprecated in the same document: GET/PUT /v2/workbooks/{id}/spec (`document`
  instead of `contents`), POST /v2/workbooks/spec, POST /v2/workbooks/spec/verify.
  `contents` = {schemaVersion: 1, kind: "workbook", pages: [{id, name}],
  elements: [flat], layout: XML placing elements on pages}; element kinds
  include table, pivot-table, kpi-chart, bar/line/area/... charts, controls,
  containers, text (schema `CommonElement`; docs/workbook-representation-
  example-library). Not represented: tagged versions, comments, sharing.
* DATA MODELS AS CODE (GA; docs/manage-data-models-as-code):
    GET  /v2/dataModels/{dataModelId}/spec    PUT same    POST /v2/dataModels/spec
  `{dataModelId, name, ..., schemaVersion: 1, pages: [{id, name, elements}]}`;
  table elements have `source` (warehouse-table {connectionId, path},
  sql {statement}, join, union, table, data-model, ...), `columns`
  [{id, formula, name?, hidden?, format?}], `metrics`, `relationships`
  [{targetElementId, keys: [{sourceColumnId, targetColumnId}], name}].
  Relationships are many-to-one or one-to-one, from the most granular table
  (docs/define-relationships-in-data-models). Datasets are gone: "As of
  September 15, 2026, you can no longer use datasets as a data source"
  (docs/about-metrics), so data models are what a 2026 Sigma shop models in.
* Version control / CI: version TAGS (`POST /v2/workbooks/tag`,
  `GET /v2/workbooks/{id}/tags`, `POST /v2/dataModels/tag`) and
  `GET /v2/workbooks/{id}/version-history` exist; there is no git
  integration or file format of Sigma's own. Teams commit the API's spec
  responses (phData, "Sigma API Integration: Sync Workbooks-as-Code to Git":
  `workbooks/<name>.yaml` from `/spec?format=yaml`, synced by GitHub Actions).
* Exports: `POST /v2/workbooks/{workbookId}/export` {elementId, format:
  {type: csv|json|jsonl|xlsx|pdf|png}, rowLimit, offset} -> {jobComplete,
  queryId}; `GET /v2/query/{queryId}/download` (204 while processing).
* Connections: `GET /v2/connections`, `GET /v2/connections/{id}`,
  `POST /v2/connections/{connectionId}/sync` {path: [...]} -> {},
  `POST /v2/connection/{connectionId}/lookup`, table columns at
  `GET /v2/connections/tables/{tableId}/columns`.
* Formulas: help.sigmacomputing.com/docs/function-index (200+ functions),
  /docs/operators-overview. Sigma types are Text, Number (every integer,
  decimal and float), Date, Logical, Variant (docs/data-types-and-formats).

THIS PACKAGE

    read_project(path, *, warehouse=None) -> (Model, reports)    project.py
    write_project(model, reports, dest) -> [paths]               project.py
    parse_expression(text, model, *, table=None) -> Expr         formula.py
    format_expression(expr, model, *, table=None) -> str         formula.py

The repo representation (project.py): `<workspace>/bi/sigma/data-models/
<name>.json` (the data model spec), `workbooks/<name>.json` (the workbook GET
with contents) and `connections/<name>.json` (the connection and the columns
Sigma has synced - catalog.py says why a Sigma project needs it: Sigma's files
carry no column types). datamodel.py and workbook.py hold the mappings and
their refusals; examples.py is the Commerce model as Sigma can say it.

`read_project`'s optional `warehouse` (the shared contract's keyword) is how
column types come from the warehouse, as they do in Sigma. Every visual read
from a workbook has `groups="rows"` (workbook.py, GROUPS).
"""
from __future__ import annotations

from .formula import format_expression, parse_expression
from .project import read_documents, read_project, write_project

__all__ = ["format_expression", "parse_expression", "read_documents", "read_project",
           "write_project"]
