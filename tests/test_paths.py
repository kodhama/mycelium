import os
from pathlib import Path

import pytest

from mycelium.paths import resolve


@pytest.fixture
def home(tmp_path):
    # A real folder, canonical already: the resolver returns canonical paths, and
    # a placeholder such as /home/maintainer canonicalises differently per OS.
    folder = tmp_path / "home"
    folder.mkdir()
    return Path(os.path.realpath(folder))


def test_xdg_state_home_moves_bindings_under_it(home):
    paths = resolve({"XDG_STATE_HOME": str(home / "x")}, home=home)
    assert paths.state == home / "x/kodhama/mycelium"
    assert paths.clusters == home / "x/kodhama/clusters"


def test_each_xdg_variable_moves_only_its_own_folder(home):
    paths = resolve(
        {"XDG_DATA_HOME": str(home / "d"), "XDG_CACHE_HOME": str(home / "c")}, home=home
    )
    assert paths.data == home / "d/kodhama/mycelium"
    assert paths.cache == home / "c/kodhama/mycelium"
    assert paths.state == home / ".local/state/kodhama/mycelium"


def test_no_xdg_falls_back_to_the_dot_local_folders(home):
    paths = resolve({}, home=home)
    assert paths.state == home / ".local/state/kodhama/mycelium"
    assert paths.data == home / ".local/share/kodhama/mycelium"
    assert paths.cache == home / ".cache/kodhama/mycelium"
    assert paths.clusters == home / ".local/state/kodhama/clusters"


@pytest.mark.parametrize("value", ["", "relative/state", "./state"])
def test_an_empty_or_relative_xdg_value_is_ignored(home, value):
    # The XDG Base Directory spec: a relative path is invalid and is ignored.
    paths = resolve({"XDG_STATE_HOME": value}, home=home)
    assert paths.state == home / ".local/state/kodhama/mycelium"


def test_the_store_lives_in_data(home):
    paths = resolve({"XDG_DATA_HOME": str(home / "d")}, home=home)
    assert paths.store == home / "d/kodhama/mycelium/store.sqlite3"


def test_a_symlinked_xdg_folder_resolves_to_its_target(home):
    # On macOS /var is a symlink to /private/var: an unresolved path would
    # compare unequal to the same folder reached the other way.
    target = home / "real-state"
    target.mkdir()
    (home / "linked-state").symlink_to(target)
    paths = resolve({"XDG_STATE_HOME": str(home / "linked-state")}, home=home)
    assert paths.state == target / "kodhama/mycelium"


def test_a_symlinked_home_resolves_to_its_target(home):
    (home / "linked-home").symlink_to(home)
    paths = resolve({}, home=home / "linked-home")
    assert paths.data == home / ".local/share/kodhama/mycelium"


def test_a_symlinked_default_folder_resolves_to_its_target(home):
    # A dotfile manager links ~/.local elsewhere: the default and the same folder
    # named in XDG_DATA_HOME must be one path.
    (home / "dotfiles").mkdir()
    (home / ".local").symlink_to(home / "dotfiles")
    by_default = resolve({}, home=home)
    by_name = resolve({"XDG_DATA_HOME": str(home / ".local/share")}, home=home)
    assert by_default.data == by_name.data == home / "dotfiles/share/kodhama/mycelium"


def test_resolving_creates_nothing(home):
    resolve({"XDG_STATE_HOME": str(home / "s")}, home=home)
    assert list(home.iterdir()) == []


def test_defaults_read_the_process_environment(monkeypatch, home):
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(home / "c"))
    monkeypatch.setenv("HOME", str(home))
    paths = resolve()
    assert paths.cache == home / "c/kodhama/mycelium"
    assert paths.data == home / ".local/share/kodhama/mycelium"
