[![CI](https://github.com/Pramaan-OSS/business-intelligence/actions/workflows/ci.yml/badge.svg)](https://github.com/Pramaan-OSS/business-intelligence/actions/workflows/ci.yml)

# Business Intelligence

A self-hosted business intelligence tool: connect your databases, build queries, charts and
dashboards, and get alerts when your numbers move.

## Quick start

You need Docker and one API key.

```sh
cp .env.example .env      # paste PRAMAAN_API_KEY=... into .env
./start
```

The command starts the app with its own database and cache, picks a free local port and prints
the address. Sign in as `Administrator`; the generated password is in
`.runtime/local-admin-password` (keep that file and your `.env` private). Stop with
`docker compose --env-file .env -f docker/docker-compose.yml down`.

Without a key the app still runs: sign-in, data sources, queries, charts and dashboards work
locally, and the features that reach outside (alert delivery, drafting help) show
"Add PRAMAAN_API_KEY to .env" and stay off until the key is set.

### Optional settings

Everything the app needs is generated on first run. `PRAMAAN_BASE_URL` may be set in `.env` if
your platform uses another address; otherwise the default applies.

`./start` serves the built frontend with the production web server. For local development,
set `BI_SERVER_MODE=development` in `.env` before starting. Dashboard previews use the local
headless browser installed in the application image.

### What it does

- **Data sources**: MariaDB, PostgreSQL, SQLite, DuckDB (including a DuckDB file over https),
  ClickHouse and BigQuery — your own databases, your own credentials, kept local. Upload CSVs
  into the built-in store, or set up sample demo data from Settings.
- **Queries and charts**: a step-by-step query builder, SQL and script queries, drill-downs and
  a drag-and-drop dashboard editor.
- **Alerts**: watch a query and get an email, a channel message, an in-app notification, or a
  hand-off to a workflow when a condition is met.
- **Ask-your-data**: draft SQL from a question in the query editor. The draft is shown to you
  first; nothing runs or saves unseen.

Every outside call — mail, chat channels, notifications, workflow hand-offs, drafting — goes
through the Pramaan platform API with your single key.

## Tests

Contract checks for the platform client run standalone:

```sh
python3 insights/tests/test_pramaan_client.py
```

The backend suite runs on a site with tests enabled (see `docker/setup.py`); the frontend build
and unit tests run with `yarn build` and `yarn test` from `frontend/`.

## Connected accounts

Connecting an outside account, such as a spreadsheet or another app used as a data source, carries a monthly Pramaan
charge for each connected account, billed to the Pramaan account behind `PRAMAAN_API_KEY`. Signing in is free.
Disconnecting an account ends its charge.

## Licence

This template is licensed under AGPL-3.0 (see `LICENSE-ORIGINAL`). The app's interface and
branding are its own; the project it was built from is credited below.

### Built on

This product is built on [Frappe Insights](https://github.com/frappe/insights), an open-source
business intelligence tool by Frappe Technologies, licensed under the GNU Affero General Public
License v3.0. `LICENSE-ORIGINAL` carries the original licence text.
