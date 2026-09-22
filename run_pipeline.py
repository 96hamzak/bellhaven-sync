#!/usr/bin/env python3
"""
The scheduled job. It PROPOSES. It never writes to the CRM.

    python run_pipeline.py

Safe to run as often as you like. The worst it can do is add rows to a local
SQLite file and save a snapshot.
"""
import argparse
import json
import sys
from datetime import date

from config import ACCOUNT_INDEX, AUTO_SNAPSHOT, BUILD, MIN_EXPECTED_LOCATIONS, RAW_DIR, SNAPSHOT_DIR
from bellhaven import snapshot, store
from bellhaven.crm_client import CRMClient
from bellhaven.planner import build_plan
from bellhaven.scraper import scrape_all


def snapshot_status():
    """
    The pipeline does not take snapshots. It only says something when there is
    no restore point at all, because this API has no delete and an approved
    `create` cannot be undone.

    Flip AUTO_SNAPSHOT in config.py if you would rather each run saved one.
    """
    existing = list(SNAPSHOT_DIR.glob("*.json"))
    if AUTO_SNAPSHOT:
        today = date.today().strftime("%Y%m%d")
        if not any(p.name.startswith(f"auto-{today}") for p in existing):
            path, accounts, contacts = snapshot.save(label=f"auto-{today}")
            return f"snapshot saved: {path.name} ({accounts} accounts, {contacts} contacts)"
        return "snapshot: already taken today"
    if not existing:
        return ("NO SNAPSHOT ON FILE. Nothing is written by this command, but take "
                "one before you approve anything:  python -m bellhaven.snapshot")
    return f"snapshots on file: {len(existing)} (most recent {sorted(p.name for p in existing)[-1]})"


def main(skip_scrape=False):
    try:
        notice = store.adopt_legacy_ledger()
    except store.LedgerInUse as exc:
        sys.exit(str(exc))
    if notice:
        print(notice)
    store.init()
    run_id = store.start_run()
    print(f"Run {run_id}  (build {BUILD})")

    cache = RAW_DIR / "locations.json"
    if skip_scrape and cache.exists():
        locations = json.loads(cache.read_text(encoding="utf-8"))
        print(f"Loaded {len(locations)} cached locations (no scrape)")
    else:
        print("Scraping the Bellhaven website...")
        locations = scrape_all()
        cache.write_text(json.dumps(locations, indent=2), encoding="utf-8")

    if len(locations) < MIN_EXPECTED_LOCATIONS:
        store.finish_run(run_id, len(locations), 0, 0, "aborted: too few locations")
        sys.exit(
            f"ABORTED. Only {len(locations)} locations found, expected at least "
            f"{MIN_EXPECTED_LOCATIONS}. The website or the scraper is broken. "
            "Nothing was proposed.")

    flagged = [l for l in locations if l["problems"]]
    if flagged:
        print(f"  {len(flagged)} location(s) with parsing problems:")
        for item in flagged:
            print(f"    - {item['name'] or item['slug']}: {'; '.join(item['problems'])}")

    print("Reading the CRM (read-only)...")
    client = CRMClient()
    accounts = client.list_accounts()
    contacts = client.list_contacts()
    print(f"  {len(accounts)} accounts, {len(contacts)} contacts")

    # id -> name, so the review app can show a parent name without another call
    ACCOUNT_INDEX.write_text(
        json.dumps({a["id"]: a.get("name", "") for a in accounts}, indent=0),
        encoding="utf-8")

    print(f"  {snapshot_status()}")

    print("Matching and planning...")
    proposals, matches, held = build_plan(locations, accounts, contacts)

    new, refreshed, skipped = store.upsert_proposals(run_id, proposals)
    expired = store.expire_stale(run_id)
    store.finish_run(run_id, len(locations), len(accounts), len(proposals))

    by_type = {}
    for proposal in proposals:
        by_type[proposal["type"]] = by_type.get(proposal["type"], 0) + 1

    print("\nGenerated this run:")
    for kind, count in sorted(by_type.items(), key=lambda kv: -kv[1]):
        print(f"  {kind:<18} {count}")
    print(f"\n  new: {new}   already pending: {refreshed}   "
          f"already decided: {skipped}   expired: {expired}")

    unmatched = sum(1 for m in matches.values() if m["primary"] is None)
    ambiguous = sum(1 for m in matches.values() if m["primary"] is None and m["ambiguous"])
    print(f"  locations with no clean match: {unmatched} "
          f"(of which {ambiguous} have a candidate to choose from)")

    if held:
        print(f"\n  {len(held)} facility change(s) are WAITING on a parent account:")
        for item in held[:8]:
            print(f"    - {item['account'].get('name')}: {item['reason']}")
        print("  Approve the 'Create parent account' item, then run this again.")

    print("\nNothing was written to the CRM. Open the review app to decide:")
    print("  python -m uvicorn app:app --reload")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-scrape", action="store_true",
                        help="reuse the last scrape, for fast iteration on matching")
    raise SystemExit(main(**vars(parser.parse_args())))
