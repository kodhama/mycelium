"""This machine's boot id, from the source grove's claim records (grove lease.mjs).

macOS gives a per-boot UUID in `kern.bootsessionuuid`; Linux gives one in
`/proc/sys/kernel/random/boot_id`. None when it can't be read: then nothing
binds, as grove acquires nothing without one, since no binding could be told
apart from an earlier boot's.
"""

import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

# By absolute path, as identity runs `ps`: PATH can't swap in another sysctl.
SYSCTL = ("/usr/sbin/sysctl", "-n", "kern.bootsessionuuid")
LINUX_SOURCE = Path("/proc/sys/kernel/random/boot_id")
TIMEOUT_SECONDS = 2.0


def boot_id(
    platform: str = sys.platform,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    linux_source: Path = LINUX_SOURCE,
) -> str | None:
    if platform == "darwin":
        try:
            done = run(
                list(SYSCTL),
                capture_output=True,
                text=True,
                timeout=TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        found = done.stdout.strip() if done.returncode == 0 else ""
        return found or None
    if platform.startswith("linux"):
        try:
            found = linux_source.read_text().strip()
        except OSError:
            return None
        return found or None
    return None
