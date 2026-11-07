import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from mycelium import clusters, paths

FIXTURES = Path(__file__).parent / "fixtures" / "clusters"
FAKE = Path(__file__).parent / "fake_cluster_helper.py"


class Tree:
    """A copy of one fixture case, with the fake helper and XDG folders of its own."""

    def __init__(self, root: Path, xdg: Path) -> None:
        self.root = root
        self.paths = paths.resolve(
            {
                "XDG_STATE_HOME": str(xdg / "state"),
                "XDG_DATA_HOME": str(xdg / "data"),
                "XDG_CACHE_HOME": str(xdg / "cache"),
            },
            home=xdg,
        )

    def read(self) -> list[clusters.ClusterRecord]:
        argv = [sys.executable, str(FAKE), "read", "--json"]
        return clusters.read(argv, env={"FAKE_CLUSTER_REGISTRY": str(self.root / "registry")})

    def home(self, relative: str) -> str:
        return os.path.realpath(self.root / relative)

    def relative(self, home: str) -> str:
        return os.path.relpath(home, os.path.realpath(self.root))

    def toml(self, relative: str) -> Path:
        return self.root / relative / ".kodhama" / "org" / "cluster.toml"

    def edit(self, relative: str, old: str, new: str, seal: bool = True) -> None:
        """Change one cluster file; sealed, as the helper would write it, by default."""
        path = self.toml(relative)
        text = path.read_text()
        assert old in text, f"{old!r} not in {path}"
        path.write_text(text.replace(old, new, 1))
        if seal:
            subprocess.run([sys.executable, str(FAKE), "seal", str(path)], check=True)


@pytest.fixture
def tree(tmp_path):
    """`tree(case)` copies tests/fixtures/clusters/<case> into a scratch folder."""

    def make(case: str) -> Tree:
        root = tmp_path / "tree"
        shutil.copytree(FIXTURES / case, root, symlinks=True)
        xdg = tmp_path / "xdg"
        xdg.mkdir()
        return Tree(Path(os.path.realpath(root)), Path(os.path.realpath(xdg)))

    return make
