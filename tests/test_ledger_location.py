"""
One ledger, one place: ledger/ledger.db.

Up to build 21h there were two copies, data/ledger.db for the app and
ledger/ledger.db for the scheduled run. These tests pin the one-time adoption
that merges them, using temporary paths only: a real ledger is never touched.
"""
import sqlite3
from contextlib import closing

import pytest

from bellhaven import store


def _ledger(path, decided=0, runs=("2026-09-21T10:00:00+00:00",)):
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.executescript(store.SCHEMA)
        for i in range(decided):
            conn.execute(
                "INSERT INTO proposals (fingerprint, type, subject_id, status) "
                "VALUES (?, 'reparent', '001X', 'executed')", (f"fp{i}",))
        for started in runs:
            conn.execute("INSERT INTO runs (started_at) VALUES (?)", (started,))
    return path


def _decided(path):
    with closing(sqlite3.connect(path)) as conn:
        return conn.execute("SELECT COUNT(*) FROM proposals WHERE status='executed'").fetchone()[0]


@pytest.fixture
def paths(tmp_path, monkeypatch):
    current = tmp_path / "ledger" / "ledger.db"
    legacy = tmp_path / "data" / "ledger.db"
    monkeypatch.setattr(store, "DB_PATH", current)
    monkeypatch.setattr(store, "LEGACY_DB_PATH", legacy)
    return current, legacy


def test_nothing_happens_when_there_is_only_the_new_ledger(paths):
    current, legacy = paths
    _ledger(current, decided=5)
    assert store.adopt_legacy_ledger() is None
    assert _decided(current) == 5


def test_an_old_ledger_alone_is_simply_moved(paths):
    current, legacy = paths
    _ledger(legacy, decided=51)
    notice = store.adopt_legacy_ledger()
    assert "moved" in notice.lower()
    assert not legacy.exists() and _decided(current) == 51


def test_history_in_ledger_folder_beats_an_empty_new_data_file(paths):
    """The Windows drag-and-drop case: the history moved into ledger/, and the
    app then created a fresh, empty data/ledger.db. The history must win."""
    current, legacy = paths
    _ledger(current, decided=51)
    _ledger(legacy, decided=0, runs=("2026-09-22T09:00:00+00:00",))   # newer, but empty
    store.adopt_legacy_ledger()
    assert _decided(current) == 51
    assert not legacy.exists()


def test_a_data_copy_with_more_decisions_wins(paths):
    """The other case: history restored into data/, then more approvals made."""
    current, legacy = paths
    _ledger(current, decided=51)
    _ledger(legacy, decided=54)
    store.adopt_legacy_ledger()
    assert _decided(current) == 54


def test_the_losing_copy_is_kept_as_a_backup_never_deleted(paths):
    current, legacy = paths
    _ledger(current, decided=51)
    _ledger(legacy, decided=0)
    store.adopt_legacy_ledger()
    backups = list(legacy.parent.glob("ledger.superseded-*.db"))
    assert len(backups) == 1 and _decided(backups[0]) == 0


def test_an_unreadable_old_file_never_replaces_a_good_ledger(paths):
    current, legacy = paths
    _ledger(current, decided=51)
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text("not a database")
    store.adopt_legacy_ledger()
    assert _decided(current) == 51


def test_the_scheduled_run_needs_no_copy_step_to_stay_rerun_safe(paths, tmp_path, monkeypatch):
    """A GitHub runner is a fresh checkout: the committed ledger/ledger.db is
    simply there, so decisions made yesterday are recognised today."""
    import shutil
    from bellhaven.planner import build_plan
    from tests.test_real_schema import ACCOUNTS, CONTACTS, LOCATIONS

    current, _ = paths
    current.parent.mkdir(parents=True, exist_ok=True)
    store.init()
    store.upsert_proposals(store.start_run(), build_plan(LOCATIONS, ACCOUNTS, CONTACTS)[0])
    for p in store.list_proposals("pending"):
        store.set_status(p["id"], "rejected", "no")

    committed = tmp_path / "repo" / "ledger" / "ledger.db"              # git commit + push
    committed.parent.mkdir(parents=True)
    shutil.copy(current, committed)
    tomorrow = tmp_path / "runner2" / "ledger" / "ledger.db"            # next day's checkout
    tomorrow.parent.mkdir(parents=True)
    shutil.copy(committed, tomorrow)

    monkeypatch.setattr(store, "DB_PATH", tomorrow)
    store.init()
    new, _, decided = store.upsert_proposals(
        store.start_run(), build_plan(LOCATIONS, ACCOUNTS, CONTACTS)[0])
    assert new == 0 and decided > 0


