#!/usr/bin/env python3
import argparse
import os

import requests


def main():
    parser = argparse.ArgumentParser(description="Trigger the config-grabber webhook.")
    parser.add_argument("message", nargs="?", default="manual trigger", help="commit/branch message")
    parser.add_argument("--url", default=os.environ.get("WEBHOOK_URL", "http://localhost:8080/"))
    args = parser.parse_args()

    response = requests.get(args.url, params={"message": args.message}, timeout=10)
    print(response.status_code, response.json())


if __name__ == "__main__":
    main()
