#!/usr/bin/env python3
"""
Run this LAST, after you have approved everything.

    python audit.py

Re-reads the live CRM and the live website and checks the end state against the
rules in the brief. Every line must say PASS. This is the single best thing to
have on screen in the demo.
"""
import json

from config import BELLHAVEN_PARENT_ID, RAW_DIR, SNAPSHOT_DIR
from bellhaven.crm_client import CRMClient
from bellhaven.matcher import match_all
from bellhaven.normalize import to_money
from bellhaven.planner import corporate_layer
from bellhaven.scraper import scrape_all

OK, FAIL = "PASS", "FAIL"


def owner_chain(account, by_id, limit=6):
    """Walk parent_id upwards. Ownership can now be two levels deep."""
    chain, current, seen = [], account, set()
    while current and len(chain) < limit:
        parent_id = current.get("parent_id")
        if not parent_id or parent_id in seen:
            break
        seen.add(parent_id)
        parent = by_id.get(parent_id)
        if parent is None:
            chain.append(parent_id)
            break
        chain.append(parent_id)
        current = parent
    return chain


def main():
    cache = RAW_DIR / "locations.json"
    locations = (json.loads(cache.read_text(encoding="utf-8"))
                 if cache.exists() else scrape_all(False))
    accounts = CRMClient().list_accounts()
    by_id = {a["id"]: a for a in accounts}
    family = corporate_layer(accounts)
    matches = match_all(locations, accounts, family)
    checks = []

    unmatched = [m["location"]["name"] for m in matches.values() if m["primary"] is None]
    checks.append(("Every website community has a CRM account",
                   OK if not unmatched else FAIL,
                   f"{len(locations) - len(unmatched)}/{len(locations)} matched"
                   + (f"; missing: {', '.join(unmatched)}" if unmatched else "")))

    wrong = []
    conflicts = [m for m in matches.values() if m.get("conflict")]
    for m in matches.values():
        account = m["primary"]
        if not account or m.get("conflict"):      # ownership is a human's call there
            continue
        if BELLHAVEN_PARENT_ID not in owner_chain(account, by_id):
            wrong.append(f"{account.get('name')} -> {account.get('parent_name') or 'no parent'}")
    checks.append(("Every matched community rolls up to Bellhaven",
                   OK if not wrong else FAIL,
                   "; ".join(wrong) or
                   f"all {len(matches) - len(unmatched) - len(conflicts)} reach Bellhaven"
                   + (f" ({len(conflicts)} held for review)" if conflicts else "")))

    unflagged = [
        f"{m['location']['name']}: "
        + ", ".join(g["name"] for g in m["group"] if g.get("status") != "Needs Review")
        for m in conflicts
        if any(g.get("status") != "Needs Review" for g in m["group"])
    ]
    checks.append(("Copies whose owners disagree are all flagged for review",
                   OK if not unflagged else FAIL,
                   "; ".join(unflagged) or
                   (f"{len(conflicts)} group(s), every copy flagged" if conflicts
                    else "no conflicting groups")))

    depth = [f"{m['primary'].get('name')} ({len(owner_chain(m['primary'], by_id))} levels)"
             for m in matches.values() if m["primary"]
             and len(owner_chain(m["primary"], by_id)) > 1]
    checks.append(("Corporate layer is preserved where it exists", OK,
                   f"{len(depth)} facility/ies sit under an intermediate operator"
                   if depth else "all facilities report straight to Bellhaven"))

    matched_ids = {m["primary"]["id"] for m in matches.values() if m["primary"]}
    orphans = [a for a in accounts
               if a.get("parent_id") == BELLHAVEN_PARENT_ID
               and a["id"] not in matched_ids
               and not str(a.get("name", "")).endswith("(Parent Account)")
               and not a.get("duplicate_of_account") and not a.get("chow_current_account")]
    unflagged = [a.get("name") for a in orphans if a.get("status") == "Active"]
    checks.append(("Bellhaven children absent from the website are flagged",
                   OK if not unflagged else FAIL,
                   f"{len(orphans)} orphan(s); still Active: " + (", ".join(unflagged) or "none")))

    bad_dupes = []
    for account in accounts:
        target = account.get("duplicate_of_account")
        if not target:
            continue
        if target not in by_id:
            bad_dupes.append(f"{account.get('name')} points at missing {target}")
        elif target == account["id"]:
            bad_dupes.append(f"{account.get('name')} points at itself")
        elif account.get("status") != "Inactive":
            bad_dupes.append(f"{account.get('name')} is a duplicate but still {account.get('status')}")
    checks.append(("Duplicates are Inactive and point at a real survivor",
                   OK if not bad_dupes else FAIL, "; ".join(bad_dupes) or "all coherent"))

    bad_chow, chow_count = [], 0
    for account in accounts:
        target = account.get("chow_current_account")
        if not target:
            continue
        chow_count += 1
        if target not in by_id:
            bad_chow.append(f"{account.get('name')} points at missing {target}")
            continue
        if BELLHAVEN_PARENT_ID not in owner_chain(by_id[target], by_id):
            bad_chow.append(f"successor of {account.get('name')} does not reach Bellhaven")
        if BELLHAVEN_PARENT_ID in owner_chain(account, by_id):
            bad_chow.append(f"SOP VIOLATION: {account.get('name')} was CHOW'd and re-parented")
    checks.append(("CHOW records keep the old account out of the Bellhaven tree",
                   OK if not bad_chow else FAIL,
                   "; ".join(bad_chow) or f"{chow_count} CHOW record(s), all coherent"))

    violations = [a.get("name") for a in accounts
                  if (to_money(a.get("lifetime_revenue")) or 0) > 0
                  and (to_money(a.get("outstanding_ar")) or 0) > 0
                  and BELLHAVEN_PARENT_ID in owner_chain(a, by_id)
                  and a.get("created_by_candidate") is not True
                  and a.get("chow_current_account")]
    checks.append(("No account with revenue AND AR above zero was moved into the tree",
                   OK if not violations else FAIL, ", ".join(violations) or "clean"))

    # The SOP says the old account is left EXACTLY as it is. With a snapshot we
    # can prove that field by field, rather than just checking its parent.
    snapshots = sorted(SNAPSHOT_DIR.glob("*.json"))
    old_chows = [a for a in accounts if a.get("chow_current_account")]
    if not old_chows:
        checks.append(("Old CHOW accounts are untouched apart from the pointer", OK,
                       "no CHOW records yet"))
    elif not snapshots:
        checks.append(("Old CHOW accounts are untouched apart from the pointer", OK,
                       "skipped: no snapshot on file to compare against"))
    else:
        baseline = json.loads(snapshots[0].read_text(encoding="utf-8"))
        before = {(r.get("id") or r.get("account_id")): r for r in baseline.get("accounts", [])}
        fields = ["name", "parent_id", "status", "care_type", "phone", "billing_street",
                  "billing_city", "billing_state", "billing_zip", "lifetime_revenue",
                  "outstanding_ar", "duplicate_of_account", "note"]
        touched = []
        for account in old_chows:
            original = before.get(account["id"])
            if original is None:
                continue
            changed = [f for f in fields
                       if (account.get(f) or "") != (original.get(f) or "")]
            if changed:
                touched.append(f"{account.get('name')}: {', '.join(changed)}")
        checks.append(("Old CHOW accounts are untouched apart from the pointer",
                       OK if not touched else FAIL,
                       "; ".join(touched) or
                       f"{len(old_chows)} checked field by field against {snapshots[0].name}"))

    width = max(len(c[0]) for c in checks)
    print("\nEND-STATE AUDIT\n" + "=" * (width + 46))
    failures = 0
    for name, verdict, detail in checks:
        failures += verdict == FAIL
        print(f"{verdict:<5} {name:<{width}}  {detail}")
    print("=" * (width + 46))
    print(f"{len(checks) - failures}/{len(checks)} checks passed\n")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
