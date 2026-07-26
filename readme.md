# config-grabber

Pulls device configs from NetBox for devices tagged with a configured tag, and pushes them to a branch in a separate device-config git repository. Runs either as a one-off CLI or as an HTTP-triggered service.

No input checking etc. — recommended to run in a venv.

## Setup

```
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

cp .config_example .config   # NetBox URL/tag, git repo URL/path, optional Slack webhook
cp .env_example .env         # NB_TOKEN, GIT_TOKEN, WEBHOOK_TOKEN, GIT_USER_NAME, GIT_USER_EMAIL
```

## Running

```
python main.py <branch-name-message>                        # one-off CLI build
python webhook_server.py                                     # HTTP service (PORT env, default 8080)
python trigger_webhook.py "message" --url http://localhost:8080/ --token <token>   # trigger a running server
```

The webhook's trigger endpoint (`POST /`) requires an `Authorization: Bearer <token>` header matching `WEBHOOK_TOKEN`; `trigger_webhook.py` reads the token from `--token` or the `WEBHOOK_TOKEN` env var. `/health` and the `/runs` history pages are not authenticated.

## Docker

```
docker compose up --build
```

Runs a single gunicorn worker deliberately: builds share one on-disk git working copy, so concurrent workers would race on the same checkout.

## Tests

```
python -m pytest tests/
```

Use `python -m pytest`, not the bare `pytest` binary — only `-m` invocation adds the repo root to `sys.path`, which the tests need to import `config_grabber`/`webhook_server`.

## Linting & git hooks

```
ruff check .
git config core.hooksPath .githooks   # enable: pre-commit lints staged .py files, pre-push runs the tests
```

`core.hooksPath` is local git config, not tracked — run it once per clone.
