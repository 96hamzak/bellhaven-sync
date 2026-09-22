#!/usr/bin/env python3
"""
One-way export of the ledger, for people who are not going to open a terminal.

    python export_ledger.py

Writes data/ledger_export.csv. Drop that into Google Sheets
(File -> Import -> Upload -> Replace current sheet) and a sales ops reviewer can
read every proposal and every decision, with the evidence, and tell you which
ones look wrong.

One way on purpose. The sheet is a window, never a source of truth. Editing a
cell there changes nothing, because correctness depends on the fingerprint
uniqueness and the transaction around the pending-to-executing flip, and a
spreadsheet gives you neither.
"""
import csv
import json

from config import DB_PATH, ROOT
from bellhaven import store

EXPORT_PATH = ROOT / "data" / "ledger_export.csv"

COLUMNS = [
    "proposal_id",
    "status",
    "type",
    "confidence",
    "account_or_location",
    "account_id",
    "what_changes",
    "why",
    "website_listing",
    "match_score",
    "sop_branch",
    "match_deductions",
    "revenue",
    "outstanding_ar",
    "reviewer_note",
    "decided_at",
    "outcome",
]


def _changes(proposal):
    """'Account Name: Old -> New; Parent Account: 001X -> 001Y'"""
    from bellhaven.planner import FIELD_LABELS

    target = proposal.get("target") or {}
    before = proposal.get("before") or {}

    if proposal["type"] == "match_ambiguous":
        options = target.get("_options", [])
        chosen = target.get("_choice")
        picked = next((o for o in options if o["key"] == chosen), None)
        return ("REVIEWER CHOICE between: "
                + " | ".join(o["label"] for o in options)
                + (f"  ->  chose: {picked['label']}" if picked else ""))

    if proposal["type"] == "duplicate_conflict":
        members = (proposal.get("evidence") or {}).get("members", [])
        return ("FLAG ALL as Needs Review, no survivor chosen: " + " | ".join(
            f"{m['name']} ({m.get('parent_label') or m.get('parent_name') or 'no parent'})"
            for m in members))

    if proposal["type"] == "chow":
        new = target.get("_new_account", {})
        return (
            "CREATE new account under Bellhaven: "
            + ", ".join(f"{FIELD_LABELS.get(k, k)}={v}" for k, v in new.items() if k != "note")
            + " | old account: set CHOW Current Account only, nothing else touched"
        )

    parts = []
    for field, new_value in target.items():
        if field.startswith("_"):
            continue
        label = FIELD_LABELS.get(field, field)
        if field == "note":
            parts.append("Note: dated line appended")
            continue
        old = before.get(field)
        old = old if old not in (None, "") else "(empty)"
        parts.append(f"{label}: {old} -> {new_value}")
    return "; ".join(parts)


def _why(proposal):
    reasons = {
        "reparent": "Listed on the Bellhaven website but parented elsewhere.",
        "chow": "Parent change blocked by the SOP: revenue and outstanding AR are both above zero.",
        "update_fields": "Website values differ from the CRM.",
        "create_account": "On the website with no CRM account close enough to link.",
        "match_ambiguous": "A CRM account looks close but is not a clean match; a reviewer picks.",
        "duplicate_conflict": "Several copies of one building sit under different parents; no one can tell who owns it.",
        "create_parent": "The corporate layer needs an account that does not exist yet.",
        "nest_parent": "This operator was acquired by Bellhaven, so it becomes a child of Bellhaven.",
        "rename_parent": "Renamed so the independent half is not confused with the acquired half.",
        "mark_duplicate": "Two accounts resolve to the same building.",
        "orphan_review": "Under Bellhaven but absent from the website.",
        "create_contact": "Website names an administrator the CRM has no contact for.",
        "update_contact": "Administrator name differs from the CRM contact.",
        "needs_human": "Blocked: a money field could not be read as a number.",
    }
    return reasons.get(proposal["type"], proposal["type"])


def build_rows():
    rows = []
    for proposal in store.list_proposals():
        evidence = proposal.get("evidence") or {}
        website = evidence.get("website") or {}
        sop = evidence.get("sop") or {}
        result = proposal.get("result") or {}

        rows.append(
            {
                "proposal_id": proposal["id"],
                "status": proposal["status"],
                "type": proposal["type"].replace("_", " "),
                "confidence": proposal.get("tier"),
                "account_or_location": proposal.get("subject_label"),
                "account_id": proposal.get("subject_id"),
                "what_changes": _changes(proposal),
                "why": _why(proposal),
                "website_listing": website.get("url", ""),
                "match_score": evidence.get("match_score", ""),
                "sop_branch": sop.get("branch", ""),
                "match_deductions": "; ".join(evidence.get("deductions", [])),
                "revenue": sop.get("lifetime_revenue", ""),
                "outstanding_ar": sop.get("outstanding_ar", ""),
                "reviewer_note": proposal.get("reviewer_note") or "",
                "decided_at": proposal.get("decided_at") or "",
                "outcome": json.dumps(result, default=str)[:300] if result else "",
            }
        )
    return rows


def export():
    store.adopt_legacy_ledger()
    store.init()
    rows = build_rows()
    EXPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(EXPORT_PATH, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    return EXPORT_PATH, len(rows)


if __name__ == "__main__":
    _ = DB_PATH
    path, count = export()
    print(f"Exported {count} proposal(s) to {path}")
    print("\nTo view it in Google Sheets:")
    print("  1. Open a new sheet at https://sheets.new")
    print("  2. File -> Import -> Upload -> drag in the CSV")
    print("  3. Choose 'Replace current sheet', then Import data")
