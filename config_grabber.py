"""Core build pipeline: fetch tagged device configs from NetBox and push them
to a branch in a separate device-config git repo. `build()` is the single
entry point used by both `main.py` (one-off CLI) and `webhook_server.py`
(HTTP-triggered service)."""

import asyncio
import configparser
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import git
import pynetbox
import requests

import tkn

logger = logging.getLogger(__name__)

# Device fetches are I/O-bound (waiting on NetBox HTTP responses), so the
# thread pool is sized explicitly rather than left at ThreadPoolExecutor's
# default of min(32, cpu_count+4) — on a small container (e.g. 2-4 CPUs)
# that default would cap concurrency at 6-8 regardless of how many devices
# there are to fetch.
POOL_MAXSIZE = 32

# Deliberately lower than POOL_MAXSIZE: config rendering is done server-side
# by NetBox, and testing against a real instance showed pushing concurrency
# up to POOL_MAXSIZE didn't reliably help (server load dominated) and caused
# an occasional request failure. 14 held up without failures.
FETCH_MAX_WORKERS = 14

# pynetbox sets no request timeout of its own, so a hung/slow NetBox server
# would otherwise block a fetch thread (and the webhook's build lock) forever.
REQUEST_TIMEOUT = 60

class TimeoutHTTPAdapter(requests.adapters.HTTPAdapter):
    """HTTPAdapter that applies a default request timeout when the caller
    (pynetbox) doesn't set one of its own."""

    def __init__(self, *args, timeout=REQUEST_TIMEOUT, **kwargs):
        self.timeout = timeout
        super().__init__(*args, **kwargs)

    def send(self, request, **kwargs):
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = self.timeout
        return super().send(request, **kwargs)

def read_config():
    """Load `.config` (INI, see `.config_example`) from the current working
    directory. Raises FileNotFoundError if it's missing."""
    if not os.path.exists('.config'):
        raise FileNotFoundError(
            "Config file '.config' not found. See .config_example for the required format."
        )
    config = configparser.ConfigParser()
    config.read('.config')
    return config

def connect(cfg):
    """Open a pynetbox API session for `cfg['NETBOX']['URL']`, authenticated
    with the NB_TOKEN env var (via `tkn.get('nb')`) and using
    `TimeoutHTTPAdapter` for both http:// and https://."""
    url = cfg.get('NETBOX', 'URL')
    nb = pynetbox.api(url, tkn.get('nb'))
    adapter = TimeoutHTTPAdapter(pool_maxsize=POOL_MAXSIZE)
    nb.http_session.mount('https://', adapter)
    nb.http_session.mount('http://', adapter)
    return nb

async def grab_config(device, path, executor=None):
    """Run the blocking file-write in a thread, driven by a caller-supplied executor."""
    loop = asyncio.get_running_loop()

    def _write():
        config = device.render_config.create()
        filename = os.path.basename(device.name) + ".set"
        with open(os.path.join(path, filename), "w") as f:
            f.write(config['content'])
        return device.name

    return await loop.run_in_executor(executor, _write)

async def _fetch_all(devices, path):
    """Fetch and write configs for all `devices` concurrently, bounded by
    FETCH_MAX_WORKERS. Returns a list of `grab_config` results/exceptions,
    positionally aligned with `devices`."""
    with ThreadPoolExecutor(max_workers=FETCH_MAX_WORKERS) as executor:
        tasks = [grab_config(device, path, executor) for device in devices]
        results = await asyncio.gather(*tasks, return_exceptions=True)
    return results

def _prune_stale_configs(path, devices):
    """Remove .set files for devices no longer in the current tagged inventory."""
    try:
        existing = os.listdir(path)
    except FileNotFoundError:
        return
    current_names = {os.path.basename(str(d.name)) for d in devices}
    for entry in existing:
        if not entry.endswith(".set"):
            continue
        if entry[: -len(".set")] not in current_names:
            os.remove(os.path.join(path, entry))
            logger.info("Removed stale config: %s", entry)

