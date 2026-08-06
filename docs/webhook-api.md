# webhook_server.py — HTTP API reference

`webhook_server.py` wraps `config_grabber.build()` as an HTTP-triggered service. It's meant to run behind a single gunicorn worker (see `docker-compose.yml`), because builds share one on-disk git working copy and are serialized by an in-process lock, not a distributed one.

## `POST /`

Trigger a build.

**Auth required** — `Authorization: Bearer <token>`, where `<token>` must match the `WEBHOOK_TOKEN` environment variable (checked with a constant-time comparison). Missing or invalid auth returns `401` before the request body is even read.

**Request body** (JSON):

```json
{ "message": "some branch/commit message" }
```

`message` is required; missing or empty returns `400` without touching the build lock or run history.

**Responses:**

| Status | Body | Meaning |
|---|---|---|
| `202` | `{"status": "accepted", "message": "...", "run_id": "...", "log_url": "/runs/<id>"}` | Build accepted and started on a background thread. |
| `401` | `{"error": "missing or invalid bearer token"}` | Auth header missing/wrong. |
| `400` | `{"error": "missing required 'message' field"}` | No `message` in the JSON body. |
| `409` | `{"error": "a config grab is already running"}` | Another build is already in progress. |

The build itself runs asynchronously after the response is sent — poll `/runs/<run_id>` (from `log_url`) to see progress and the final result.

**Example:**

```bash
curl -X POST http://localhost:8080/ \
  -H "Authorization: Bearer $WEBHOOK_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"message": "nightly sync"}'
```

Or via the bundled client:

```bash
python trigger_webhook.py "nightly sync" --url http://localhost:8080/ --token "$WEBHOOK_TOKEN"
```

## `GET /health`

Unauthenticated liveness check.

```json
{ "status": "ok" }
```

## `GET /runs`

Unauthenticated. Lists recent runs, newest first, capped at the last `MAX_RUNS` (30).

- Default: renders an HTML table (id, message, status, started, duration).
- `?format=json`: returns `{"runs": [ { "id", "message", "status", "started_at", "finished_at", "duration_seconds" }, ... ]}`.

## `GET /runs/<run_id>`

Unauthenticated. Full detail for one run, including its captured log transcript.

- Default: renders an HTML page. Auto-refreshes every 2 seconds while the run's `status` is `"running"`.
- `?format=json`: returns the full run record — `id`, `message`, `status`, `started_at`, `finished_at`, `duration_seconds`, `result`, `lines` (log lines).
- Returns `404` if `run_id` doesn't exist.

## Run lifecycle

A run's `status` moves through:

- `running` — build in progress.
- `success` — `config_grabber.build()` returned normally; `result` holds its summary string (e.g. `"Pushed with message: ..."` or `"No changes found, no push needed."`).
- `error` — `config_grabber.build()` raised; `result` holds `str(exception)`.
- `interrupted` — the run was still `"running"` in the persisted history when the server process restarted (no thread survives a restart to finish it), so it's relabeled rather than shown as perpetually in-progress.

Run history is mirrored to disk (`RUN_HISTORY_PATH`, default `data/run_history.json`) on every run start and finish, so it survives a container restart. It's stored outside the device-config git working copy so it's never picked up by that repo's `git add --all`.

## Slack notifications

If `[SLACK] WEBHOOK_URL` is set in `.config`, the server posts best-effort notifications on build start, success, and failure. Notification failures are logged but never affect the build or the HTTP response — a broken Slack webhook must not be mistaken for (or mask) a real build failure.