# ------------------------------------------------ files in use by something else
def _held_open(path):
    """Another program holding the file, e.g. a second app window or a viewer."""
    conn = sqlite3.connect(path)
    conn.execute("SELECT 1").fetchone()
    return conn


def test_a_ledger_open_elsewhere_stops_cleanly_and_changes_nothing(paths, monkeypatch):
    current, legacy = paths
    monkeypatch.setattr(store.time, "sleep", lambda s: None)
    _ledger(current, decided=51)
    _ledger(legacy, decided=54)
    holder = _held_open(current)
    try:
        with pytest.raises(store.LedgerInUse, match="Nothing was changed"):
            store.adopt_legacy_ledger()
    finally:
        holder.close()
    assert _decided(current) == 51 and _decided(legacy) == 54   # both untouched
    assert not list(legacy.parent.glob("ledger.superseded-*.db"))


def test_if_the_second_move_fails_the_first_is_undone(paths, monkeypatch):
    """Winning copy is data/, so the merge needs two moves. Hold data/ open so
    the second fails after the first succeeded: the first must be reversed."""
    current, legacy = paths
    monkeypatch.setattr(store.time, "sleep", lambda s: None)
    _ledger(current, decided=51)
    _ledger(legacy, decided=54)
    holder = _held_open(legacy)
    try:
        with pytest.raises(store.LedgerInUse):
            store.adopt_legacy_ledger()
    finally:
        holder.close()
    assert _decided(current) == 51 and _decided(legacy) == 54
    assert not list(legacy.parent.glob("ledger.superseded-*.db"))


def test_once_the_other_program_lets_go_the_merge_succeeds(paths, monkeypatch):
    current, legacy = paths
    monkeypatch.setattr(store.time, "sleep", lambda s: None)
    _ledger(current, decided=51)
    _ledger(legacy, decided=54)
    holder = _held_open(current)
    with pytest.raises(store.LedgerInUse):
        store.adopt_legacy_ledger()
    holder.close()                                   # the other window is closed
    assert "more decisions (54 vs 51)" in store.adopt_legacy_ledger()
    assert _decided(current) == 54


def test_the_notice_says_why_a_copy_was_kept_when_decisions_tie(paths):
    current, legacy = paths
    _ledger(current, decided=51, runs=("2026-09-21T10:00:00+00:00",))
    _ledger(legacy, decided=51, runs=("2026-09-22T09:30:00+00:00",))
    notice = store.adopt_legacy_ledger()
    assert "both held 51 decisions and it had the more recent run" in notice
    assert "51 vs 51" not in notice


# ------------------------------------------------------- expired, then back
def test_an_expired_proposal_that_returns_goes_back_in_the_queue(paths):
    """A community missing from the site for one day, back the next. Its fix
    must reappear, not stay expired and invisible forever."""
    current, _ = paths
    current.parent.mkdir(parents=True, exist_ok=True)
    store.init()
    p = {"type": "update_fields", "subject_id": "001X", "subject_label": "Bellhaven of Wooster",
         "before": {"phone": "1"}, "target": {"phone": "2"}, "evidence": {}, "tier": "confident"}
    store.upsert_proposals(store.start_run(), [p])
    gone = store.start_run(); store.upsert_proposals(gone, []); store.expire_stale(gone)
    assert store.counts() == {"expired": 1}
    new, refreshed, decided = store.upsert_proposals(store.start_run(), [p])
    assert (new, refreshed, decided) == (1, 0, 0)
    assert [x["subject_label"] for x in store.list_proposals("pending")] == ["Bellhaven of Wooster"]
