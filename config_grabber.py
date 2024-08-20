import pynetbox
import tkn
import git
import configparser
import sys, getopt
from datetime import datetime

def read_config():
    config = configparser.ConfigParser()
    config.read('.config')
    return config

def connect(cfg): 
    url = cfg.get('NETBOX','URL')

    nb = pynetbox.api(url, tkn.get('nb'))
    return nb

def get_device_configs(cfg, nb, t, f):
    path = cfg.get('GIT','PATH')
    if t == "role":
        devices = nb.dcim.devices.filter(role=f, tag=cfg.get('NETBOX','TAGNAME'))
    elif t == "device":
        devices = nb.dcim.devices.filter(name=f, tag=cfg.get('NETBOX','TAGNAME'))
    elif t == "all":
        devices = nb.dcim.devices.filter(tag=cfg.get('NETBOX','TAGNAME'))
    for device in devices:
        print(device.name)
        config = device.render_config.create()
        f = open(path+device.name+".set", "w")
        f.write(config['content'])
        f.close()

def is_git_repo(path):
    try:
        _ = git.Repo(path).git_dir
        return True
    except git.exc.InvalidGitRepositoryError:
        return False

def git_clone(cfg):
    path = cfg.get('GIT','PATH')
    urlprefix = cfg.get('GIT','URLPREFIX')
    url = cfg.get('GIT','URL')

    if is_git_repo(path) == False:
        repo = git.Repo.clone_from(urlprefix + tkn.get('git') + "@" + url, path, branch="main")
    else:
        repo = git.Repo(path)
        repo.remotes.origin.pull()
    return repo

def git_add(repo, msg):
    repo.git.add(all=True)
    repo.index.commit(msg)

def git_push(repo):
    repo.git.push()

def parse_opts(argv):
    try:
        opts, args = getopt.getopt(argv,"hm:",["message="])
    except getopt.GetoptError:
        print ('config_grabber.py -m <message>')
        sys.exit(2)
    for opt, arg in opts:
        if opt == '-h':
            print ('config_grabber.py -m <message>')
            sys.exit()
        elif opt in ("-m", "--message"):
            m = arg

    return m

def main(argv):
    t = "all"
    f = "all"
    m = parse_opts(argv)
    now = datetime.now()
    dt_string = now.strftime("%Y%m%d %H:%M:%S")
    m = m + " " + dt_string
    cfg = read_config()
    nb = connect(cfg)
    repo = git_clone(cfg)
    get_device_configs(cfg, nb, t, f)
    git_add(repo, m)
    git_push(repo)

if __name__ == "__main__":
    main(sys.argv[1:])