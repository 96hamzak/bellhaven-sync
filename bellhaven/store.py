"""
The ledger. This is what makes a second run safe.

The whole trick is one line: a FINGERPRINT.

    fingerprint = sha256(type + subject + the exact values we want to write)

If a fingerprint has already been approved or rejected, we never raise it
again. If the website changes tomorrow, the target values change, so the
fingerprint changes, so it comes back once as a fresh proposal. That is the
behaviour the brief asks for: no re-proposing what was already decided, but no
silently swallowing genuinely new information either.

Deliberately NOT in the fingerprint: run ids, timestamps, match scores.
Those change on every run and would defeat the whole thing.
"""
import hashlib
import json
import os
import sqlite3
import time
from contextlib import closing, contextmanager
from datetime import datetime, timezone

from config import DB_PATH, LEGACY_DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    locations     INTEGER,
    accounts      INTEGER,
    proposed      INTEGER,
    notes         TEXT
);

CREATE TABLE IF NOT EXISTS proposals (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint    TEXT NOT NULL UNIQUE,
    run_id         INTEGER,
    last_seen_run  INTEGER,
    type           TEXT NOT NULL,
    subject_id     TEXT NOT NULL,
    subject_label  TEXT,
    subject_kind   TEXT,
    account_id     TEXT,
    tier           TEXT,
    before_json    TEXT,
    target_json    TEXT,
    calls_json     TEXT,
    evidence_json  TEXT,
    editable_json  TEXT,
    status         TEXT NOT NULL DEFAULT 'pending',
    reviewer_note  TEXT,
    result_json    TEXT,
    created_at     TEXT,
    decided_at     TEXT
);

