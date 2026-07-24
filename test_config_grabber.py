"""
pytest suite for config_grabber.py

Run with:
    pytest test_config_grabber.py -v

Dependencies (add to requirements-dev.txt):
    pytest
    pytest-mock
"""

import asyncio
import configparser
from unittest.mock import MagicMock, patch

import git
import pytest

# ---------------------------------------------------------------------------
# The module under test – adjust the import name to match your file.
# ---------------------------------------------------------------------------
import config_grabber as cg


# ===========================================================================
# Fixtures
# ===========================================================================

@pytest.fixture
def cfg():
    """Minimal ConfigParser that satisfies every cfg.get() call in the module."""
    config = configparser.ConfigParser()
    config.read_dict({
        "NETBOX": {"URL": "https://netbox.example.com", "TAGNAME": "backup"},
        "GIT":    {"PATH": "/tmp/repo/", "URLPREFIX": "https://", "URL": "git.example.com/repo.git"},
    })
    return config


@pytest.fixture
def mock_device():
    """A fake NetBox device object."""
    device = MagicMock()
    device.name = "router-01"
    device.render_config.create.return_value = {"content": "set interfaces eth0"}
    return device


@pytest.fixture
def mock_repo():
    """A fake git.Repo object."""
    repo = MagicMock(spec=git.Repo)
    repo.is_dirty.return_value = False
    repo.untracked_files = []
    return repo


