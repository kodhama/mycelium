"""The boot id: the same source grove's claim records (grove lease.mjs `readBootId`)."""

import subprocess

from mycelium import boot

BOOT = "00000000-0000-0000-0000-0000000000b1"


def answering(stdout: str, returncode: int = 0):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr="")

    run.calls = calls
    return run


def test_macos_reads_the_boot_session_uuid():
    run = answering(BOOT + "\n")
    assert boot.boot_id(platform="darwin", run=run) == BOOT
    assert run.calls == [["/usr/sbin/sysctl", "-n", "kern.bootsessionuuid"]]


def test_macos_with_sysctl_failing_has_no_boot_id():
    assert boot.boot_id(platform="darwin", run=answering("", returncode=1)) is None
    assert boot.boot_id(platform="darwin", run=answering("  \n")) is None


def test_macos_with_sysctl_missing_or_slow_has_no_boot_id():
    def missing(argv, **kwargs):
        raise FileNotFoundError(argv[0])

    def slow(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    assert boot.boot_id(platform="darwin", run=missing) is None
    assert boot.boot_id(platform="darwin", run=slow) is None


def test_linux_reads_the_kernels_boot_id(tmp_path):
    source = tmp_path / "boot_id"
    source.write_text(BOOT + "\n")
    assert boot.boot_id(platform="linux", linux_source=source) == BOOT


def test_linux_without_the_file_has_no_boot_id(tmp_path):
    assert boot.boot_id(platform="linux", linux_source=tmp_path / "absent") is None


def test_another_platform_has_no_boot_id():
    assert boot.boot_id(platform="win32") is None
