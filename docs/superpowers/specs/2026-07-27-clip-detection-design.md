# Clip Detection — Design

Status: Approved
Date: 2026-07-27
Sub-project: 3 of 11 (see roadmap below)

## Context

Sub-project 2 (Stream Discovery) tracks when authorized creators are live and
periodic viewer-count snapshots, but nothing yet identifies *which moments*
within a live session are clip-worthy. Sub-project 3 adds that: it ingests
chat activity for authorized, currently-live creators and detects spikes that
likely correspond to highlight-worthy moments, storing them as scored
candidates for later sub-projects (Video Processing, Quality Control) to
consume.

## Roadmap (for context — not all built now)

1. Foundation + Creator Permission System — done
2. Stream Discovery — done
3. **Clip Detection** ← this spec
4. Video Processing
5. AI Content Writer
6. Quality Control
7. Social Media Publishing (official APIs only)
8. Analytics + feedback loop
9. Dashboard/UI
10. Automation/orchestration polish
11. (ongoing) Documentation

## Architecture

Four new pieces, extending the existing FastAPI + arq monolith:

1. **Twitch chat listener** — a long-running arq job (not a short cron job)
   that opens a Twitch EventSub WebSocket (`channel.chat.message`
   subscription) for one live creator, buckets incoming message counts into
   fixed windows (15s default), and periodically flushes buckets to the DB.
   Enqueued by the existing webhook handler's `stream.online` processing
   (sub-project 2), reusing the existing queue-enqueue pattern. Exits when
   the stream ends — detected via WebSocket close, or by polling
   `StreamSession.ended_at`, or a 12h safety-net timeout, whichever first.
   This is a new job *shape* for the codebase (long-lived vs. short
   cron/webhook jobs); arq supports it via a generous `job_timeout`, so no
   new process type is needed.
2. **YouTube chat poller** — an arq cron job, same shape as the existing
   `poll_youtube_streams`, calling `liveChatMessages.list` for each live
   authorized YouTube creator and bucketing counts the same way. Adds to
   YouTube's already-tight quota (per sub-project 2's known constraint), so
   its interval is configurable, not hardcoded.
3. **Shared bucket storage** — both ingestion paths write to the same table,
   so detection logic is platform-agnostic.
4. **Detection job** — a separate arq cron job (every 2 min) that scans each
   open `StreamSession`'s recent buckets, computes a rolling baseline
   (mean + stddev over a trailing window), flags buckets exceeding
   `baseline + k·stddev` as "hot," merges adjacent hot buckets (with small
   pre/post padding) into a candidate, and stores it with a score. Decoupled
   from ingestion so detection parameters can be retuned independently.

**v1 scope:** chat-message-rate spikes only. Viewer-count data (5-minute
granularity from sub-project 2) is too coarse to combine cleanly with
15-second chat buckets, so it's left as future work rather than force-fit
into one algorithm now. No LLM/AI scoring in v1 — purely numeric heuristics;
AI-assisted scoring is additive future work (likely alongside sub-project 5).

**Authorization re-check (carrying forward sub-project 2's Critical
finding):** the chat listener is long-lived and can run for hours, so it
cannot check `is_authorized()` once at start and trust that forever — it
re-checks on every bucket flush, with a fresh DB query each time (never a
cached/stale object), and disconnects immediately if a creator revokes
mid-stream.

## Data model

```
chat_activity_buckets
  id, stream_session_id (FK -> stream_sessions.id),
  bucket_start (datetime), message_count (int)
  UNIQUE(stream_session_id, bucket_start)

clip_candidates
  id, stream_session_id (FK -> stream_sessions.id),
  start_at, end_at, score (float), signal_type (str, e.g. "chat_spike"),
  created_at
```

`signal_type` exists now (even with only one value) so viewer-count-based or
future AI-scored candidates can be added later without a schema change. No
status/workflow field on `ClipCandidate` yet — nothing downstream consumes
these yet (Quality Control is sub-project 6), so a review/approval state
machine would be speculative; adding it later is a pure additive migration.
Every detected spike above threshold gets stored — no second "good enough"
filter beyond the detection threshold itself, keeping the algorithm to one
tunable knob (`k`, the z-score multiplier).

