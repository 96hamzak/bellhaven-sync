"""
Fetching the scheduled run's ledger from GitHub and merging it into this
machine's. Every rule of the merge is pinned here, on throwaway ledgers only.
"""
import sqlite3
from contextlib import closing

import pytest

import sync_ledger
from bellhaven import store


def _p(subject, value):
    return {"type": "update_fields", "subject_id": subject, "subject_label": subject,
            "before": {"phone": "old"}, "target": {"phone": value}, "evidence": {},
            "tier": "confident"}


A, B, X, Y = _p("A", "1"), _p("B", "2"), _p("X", "3"), _p("Y", "4")


class Ledger:
    """A ledger file built step by step, with runs stamped at chosen times."""

    def __init__(self, path, monkeypatch):
        self.path, self.mp = path, monkeypatch
        path.parent.mkdir(parents=True, exist_ok=True)
        self._use(); store.init()

    def _use(self):
        self.mp.setattr(store, "DB_PATH", self.path)

    def run(self, when, proposals):
        self._use()
        run_id = store.start_run()
        store.upsert_proposals(run_id, proposals)
        store.expire_stale(run_id)
        with closing(sqlite3.connect(self.path)) as c, c:
            c.execute("UPDATE runs SET started_at=?, finished_at=? WHERE id=?", (when, when, run_id))
        return self

    def decide(self, proposal, status):
        self._use()
        fp = store.fingerprint(proposal)
        with closing(sqlite3.connect(self.path)) as c, c:
            c.execute("UPDATE proposals SET status=?, decided_at='2026-09-22' WHERE fingerprint=?",
                      (status, fp))
        return self

    def status(self, proposal):
        with closing(sqlite3.connect(self.path)) as c:
            row = c.execute("SELECT status FROM proposals WHERE fingerprint=?",
                            (store.fingerprint(proposal),)).fetchone()
        return row[0] if row else None

    def count(self, table="proposals"):
        with closing(sqlite3.connect(self.path)) as c:
            return c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


@pytest.fixture
def world(tmp_path, monkeypatch):
    local = Ledger(tmp_path / "ledger" / "ledger.db", monkeypatch)
    github = Ledger(tmp_path / "github" / "ledger.db", monkeypatch)

    def merge():
        monkeypatch.setattr(store, "DB_PATH", local.path)
        return sync_ledger.merge(github.path, local.path, tmp_path / "data")
    return local, github, merge, tmp_path


def test_what_the_scheduled_run_found_lands_in_your_queue(world):
    local, github, merge, _ = world
    local.run("2026-09-21T10:00", [A]).decide(A, "executed")
    github.run("2026-09-22T07:00", [A, B])
    summary = merge()
    assert summary["added_pending"] == 1 and summary["your_decisions"] == 1
    assert local.status(B) == "pending"
    assert [p["subject_id"] for p in store.list_proposals("pending")] == ["B"]


def test_your_decision_always_wins(world):
    """You rejected X and never uploaded; GitHub still thinks it is pending."""
    local, github, merge, _ = world
    local.run("2026-09-21T10:00", [X]).decide(X, "rejected")
    github.run("2026-09-22T07:00", [X])
    merge()
    assert local.status(X) == "rejected"


def test_a_decision_made_elsewhere_is_taken(world):
    local, github, merge, _ = world
    local.run("2026-09-21T10:00", [X])
    github.run("2026-09-22T07:00", [X]).decide(X, "rejected")
    assert merge()["decisions_taken"] == 1
    assert local.status(X) == "rejected"


def test_github_newer_its_expiry_is_applied(world):
    local, github, merge, _ = world
    local.run("2026-09-21T10:00", [Y])
    github.run("2026-09-21T10:00", [Y]).run("2026-09-22T07:00", [])     # Y no longer found
    assert merge()["statuses_updated"] == 1
    assert local.status(Y) == "expired"


def test_your_machine_newer_your_view_is_kept(world):
    local, github, merge, _ = world
    github.run("2026-09-21T10:00", [Y]).run("2026-09-22T07:00", [])      # expired on GitHub
    local.run("2026-09-22T09:00", [Y])                                   # you ran later
    merge()
    assert local.status(Y) == "pending"


def test_fetching_twice_changes_nothing_more(world):
    local, github, merge, _ = world
    local.run("2026-09-21T10:00", [A]).decide(A, "executed")
    github.run("2026-09-22T07:00", [A, B])
    merge()
    before = (local.count(), local.count("runs"), local.status(A), local.status(B))
    second = merge()
    assert (local.count(), local.count("runs"), local.status(A), local.status(B)) == before
    assert second["added_pending"] == 0 and second["runs_imported"] == 0


def test_runs_you_uploaded_are_not_duplicated_on_the_way_back(world):
    """Round trip: your ledger went up, the schedule added a run, it comes back."""
    import shutil
    local, github, merge, _ = world
    local.run("2026-09-21T10:00", [A]).decide(A, "executed")
    shutil.copy(local.path, github.path)                                 # you uploaded
    github.run("2026-09-22T07:00", [A, B])                               # the schedule ran
    summary = merge()
    assert summary["runs_imported"] == 1 and local.count("runs") == 2


def test_a_bad_download_changes_nothing(world):
    local, github, merge, tmp_path = world
    local.run("2026-09-21T10:00", [A]).decide(A, "executed")
    github.path.write_text("<html>404</html>")
    with pytest.raises(sync_ledger.SyncError, match="Nothing was changed"):
        merge()
    assert local.status(A) == "executed" and local.count() == 1
    assert not list((tmp_path / "data").glob("ledger.before-sync-*.db"))


def test_your_ledger_is_backed_up_before_every_merge(world):
    local, github, merge, tmp_path = world
    local.run("2026-09-21T10:00", [A]).decide(A, "executed")
    github.run("2026-09-22T07:00", [A, B])
    summary = merge()
    backup = tmp_path / "data" / summary["backup"]
    with closing(sqlite3.connect(backup)) as c:
        assert c.execute("SELECT COUNT(*) FROM proposals").fetchone()[0] == 1   # pre-merge state
