import logging
import os
import re
import pynetbox
import tkn
import git
import configparser
import requests
from datetime import datetime
import asyncio
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)

# Matches ThreadPoolExecutor's default max_workers (min(32, cpu_count+4)) so
# concurrent device fetches don't exceed the session's connection pool.
POOL_MAXSIZE = 32

# pynetbox sets no request timeout of its own, so a hung/slow NetBox server
# would otherwise block a fetch thread (and the webhook's build lock) forever.
REQUEST_TIMEOUT = 60

class TimeoutHTTPAdapter(requests.adapters.HTTPAdapter):
    def __init__(self, *args, timeout=REQUEST_TIMEOUT, **kwargs):
        self.timeout = timeout
        super().__init__(*args, **kwargs)

    def send(self, request, **kwargs):
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = self.timeout
        return super().send(request, **kwargs)

def read_config():
    if not os.path.exists('.config'):
        raise FileNotFoundError(
            "Config file '.config' not found. See .config_example for the required format."
        )
    config = configparser.ConfigParser()
    config.read('.config')
    return config

def connect(cfg):
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
    with ThreadPoolExecutor() as executor:
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
    path = cfg.get('GIT', 'PATH') + 'configs/'
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
    try:
        _ = git.Repo(path).git_dir
        return True
    except (git.exc.InvalidGitRepositoryError, git.exc.NoSuchPathError):
        return False

def git_clone(cfg):
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
    repo.git.checkout("HEAD", b=name)

def git_main(repo):
    repo.git.checkout("main")

def git_add(repo, msg):
    repo.git.add(all=True)
    repo.index.commit(msg)

def git_push(repo, branch_name):
    repo.git.push('origin', '-u', branch_name)

def sanitize_branch_component(value):
    """Make an arbitrary string safe to use as part of a git ref name."""
    value = re.sub(r'[^A-Za-z0-9._-]+', '_', value)
    value = re.sub(r'\.{2,}', '.', value)
    value = value.strip('-.')
    return value or "webhook"

def build(message):
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