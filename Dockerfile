FROM python:3.11-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY config_grabber.py tkn.py webhook_server.py entrypoint.sh ./
RUN chmod +x entrypoint.sh

ENV HOME=/app
ENV PORT=8080
ENV PYTHONUNBUFFERED=1
EXPOSE 8080

ENTRYPOINT ["./entrypoint.sh"]
# Single worker: builds mutate a shared git working copy in ./configs,
# so concurrent workers would race on the same checkout.
CMD ["gunicorn", "-b", "0.0.0.0:8080", "-w", "1", "webhook_server:app"]