import logging
import os
import threading

from flask import Flask, jsonify, request

import config_grabber

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("webhook")

# A single lock (not per-worker-safe) is intentional: the app is meant to run
# with a single gunicorn worker since builds share one git working copy.
_lock = threading.Lock()


def _run_build(message):
    try:
        result = config_grabber.build(message)
        logger.info("Build finished: %s", result)
    except Exception:
        logger.exception("Build failed")
    finally:
        _lock.release()


@app.get("/health")
def health():
    return jsonify(status="ok")


@app.get("/")
def webhook():
    message = request.args.get("message", "webhook")

    if not _lock.acquire(blocking=False):
        return jsonify(error="a config grab is already running"), 409

    threading.Thread(target=_run_build, args=(message,), daemon=True).start()
    return jsonify(status="accepted", message=message), 202


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
