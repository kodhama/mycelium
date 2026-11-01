import re
import sys
from pathlib import Path

import pytest

from mycelium.clusters import ClusterRecord, HelperUnavailable, Instance, read

FAKE = Path(__file__).parent / "fake_cluster_helper.py"


def fake(registry: Path) -> tuple[list[str], dict[str, str]]:
    return [sys.executable, str(FAKE), "read", "--json"], {"FAKE_CLUSTER_REGISTRY": str(registry)}


def script(tmp_path: Path, body: str) -> list[str]:
    path = tmp_path / "helper.py"
    path.write_text(body)
    return [sys.executable, str(path)]


def test_the_helpers_json_becomes_one_record_per_registered_cluster(tmp_path):
    home = tmp_path / "home"
    org = home / ".kodhama" / "org"
    (org / "local").mkdir(parents=True)
    (org / "cluster.toml").write_text(
        'schema = 1\n[[agent]]\nname = "role-01"\nrole = "grove:manager"\nmanager = "@maintainer"\n'
    )
    (org / "local" / "instance.toml").write_text('id = "id-1"\nsuffix = "7"\n')
    registry = tmp_path / "registry"
    registry.write_text("home\ngone\n")
    argv, env = fake(registry)

    found, gone = read(argv, env=env)

    assert found == ClusterRecord(
        home=str(home),
        status="ok",
        reason=None,
        revision=found.revision,
        stored_revision=None,
        instance=Instance(id="id-1", suffix="7"),
        declaration={
            "schema": 1,
            "agent": [{"name": "role-01", "role": "grove:manager", "manager": "@maintainer"}],
        },
    )
    assert found.revision.startswith("sha256:")
    assert gone.status == "missing"
    assert gone.declaration is None
    assert gone.reason


def test_a_missing_helper_is_reported_plainly(tmp_path):
    with pytest.raises(HelperUnavailable, match="not installed"):
        read([str(tmp_path / "no-such-helper"), "read", "--json"])


def test_a_failing_helper_is_reported_with_what_it_said(tmp_path):
    body = "import sys\nprint('registry is locked', file=sys.stderr)\nsys.exit(3)\n"
    argv = script(tmp_path, body)
    with pytest.raises(HelperUnavailable, match="exited 3: registry is locked"):
        read(argv)


def test_a_helper_that_hangs_is_cut_off(tmp_path):
    argv = script(tmp_path, "import time\ntime.sleep(10)\n")
    with pytest.raises(HelperUnavailable, match="did not answer"):
        read(argv, timeout=0.5)


@pytest.mark.parametrize(
    "output",
    [
        "not json",
        "[]",
        '{"clusters": {}}',
        '{"clusters": [{"status": "ok"}]}',
        '{"clusters": [{"home": "/h", "status": "lost"}]}',
        '{"clusters": [{"home": "/h", "status": "ok", "revision": "r", "declaration": []}]}',
        '{"clusters": [{"home": "/h", "status": "ok", "revision": "r", "declaration": {},'
        ' "instance": {"id": "x"}}]}',
    ],
)
def test_output_that_is_not_the_shape_is_refused(tmp_path, output):
    argv = script(tmp_path, f"print({output!r})\n")
    with pytest.raises(HelperUnavailable, match="unexpected output"):
        read(argv)


def test_mycelium_itself_never_parses_toml():
    # KTD1, cluster spec §2.4: the helper parses cluster files, mycelium reads its JSON.
    source = Path(__file__).parent.parent / "src" / "mycelium"
    offenders = [
        str(path.relative_to(source))
        for path in source.rglob("*.py")
        if re.search(r"^\s*(import|from)\s+(tomllib|tomli|toml)\b", path.read_text(), re.M)
    ]
    assert offenders == []
