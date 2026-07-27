# Foundation + Creator Permission System — Design

Status: Approved
Date: 2026-07-27
Sub-project: 1 of 11 (see roadmap below)

## Context

StreamClip Co. is an automated short-form clipping business: discover livestreams,
detect highlight moments, generate clips, and publish them to TikTok/Reels/Shorts/X.

The core legal constraint shapes the architecture: clipping and republishing a
streamer's content without their permission is copyright infringement, and
Twitch/YouTube/Kick ToS prohibit scraping streams for redistribution without
authorization. The only viable long-term version of this business is a
**creator-partner model** — streamers opt in via a signed agreement (rev-share or
flat license), and the pipeline is hard-gated to only ever touch content from
creators with an active agreement on file.

This sub-project builds that gate, plus the minimal repo/infra foundation
everything else will build on.

## Roadmap (for context — not all built now)

1. **Foundation + Creator Permission System** ← this spec
2. Stream Discovery (authorized creators only)
3. Clip Detection
4. Video Processing
5. AI Content Writer
6. Quality Control
7. Social Media Publishing (official APIs only)
8. Analytics + feedback loop
9. Dashboard/UI
10. Automation/orchestration polish
11. (ongoing) Documentation

Each gets its own spec + implementation plan when we get to it.

## Architecture

Single FastAPI monolith with clean internal module boundaries — not
microservices. No proven load yet; splitting into services now is premature.
Later sub-projects add modules/workers to this same repo, and only get split
into independently-deployed services once they demonstrably need it (video
processing, being CPU-heavy, is the likely first candidate).

```
backend/
  app/
    api/          # FastAPI routers (public + internal)
    models/       # SQLAlchemy models
    services/     # business logic (permission gate, outreach, agreements)
    workers/      # arq background jobs (send email, etc.)
    core/         # config, security, db session
  migrations/     # Alembic
infra/
  docker-compose.yml   # postgres, redis, api, worker
tests/
```

- Postgres: persistent state
- Redis + **arq**: background job queue (async, pairs with FastAPI's async style)
- Deploy target: Railway — 1 web service (API), 1 worker service, managed
  Postgres + Redis addons
- Public agreement page: server-rendered Jinja2 (no separate frontend build
  needed for one page; the real dashboard is sub-project 9)

## Data model

```
creators
  id, platform (twitch/youtube/kick), platform_channel_id, display_name,
  contact_email, status (prospect/contacted/applied/authorized/declined/revoked),
  created_at, updated_at
  UNIQUE(platform, platform_channel_id)

agreement_terms_versions
  id, version, effective_date, body_markdown
  -- versioned so we always know exactly what text a creator agreed to

agreements
  id, creator_id, terms_version_id, rev_share_pct, scope_notes,
  accepted_at, accepted_ip, accepted_user_agent, signature_name,
  status (pending/active/revoked), revoked_at

outreach_emails
  id, creator_id, template_version, subject, body,
  status (drafted/approved/sent/bounced/replied),
  approved_by, sent_at, provider_message_id

users
  id, email, hashed_password, role
  -- internal team logins; minimal for now, full admin dashboard is sub-project 9
```

The load-bearing piece: `is_authorized(creator_id) -> bool`, backed by a live
DB check (`creators.status == 'authorized' AND agreements.status == 'active'`).
Every future sub-project that touches a creator's stream must call this first.
Never cached — a revocation must take effect immediately.

## Onboarding flow (outbound → clickwrap)

1. **Targeting**: v1 has no auto-discovery (that's sub-project 2). Creators are
   added manually / via seed script as `creators` rows, status `prospect`.
2. **Draft**: a service renders an outreach email from a template +
   `creator.display_name` → `outreach_emails` row, status `drafted`.
3. **Human approval**: an internal admin endpoint lists drafts for review/edit;
   approving sets status `approved` and records `approved_by`. No email reaches
   a real creator without this step.
4. **Send**: an arq worker job sends via **Resend** (chosen for v1: simple API,
   good free tier, easy to swap later — sending lives behind one interface,
   `EmailSender`). Email contains a signed JWT magic link to
   `/partner/agree?token=...`, short expiry, single-purpose (agree vs revoke),
   regenerable if expired. Status → `sent`.
5. **Landing page**: Jinja2-rendered, shows current
   `agreement_terms_versions.body_markdown` + rev-share terms; "I Agree"
   requires typed name confirmation.
6. **Accept**: creates `agreements` row (`accepted_at`, `accepted_ip`,
   `accepted_user_agent`, `signature_name` captured as evidence), sets
   `creators.status → authorized`.
7. **Revoke**: every email and the agreement page footer includes a
   token-based revoke link → `agreements.status = revoked`,
   `creators.status = revoked`. Takes effect immediately (no downstream cache).

## Security & error handling

- Secrets (DB URL, Redis URL, Resend API key, JWT signing key) via environment
  variables; `.env.example` committed with placeholders, real `.env`
  gitignored.
- Magic-link tokens: signed JWTs, scoped to one creator + one purpose
  (`agree`/`revoke`), short expiry.
- Public endpoints (`/partner/agree`, `/partner/revoke`) rate-limited per-IP.
- `UNIQUE(platform, platform_channel_id)` enforced at the DB level, not just
  app logic.
- Bounced/failed sends update `outreach_emails.status` and stop — no blind
  retry.
- HTTPS-only once deployed (default on Railway).

## Testing

- pytest against a real Postgres test database (docker-compose test service),
  not sqlite — migration and constraint behavior needs to be real.
- Priority coverage: `is_authorized()` gate, token signing/verification
  (expiry, tampering, wrong-purpose token), agree/revoke endpoints end-to-end.
- CI (GitHub Actions): ruff (lint), mypy (typecheck), pytest — on every push.

## Out of scope for this sub-project

- Auto-discovery of streamers (sub-project 2)
- Full admin dashboard UI (sub-project 9) — approvals happen via a minimal
  internal endpoint/CLI for now
- Any actual stream downloading/clipping (sub-projects 3–4) — this sub-project
  only builds the gate they must call
