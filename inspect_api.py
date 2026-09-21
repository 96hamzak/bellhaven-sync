#!/usr/bin/env python3
"""
Shows you exactly what the CRM API sends back.

    python inspect_api.py

Run this whenever something downstream complains about a missing field. It
prints the envelope shape, the field names on a real account and a real
contact, and the distinct values in the fields we care about. Read-only.
"""
import json

import requests

from config import API_BASE, API_TOKEN

HEADERS = {"Authorization": f"Bearer {API_TOKEN}", "Content-Type": "application/json"}


def get(path, **params):
    response = requests.get(f"{API_BASE}{path}", headers=HEADERS, params=params, timeout=20)
    response.raise_for_status()
    return response.json()


def describe(payload, label):
    print(f"\n=== {label} ===")
    if isinstance(payload, list):
        print(f"Envelope   : a bare JSON list of {len(payload)} item(s)")
        items = payload
    elif isinstance(payload, dict):
        print(f"Envelope   : a JSON object with keys {sorted(payload)}")
        items = next(
            (v for v in payload.values() if isinstance(v, list) and v and isinstance(v[0], dict)),
            [],
        )
        for key, value in payload.items():
            if not isinstance(value, (list, dict)):
                print(f"  {key} = {value}")
        print(f"List found : {len(items)} item(s)")
    else:
        print(f"Unexpected type: {type(payload)}")
        return []

    if items:
        first = items[0]
        print(f"\nFields on the first record ({len(first)}):")
        for key in sorted(first):
            value = first[key]
            shown = json.dumps(value, default=str)
            print(f"  {key:<26} {type(value).__name__:<8} {shown[:60]}")
        id_like = [k for k in first if k.lower().endswith("id") or k.lower() == "id"]
        print(f"\nId-looking fields: {id_like or 'NONE FOUND — this is the problem'}")
    return items


def main():
    if not API_TOKEN:
        raise SystemExit("No CRM token. Check that .env exists and is not named .env.txt")

    print("Token check:", json.dumps(get("/me"), indent=2)[:400])

    accounts = describe(get("/accounts", page=1, page_size=5), "GET /accounts (page 1, size 5)")
    contacts = describe(get("/contacts", page=1, page_size=5), "GET /contacts (page 1, size 5)")

    # Does page_size actually work? If it is capped, the pager has to know.
    big = get("/accounts", page=1, page_size=200)
    count = len(big) if isinstance(big, list) else len(
        next((v for v in big.values() if isinstance(v, list)), [])
    )
    print(f"\n=== Paging ===\npage_size=200 returned {count} record(s)")
    if count < 121:
        print("  -> page_size is capped. _paged() in crm_client.py must keep walking pages.")

    if accounts:
        print("\n=== Values worth knowing ===")
        every = get("/accounts", page=1, page_size=200)
        rows = every if isinstance(every, list) else next(
            (v for v in every.values() if isinstance(v, list)), []
        )
        for field in ("status", "care_type"):
            values = sorted({str(r.get(field)) for r in rows if field in r})
            print(f"  {field:<12} {values}")
        parents = [r for r in rows if str(r.get("name", "")).endswith("(Parent Account)")]
        print(f"  parent accounts: {len(parents)}")
        for parent in parents:
            key = next((k for k in ("id", "account_id", "Id") if k in parent), None)
            print(f"    {parent.get(key)}  {parent.get('name')}")

    if contacts:
        first = contacts[0]
        print(f"\n  contact links to its account via: "
              f"{[k for k in first if 'account' in k.lower()] or 'NOT FOUND'}")


if __name__ == "__main__":
    main()
