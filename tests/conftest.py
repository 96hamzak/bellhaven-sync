"""
Shared test setup.

Every test gets its own throwaway ledger. Running `pytest` on a machine that
holds real review history can therefore never read it, change it, or trigger
the one-time adoption of an old data/ledger.db.
"""
import pytest

from bellhaven import store


@pytest.fixture(autouse=True)
def _never_touch_a_real_ledger(tmp_path, monkeypatch):
    (tmp_path / "ledger").mkdir(exist_ok=True)
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "ledger" / "ledger.db")
    monkeypatch.setattr(store, "LEGACY_DB_PATH", tmp_path / "data" / "ledger.db")


# ---------------------------------------------------------------------------
# Windows file-locking, enforced on Linux.
#
# Windows refuses to move or rename a file while any handle to it is open;
# Linux allows it. A test suite run only on Linux therefore passes code that
# fails on a Windows laptop, which is exactly how a startup crash shipped once.
# On Linux this makes os.replace follow the Windows rule for handles held by
# the test process itself. On Windows the operating system enforces it anyway,
# so the check is simply skipped there.
# ---------------------------------------------------------------------------
import os
from pathlib import Path

_PROC_FD = Path("/proc/self/fd")


def _open_in_this_process(path):
    target = os.path.realpath(path)
    for fd in os.listdir(_PROC_FD):
        try:
            if os.path.realpath(os.readlink(_PROC_FD / fd)) == target:
                return True
        except OSError:
            continue
    return False


@pytest.fixture(autouse=True)
def _windows_style_file_locks(monkeypatch):
    if not _PROC_FD.exists():
        return
    real_replace = os.replace

    def strict_replace(src, dst, *args, **kwargs):
        for path in (src, dst):
            if os.path.exists(path) and _open_in_this_process(path):
                raise PermissionError(
                    32, "The process cannot access the file because it is being "
                        "used by another process (Windows rule, emulated)", str(path))
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", strict_replace)
