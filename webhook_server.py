import hmac
import json
import logging
import os
import threading
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from html import escape

import requests
from flask import Flask, Response, abort, jsonify, request

import config_grabber
import tkn

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("webhook")

# A single lock (not per-worker-safe) is intentional: the app is meant to run
# with a single gunicorn worker since builds share one git working copy.
_lock = threading.Lock()

# Builds are serialized by _lock, so a single in-memory run history (capped
# so a long-lived process doesn't accumulate logs forever) is safe without
# per-run locking. Mirrored to disk (see RUN_HISTORY_PATH) so it survives a
# container restart. Deliberately outside cfg['GIT']['PATH'] (the device-config
# checkout) so it never gets swept up by that repo's `git add --all`.
MAX_RUNS = 30
RUN_HISTORY_PATH = os.environ.get("RUN_HISTORY_PATH", os.path.join("data", "run_history.json"))
_runs = OrderedDict()
_runs_lock = threading.Lock()


class _RunLogHandler(logging.Handler):
    """Appends formatted log records to a run's transcript as they happen."""

    def __init__(self, run):
        super().__init__()
        self.run = run
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))

    def emit(self, record):
        self.run["lines"].append(self.format(record))


def _new_run(message):
    run = {
        "id": uuid.uuid4().hex[:8],
        "message": message,
        "status": "running",
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "finished_at": None,
        "result": None,
        "lines": [],
    }
    with _runs_lock:
        _runs[run["id"]] = run
        while len(_runs) > MAX_RUNS:
            _runs.popitem(last=False)
    _save_runs()
    return run


def _save_runs():
    """Best-effort mirror of the run history to disk; a write failure (e.g.
    read-only filesystem) must never break a build, only lose persistence."""
    try:
        with _runs_lock:
            data = list(_runs.values())
        dirname = os.path.dirname(RUN_HISTORY_PATH)
        if dirname:
            os.makedirs(dirname, exist_ok=True)
        tmp_path = RUN_HISTORY_PATH + ".tmp"
        with open(tmp_path, "w") as f:
            json.dump(data, f)
        os.replace(tmp_path, RUN_HISTORY_PATH)
    except OSError:
        logger.warning("Failed to persist run history to %s", RUN_HISTORY_PATH, exc_info=True)


def _load_runs():
    """Restore run history saved by a previous process. A run still marked
    "running" belonged to a process that's gone (no thread survives a
    restart to finish it), so it's relabeled "interrupted" rather than left
    to look like a build that's hung forever."""
    if not os.path.exists(RUN_HISTORY_PATH):
        return
    try:
        with open(RUN_HISTORY_PATH) as f:
            data = json.load(f)
    except (OSError, ValueError):
        logger.warning("Failed to load run history from %s", RUN_HISTORY_PATH, exc_info=True)
        return
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for run in data:
        if run.get("status") == "running":
            run["status"] = "interrupted"
            run["finished_at"] = run.get("finished_at") or now
            run.setdefault("lines", []).append("[server restarted while this run was in progress]")
        _runs[run["id"]] = run
    while len(_runs) > MAX_RUNS:
        _runs.popitem(last=False)


def _slack_escape(text):
    # Per Slack's formatting spec: https://api.slack.com/reference/surfaces/formatting#escaping
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _post_to_slack(text):
    """Best-effort notification; Slack being unreachable/unconfigured must
    never block or fail an actual build."""
    try:
        cfg = config_grabber.read_config()
        webhook_url = cfg.get("SLACK", "WEBHOOK_URL", fallback="").strip()
        if not webhook_url:
            return
        requests.post(webhook_url, json={"text": text}, timeout=5)
    except Exception:
        logger.warning("Slack notification failed", exc_info=True)


def _notify_slack_build_started(message, run_id, log_url):
    # Each notify function must never raise: it runs inline with build-status
    # tracking in _run_build, and a bug here must not get mistaken for the
    # build itself failing (or mask a real failure).
    try:
        _post_to_slack(f":gear: Config build started — *{_slack_escape(message)}*\n<{log_url}|View run {run_id}>")
    except Exception:
        logger.warning("Failed to send Slack 'build started' notification", exc_info=True)


def _notify_slack_build_finished(message, run_id, log_url, result):
    try:
        _post_to_slack(
            f":white_check_mark: Config build finished — *{_slack_escape(message)}*\n"
            f"{_slack_escape(result)}\n<{log_url}|View run {run_id}>"
        )
    except Exception:
        logger.warning("Failed to send Slack 'build finished' notification", exc_info=True)


def _notify_slack_build_failed(message, run_id, log_url, error):
    try:
        _post_to_slack(
            f":x: Config build failed — *{_slack_escape(message)}*\n"
            f"{_slack_escape(error)}\n<{log_url}|View run {run_id}>"
        )
    except Exception:
        logger.warning("Failed to send Slack 'build failed' notification", exc_info=True)


