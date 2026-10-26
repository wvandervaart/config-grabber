FROM python:3.11-alpine

# Alpine base: far fewer (and faster-patched) OS-level CVEs than Debian slim.
# `apk upgrade` pulls in any security fixes published since the base image was built.
RUN apk upgrade --no-cache \
    && apk add --no-cache git ca-certificates

WORKDIR /app

COPY requirements.txt .
# pip/setuptools/wheel are only needed to install deps, and they vendor their own
# (often outdated) copies of urllib3, msgpack, etc. that scanners flag. Remove them,
# along with ensurepip's bundled wheels, so the runtime image ships only what the app uses.
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt \
    && pip uninstall -y setuptools wheel \
    && python -m pip uninstall -y pip \
    && rm -rf /usr/local/lib/python3.11/ensurepip/_bundled

COPY config_grabber.py tkn.py webhook_server.py entrypoint.sh ./
RUN chmod +x entrypoint.sh

ENV HOME=/app
ENV PORT=8080
ENV PYTHONUNBUFFERED=1
EXPOSE 8080

ENTRYPOINT ["./entrypoint.sh"]
# Single worker: builds mutate a shared git working copy in ./configs,
# so concurrent workers would race on the same checkout.
# --timeout raised well above gunicorn's 30s default: the webhook request
# itself returns immediately, but the background build thread's GIL
# contention (many concurrent device fetches) can starve the worker's
# heartbeat long enough for the default timeout to kill it mid-build.
CMD ["gunicorn", "-b", "0.0.0.0:8080", "-w", "1", "--timeout", "300", "webhook_server:app"]
