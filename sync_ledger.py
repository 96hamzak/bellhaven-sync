#!/usr/bin/env python3
"""
Bring the scheduled run's findings from GitHub onto this machine.

    python sync_ledger.py            (or press "Fetch from GitHub" on the Tools tab)

Then open the review app: whatever the midnight run found is already in the
queue, with no scrape and no pipeline run on your side.

It MERGES rather than overwrites, so a decision you made on this machine is
never lost, even one you never uploaded. Matching is by fingerprint:

    on your machine        on GitHub                result
    ---------------        ---------                ------
    not there              anything                 added (the scheduled run found it)
    approved / rejected    anything                 yours wins, always
    pending / expired      approved / rejected      GitHub's decision is taken
    pending / expired      pending / expired        whichever ledger ran more recently

Your ledger is backed up first (data/ledger.before-sync-<time>.db), the merge
is one all-or-nothing transaction, and the ledger file is never replaced, only
written to: replacing a file is what Windows refuses while anything holds it.
"""
import json
import sqlite3
import sys
import urllib.error
import urllib.request
from contextlib import closing
from datetime import datetime, timezone

from config import ACCOUNT_INDEX, GITHUB_BRANCH, GITHUB_REPO, ROOT
from bellhaven import store

DECIDED = {"approved", "executed", "rejected", "failed", "executing"}
SQLITE_MAGIC = b"SQLite format 3\x00"
USER_AGENT = "bellhaven-sync"


class SyncError(RuntimeError):
    """Nothing was changed; the message says why."""


# ------------------------------------------------------------------ fetch
def _latest_commit():
    """The newest commit id on the branch, so the download is exact rather than
    a cached copy. Optional: without it we fall back to the branch name."""
    request = urllib.request.Request(
        f"https://api.github.com/repos/{GITHUB_REPO}/commits/{GITHUB_BRANCH}",
        headers={"Accept": "application/vnd.github.sha", "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            sha = response.read().decode().strip()
        return sha if len(sha) >= 7 else None
    except (urllib.error.URLError, TimeoutError, OSError):
        return None


def download(target=None):
    """Fetch ledger/ledger.db from GitHub. Returns (path, commit id or None)."""
    target = target or (ROOT / "data" / "github-ledger.db")
    sha = _latest_commit()
    url = (f"https://raw.githubusercontent.com/{GITHUB_REPO}/"
           f"{sha or GITHUB_BRANCH}/ledger/ledger.db")
    try:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=60) as response:
            data = response.read()
    except urllib.error.HTTPError as exc:
        raise SyncError(
            f"GitHub answered {exc.code} for {url}. Check the repository is public and "
            "that ledger/ledger.db has been committed. Nothing was changed.") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise SyncError(f"Could not reach GitHub ({exc}). Nothing was changed.") from exc
    if not data.startswith(SQLITE_MAGIC):
        raise SyncError("What GitHub sent back is not a ledger file. Nothing was changed.")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return target, sha


# ------------------------------------------------------------------ merge
def _columns(conn, table):
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]


def _read(path):
    try:
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            columns = _columns(conn, "proposals")
            proposals = {r["fingerprint"]: dict(r) for r in conn.execute("SELECT * FROM proposals")}
            runs = [dict(r) for r in conn.execute("SELECT * FROM runs")]
    except sqlite3.Error as exc:
        raise SyncError(f"The downloaded file is not a readable ledger ({exc}). "
                        "Nothing was changed.") from exc
    return columns, proposals, runs


