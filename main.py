import logging
import sys

import config_grabber

logging.basicConfig(level=logging.INFO)

if len(sys.argv) == 2:
    print(f"Grab configs, and name BRANCH: { sys.argv[1] }")
    config_grabber.build(sys.argv[1])
else:
    print(f"Usage is: main.py <name>")