@pytest.fixture
def event_loop():
    """
    Provide an explicit event loop for tests that drive async functions
    directly.  asyncio.run() creates its own loop internally, but
    event_loop.run_until_complete() is needed for tests that call
    async functions without going through asyncio.run().
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    yield loop
    loop.close()
    asyncio.set_event_loop(None)


# ===========================================================================
# read_config
# ===========================================================================

class TestReadConfig:
    def test_returns_configparser(self):
        with patch("configparser.ConfigParser.read"):
            result = cg.read_config()
        assert isinstance(result, configparser.ConfigParser)

    def test_reads_dot_config_file(self):
        with patch("configparser.ConfigParser.read") as mock_read:
            cg.read_config()
        mock_read.assert_called_once_with(".config")


# ===========================================================================
# connect
# ===========================================================================

class TestConnect:
    def test_calls_pynetbox_with_url_and_token(self, cfg):
        with patch("config_grabber.pynetbox.api") as mock_api, \
             patch("config_grabber.tkn.get", return_value="mytoken"):
            cg.connect(cfg)
        mock_api.assert_called_once_with("https://netbox.example.com", "mytoken")

    def test_returns_nb_object(self, cfg):
        fake_nb = MagicMock()
        with patch("config_grabber.pynetbox.api", return_value=fake_nb), \
             patch("config_grabber.tkn.get", return_value="tok"):
            result = cg.connect(cfg)
        assert result is fake_nb


# ===========================================================================
# grab_config  (wrapped with @background → returns a Future, not a coroutine)
# ===========================================================================

class TestGrabConfig:
    def test_writes_config_to_file(self, mock_device, tmp_path, event_loop):
        """grab_config is an async def; run it on our event loop."""
        path = str(tmp_path) + "/"
        result = event_loop.run_until_complete(cg.grab_config(mock_device, path))

        written = (tmp_path / "router-01.set").read_text()
        assert written == "set interfaces eth0"
        assert result == "router-01"

    def test_creates_file_with_correct_name(self, mock_device, tmp_path, event_loop):
        path = str(tmp_path) + "/"
        event_loop.run_until_complete(cg.grab_config(mock_device, path))
        assert (tmp_path / "router-01.set").exists()


# ===========================================================================
# get_device_configs
# ===========================================================================

async def _fake_fetch_all(devices, path):
    """Stub for _fetch_all — returns device names without doing any I/O."""
    return [d.name for d in devices]


class TestGetDeviceConfigs:
    def _make_nb(self, devices):
        nb = MagicMock()
        nb.dcim.devices.filter.return_value = devices
        return nb

    def test_filter_by_role(self, cfg):
        nb = self._make_nb([])
        with patch("config_grabber._fetch_all", side_effect=_fake_fetch_all):
            cg.get_device_configs(cfg, nb, "role", "leaf")
        nb.dcim.devices.filter.assert_called_once_with(role="leaf", tag="backup")

    def test_filter_by_device(self, cfg):
        nb = self._make_nb([])
        with patch("config_grabber._fetch_all", side_effect=_fake_fetch_all):
            cg.get_device_configs(cfg, nb, "device", "router-01")
        nb.dcim.devices.filter.assert_called_once_with(name="router-01", tag="backup")

    def test_filter_all(self, cfg):
        nb = self._make_nb([])
        with patch("config_grabber._fetch_all", side_effect=_fake_fetch_all):
            cg.get_device_configs(cfg, nb, "all", "all")
        nb.dcim.devices.filter.assert_called_once_with(tag="backup")

    def test_grab_config_called_for_each_device(self, cfg):
        devices = [MagicMock() for _ in range(3)]
        nb = self._make_nb(devices)
        with patch("config_grabber._fetch_all", side_effect=_fake_fetch_all) as mock_fetch:
            cg.get_device_configs(cfg, nb, "all", "all")
        assert mock_fetch.call_count == 1
        assert len(mock_fetch.call_args[0][0]) == 3


# ===========================================================================
# is_git_repo
# ===========================================================================

class TestIsGitRepo:
    def test_returns_true_for_valid_repo(self, tmp_path):
        with patch("config_grabber.git.Repo") as mock_repo_cls:
            mock_repo_cls.return_value.git_dir = str(tmp_path / ".git")
            assert cg.is_git_repo(str(tmp_path)) is True

    def test_returns_false_for_invalid_repo(self, tmp_path):
        with patch("config_grabber.git.Repo", side_effect=git.exc.InvalidGitRepositoryError):
            assert cg.is_git_repo(str(tmp_path)) is False


# ===========================================================================
# git_clone
# ===========================================================================

class TestGitClone:
    def test_clones_when_not_a_repo(self, cfg, mock_repo):
        with patch("config_grabber.is_git_repo", return_value=False), \
             patch("config_grabber.tkn.get", return_value="gittoken"), \
             patch("config_grabber.git.Repo.clone_from", return_value=mock_repo) as mock_clone:
            result = cg.git_clone(cfg)
        mock_clone.assert_called_once_with(
            "https://gittoken@git.example.com/repo.git",
            "/tmp/repo/",
            branch="main",
        )
        assert result is mock_repo

    def test_pulls_when_already_a_repo(self, cfg, mock_repo):
        with patch("config_grabber.is_git_repo", return_value=True), \
             patch("config_grabber.git.Repo", return_value=mock_repo):
            result = cg.git_clone(cfg)
        mock_repo.remotes.origin.pull.assert_called_once()
        assert result is mock_repo


# ===========================================================================
# git helpers
# ===========================================================================

class TestGitHelpers:
    def test_git_branch_checks_out_new_branch(self, mock_repo):
        cg.git_branch(mock_repo, "feature/x")
        mock_repo.git.checkout.assert_called_once_with("HEAD", b="feature/x")

    def test_git_main_checks_out_main(self, mock_repo):
        cg.git_main(mock_repo)
        mock_repo.git.checkout.assert_called_once_with("main")

    def test_git_add_stages_and_commits(self, mock_repo):
        cg.git_add(mock_repo, "my commit message")
        mock_repo.git.add.assert_called_once_with(all=True)
        mock_repo.index.commit.assert_called_once_with("my commit message")

    def test_git_push_pushes_branch(self, mock_repo):
        cg.git_push(mock_repo, "my-branch")
        mock_repo.git.push.assert_called_once_with("origin", "-u", "my-branch")


# ===========================================================================
# build (integration-style, all external calls mocked)
# ===========================================================================

class TestBuild:
    """Tests for the top-level build() orchestrator."""

    def _patches(self, mock_repo):
        return [
            patch("config_grabber.read_config"),
            patch("config_grabber.connect"),
            patch("config_grabber.git_clone", return_value=mock_repo),
            patch("config_grabber.git_branch"),
            patch("config_grabber.get_device_configs", return_value=[]),
            patch("config_grabber.git_add"),
            patch("config_grabber.git_push"),
            patch("config_grabber.git_main"),
        ]

    def test_returns_pushed_message_when_dirty(self, mock_repo):
        mock_repo.is_dirty.return_value = True
        with patch("config_grabber.read_config"), \
             patch("config_grabber.connect"), \
             patch("config_grabber.git_clone", return_value=mock_repo), \
             patch("config_grabber.git_branch"), \
             patch("config_grabber.get_device_configs", return_value=[]), \
             patch("config_grabber.git_add"), \
             patch("config_grabber.git_push"), \
             patch("config_grabber.git_main"):
            result = cg.build("deploy configs")
        assert result.startswith("Pushed with message:")

    def test_returns_no_changes_message_when_clean(self, mock_repo):
        with patch("config_grabber.read_config"), \
             patch("config_grabber.connect"), \
             patch("config_grabber.git_clone", return_value=mock_repo), \
             patch("config_grabber.git_branch"), \
             patch("config_grabber.get_device_configs", return_value=[]), \
             patch("config_grabber.git_add"), \
             patch("config_grabber.git_push"), \
             patch("config_grabber.git_main"):
            result = cg.build("deploy configs")
        assert result == "No changes found, no push needed."

    def test_push_not_called_when_clean(self, mock_repo):
        with patch("config_grabber.read_config"), \
             patch("config_grabber.connect"), \
             patch("config_grabber.git_clone", return_value=mock_repo), \
             patch("config_grabber.git_branch"), \
             patch("config_grabber.get_device_configs", return_value=[]), \
             patch("config_grabber.git_add"), \
             patch("config_grabber.git_push") as mock_push, \
             patch("config_grabber.git_main"):
            cg.build("deploy configs")
        mock_push.assert_not_called()

    def test_push_called_when_untracked_files(self, mock_repo):
        mock_repo.untracked_files = ["new.set"]
        with patch("config_grabber.read_config"), \
             patch("config_grabber.connect"), \
             patch("config_grabber.git_clone", return_value=mock_repo), \
             patch("config_grabber.git_branch"), \
             patch("config_grabber.get_device_configs", return_value=[]), \
             patch("config_grabber.git_add"), \
             patch("config_grabber.git_push") as mock_push, \
             patch("config_grabber.git_main"):
            cg.build("deploy configs")
        mock_push.assert_called_once()

    def test_git_main_always_called(self, mock_repo):
        """Ensure we always return to main, even when there's nothing to push."""
        with patch("config_grabber.read_config"), \
             patch("config_grabber.connect"), \
             patch("config_grabber.git_clone", return_value=mock_repo), \
             patch("config_grabber.git_branch"), \
             patch("config_grabber.get_device_configs", return_value=[]), \
             patch("config_grabber.git_add"), \
             patch("config_grabber.git_push"), \
             patch("config_grabber.git_main") as mock_gm:
            cg.build("deploy configs")
        mock_gm.assert_called_once_with(mock_repo)

    def test_branch_name_derived_from_message(self, mock_repo):
        with patch("config_grabber.read_config"), \
             patch("config_grabber.connect"), \
             patch("config_grabber.git_clone", return_value=mock_repo), \
             patch("config_grabber.git_branch") as mock_gb, \
             patch("config_grabber.get_device_configs", return_value=[]), \
             patch("config_grabber.git_add"), \
             patch("config_grabber.git_push"), \
             patch("config_grabber.git_main"):
            cg.build("my deploy")
        branch_name_used = mock_gb.call_args[0][1]
        assert branch_name_used.startswith("my_deploy_")

    def test_commit_message_includes_original_message(self, mock_repo):
        mock_repo.is_dirty.return_value = True
        with patch("config_grabber.read_config"), \
             patch("config_grabber.connect"), \
             patch("config_grabber.git_clone", return_value=mock_repo), \
             patch("config_grabber.git_branch"), \
             patch("config_grabber.get_device_configs", return_value=[]), \
             patch("config_grabber.git_add") as mock_ga, \
             patch("config_grabber.git_push"), \
             patch("config_grabber.git_main"):
            cg.build("nightly sync")
        commit_msg = mock_ga.call_args[0][1]
        assert "nightly sync" in commit_msg