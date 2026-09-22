#!/usr/bin/env python3
"""
Tells you, in plain words, what you are actually running.

    python doctor.py

Read-only. Run it whenever the screens do not match what you expect. It checks
that the files in this folder all come from the same build, that the review app
answering on port 8000 is THIS folder's app and not a leftover from an old one,
and that your token and ledger are in order.
"""
import json
import socket
import sqlite3
from contextlib import closing
import urllib.error
import urllib.request
from pathlib import Path

EXPECTED_BUILD = "2026-09-21k"      # ships with this file; must match config.py
ROOT = Path(__file__).resolve().parent
PORT = 8000
SEED_TIME = "2026-09-20 13:56:24Z"  # when the sandbox was created; untouched records show this
HARBORVIEW = "001FJZYHR7MLFMNPLL"
BELLHAVEN = "0015QAPLGS3FVYEEEM"
TRACKED = ["name", "parent_id", "status", "care_type", "phone", "billing_street",
           "billing_city", "billing_state", "billing_zip", "chow_current_account",
           "duplicate_of_account", "note"]
problems = []
port_problem = []            # only this one warrants the "stop port 8000" advice


def say(ok, label, detail=""):
    mark = "OK " if ok else "FIX"
    print(f"  [{mark}] {label}" + (f"\n        {detail}" if detail else ""))
    if not ok:
        problems.append(label)


def main():
    print(f"\nChecking the folder you ran this from:\n  {ROOT}\n")

    # 1. are the files on disk all from the same build?
    try:
        import config
        build = getattr(config, "BUILD", None)
    except Exception as exc:                                   # noqa: BLE001
        say(False, "config.py loads", str(exc))
        return finish()
    say(build == EXPECTED_BUILD, f"config.py is build {EXPECTED_BUILD}",
        "" if build == EXPECTED_BUILD else
        f"it says {build!r}. This folder mixes files from two different downloads. "
        "Extract the newest zip into a brand-new empty folder.")

    base = ROOT / "templates" / "base.html"
    new_theme = base.exists() and "--bg:#0e1118" in base.read_text(encoding="utf-8")
    say(new_theme, "templates are the new dark-theme ones",
        "" if new_theme else "templates/base.html is the old version.")

    # 2. token
    env = ROOT / ".env"
    has_token = env.exists() and "CRM_API_TOKEN=bh_" in env.read_text(encoding="utf-8")
    say(has_token, ".env exists here and holds a token",
        "" if has_token else
        "Copy .env from your old folder into this one. On Windows check it is not "
        "saved as .env.txt.")

    # 3. ledger
    from config import DB_PATH, LEGACY_DB_PATH
    db = DB_PATH
    if LEGACY_DB_PATH.exists():
        say(True, "an old ledger is still in data/",
            "It will be merged into ledger/ledger.db the next time the app or "
            "pipeline starts: the copy holding more decisions is kept, the other "
            "renamed. Nothing is deleted.")
    if db.exists():
        with closing(sqlite3.connect(db)) as conn:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(proposals)")}
            pending = conn.execute(
                "SELECT COUNT(*) FROM proposals WHERE status='pending'").fetchone()[0]
        current = {"subject_kind", "account_id"} <= cols
        say(True, "ledger found",
            f"{pending} pending proposal(s). "
            + ("Schema is current." if current else
               "Schema is from an older build; the app upgrades it automatically on start."))
    else:
        say(True, "no ledger yet", "Normal for a fresh folder. run_pipeline.py creates it.")

    # 4. who is answering on port 8000?
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(2)
        busy = probe.connect_ex(("127.0.0.1", PORT)) == 0
    if not busy:
        say(True, f"nothing is running on port {PORT}",
            "Start the app from THIS folder:  python -m uvicorn app:app --reload")
        if has_token:
            crm_changes()
        return finish()

    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/version", timeout=3) as r:
            info = json.loads(r.read().decode())
        same_folder = Path(info.get("folder", "")).resolve() == ROOT
        same_build = info.get("build") == EXPECTED_BUILD
        if not (same_folder and same_build):
            port_problem.append(True)
        say(same_folder and same_build,
            f"the app on port {PORT} is this folder's app",
            "" if same_folder and same_build else
            f"Port {PORT} is served by build {info.get('build')} from "
            f"{info.get('folder')}. That is a DIFFERENT folder. Stop it (see below).")
    except urllib.error.HTTPError as exc:
        port_problem.append(True)
        say(False, f"the app on port {PORT} is this folder's app",
            f"Port {PORT} answered {exc.code} to /version, which means an OLD build "
            "is still running from an earlier folder. That is why the screens have "
            "not changed. Stop it (see below).")
    except Exception as exc:                                   # noqa: BLE001
        port_problem.append(True)
        say(False, f"something unknown holds port {PORT}", str(exc))

    if has_token:
        crm_changes()
    return finish()


