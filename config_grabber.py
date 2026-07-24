import logging
import os
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
REQUEST_TIMEOUT = 30

class TimeoutHTTPAdapter(requests.adapters.HTTPAdapter):
    def __init__(self, *args, timeout=REQUEST_TIMEOUT, **kwargs):
        self.timeout = timeout
        super().__init__(*args, **kwargs)

    def send(self, request, **kwargs):
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = self.timeout
        return super().send(request, **kwargs)

def read_config():
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
    results = asyncio.run(_fetch_all(list(devices), path))
    for result in results:
        if isinstance(result, Exception):
            logger.error("Failed to grab config: %s", result)
        else:
            logger.info("Grabbed config: %s", result)

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

def build(message):
    t = "all"
    f = "all"
    m = message
    now = datetime.now()
    dt_string = now.strftime("%Y%m%d %H:%M:%S")
    branch_name = m.replace(" ", "_") + "_" + now.strftime("%Y%m%d%H%M%S")
    m = m + " " + dt_string
    cfg = read_config()
    nb = connect(cfg)
    repo = git_clone(cfg)
    try:
        git_branch(repo, branch_name)
        get_device_configs(cfg, nb, t, f)
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
    return returnmsg