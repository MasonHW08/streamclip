# StreamClip Co. — Foundation & Creator Permission System

Nothing in this codebase touches a creator's stream unless that creator has an
active, signed agreement on file. See `docs/superpowers/specs/2026-07-27-foundation-permission-system-design.md`
for the full design.

## Local setup

1. Copy `.env.example` to `backend/.env` and fill in `JWT_SECRET` (any long random
   string) and `RESEND_API_KEY` (leave blank to skip real sends — the worker will
   just log a failure and leave the email in `approved` status for retry). The app
   loads settings relative to the `backend/` working directory, which is where all
   the commands below (`alembic`, `uvicorn`, `arq`, the `scripts.*` modules) run from.
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

## Adding a creator to the outreach pipeline

There's no discovery system yet (that's sub-project 2). For now, add a `Creator`
row directly (e.g. via a Python shell using `SessionLocal`), then draft outreach
for them via `outreach_service.draft_outreach_email`. Review and approve drafts
via `GET /internal/outreach` and `POST /internal/outreach/{id}/approve`
(HTTP Basic auth, using the user created in step 4).
