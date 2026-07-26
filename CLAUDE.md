# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Pulls device configs from NetBox (via pynetbox) for devices tagged with a given tag, writes them to a local git working copy, and pushes them to a branch in a separate device-config git repo. Runnable either as a one-off CLI (`main.py`) or as an HTTP-triggered service (`webhook_server.py`).

## Commands

Setup (venv is expected — see `readme.md`):
```
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
pip install pytest ruff   # dev-only, not in requirements.txt
```

Run tests — must use `python -m pytest`, not the bare `pytest` binary: only `-m` invocation adds the repo root to `sys.path`, which `config_grabber`/`webhook_server` imports in `tests/` depend on (no root `conftest.py`/`pyproject.toml` sets this up).
```
python -m pytest tests/
python -m pytest tests/test_webhook_server.py::TestWebhookMessageValidation::test_missing_message_returns_400   # single test
```

Lint (rules and per-file ignores live in `ruff.toml`):
```
ruff check .
```

Enable git hooks (opt-in per clone — `core.hooksPath` is local git config, not tracked):
```
git config core.hooksPath .githooks
```
`.githooks/pre-commit` runs ruff on staged `.py` files; `.githooks/pre-push` runs the full test suite (also via `python -m pytest`, for the same sys.path reason).

Run the webhook server locally (reads `PORT` env, defaults 8080):
```
python webhook_server.py
```

Run a one-off build from the CLI:
```
python main.py <branch-name-message>
```

Trigger a running webhook server (token via `--token` or `WEBHOOK_TOKEN` env):
```
python trigger_webhook.py "commit message" --url http://localhost:8080/ --token <token>
```

Docker (single gunicorn worker — see below):
```
docker compose up --build
```

## Architecture

**`config_grabber.build(message)`** is the single entry point for a build; both `main.py` and `webhook_server.py` call it. Pipeline:
1. `read_config()` loads `.config` (gitignored INI, see `.config_example`) for NetBox URL/tag, git repo URL/path, and optional Slack webhook.
2. `connect()` opens a pynetbox session (token from `tkn.get('nb')`), with a `TimeoutHTTPAdapter` since pynetbox sets no request timeout of its own.
3. `git_clone()` clones or pulls the device-config repo into `cfg['GIT']['PATH']` (default `./configs/`) — a **separate git repository** from this one, gitignored here. If already cloned, it force-checks-out `main` before pulling, so a prior run killed mid-build (no chance to clean up) can't leave the working copy stuck on a stray branch.
4. `git_branch()` checks out a new branch named from a sanitized version of `message` plus a timestamp (`sanitize_branch_component`).
5. `get_device_configs()` fetches all tagged devices from NetBox and writes each one's rendered config to a `.set` file, concurrently via `asyncio` + `ThreadPoolExecutor` (`grab_config`/`_fetch_all`). When called with the "all" filter, it also prunes `.set` files for devices no longer in the tagged inventory (`_prune_stale_configs`).
6. If the working copy is dirty, commits and pushes the new branch; otherwise reports no changes. `git_main()` always runs in a `finally` to leave the working copy back on `main` regardless of outcome.

**`webhook_server.py`** wraps `build()` for HTTP triggering:
- `POST /` with a JSON body `{"message": "..."}` — requires `Authorization: Bearer <token>` matching `WEBHOOK_TOKEN` (checked via `_is_authorized`, `hmac.compare_digest` against `tkn.get('webhook')`); missing/invalid auth returns 401 before the message is even read. `message` is required (missing/empty returns 400 without touching the build lock or run history). Runs `build()` in a background thread; a single `threading.Lock` serializes builds (a second concurrent request gets 409) because builds share one on-disk git working copy — this is also why the Docker image runs a single gunicorn worker.
- `/health`, `/runs`, and `/runs/<id>` are deliberately left open (no auth) — they're read-only status views, not the build trigger.
- Run history (`_runs`, capped at `MAX_RUNS=30`) captures each run's log lines via a custom `logging.Handler` attached for the run's duration; browsable at `/runs` and `/runs/<id>` (HTML, or `?format=json`). Mirrored to disk at `RUN_HISTORY_PATH` (env, default `data/run_history.json`) on run start and finish (`_save_runs`/`_load_runs`) so history survives a container restart — deliberately outside `cfg['GIT']['PATH']` so it's never swept up by that repo's `git add --all`. A run still `"running"` when the file is loaded belonged to a process that no longer exists, so it's relabeled `"interrupted"` rather than shown as perpetually in-progress. `docker-compose.yml` mounts a dedicated `run-history-data` volume at `/app/data` for this.
- Slack notifications (build started/finished/failed) are best-effort: `_post_to_slack` and each `_notify_slack_*` wrapper swallow all exceptions, since a notification bug must never be mistaken for — or mask — an actual build failure. Only sent if `[SLACK] WEBHOOK_URL` is set in `.config`.

**Secrets vs. config**: `.config` (INI, gitignored) holds non-secret settings (NetBox URL, git URL/path, Slack webhook). Actual credentials (`NB_TOKEN`, `GIT_TOKEN`, `WEBHOOK_TOKEN`) and git identity (`GIT_USER_NAME`/`GIT_USER_EMAIL`) come from environment variables — see `.env_example` — read via `tkn.get()`, which exits(1) if a required token is unset. `entrypoint.sh` sets the global git identity from those env vars and installs any mounted CA certs before handing off to gunicorn.

## Tests

`tests/test_config_grabber.py` mocks `git.Repo` and the pynetbox client heavily rather than hitting real services. `tests/test_webhook_server.py` uses Flask's test client and an `ImmediateThread` stand-in (patched over `threading.Thread`) to run the background build synchronously within a test; an autouse fixture releases `webhook_server._lock` before/after each test in case a prior test left it held. Another autouse fixture stubs `tkn.get` to return a fixed `WEBHOOK_TOKEN` (`"test-token"`) so tests can hit `POST /` with a real `Authorization: Bearer` header instead of touching the environment.
