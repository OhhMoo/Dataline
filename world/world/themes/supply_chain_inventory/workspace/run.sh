#!/bin/sh
set -eu
cd "$(dirname "$0")"
dbt build --project-dir dbt_project
