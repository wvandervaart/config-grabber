#!/usr/bin/env python3
"""CLI client that remotely triggers a running `webhook_server.py` instance
by POSTing to its `/` endpoint."""

import argparse
import os

import requests


def main():
    """Parse CLI args and POST `{"message": <message>}` to `<url>` with a
    bearer-token Authorization header. Exits via `parser.error` (status 2)
    if no token is available from `--token` or the WEBHOOK_TOKEN env var.
    Prints the response status code and JSON body."""
    parser = argparse.ArgumentParser(description="Trigger the config-grabber webhook.")
    parser.add_argument("message", nargs="?", default="manual trigger", help="commit/branch message")
    parser.add_argument("--url", default=os.environ.get("WEBHOOK_URL", "http://localhost:8080/"))
    parser.add_argument("--token", default=os.environ.get("WEBHOOK_TOKEN"), help="bearer token (or set WEBHOOK_TOKEN)")
    args = parser.parse_args()

    if not args.token:
        parser.error("a bearer token is required: pass --token or set WEBHOOK_TOKEN")

    headers = {"Authorization": f"Bearer {args.token}"}
    response = requests.post(args.url, json={"message": args.message}, headers=headers, timeout=10)
    print(response.status_code, response.json())


if __name__ == "__main__":
    main()