def _run_build(run, message, log_url):
    cg_logger = logging.getLogger("config_grabber")
    handler = _RunLogHandler(run)
    cg_logger.addHandler(handler)
    logger.addHandler(handler)
    try:
        _notify_slack_build_started(message, run["id"], log_url)
        result = config_grabber.build(message)
        run["status"] = "success"
        run["result"] = result
        logger.info("Build finished: %s", result)
        _notify_slack_build_finished(message, run["id"], log_url, result)
    except Exception as exc:
        run["status"] = "error"
        run["result"] = str(exc)
        logger.exception("Build failed")
        _notify_slack_build_failed(message, run["id"], log_url, str(exc))
    finally:
        cg_logger.removeHandler(handler)
        logger.removeHandler(handler)
        run["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        _save_runs()
        _lock.release()


def _run_summary(run):
    return {
        "id": run["id"],
        "message": run["message"],
        "status": run["status"],
        "started_at": run["started_at"],
        "finished_at": run["finished_at"],
    }


_STATUS_COLORS = {"running": "#b58900", "success": "#2e7d32", "error": "#c62828", "interrupted": "#757575"}

_load_runs()


def _page(title, body):
    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>{escape(title)}</title>
<style>
body {{ font-family: -apple-system, Helvetica, sans-serif; background:#1e1e1e; color:#ddd; margin:0; padding:1.5rem; }}
h1 {{ font-size:1.1rem; }}
a {{ color:#8ab4f8; }}
.meta {{ color:#999; font-size:0.85rem; margin-bottom:1rem; }}
.status {{ display:inline-block; padding:2px 8px; border-radius:4px; color:#fff; font-size:0.8rem; }}
pre {{ background:#111; padding:1rem; border-radius:6px; overflow-x:auto; white-space:pre-wrap; word-break:break-word; font-size:0.85rem; line-height:1.4; }}
table {{ border-collapse:collapse; width:100%; }}
td, th {{ text-align:left; padding:0.4rem 0.6rem; border-bottom:1px solid #333; font-size:0.9rem; }}
</style>
</head>
<body>
{body}
</body>
</html>"""


def _is_authorized(req):
    """Constant-time check of a bearer token against WEBHOOK_TOKEN. Only the
    trigger endpoint requires this; /runs stays open for browsing history."""
    auth_header = req.headers.get("Authorization", "")
    scheme, _, token = auth_header.partition(" ")
    if scheme != "Bearer" or not token:
        return False
    return hmac.compare_digest(token, tkn.get("webhook"))


@app.get("/health")
def health():
    return jsonify(status="ok")


@app.post("/")
def webhook():
    if not _is_authorized(request):
        return jsonify(error="missing or invalid bearer token"), 401

    payload = request.get_json(silent=True) or {}
    message = payload.get("message")
    if not message:
        return jsonify(error="missing required 'message' field"), 400

    if not _lock.acquire(blocking=False):
        logger.info("a config grab is already running")
        return jsonify(error="a config grab is already running"), 409

    run = _new_run(message)
    log_url = request.url_root.rstrip("/") + f"/runs/{run['id']}"
    threading.Thread(target=_run_build, args=(run, message, log_url), daemon=True).start()
    return jsonify(status="accepted", message=message, run_id=run["id"], log_url=f"/runs/{run['id']}"), 202


@app.get("/runs")
def list_runs():
    with _runs_lock:
        runs = [_run_summary(r) for r in reversed(_runs.values())]

    if request.args.get("format") == "json":
        return jsonify(runs=runs)

    if not runs:
        rows = "<tr><td colspan=4>No runs yet.</td></tr>"
    else:
        rows = "\n".join(
            f'<tr><td><a href="/runs/{r["id"]}">{escape(r["id"])}</a></td>'
            f'<td>{escape(r["message"])}</td>'
            f'<td><span class="status" style="background:{_STATUS_COLORS.get(r["status"], "#555")}">{escape(r["status"])}</span></td>'
            f'<td>{escape(r["started_at"])}</td></tr>'
            for r in runs
        )
    body = f"""<h1>Recent runs</h1>
<table>
<tr><th>ID</th><th>Message</th><th>Status</th><th>Started</th></tr>
{rows}
</table>"""
    return Response(_page("Runs", body), content_type="text/html; charset=utf-8")


@app.get("/runs/<run_id>")
def view_run(run_id):
    with _runs_lock:
        run = _runs.get(run_id)
        if run is None:
            abort(404)
        snapshot = dict(run)

    if request.args.get("format") == "json":
        return jsonify(snapshot)

    refresh = '<meta http-equiv="refresh" content="2">' if snapshot["status"] == "running" else ""
    log_text = escape("\n".join(snapshot["lines"])) or "(no output yet)"
    result_html = f"<p><strong>Result:</strong> {escape(str(snapshot['result']))}</p>" if snapshot["result"] else ""
    body = f"""{refresh}
<h1>Run {escape(snapshot['id'])}
<span class="status" style="background:{_STATUS_COLORS.get(snapshot['status'], '#555')}">{escape(snapshot['status'])}</span></h1>
<div class="meta">message: {escape(snapshot['message'])}<br>
started: {escape(snapshot['started_at'])}<br>
finished: {escape(snapshot['finished_at'] or '-')}</div>
<pre>{log_text}</pre>
{result_html}
<p><a href="/runs">all runs</a></p>"""
    return Response(_page(f"Run {snapshot['id']}", body), content_type="text/html; charset=utf-8")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))