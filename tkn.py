# -----------------------
# Reads tokens from environment variables instead of ~/.token_<target> files.
import os
import sys

ENV_VARS = {
    "nb": "NB_TOKEN",
    "git": "GIT_TOKEN",
    "webhook": "WEBHOOK_TOKEN",
}


def get(TARGET):
    """Read token from the environment variable mapped to TARGET."""
    env_var = ENV_VARS.get(TARGET, f"{TARGET.upper()}_TOKEN")
    token = os.environ.get(env_var)
    if not token:
        print(f"{env_var} environment variable is not set.")
        print("EXITING")
        sys.exit(1)
    return token