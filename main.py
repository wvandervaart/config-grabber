"""One-off CLI entry point: `python main.py <name>` runs a single
`config_grabber.build()` pass, using `<name>` as the branch/commit message."""

import logging
import sys

import config_grabber

logging.basicConfig(level=logging.INFO)

if len(sys.argv) == 2:
    print(f"Grab configs, and name BRANCH: { sys.argv[1] }")
    config_grabber.build(sys.argv[1])
else:
    print("Usage is: main.py <name>")