CREATE TABLE IF NOT EXISTS api_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_id  INTEGER,
    at           TEXT,
    method       TEXT,
    path         TEXT,
    body         TEXT,
    status       INTEGER,
    response     TEXT
);
"""

DECIDED = ("approved", "executed", "rejected", "failed")


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _strength(path):
    """
    How much history a ledger file holds: (decisions, most recent run).

    The connection is closed explicitly. `with sqlite3.connect(...)` only ends
    the transaction and leaves the file OPEN until garbage collection, and
    Windows refuses to move a file that is still open. That exact leak once
    crashed the app on startup; Linux allows the move, so it went unnoticed.
    """
    try:
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as conn:
            decided = conn.execute(
                "SELECT COUNT(*) FROM proposals WHERE status IN ('executed','rejected')"
            ).fetchone()[0]
            last_run = conn.execute("SELECT MAX(started_at) FROM runs").fetchone()[0] or ""
        return decided, last_run
    except sqlite3.Error:
        return -1, ""                    # unreadable or not a ledger: never preferred


MOVE_ATTEMPTS = 5


def _move(src, dst):
    """os.replace, retried briefly. On Windows an antivirus scan or the search
    indexer can hold a freshly touched file for a moment."""
    for attempt in range(MOVE_ATTEMPTS):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == MOVE_ATTEMPTS - 1:
                raise
            time.sleep(0.4 * (attempt + 1))


class LedgerInUse(RuntimeError):
    """A ledger file is open in another program, so the merge cannot happen."""


def adopt_legacy_ledger():
    """
    One ledger, one place: ledger/ledger.db.

    Up to build 21h the app used data/ledger.db while the scheduled run kept a
    second copy in ledger/. Two copies of one record drift apart, and in Windows
    dragging a file between folders MOVES it, which is how history went missing.
    Called once when the app or pipeline starts (deliberately NOT from init(),
    so the test suite can never touch a real ledger):

      * only the old file exists  -> moved to the new place
      * both exist                -> the one holding more decisions is kept,
                                     ties going to the most recent run; the other
                                     is renamed into data/, never deleted
    """
    legacy, current = LEGACY_DB_PATH, DB_PATH
    if not legacy.exists() or legacy.resolve() == current.resolve():
        return None
    current.parent.mkdir(parents=True, exist_ok=True)
    in_use = LedgerInUse(
        "Could not merge your two ledger files: one of them is open in another "
        "program, such as a second window already running the app or a database "
        "viewer. Close it and start again. Nothing was changed; both "
        f"{legacy} and {current} are exactly as they were.")

    if not current.exists():
        try:
            _move(legacy, current)
        except OSError as exc:
            raise in_use from exc
        return f"Ledger moved from {legacy} to {current}. It now lives in one place."

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup = legacy.with_name(f"ledger.superseded-{stamp}.db")
    old_strength, new_strength = _strength(legacy), _strength(current)
    legacy_wins = old_strength > new_strength
    try:
        if legacy_wins:
            _move(current, backup)
            try:
                _move(legacy, current)
            except OSError:
                _move(backup, current)       # undo the first move: all or nothing
                raise
        else:
            _move(legacy, backup)
    except OSError as exc:
        raise in_use from exc
    if old_strength == new_strength:
        why = "both held the same history"
    elif old_strength[0] != new_strength[0]:
        why = (f"it held more decisions ({max(old_strength[0], new_strength[0])} vs "
               f"{min(old_strength[0], new_strength[0])})")
    else:
        why = (f"both held {old_strength[0]} decisions and it had the more recent run")
    kept = "data/ledger.db" if legacy_wins else "ledger/ledger.db"
    return (f"Two ledgers found; kept the copy from {kept} because {why}. "
            f"The app now reads only {current}. The other copy is saved as "
            f"{backup.name} in data/ and no longer used.")


def init():
    with connect() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)


def _migrate(conn):
    """
    Ledgers created by older builds lack some columns. CREATE TABLE IF NOT
    EXISTS will not add them, so add them here rather than crash on insert.
    """
    have = {row[1] for row in conn.execute("PRAGMA table_info(proposals)")}
    for column, declaration in (("subject_kind", "TEXT"), ("account_id", "TEXT")):
        if column not in have:
            conn.execute(f"ALTER TABLE proposals ADD COLUMN {column} {declaration}")
    # Backfill so rows written by older builds still render with a CRM link.
    conn.execute("""UPDATE proposals SET subject_kind = CASE
                        WHEN type IN ('create_contact','update_contact') THEN 'contact'
                        WHEN type IN ('create_account','match_ambiguous') THEN 'location'
                        ELSE 'account' END
                    WHERE subject_kind IS NULL""")
    conn.execute("""UPDATE proposals SET account_id = subject_id
                    WHERE account_id IS NULL AND subject_kind = 'account'""")


# Words that DESCRIBE a change rather than make it. A note carries today's date
# and the account's existing note text; labels and scores are explanation. None
# of them may decide identity, or a proposal rejected today returns tomorrow.
DESCRIPTIVE_KEYS = {"note", "label", "sop", "routing", "deductions", "score", "blocked_by"}


def _what_gets_written(value):
    if isinstance(value, dict):
        return {k: _what_gets_written(v) for k, v in value.items()
                if k not in DESCRIPTIVE_KEYS}
    if isinstance(value, list):
        return [_what_gets_written(v) for v in value]
    return value


def fingerprint(proposal):
    payload = json.dumps(
        {
            "type": proposal["type"],
            "subject": proposal["subject_id"],
            "target": _what_gets_written(proposal["target"]),
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:32]


def start_run():
    with connect() as conn:
        cursor = conn.execute("INSERT INTO runs (started_at) VALUES (?)", (now(),))
        return cursor.lastrowid


def finish_run(run_id, locations, accounts, proposed, notes=""):
    with connect() as conn:
        conn.execute(
            "UPDATE runs SET finished_at=?, locations=?, accounts=?, proposed=?, notes=? "
            "WHERE id=?",
            (now(), locations, accounts, proposed, notes, run_id),
        )


def upsert_proposals(run_id, proposals):
    """
    Returns (new, refreshed, skipped_because_decided).

    * brand new fingerprint      -> insert as pending
    * pending fingerprint again  -> just touch last_seen_run, do not duplicate
    * expired fingerprint again  -> back to pending, counted as new: it went
                                    missing for a run (a page that failed to
                                    list, a CRM edit later undone) and is real
                                    again. Leaving it expired hid it forever.
    * decided fingerprint        -> skip entirely, the reviewer already ruled
    """
    new = refreshed = skipped = 0
    with connect() as conn:
        for proposal in proposals:
            fp = fingerprint(proposal)
            row = conn.execute(
                "SELECT id, status FROM proposals WHERE fingerprint=?", (fp,)
            ).fetchone()

            if row and row["status"] in DECIDED:
                conn.execute(
                    "UPDATE proposals SET last_seen_run=? WHERE id=?", (run_id, row["id"])
                )
                skipped += 1
                continue

            if row:
                revived = row["status"] == "expired"
                conn.execute(
                    "UPDATE proposals SET last_seen_run=?, evidence_json=?, subject_label=?, "
                    "status=CASE WHEN status='expired' THEN 'pending' ELSE status END, "
                    "decided_at=CASE WHEN status='expired' THEN NULL ELSE decided_at END "
                    "WHERE id=?",
                    (run_id, json.dumps(proposal["evidence"], default=str),
                     proposal["subject_label"], row["id"]),
                )
                if revived:
                    new += 1
                else:
                    refreshed += 1
                continue

            conn.execute(
                """INSERT INTO proposals
                   (fingerprint, run_id, last_seen_run, type, subject_id, subject_label,
                    subject_kind, account_id, tier, before_json, target_json,
                    calls_json, evidence_json, editable_json, status, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'pending', ?)""",
                (
                    fp, run_id, run_id, proposal["type"], str(proposal["subject_id"]),
                    proposal["subject_label"], proposal.get("subject_kind", "account"),
                    proposal.get("account_id"), proposal["tier"],
                    json.dumps(proposal["before"], default=str),
                    json.dumps(proposal["target"], default=str),
                    json.dumps(proposal.get("care_type_options", []), default=str),
                    json.dumps(proposal["evidence"], default=str),
                    json.dumps(proposal.get("editable", []), default=str),
                    now(),
                ),
            )
            new += 1
    return new, refreshed, skipped


def expire_stale(run_id):
    """A pending item the pipeline no longer generates is no longer true."""
    with connect() as conn:
        cursor = conn.execute(
            # IS NOT, not !=. In SQL, NULL != 5 is NULL rather than true, so a
            # row with no last_seen_run would never expire and a stale proposal
            # from an older build could sit in the queue forever.
            "UPDATE proposals SET status='expired', decided_at=? "
            "WHERE status='pending' AND last_seen_run IS NOT ?",
            (now(), run_id),
        )
        return cursor.rowcount


# The queue is ordered by consequence, not by when it was generated: the
# corporate layer has to exist before anything can be routed into it, ownership
# is the point of the exercise, and cosmetic fixes come last.
TYPE_ORDER = {
    "create_parent": 0, "nest_parent": 1, "rename_parent": 2,
    "chow": 3, "reparent": 4,
    "duplicate_conflict": 5, "match_ambiguous": 6, "create_account": 7,
    "mark_duplicate": 8, "orphan_review": 9,
    "update_fields": 10, "update_contact": 11, "create_contact": 12,
    "needs_human": 13,
}

# Named groups for the filter bar.
FILTER_GROUPS = {
    "all": None,
    "hierarchy": ("create_parent", "nest_parent", "rename_parent"),
    "chow": ("chow",),
    "ownership": ("chow", "reparent"),
    "accounts": ("update_fields", "create_account", "match_ambiguous",
                 "mark_duplicate", "duplicate_conflict", "orphan_review",
                 "reparent", "chow"),
    "contacts": ("create_contact", "update_contact"),
    "duplicates": ("mark_duplicate", "duplicate_conflict"),
    "orphans": ("orphan_review",),
    "needs_choice": ("match_ambiguous", "duplicate_conflict", "needs_human"),
}


DECIDED_OUTCOMES = ("executed", "rejected", "expired", "failed")


def list_proposals(status=None, group="all", q="", kind="", outcome=""):
    """
    status : 'pending' | 'decided' | None
    group  : a key of FILTER_GROUPS
    q      : free text over label, subject id, account id, notes and reviewer note
    kind   : one proposal type, e.g. 'chow', narrowing the group further
    outcome: one of DECIDED_OUTCOMES, for the Decided tab
    """
    clauses, params = [], []
    if status == "pending":
        clauses.append("status='pending'")
    elif status == "decided":
        if outcome in DECIDED_OUTCOMES:
            clauses.append("status=?")
            params.append(outcome)
        else:
            clauses.append("status IN ('approved','executed','rejected','failed','expired')")

    if kind:
        clauses.append("type=?")
        params.append(kind)

    kinds = FILTER_GROUPS.get(group)
    if kinds:
        clauses.append("type IN (%s)" % ",".join("?" * len(kinds)))
        params.extend(kinds)

    if q:
        needle = f"%{q.lower()}%"
        clauses.append(
            "(LOWER(subject_label) LIKE ? OR LOWER(subject_id) LIKE ? "
            "OR LOWER(COALESCE(account_id,'')) LIKE ? "
            "OR LOWER(COALESCE(target_json,'')) LIKE ? "
            "OR LOWER(COALESCE(reviewer_note,'')) LIKE ?)"
        )
        params.extend([needle] * 5)

    query = "SELECT * FROM proposals"
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY id"

    with connect() as conn:
        rows = [_hydrate(r) for r in conn.execute(query, params).fetchall()]
    # Grouped by type in consequence order, then alphabetical by account within
    # each group. Sorting by id instead would put an item generated on a later
    # run after every same-type item from earlier runs, which reads as random.
    rows.sort(key=lambda r: (TYPE_ORDER.get(r["type"], 99),
                             (r["subject_label"] or "").lower(), r["id"]))
    return rows


def group_counts(status="pending"):
    rows = list_proposals(status)
    out = {"all": len(rows)}
    for name, kinds in FILTER_GROUPS.items():
        if kinds:
            out[name] = sum(1 for r in rows if r["type"] in kinds)
    return out


def get_proposal(proposal_id):
    with connect() as conn:
        row = conn.execute("SELECT * FROM proposals WHERE id=?", (proposal_id,)).fetchone()
        return _hydrate(row) if row else None


def claim_for_execution(proposal_id):
    """
    Double-click guard. Flip pending -> executing inside one transaction.
    Only the caller that wins the flip is allowed to hit the API.
    """
    with connect() as conn:
        cursor = conn.execute(
            "UPDATE proposals SET status='executing' WHERE id=? AND status='pending'",
            (proposal_id,),
        )
        return cursor.rowcount == 1


def set_status(proposal_id, status, reviewer_note=None, result=None):
    with connect() as conn:
        conn.execute(
            "UPDATE proposals SET status=?, reviewer_note=COALESCE(?, reviewer_note), "
            "result_json=?, decided_at=? WHERE id=?",
            (status, reviewer_note, json.dumps(result, default=str) if result else None,
             now(), proposal_id),
        )


def override_target(proposal_id, target):
    """The reviewer edited the values before approving."""
    with connect() as conn:
        conn.execute(
            "UPDATE proposals SET target_json=? WHERE id=?",
            (json.dumps(target, default=str), proposal_id),
        )


def log_call(proposal_id, call):
    with connect() as conn:
        conn.execute(
            "INSERT INTO api_log (proposal_id, at, method, path, body, status, response) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                proposal_id, now(), call.get("method"), call.get("url") or call.get("path"),
                json.dumps(call.get("body"), default=str), call.get("status"),
                json.dumps(call.get("response"), default=str)[:4000],
            ),
        )


def counts():
    with connect() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM proposals GROUP BY status"
        ).fetchall()
        return {r["status"]: r["n"] for r in rows}


def reset_history():
    """
    Wipe every local run, proposal and log.

    Use this only after you have restored the CRM from a snapshot, so the next
    run behaves exactly as if nothing had ever happened.
    """
    with connect() as conn:
        conn.executescript(
            "DELETE FROM api_log; DELETE FROM proposals; DELETE FROM runs; "
            "DELETE FROM sqlite_sequence WHERE name IN ('api_log','proposals','runs');"
        )


def _hydrate(row):
    item = dict(row)
    for key in ("before", "target", "care_type_options", "evidence", "editable", "result"):
        raw = item.pop("calls_json" if key == "care_type_options" else f"{key}_json", None)
        try:
            default = [] if key in ("care_type_options", "editable") else {}
            item[key] = json.loads(raw) if raw else default
        except (TypeError, json.JSONDecodeError):
            item[key] = {}
    return item
