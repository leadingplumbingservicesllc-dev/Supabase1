# Palm Beach commercial lead collector

**Deployment status: not live.** Worker infrastructure is implemented; real county
portal adapters, the existing Supabase table, and cloud deployment require validation.
The example selectors and endpoint names are illustrative and intentionally disabled.
This repository does not yet constitute a working Palm Beach County scraper.

## What the worker does

Each flight visits both configured sources and inserts new commercial properties
with status `New`. Explicit commercial/industrial/mixed-use classification is required;
unknown uses are skipped. Active/open violations qualify. Backflow deadlines must be
ISO dates from today through 90 days ahead (configurable). The same property is stored
once; existing lead statuses are preserved. Comparison normalizes case and punctuation
but deliberately retains units and suffixes. Address aliases require a separate,
verified address-standardization integration.

Requests use HTTPS verification, timeouts, bounded retries, five-second pacing,
robots.txt checks, pagination limits, and transparent rotating user-agent profiles.
403s stop that source. No CAPTCHA or access-control bypass is implemented. JavaScript,
login, search forms, differently named statuses, and date formats need a verified
portal-specific adapter rather than blind configuration changes. Robots network failures
fail closed. Redirects fail closed until the target host is reviewed.

## Required before live use

1. Confirm official Code Enforcement and Building Permits URLs and permitted public
   record access. During development, outbound requests to discover.pbc.gov and
   www.pbcgov.org were blocked by the environment proxy. No live portal was inspected.
   Confirm where backflow deadlines are actually published; their presence in a
   building-permit system must not be assumed.
2. Inspect actual records and configure `portals.json` using `portals.example.json`.
   Map all fields to canonical values: `kind` must be `violation` or `backflow`, `use`
   must explicitly identify commercial use, `status` active/open for violations, and
   `deadline` YYYY-MM-DD for backflow. The current adapters read flat JSON fields or
   HTML row text; extend them if the real portal requires more. Verify filters,
   pagination, dates, classification, empty-result pages, and known positive records
   before setting `verified` to true. Both sources must be configured.
3. Review `migrations/001_commercial_leads.sql` in Supabase SQL Editor. For an existing
   table, inspect its schema first; map columns and backfill address_key for **all**
   existing addresses using `address_key` from the worker. Review collisions and add
   a unique constraint on address_key before running. CREATE TABLE IF NOT EXISTS does
   not migrate an existing table. Otherwise old addresses could be inserted twice.
4. Copy `.env.example` to `.env` and supply project URL and server-only service-role
   key through your server's secret manager. Never commit `.env` or expose this key
   to a browser. Restrict host/account access and rotate leaked keys. The client uses
   supabase-py. Anonymous Supabase access is disabled by the provided RLS schema.

## Development and verification

```sh
cd /workspace/Supabase1
python -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
```

Use this existing checkout; cloud tasks are isolated and need no extra Git worktree.
Tests exercise fixture data, not live county records or Supabase. After configuration,
export credentials securely, then run:

```sh
PORTAL_CONFIG=portals.json RUN_ONCE=1 .venv/bin/python -m lead_agent.worker
```

A nonzero exit or `sources_failed` in state/health.json means incomplete collection.
Check a known qualifying record in Supabase, rerun the flight, and verify no duplicate
was inserted and existing statuses were preserved before enabling continuous operation.
Logs omit record addresses, response bodies, and credentials. The health file records
counts and completion time; zero eligible records alone do not prove correct extraction.

## Persistent server deployment

On a Docker Compose server with `.env` and a verified `portals.json`:

```sh
docker compose up -d --build
docker compose logs --tail=100 collector
docker compose ps
```

Default polling interval is six hours with jitter, minimum five minutes. The process
runs continuously, handles SIGTERM, and restarts on crash/server restart when Docker
is enabled at boot. The container runs unprivileged with a read-only root filesystem,
log rotation, and a persistent health volume. Configure an external monitor/alert for
unhealthy or absent containers; Docker health checks alone do not restart an unhealthy
process. Only run one replica. The database unique constraint prevents duplicate
inserts if flights overlap. A failed source is retried next flight; other sources continue.
The cloud coding workspace is not a production hosting service. No 24/7 deployment
is claimed until a server, credentials, verified adapters, and live checks are available.

## Render

`render.yaml` defines a paid Starter background worker, with automatic deployments
turned off. Import this GitHub repository as a Blueprint in your Render account.
Review Render's current price before provisioning. Supply SUPABASE_URL,
SUPABASE_SERVICE_ROLE_KEY, and the complete verified portal configuration as
PORTAL_CONFIG_JSON through Render's secret environment settings. JSON configuration
takes precedence over a local file. No inbound web port is required.

Review the SQL/schema and complete a one-flight validation before launching the
continuous worker. For a one-flight check on Render, temporarily set RUN_ONCE=1 and
inspect logs/database, then remove it for continuous operation. Render may restart a
one-flight worker, so suspend it promptly after checking. Health state is ephemeral
on Render; use its logs and an external alerting system to monitor flights. Nothing
in this repository automatically creates a paid Render service.
