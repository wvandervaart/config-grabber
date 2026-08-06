# CLI entry points

Both CLIs below are thin wrappers around `config_grabber.build(message)`, the single build pipeline shared with `webhook_server.py`. See `CLAUDE.md` for the full pipeline architecture.

## `main.py` — one-off build

```
python main.py <name>
```

Runs a single build synchronously in the foreground, using `<name>` as the branch/commit message. No flags — exactly one positional argument is required; anything else prints a usage message and exits without running a build.

## `trigger_webhook.py` — remote trigger client

```
python trigger_webhook.py [message] [--url URL] [--token TOKEN]
```

POSTs `{"message": message}` to a running `webhook_server.py` instance's `/` endpoint (see `docs/webhook-api.md` for the full API) and prints the response status code and JSON body.

| Argument | Default | Notes |
|---|---|---|
| `message` (positional, optional) | `"manual trigger"` | Becomes the branch/commit message. |
| `--url` | `$WEBHOOK_URL` env var, else `http://localhost:8080/` | Target webhook server. |
| `--token` | `$WEBHOOK_TOKEN` env var | Bearer token. If neither `--token` nor `$WEBHOOK_TOKEN` is set, the script exits with an argparse error (exit code 2) before making a request. |

**Example:**

```bash
python trigger_webhook.py "deploy fix" --url https://config-grabber.internal/ --token "$WEBHOOK_TOKEN"
```

## `config_grabber.build(message)` — the underlying pipeline

Both CLIs above (and `webhook_server.py`'s `POST /`) ultimately call this one function. It:

1. Sanitizes `message` into a git-ref-safe branch name and appends a timestamp, so concurrent builds can't collide on branch names.
2. Clones (or pulls, resetting to `main` first) the device-config repo.
3. Checks out the new branch and fetches every NetBox device tagged with the configured tag, writing each device's rendered config to a `.set` file — and prunes `.set` files for devices no longer in the tagged inventory.
4. Commits and pushes the branch if anything changed; otherwise makes no commit.
5. Always leaves the working copy back on `main`, even if a step above raised.

Returns a summary string:

- `"Pushed with message: <message> <timestamp>"` if changes were pushed.
- `"No changes found, no push needed."` if nothing changed.
- Either of the above with a `" (N device(s) failed: dev1, dev2, ...)"` suffix if any individual device's config fetch failed (a partial failure doesn't abort the whole build).
