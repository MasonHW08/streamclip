# StreamClip Co. — Foundation & Creator Permission System

Nothing in this codebase touches a creator's stream unless that creator has an
active, signed agreement on file. See `docs/superpowers/specs/2026-07-27-foundation-permission-system-design.md`
for the full design.

## Local setup

1. Copy `.env.example` to `backend/.env` and fill in `JWT_SECRET` (any long random
   string — at least 32 characters, and the app refuses to start with the
   `.env.example` placeholder unless `ENVIRONMENT=development`) and `RESEND_API_KEY`
   (leave blank to skip real sends — the worker logs the failure and leaves the email
   in `approved` status, retriable via `POST /internal/outreach/{id}/retry`). The app
   loads settings relative to the `backend/` working directory, which is where all
   the commands below (`alembic`, `uvicorn`, `arq`, the `scripts.*` modules) run from.
   `infra/docker-compose.yml` reads the same `backend/.env` file.
2. Start Postgres and Redis:
   ```
   docker compose -f infra/docker-compose.yml up -d postgres redis
   ```
3. Install backend deps and run migrations:
   ```
   cd backend
   pip install -e ".[dev]"
   alembic upgrade head
   ```
4. Seed the v1 partner terms and create your first internal user:
   ```
   python -m scripts.seed_terms
   python -m scripts.create_user --email you@streamclip.co --password <pick one>
   ```
5. Run the API and worker (separate terminals):
   ```
   uvicorn app.main:app --reload
   arq app.workers.settings.WorkerSettings
   ```

## Running tests

Requires Postgres + Redis running (step 2 above).

```
cd backend
pytest -v
```

The suite creates and drops the whole schema, so `tests/conftest.py` refuses to run
unless the resolved database name contains `test`. If you've `export`ed a
`DATABASE_URL` pointing at the dev database in your shell, unset it first — an
exported value overrides the test default.

## Deployment: proxy headers

`backend/Dockerfile` runs uvicorn with `--proxy-headers --forwarded-allow-ips='*'`.
Per-IP rate limiting on `/partner/*` and the `agreements.accepted_ip` evidence field
both read the client address, and behind a load balancer the raw socket peer is the
balancer — collapsing every creator into one rate-limit bucket and recording a
useless IP on the consent record. These flags make uvicorn trust `X-Forwarded-For`
instead.

This assumes the deploy target is a PaaS edge (Railway et al.) that sets and strips
`X-Forwarded-For` itself and is the only thing that can reach the container. Do not
expose the container directly to the internet with these flags set — a caller could
then spoof any client IP by supplying the header themselves. If you deploy somewhere
without a trusted edge, narrow `--forwarded-allow-ips` to your balancer's addresses.

## Stream Discovery (sub-project 2)

Monitors already-authorized creators' livestreams on Twitch (via EventSub
webhook, `POST /webhooks/twitch/eventsub`) and YouTube (via polling). Nothing
here works without real credentials:

- **Twitch**: register an app at https://dev.twitch.tv/console, set
  `TWITCH_CLIENT_ID`/`TWITCH_CLIENT_SECRET`. Set `TWITCH_WEBHOOK_SECRET` to any
  long random string (used to verify EventSub payloads). Set
  `TWITCH_EVENTSUB_CALLBACK_URL` to your deployed app's public URL + the
  webhook path — Twitch must be able to reach it, so this doesn't work against
  `localhost` without a tunnel (e.g. ngrok) during development.
- **YouTube**: create a Google Cloud project, enable the YouTube Data API v3,
  create an API key, set `YOUTUBE_API_KEY`. The default quota (10,000
  units/day) does not cover the shipped poll schedule even for a single
  creator: `poll_youtube_streams` runs every 5 minutes (288 times/day) and
  `search.list` (used to check live status) costs 100 units per call, so one
  creator alone costs ~28,800 units/day — already ~3x the default quota
  before adding any more creators. Lower the poll frequency in
  `app/workers/settings.py`'s `cron_jobs` and/or request a quota increase
  from Google before relying on this in production.

Until these are set, the system runs fine with `Fake*Client` test doubles in
tests, but the real workers (`poll_youtube_streams`,
`poll_twitch_streams_backup`, `reconcile_twitch_subscriptions_task`) will
either no-op or error against a live Twitch/YouTube API without them.

## Adding a creator to the outreach pipeline

There's no discovery system yet (that's sub-project 2). For now, add a `Creator`
row directly (e.g. via a Python shell using `SessionLocal`), then draft outreach
for them via `outreach_service.draft_outreach_email`. Review and approve drafts
via `GET /internal/outreach` and `POST /internal/outreach/{id}/approve`
(HTTP Basic auth, using the user created in step 4).