def _key(record):
    return record.get("id") or record.get("account_id")


def crm_changes():
    """
    What has ALREADY been written to the CRM, whichever folder or build did it.

    Uses the CRM itself as the source of truth, not a ledger, because a ledger
    only knows about the approvals made from its own folder.
    """
    print("\nWhat has already been written to the CRM (read-only):\n")
    try:
        from bellhaven.crm_client import CRMClient
        accounts = CRMClient().list_accounts()
    except Exception as exc:                                   # noqa: BLE001
        say(False, "could read the CRM", str(exc))
        return

    created = [a for a in accounts if a.get("created_by_candidate")]
    snapshots = sorted((ROOT / "data" / "snapshots").glob("*.json"))
    changed, harborview_moves = [], []

    if snapshots:
        baseline = json.loads(snapshots[0].read_text(encoding="utf-8"))
        before = {_key(a): a for a in baseline.get("accounts", [])}
        basis = f"compared field by field with your earliest snapshot, {snapshots[0].name}"
        for account in accounts:
            original = before.get(_key(account))
            if original is None:
                continue
            fields = [f for f in TRACKED if (account.get(f) or "") != (original.get(f) or "")]
            if fields:
                changed.append((account, fields))
                if original.get("parent_id") == HARBORVIEW and account.get("parent_id") == BELLHAVEN:
                    harborview_moves.append(account)
    else:
        basis = ("no snapshot in this folder, so judged by updated_at alone. Copy "
                 "data\\snapshots across from your old folder for an exact answer")
        changed = [(a, ["updated_at"]) for a in accounts
                   if (a.get("updated_at") or SEED_TIME) != SEED_TIME
                   and not a.get("created_by_candidate")]

    if not created and not changed:
        say(True, "nothing has been written to the CRM yet", basis)
        return

    say(True, f"{len(created)} account(s) created, {len(changed)} account(s) edited", basis)
    for account in created[:10]:
        print(f"        created  {account.get('name')}  ({_key(account)})")
    for account, fields in changed[:15]:
        print(f"        edited   {account.get('name')}: {', '.join(fields)}")
    if len(changed) > 15:
        print(f"        ... and {len(changed) - 15} more")

    if harborview_moves:
        say(False, f"{len(harborview_moves)} facility/ies were moved OUT of Harborview",
            "An older build pointed Harborview facilities straight at Bellhaven. The "
            "current model keeps them under Harborview, which itself moves under "
            "Bellhaven. Send this output to Claude before approving anything else.")


def finish():
    print()
    if not problems:
        print("Everything checks out. If the browser still shows old screens, press "
              "Ctrl+F5 to force a full reload.\n")
        return 0
    if not port_problem:
        print("Fix the lines marked [FIX] above, then run  python doctor.py  again.\n")
        return 1
    print("An old app is answering on port 8000. Close EVERY terminal window, then")
    print("open one fresh terminal and cd into")
    print(f"  {ROOT}")
    print("\nIf http://127.0.0.1:8000 still loads with every terminal closed, paste")
    print("this into PowerShell to stop whatever is holding the port:\n")
    print("  Get-NetTCPConnection -LocalPort 8000 -State Listen | "
          "ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }\n")
    print("Then start the app from this folder and run  python doctor.py  once more.\n")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
