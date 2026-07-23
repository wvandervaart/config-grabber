# -----------------------
# Reads tokens from environment variables instead of ~/.token_<target> files.
import os
import sys

ENV_VARS = {
    "nb": "NB_TOKEN",
    "git": "GIT_TOKEN",
}


def get(TARGET):
    """Read token from the environment variable mapped to TARGET."""
    env_var = ENV_VARS.get(TARGET, "{}_TOKEN".format(TARGET.upper()))
    token = os.environ.get(env_var)
    if not token:
        print("{} environment variable is not set.".format(env_var))
        print("EXITING")
        sys.exit(1)
    return token