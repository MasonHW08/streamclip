# Stream Discovery — Design

Status: Approved
Date: 2026-07-27
Sub-project: 2 of 11 (see roadmap below)

## Context

Sub-project 1 (Foundation + Creator Permission System) built the hard gate: nothing
may touch a creator's stream unless `is_authorized(db, creator_id)` returns true.
Sub-project 2 is the first thing downstream of that gate — it monitors *already
authorized* creators' livestreams on Twitch and YouTube, using official APIs only,
and records when they're live and basic engagement data (viewer counts over time).
It does not discover or reach out to new creators — that path (manual creator
entry → outreach → agreement) is Foundation's job and stays unchanged.

This gives sub-project 3 (Clip Detection) something to work with: a record of
live sessions and viewer-count history for creators who have actually opted in.

## Roadmap (for context — not all built now)

1. Foundation + Creator Permission System — done
2. **Stream Discovery** ← this spec
3. Clip Detection
4. Video Processing
5. AI Content Writer
6. Quality Control
7. Social Media Publishing (official APIs only)
8. Analytics + feedback loop
9. Dashboard/UI
10. Automation/orchestration polish
11. (ongoing) Documentation

## Architecture

New modules in the same FastAPI monolith — no new service, consistent with
Foundation's "split only when it needs to" approach.

```
backend/app/
  models/
    stream_session.py        # StreamSession, ViewerSnapshot
  services/
    twitch_client.py          # TwitchClient protocol + real (Helix) + Fake
    youtube_client.py         # YouTubeClient protocol + real (Data API v3) + Fake
    stream_discovery_service.py  # reconcile_creator_stream_state(), subscription reconciliation
  api/
    webhooks.py                # POST /webhooks/twitch/eventsub
  workers/
    stream_discovery_tasks.py  # arq cron jobs: poll_youtube, poll_twitch_backup,
                                # reconcile_twitch_subscriptions
```

Four pieces:

1. **Platform clients** — `TwitchClient`/`YouTubeClient` protocols (`get_stream_status(channel_id) -> StreamInfo | None`, plus Twitch-only EventSub subscription management), each with a real implementation and a `Fake*Client` for tests. Same pattern as `EmailSender`/`ResendEmailSender`/`FakeEmailSender` in Foundation — the whole system is built and tested without real Twitch/Google credentials; those get provided later as env vars.
2. **Twitch EventSub webhook receiver** (`POST /webhooks/twitch/eventsub`) — verifies Twitch's HMAC signature, handles the one-time subscription-verification handshake, and turns `stream.online`/`stream.offline` notifications into `StreamSession` rows. Primary (fast) detection path for Twitch.
3. **`reconcile_creator_stream_state(db, creator, client)`** — checks one creator's current live status and does the right thing: opens a new session, adds a viewer snapshot to an already-open session, or closes a session that's gone offline. Idempotent, keyed on `(platform, external_stream_id)`. Reused by two schedules:
   - YouTube poller (arq cron, ~5 min) — YouTube has no webhook equivalent for arbitrary channels, so this is YouTube's *primary* detection path.
   - Twitch backup poller (arq cron, ~15 min) — a low-frequency safety net in case an EventSub delivery is missed; not the primary path.
4. **Twitch subscription reconciliation** (arq cron, ~30 min) — diffs Twitch's actual EventSub subscription list against the current set of `is_authorized()` Twitch creators; subscribes newly-authorized creators, deletes subscriptions for revoked ones. Deliberately loosely coupled to `agreement_service.py` — nothing in Foundation changes.

Every code path that touches `stream_sessions`/`viewer_snapshots` — webhook handler and both pollers — calls `is_authorized()` first and no-ops if false, so a revoked creator's data stops updating immediately even if a subscription or poll briefly lingers.

## Data model

```
stream_sessions
  id, creator_id (FK -> creators.id), platform (twitch/youtube),
  external_stream_id (platform's own id for this broadcast),
  title, category, started_at, ended_at (NULL while live),
  created_at
  UNIQUE(platform, external_stream_id)

viewer_snapshots
  id, stream_session_id (FK -> stream_sessions.id),
  viewer_count, captured_at
```

A session is "live" iff `ended_at IS NULL`. No `creators` schema changes — Foundation's
`platform`/`platform_channel_id` fields are exactly what the clients need to query.
Twitch subscription state isn't tracked locally; the reconciliation job asks Twitch
directly ("list my subscriptions") each run rather than risking local/remote drift.

## Detection & reconciliation flow

**Twitch (primary: EventSub webhook)**
1. Twitch POSTs to `/webhooks/twitch/eventsub`; handler verifies the HMAC signature
   against `twitch_webhook_secret`, rejecting (403) on mismatch.
2. Handles Twitch's subscription-verification handshake (echoes back a `challenge`
   field once, at subscription-creation time).
3. `stream.online`: look up `Creator` by `platform_channel_id`, check
   `is_authorized()` — false means ignore and return 200 (not an error; Twitch
   retries on non-2xx). True means create a `StreamSession`.
4. `stream.offline`: find the creator's open session, set `ended_at`.

**Twitch (backup) + YouTube (primary): polling**
- Cron job iterates `is_authorized()` creators for the platform, calls
  `client.get_stream_status(channel_id)`, passes the result to
  `reconcile_creator_stream_state` — which handles new/existing/closed uniformly.

**Twitch subscription reconciliation**
- Every ~30 min: fetch live EventSub subscriptions from Twitch, fetch current
  `is_authorized()` Twitch creators from the DB, subscribe/unsubscribe the diff.

**Viewer snapshots**
- Folded into `reconcile_creator_stream_state` — every time it finds a session
  still live, it records a `ViewerSnapshot`. No separate snapshot job.

## Security & error handling

- New settings (env vars, empty-string defaults so dev/tests don't need real
  credentials): `twitch_client_id`, `twitch_client_secret`, `twitch_webhook_secret`,
  `youtube_api_key`.
- Webhook signature verification happens before any DB access — an invalid
  signature never reaches business logic.
- Twitch App Access Token (client-credentials grant, needed for Helix calls and
  EventSub subscription management) is cached in memory inside `TwitchClient`
  with expiry tracking, refreshed on expiry or a 401.
- YouTube Data API quota (10,000 units/day default; `search.list` costs 100
  units) is the real constraint on polling frequency — at even modest creator
  counts and a 5-minute interval, naive polling exceeds default quota. The
  client should prefer the cheaper way to check "is this channel live right
  now" over `search.list` where the API allows it, and polling interval /
  per-run batch size are configurable so they can be tuned against real quota
  once credentials exist. Flagging this now as a known constraint.
- All external calls go through the client interface, never called directly
  from webhook/worker code.
- A platform API failure for one creator during a poll run is logged and
  skipped, not allowed to abort the run for every other creator.

## Testing

- Fake clients (`FakeTwitchClient`, `FakeYouTubeClient`) exercise every
  reconciliation/webhook path without real API calls.
- Webhook signature verification: valid, invalid, tampered/replayed cases.
- `reconcile_creator_stream_state`: not-live→live (opens session), live→live
  (adds snapshot), live→not-live (closes session), and the `is_authorized()`
  gate (revoked creator's data never updates even if the platform reports live).
- Twitch subscription reconciliation: new authorized creator gets subscribed,
  revoked creator gets unsubscribed, already-correct state is a no-op.

## Out of scope for this sub-project

- Discovering/prospecting new creators (stays manual, per Foundation)
- Clip detection, chat-activity scoring, or any content processing (sub-project 3+)
- Kick or any platform beyond Twitch/YouTube
- A UI for viewing stream/viewer history (sub-project 9)
