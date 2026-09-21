"""
Save the CRM before you touch it, and put it back if you need to.

An honest warning about "restore". This API has POST, GET and PATCH. It has no
DELETE. So:

  * Fields we CHANGED can be put back exactly, by patching the old values in.
  * Accounts we CREATED cannot be removed. The closest we can get is marking
    them Inactive with a note saying they were created by a reverted run.

So a restore gets you back to a working state, not to a byte-identical one.
Take a snapshot before your very first approval and keep it.
"""
import json
from datetime import datetime, timezone

from config import (
    SNAPSHOT_DIR,
    STATUS_INACTIVE,
)
from bellhaven.crm_client import CRMClient

RESTORABLE_FIELDS = [
    "name", "parent_id", "status", "care_type", "phone",
    "billing_street", "billing_city", "billing_state", "billing_zip",
    "lifetime_revenue", "outstanding_ar",
    "chow_current_account", "duplicate_of_account", "note",
]


def save(label="snapshot"):
    client = CRMClient()
    accounts = client.list_accounts()
    contacts = client.list_contacts()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    path = SNAPSHOT_DIR / f"{label}-{stamp}.json"
    path.write_text(
        json.dumps(
            {"taken_at": stamp, "accounts": accounts, "contacts": contacts},
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    return path, len(accounts), len(contacts)


def latest():
    files = sorted(SNAPSHOT_DIR.glob("*.json"))
    return files[-1] if files else None


def list_snapshots():
    return sorted(p.name for p in SNAPSHOT_DIR.glob("*.json"))


def restore(path=None, dry_run=True):
    """
    Patch every account back to its snapshot values.

    dry_run=True (the default) tells you what it WOULD do and writes nothing.
    """
    path = path or latest()
    if path is None:
        raise RuntimeError("No snapshot found. Run a snapshot before approving anything.")

    data = json.loads(open(path, encoding="utf-8").read())
    saved = {a["id"]: a for a in data["accounts"]}

    client = CRMClient(allow_writes=not dry_run)
    live = client.list_accounts()

    plan = []
    for account in live:
        original = saved.get(account["id"])
        if original is None or account.get("created_by_candidate"):
            plan.append(
                {
                    "id": account["id"],
                    "name": account.get("name"),
                    "action": "created after the snapshot; cannot delete, will mark Inactive",
                    "body": {
                        "status": STATUS_INACTIVE,
                        "note": "Created by a bellhaven-sync run that was later reverted.",
                    },
                }
            )
            continue
        diff = {
            field: original.get(field)
            for field in RESTORABLE_FIELDS
            if (account.get(field) or None) != (original.get(field) or None)
        }
        if diff:
            plan.append({"id": account["id"], "name": account.get("name"),
                         "action": "revert fields", "body": diff})

    if dry_run:
        return {"dry_run": True, "snapshot": str(path), "changes": plan}

    for item in plan:
        client.update_account(item["id"], item["body"])
    return {"dry_run": False, "snapshot": str(path), "changes": plan}


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "restore":
        confirm = "--yes" in sys.argv
        print(json.dumps(restore(dry_run=not confirm), indent=2)[:8000])
    else:
        p, n_acc, n_con = save()
        print(f"Saved {n_acc} accounts and {n_con} contacts to {p}")
