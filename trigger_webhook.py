#!/usr/bin/env python3
import argparse
import os
import sys

import requests


def main():
    parser = argparse.ArgumentParser(description="Trigger the config-grabber webhook.")
    parser.add_argument("message", nargs="?", default="manual trigger", help="commit/branch message")
    parser.add_argument("--url", default=os.environ.get("WEBHOOK_URL", "http://localhost:8080/webhook"))
    parser.add_argument("--secret", default=os.environ.get("WEBHOOK_SECRET"))
    args = parser.parse_args()

    if not args.secret:
        sys.exit("No secret provided. Set WEBHOOK_SECRET or pass --secret.")

    response = requests.post(
        args.url,
        headers={"X-Webhook-Secret": args.secret},
        json={"message": args.message},
        timeout=10,
    )
    print(response.status_code, response.json())


if __name__ == "__main__":
    main()
