import logging
import os
import threading
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from html import escape

from flask import Flask, Response, abort, jsonify, request

import config_grabber

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("webhook")

# A single lock (not per-worker-safe) is intentional: the app is meant to run
# with a single gunicorn worker since builds share one git working copy.
_lock = threading.Lock()

# Builds are serialized by _lock, so a single in-memory run history (capped
# so a long-lived process doesn't accumulate logs forever) is safe without
# per-run locking.
MAX_RUNS = 20
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
    return run


def _run_build(run, message):
    cg_logger = logging.getLogger("config_grabber")
    handler = _RunLogHandler(run)
    cg_logger.addHandler(handler)
    logger.addHandler(handler)
    try:
        result = config_grabber.build(message)
        run["status"] = "success"
        run["result"] = result
        logger.info("Build finished: %s", result)
    except Exception as exc:
        run["status"] = "error"
        run["result"] = str(exc)
        logger.exception("Build failed")
    finally:
        cg_logger.removeHandler(handler)
        logger.removeHandler(handler)
        run["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        _lock.release()


def _run_summary(run):
    return {
        "id": run["id"],
        "message": run["message"],
        "status": run["status"],
        "started_at": run["started_at"],
        "finished_at": run["finished_at"],
    }


_STATUS_COLORS = {"running": "#b58900", "success": "#2e7d32", "error": "#c62828"}


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


@app.get("/health")
def health():
    return jsonify(status="ok")


@app.get("/")
def webhook():
    message = request.args.get("message", "webhook")

    if not _lock.acquire(blocking=False):
        logger.info("a config grab is already running")
        return jsonify(error="a config grab is already running"), 409

    run = _new_run(message)
    threading.Thread(target=_run_build, args=(run, message), daemon=True).start()
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