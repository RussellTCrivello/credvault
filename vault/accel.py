"""
GPU detection for the Settings page.

The Settings page offers a CPU or GPU compute choice. This module reports
whether an NVIDIA GPU driver is installed and responds. No import or search
step runs on the GPU yet, so the choice does not change any work. Moving the
record fingerprint hash to a GPU would need PostgreSQL to send its ids out and
receive the hashes back, which costs more than the hash saves. The import is
also bound by PostgreSQL writes rather than by parsing or hashing. See
README.md, "Compute".
"""

import functools
import shutil
import subprocess
from dataclasses import dataclass

PROBE_TIMEOUT_SECONDS = 5


@dataclass(frozen=True)
class GpuStatus:
    available: bool
    name: str = ''


@functools.lru_cache(maxsize=1)
def detect_gpu():
    """Return the first NVIDIA GPU that ``nvidia-smi`` reports, if any."""
    executable = shutil.which('nvidia-smi')
    if executable is None:
        return GpuStatus(False)
    try:
        result = subprocess.run(
            [executable, '--query-gpu=name', '--format=csv,noheader'],
            capture_output=True, text=True, timeout=PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return GpuStatus(False)
    if result.returncode != 0:
        return GpuStatus(False)
    names = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not names:
        return GpuStatus(False)
    return GpuStatus(True, names[0])
