import os
import subprocess
import sys
from importlib.metadata import version
from pathlib import Path

from mycelium.cli import main


def test_version_flag_prints_the_installed_version(capsys):
    assert main(["--version"]) == 0
    assert capsys.readouterr().out == f"mycelium {version('mycelium')}\n"


def test_python_dash_m_runs_the_same_command():
    result = subprocess.run(
        [sys.executable, "-m", "mycelium", "--version"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout == f"mycelium {version('mycelium')}\n"


def test_paths_prints_each_folder(tmp_path, monkeypatch, capsys):
    tmp_path = Path(os.path.realpath(tmp_path))
    for name in ("XDG_STATE_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert main(["paths"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out == [
        f"state {tmp_path}/.local/state/kodhama/mycelium",
        f"data {tmp_path}/.local/share/kodhama/mycelium",
        f"cache {tmp_path}/.cache/kodhama/mycelium",
        f"clusters {tmp_path}/.local/state/kodhama/clusters",
    ]
    assert list(tmp_path.iterdir()) == []