## Ingestion & detection flow

**Twitch chat (primary, real-time)**
1. The webhook's existing `stream.online` handling additionally enqueues a
   `listen_to_twitch_chat` job for that creator/session.
2. The job opens a Twitch EventSub WebSocket, subscribes to
   `channel.chat.message` for that channel, and increments an in-memory
   counter per 15s bucket as messages arrive.
3. Every bucket boundary: flush the completed bucket's count to
   `chat_activity_buckets`, then re-check `is_authorized()` fresh from the
   DB — disconnect and exit immediately if false.
4. Exit condition: WebSocket closes (stream ended), or
   `StreamSession.ended_at` is observed set, or a 12h safety-net timeout —
   whichever comes first.

**YouTube chat (polling)**
- `poll_youtube_chat` (arq cron, interval configurable via `Settings`,
  conservative default given known quota pressure) calls
  `liveChatMessages.list` for each live authorized YouTube creator, counts
  messages since the last poll, and writes one bucket per poll interval
  (bucket width = poll interval; coarser than Twitch's, but the schema
  doesn't care).

**Detection**
- `detect_clip_candidates` (arq cron, every 2 min) scans each currently-open
  `StreamSession`'s buckets from the last N minutes, computes rolling
  mean/stddev, flags buckets over `mean + k·stddev`, merges adjacent hot
  buckets (+ padding) into a `ClipCandidate`, stores it. Dedupes against
  existing overlapping `(stream_session_id, start_at, end_at)` candidates
  before inserting, so re-running periodically doesn't create duplicates for
  already-detected windows.

## Security & error handling

- New settings: `twitch_chat_bucket_seconds: int = 15`,
  `clip_detection_z_threshold: float = 2.0`,
  `clip_detection_min_gap_seconds: int = 30` (candidates closer than this
  merge into one), `youtube_chat_poll_interval_minutes: int = 5`. No new
  secrets — chat reuses the existing Twitch/YouTube credentials from
  sub-project 2.
- The chat listener's periodic `is_authorized()` re-check (fresh query, not
  cached) is the load-bearing safety property, directly incorporating
  sub-project 2's lesson — every bucket flush doubles as an authorization
  checkpoint.
- WebSocket disconnects/errors are caught and logged, with a bounded
  reconnect-retry policy (exponential backoff, capped attempts) before
  exiting cleanly — a permanently-failing reconnect loop must not run
  forever.
- `detect_clip_candidates` and `poll_youtube_chat` both filter through
  `list_authorized_creators` (sub-project 2), so a revoked creator's stale
  buckets are never scanned for new candidates and no new buckets get
  written for them.
- If a chat listener job crashes or the worker restarts, an open
  `StreamSession` with no active listener just accumulates a gap in its
  buckets — a detection-quality issue, not a data-integrity one. No special
  recovery logic in v1; a future reconciliation job could detect
  "open session, no active listener" and re-enqueue, but that's deferred.

## Testing

- A fake WebSocket transport (mirroring the `Fake*Client` pattern from
  sub-projects 1-2) lets the listener job's bucketing, flush, and
  re-auth-check logic be tested without a real WebSocket connection.
- Detection algorithm gets dedicated unit tests: no spike → no candidate,
  one clear spike → one candidate with correct start/end/score, two adjacent
  spikes → merged into one candidate, two far-apart spikes → two separate
  candidates, revoked creator → skipped entirely.
- Re-running detection on the same data twice must not create
  duplicate/overlapping candidates.
- Authorization re-check test: creator authorized at listener start, revoked
  mid-run via a raw-SQL update (a Python-level mutation wouldn't reproduce a
  caching bug, per sub-project 2's regression-test precedent) → listener
  disconnects and stops writing buckets.

## Out of scope for this sub-project

- Viewer-count-based or AI/LLM-based scoring signals (future work)
- Any video/audio downloading, clipping, or processing (sub-project 4)
- A review/approval workflow for candidates (sub-project 6, Quality Control)
- Kick or any platform beyond Twitch/YouTube
- Automatic recovery/re-enqueue of chat listeners after a worker restart