def _backup(local_path, backup_dir):
    """A consistent copy even while the app is running: SQLite's own backup."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / f"ledger.before-sync-{stamp}.db"
    with closing(sqlite3.connect(local_path)) as source, closing(sqlite3.connect(backup)) as dest:
        source.backup(dest)
    return backup


def merge(remote_path, local_path=None, backup_dir=None):
    """Merge a downloaded ledger into this machine's ledger. Returns a summary."""
    local_path = local_path or store.DB_PATH
    backup_dir = backup_dir or store.LEGACY_DB_PATH.parent          # data/, git-ignored
    remote_columns, remote_props, remote_runs = _read(remote_path)

    local_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(local_path)) as conn, conn:        # schema current
        conn.executescript(store.SCHEMA)
        store._migrate(conn)
    backup = _backup(local_path, backup_dir)

    summary = {"added_pending": 0, "added_other": 0, "decisions_taken": 0,
               "statuses_updated": 0, "runs_imported": 0, "backup": backup.name}

    with closing(sqlite3.connect(local_path, timeout=30)) as conn:
        conn.row_factory = sqlite3.Row
        local_columns = _columns(conn, "proposals")
        shared = [c for c in remote_columns if c in local_columns and c != "id"]
        local_props = {r["fingerprint"]: dict(r) for r in conn.execute("SELECT * FROM proposals")}
        local_latest = conn.execute("SELECT MAX(started_at) FROM runs").fetchone()[0] or ""
        remote_latest = max((r.get("started_at") or "" for r in remote_runs), default="")
        github_is_newer = remote_latest > local_latest
        summary["your_decisions"] = sum(1 for p in local_props.values() if p["status"] in DECIDED)

        with conn:                                                  # one transaction
            # Runs first, keyed by start time, so a run already known here
            # (because you uploaded your ledger earlier) is never duplicated.
            known = {(r["started_at"], r["finished_at"]): r["id"]
                     for r in conn.execute("SELECT id, started_at, finished_at FROM runs")}
            run_map = {}
            for run in remote_runs:
                key = (run.get("started_at"), run.get("finished_at"))
                if key in known:
                    run_map[run["id"]] = known[key]
                    continue
                cursor = conn.execute(
                    "INSERT INTO runs (started_at, finished_at, locations, accounts, proposed, notes) "
                    "VALUES (?,?,?,?,?,?)",
                    (run.get("started_at"), run.get("finished_at"), run.get("locations"),
                     run.get("accounts"), run.get("proposed"),
                     ((run.get("notes") or "") + " [run on GitHub]").strip()))
                run_map[run["id"]] = known[key] = cursor.lastrowid
                summary["runs_imported"] += 1

            for fp, remote in remote_props.items():
                remote = dict(remote)
                for key in ("run_id", "last_seen_run"):
                    if remote.get(key) in run_map:
                        remote[key] = run_map[remote[key]]
                local = local_props.get(fp)

                if local is None:                                   # found on GitHub
                    conn.execute(
                        f"INSERT INTO proposals ({', '.join(shared)}) "
                        f"VALUES ({', '.join('?' * len(shared))})",
                        [remote.get(c) for c in shared])
                    summary["added_pending" if remote["status"] == "pending" else "added_other"] += 1
                    continue

                if local["status"] in DECIDED:                      # yours always wins
                    continue

                if remote["status"] in DECIDED:                     # decided elsewhere
                    conn.execute(
                        "UPDATE proposals SET status=?, decided_at=?, reviewer_note=?, "
                        "result_json=?, target_json=? WHERE fingerprint=?",
                        (remote["status"], remote.get("decided_at"), remote.get("reviewer_note"),
                         remote.get("result_json"), remote.get("target_json"), fp))
                    summary["decisions_taken"] += 1
                    continue

                if github_is_newer and (remote["status"], remote.get("last_seen_run")) != \
                        (local["status"], local.get("last_seen_run")):
                    conn.execute(
                        "UPDATE proposals SET status=?, decided_at=?, last_seen_run=?, "
                        "evidence_json=? WHERE fingerprint=?",
                        (remote["status"], remote.get("decided_at"), remote.get("last_seen_run"),
                         remote.get("evidence_json"), fp))
                    if remote["status"] != local["status"]:
                        summary["statuses_updated"] += 1

            store._migrate(conn)          # rows from an older build get their fields now
    return summary


# ------------------------------------------------------------------ extras
def refresh_account_names():
    """Parent names on the review cards come from a small index the pipeline
    normally writes. Without a local pipeline run, refresh it from the CRM
    (read-only). Best effort: the merge has already succeeded either way."""
    try:
        from bellhaven.crm_client import CRMClient
        accounts = CRMClient().list_accounts()
        ACCOUNT_INDEX.write_text(
            json.dumps({a["id"]: a.get("name", "") for a in accounts}), encoding="utf-8")
        return len(accounts)
    except Exception:                                               # noqa: BLE001
        return None


def sync():
    notice = store.adopt_legacy_ledger()
    path, sha = download()
    summary = merge(path)
    summary["commit"] = sha[:7] if sha else None
    summary["account_names"] = refresh_account_names()
    summary["notice"] = notice
    return summary


def describe(summary):
    source = (f"commit {summary['commit']}" if summary.get("commit")
              else f"the latest on {GITHUB_BRANCH} (GitHub may serve a copy a few minutes old)")
    lines = [f"Fetched the ledger from GitHub, {source}."]
    if summary.get("notice"):
        lines.append(summary["notice"])
    lines += [
        f"  new proposals for your queue : {summary['added_pending']}",
        f"  other new history            : {summary['added_other']}",
        f"  statuses updated from GitHub : {summary['statuses_updated']}",
        f"  decisions taken from GitHub  : {summary['decisions_taken']}",
        f"  your decisions kept as-is    : {summary['your_decisions']}",
        f"  runs added to the history    : {summary['runs_imported']}",
        f"  backup of your ledger        : data/{summary['backup']}",
    ]
    if summary.get("account_names") is None:
        lines.append("  (account names not refreshed: the CRM was unreachable; cards may "
                     "show a parent's id instead of its name until the next pipeline run)")
    lines.append("Open the review app to work through the queue.")
    return "\n".join(lines)


if __name__ == "__main__":
    try:
        result = sync()
    except (SyncError, store.LedgerInUse) as exc:
        sys.exit(str(exc))
    print(describe(result))