def get_device_configs(cfg, nb, t, f):
    """Fetch and write configs for NetBox devices tagged with
    `cfg['NETBOX']['TAGNAME']`, filtered by `t` ("role", "device", or "all")
    with `f` as the filter value (role name, device name, or ignored for
    "all"). When `t` is "all", also prunes `.set` files for devices no
    longer in the tagged inventory. Returns a list of device names whose
    fetch failed."""
    path = os.path.join(cfg.get('GIT', 'PATH'), 'configs')
    if t == "role":
        devices = nb.dcim.devices.filter(role=f, tag=cfg.get('NETBOX', 'TAGNAME'))
    elif t == "device":
        devices = nb.dcim.devices.filter(name=f, tag=cfg.get('NETBOX', 'TAGNAME'))
    elif t == "all":
        devices = nb.dcim.devices.filter(tag=cfg.get('NETBOX', 'TAGNAME'))
    else:
        raise ValueError(f"Unknown device filter type: {t!r}")
    devices = list(devices)
    results = asyncio.run(_fetch_all(devices, path))
    failures = []
    for device, result in zip(devices, results):
        if isinstance(result, Exception):
            logger.error("Failed to grab config for %s: %s", device.name, result)
            failures.append(device.name)
        else:
            logger.info("Grabbed config: %s", result)
    if t == "all":
        _prune_stale_configs(path, devices)
    return failures

def is_git_repo(path):
    """Return True if `path` is the root of an existing git working copy."""
    try:
        _ = git.Repo(path).git_dir
        return True
    except (git.exc.InvalidGitRepositoryError, git.exc.NoSuchPathError):
        return False

def git_clone(cfg):
    """Clone the device-config repo (`cfg['GIT']['URL']`, authenticated with
    GIT_TOKEN via `tkn.get('git')`) into `cfg['GIT']['PATH']` if not already
    present there, otherwise reset the existing working copy to `main` and
    pull. Returns the `git.Repo`."""
    path = cfg.get('GIT', 'PATH')
    urlprefix = cfg.get('GIT', 'URLPREFIX')
    url = cfg.get('GIT', 'URL')

    if not is_git_repo(path):
        repo = git.Repo.clone_from(urlprefix + tkn.get('git') + "@" + url, path, branch="main")
    else:
        repo = git.Repo(path)
        # A prior run that got killed outright (e.g. SIGKILL) has no chance to
        # run its own cleanup, so the working copy can be left checked out on
        # a stray build branch. Force back onto main before pulling so the
        # next build always starts from a clean, known state.
        repo.git.checkout("main", force=True)
        repo.remotes.origin.pull()
    return repo

def git_branch(repo, name):
    """Create and check out a new branch `name` from the current HEAD."""
    repo.git.checkout("HEAD", b=name)

def git_main(repo):
    """Check out `main`."""
    repo.git.checkout("main")

def git_add(repo, msg):
    """Stage all changes (including untracked files) and commit with `msg`."""
    repo.git.add(all=True)
    repo.index.commit(msg)

def git_push(repo, branch_name):
    """Push `branch_name` to origin, setting it as the upstream tracking branch."""
    repo.git.push('origin', '-u', branch_name)

def sanitize_branch_component(value):
    """Make an arbitrary string safe to use as part of a git ref name."""
    value = re.sub(r'[^A-Za-z0-9._-]+', '_', value)
    value = re.sub(r'\.{2,}', '.', value)
    value = value.strip('-.')
    return value or "webhook"

def build(message):
    """Run one full build: fetch all NetBox-tagged device configs into a new
    branch of the device-config repo, and push it if anything changed.

    `message` becomes both the branch name (sanitized, with a timestamp
    suffix so concurrent builds can't collide) and the commit message
    (with a timestamp appended). The working copy is always left back on
    `main` before returning, even if a step above raises.

    Returns a human-readable summary string: "Pushed with message: ..." or
    "No changes found, no push needed.", with a " (N device(s) failed: ...)"
    suffix appended if any device config fetch failed.
    """
    t = "all"
    f = "all"
    m = message
    now = datetime.now()
    dt_string = now.strftime("%Y%m%d %H:%M:%S")
    branch_name = sanitize_branch_component(m) + "_" + now.strftime("%Y%m%d%H%M%S")
    m = m + " " + dt_string
    cfg = read_config()
    nb = connect(cfg)
    try:
        repo = git_clone(cfg)
        try:
            git_branch(repo, branch_name)
            failures = get_device_configs(cfg, nb, t, f)
            if repo.is_dirty() or repo.untracked_files:
                git_add(repo, m)
                logger.info("Pushing config with message: %s", m)
                git_push(repo, branch_name)
                returnmsg = f"Pushed with message: {m}"
            else:
                logger.info("No changes found, no push needed.")
                returnmsg = "No changes found, no push needed."
        finally:
            git_main(repo)
    finally:
        nb.http_session.close()
    if failures:
        returnmsg += f" ({len(failures)} device(s) failed: {', '.join(failures)})"
    return returnmsg