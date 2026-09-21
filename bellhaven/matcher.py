"""
Deciding which CRM account is which website community.

Address first, name and phone as corroboration, and a two-level veto.

WHY TWO LEVELS. The first version had one kind of veto and it cost us a real
match. The website's "Bellhaven at Union Square" sits at 118 <street>; the CRM's
"Union Square Senior Living" has a stale street number at the same city, state
and zip. A single hard veto on "street number differs" zeroed that score, which
pushed the right account below three irrelevant ones and produced a "no account
exists, create one" proposal. That would have created a duplicate of a record
that was sitting right there.

So:

  HARD veto   provably a different building. Different state; or different zip
              AND different city. Scored 0, never matched, still listed as a
              rejected candidate with the reason.

  SOFT veto   suspicious but survivable. A different street number, or street
              text that barely resembles. Keeps its score, is capped below the
              confident threshold, and is offered to the reviewer as
              "did you mean this account?" alongside the option to create.

The traps this has to survive, all real in this dataset:
  * "Amberly Manor" exists in Hudson OH and in Colorado Springs CO, under two
    different operators.
  * "Bellhaven of Carlisle" and "Bellhaven of New Carlisle" are different towns.
  * "Bellhaven of Marion" and "Bellhaven of Marietta" are different towns.
  * Four facilities are renamed between the CRM and the website.
"""
from rapidfuzz import fuzz

from config import AMBIGUOUS_SCORE, BLANK_PARENT_CONFLICTS, CONFIDENT_SCORE, PROBABLE_SCORE
from bellhaven.normalize import (
    norm_city,
    norm_name,
    norm_phone,
    norm_state,
    norm_street,
    norm_zip,
    street_number,
)

WEIGHTS = {"street": 50, "zip": 20, "city": 10, "phone": 10, "name": 10}


def account_view(account):
    return {
        "id": account.get("id"),
        "name": account.get("name") or "",
        "street": account.get("billing_street") or "",
        "city": account.get("billing_city") or "",
        "state": account.get("billing_state") or "",
        "zip": account.get("billing_zip") or "",
        "phone": account.get("phone") or "",
        "n_street": norm_street(account.get("billing_street")),
        "n_number": street_number(account.get("billing_street")),
        "n_city": norm_city(account.get("billing_city")),
        "n_state": norm_state(account.get("billing_state")),
        "n_zip": norm_zip(account.get("billing_zip")),
        "n_phone": norm_phone(account.get("phone")),
        "n_name": norm_name(account.get("name")),
    }


def location_view(location):
    return {
        "n_street": norm_street(location["street"]),
        "n_number": street_number(location["street"]),
        "n_city": norm_city(location["city"]),
        "n_state": norm_state(location["state"]),
        "n_zip": norm_zip(location["zip"]),
        "n_phone": norm_phone(location["phone"]),
        "n_name": norm_name(location["name"]),
    }


def hard_veto(loc, acct):
    """Provably a different building."""
    reasons = []
    if loc["n_state"] and acct["n_state"] and loc["n_state"] != acct["n_state"]:
        reasons.append(f"different state ({acct['n_state']}, site says {loc['n_state']})")
    zip_differs = loc["n_zip"] and acct["n_zip"] and loc["n_zip"] != acct["n_zip"]
    city_differs = loc["n_city"] and acct["n_city"] and loc["n_city"] != acct["n_city"]
    if zip_differs and city_differs:
        reasons.append(f"different city and zip ({acct['city']} {acct['zip']})")
    return reasons


def soft_veto(loc, acct):
    """Suspicious, but the same building often survives it."""
    reasons = []
    if loc["n_number"] and acct["n_number"] and loc["n_number"] != acct["n_number"]:
        reasons.append(
            f"street number differs (CRM {acct['n_number']}, site {loc['n_number']})"
        )
    if loc["n_street"] and acct["n_street"]:
        similarity = fuzz.token_sort_ratio(loc["n_street"], acct["n_street"]) / 100
        if similarity < 0.55:
            reasons.append(f"street text barely matches (CRM {acct['street'] or 'blank'})")
    elif not acct["n_street"]:
        reasons.append("no street on the CRM record")
    return reasons


def score_pair(loc, acct):
    """
    Returns (score, deductions).

    Deductions, not matches. A reviewer does not need to be told that the zip
    matched; they need to know why it is not 100.
    """
    earned, deductions = {}, []

    # ---- street
    if loc["n_street"] and acct["n_street"]:
        if loc["n_street"] == acct["n_street"]:
            earned["street"] = WEIGHTS["street"]
        else:
            similarity = fuzz.token_sort_ratio(loc["n_street"], acct["n_street"]) / 100
            earned["street"] = WEIGHTS["street"] * similarity
            deductions.append(
                f"street text differs, CRM has {acct['street'] or 'blank'} "
                f"(-{WEIGHTS['street'] - earned['street']:.0f})"
            )
    else:
        earned["street"] = 0
        deductions.append(f"no street to compare (-{WEIGHTS['street']})")

    # ---- zip / city / phone: exact or nothing
    for field, label in (("zip", "zip"), ("city", "city"), ("phone", "phone")):
        key = f"n_{field}"
        weight = WEIGHTS[field]
        if loc[key] and loc[key] == acct[key]:
            earned[field] = weight
        elif not acct[key]:
            earned[field] = 0
            deductions.append(f"{label} not on file in CRM (-{weight})")
        elif not loc[key]:
            earned[field] = 0
            deductions.append(f"{label} not listed on the website (-{weight})")
        else:
            earned[field] = 0
            deductions.append(f"{label} differs, CRM has {acct[field]} (-{weight})")

    # ---- name
    if loc["n_name"] and acct["n_name"]:
        similarity = fuzz.token_set_ratio(loc["n_name"], acct["n_name"]) / 100
        earned["name"] = WEIGHTS["name"] * similarity
        if similarity < 0.999:
            deductions.append(
                f"name differs, CRM has {acct['name']} "
                f"(-{WEIGHTS['name'] - earned['name']:.0f})"
            )
    else:
        earned["name"] = 0
        deductions.append(f"no name to compare (-{WEIGHTS['name']})")

    score = round(min(sum(earned.values()), 100.0), 1)
    return score, [d for d in deductions if not d.endswith("(-0)")]


def tier(score):
    if score >= CONFIDENT_SCORE:
        return "confident"
    if score >= PROBABLE_SCORE:
        return "probable"
    return "weak"


def _completeness(account):
    fields = ("billing_street", "billing_city", "billing_state", "billing_zip",
              "phone", "care_type")
    return sum(1 for f in fields if account.get(f))


def _survivor_rank(entry):
    """
    Copies of one building, all under the SAME parent: which one should live?

      1. billing history   finance already references that record
      2. contacts          sales relationships are attached to it
      3. in the family     only differs when a blank parent is allowed to count
      4. Active
      5. closest to the website, by raw match score
      6. most complete
      7. lowest id         deterministic, and only ever the last resort

    Before this, a tie on the first four fell straight through to the account
    id, which is how Monroe Gardens Care Center "won" over Bellhaven Gardens of
    Monroe: 00159... sorts before 001U1....
    """
    account = entry["account"]
    has_billing = (account.get("_revenue") or 0) > 0 or (account.get("_ar") or 0) > 0
    return (
        0 if has_billing else 1,
        0 if account.get("_contacts") else 1,
        0 if account.get("_in_family") else 1,
        0 if account.get("status") == "Active" else 1,
        -entry["raw"],
        -_completeness(account),
        str(account.get("id")),
    )


def parents_disagree(accounts):
    """More than one parent among copies of one building = nobody can say who
    owns it. That decision belongs to a human, not a tiebreaker."""
    parents = {(a.get("parent_id") or "") for a in accounts}
    if not BLANK_PARENT_CONFLICTS:
        parents.discard("")
    return len(accounts) > 1 and len(parents) > 1


def is_parent_shell(account):
    return str(account.get("name", "")).strip().endswith("(Parent Account)")


def match_all(locations, accounts, family_ids, contact_counts=None):
    """
    Score every location against every non-shell account.

    35 locations x ~120 accounts is about 4,200 comparisons, which takes
    milliseconds. There is no need to be clever about blocking.

    Each result carries:
      primary      the clean match, or None
      ambiguous    soft-vetoed candidates worth offering to a reviewer
      duplicates   other clean matches for the same building
      rejected     the closest hard-vetoed records, with the reason
    """
    from bellhaven.normalize import to_money

    candidates = []
    for account in accounts:
        if is_parent_shell(account):
            continue
        if account.get("duplicate_of_account") or account.get("chow_current_account"):
            continue
        enriched = dict(account)
        enriched["_revenue"] = to_money(account.get("lifetime_revenue")) or 0
        enriched["_ar"] = to_money(account.get("outstanding_ar")) or 0
        enriched["_in_family"] = account.get("parent_id") in family_ids
        enriched["_contacts"] = (contact_counts or {}).get(account["id"], 0)
        candidates.append(enriched)

    results = {}
    for location in locations:
        loc = location_view(location)
        scored = []
        for account in candidates:
            acct = account_view(account)
            hard = hard_veto(loc, acct)
            soft = [] if hard else soft_veto(loc, acct)
            raw, deductions = score_pair(loc, acct)
            # A soft veto cannot reach the confident tier on its own.
            effective = 0.0 if hard else min(raw, CONFIDENT_SCORE - 1) if soft else raw
            scored.append(
                {
                    "account": account,
                    "raw": raw,
                    "score": effective,
                    "deductions": deductions,
                    "hard": hard,
                    "soft": soft,
                }
            )

        # Ranking is by RAW score throughout. The earlier bug was sorting by a
        # vetoed score of 0, which made "nearest" an arbitrary list.
        scored.sort(key=lambda s: (-s["raw"], str(s["account"]["id"])))

        clean = [s for s in scored if not s["hard"] and not s["soft"]
                 and s["score"] >= PROBABLE_SCORE]
        # Every clean match is the same building, so the survivor is chosen
        # across ALL of them by the rank above, not just among the top score.
        if clean:
            primary = min(clean, key=_survivor_rank)
            duplicates = [s["account"] for s in clean
                          if s["account"]["id"] != primary["account"]["id"]]
        else:
            primary, duplicates = None, []
        conflict = parents_disagree([s["account"] for s in clean])

        ambiguous = [
            s for s in scored
            if s["soft"] and not s["hard"] and s["raw"] >= AMBIGUOUS_SCORE
        ][:4]

        results[location["location_key"]] = {
            "location": location,
            "primary": primary["account"] if primary else None,
            "score": primary["score"] if primary else 0.0,
            "tier": tier(primary["score"]) if primary else "none",
            "deductions": primary["deductions"] if primary else [],
            "duplicates": duplicates,
            "conflict": conflict,
            "group": [_candidate_row(s) for s in clean] if conflict else [],
            "ambiguous": [_candidate_row(s) for s in ambiguous],
            "rejected": [_candidate_row(s) for s in scored[:4]
                         if s["hard"] or s["raw"] < AMBIGUOUS_SCORE][:3],
        }
    return results


def _candidate_row(entry):
    account = entry["account"]
    return {
        "id": account["id"],
        "name": account.get("name"),
        "street": account.get("billing_street"),
        "city": account.get("billing_city"),
        "state": account.get("billing_state"),
        "zip": account.get("billing_zip"),
        "phone": account.get("phone"),
        "parent_id": account.get("parent_id"),
        "parent_name": account.get("parent_name"),
        "status": account.get("status"),
        "lifetime_revenue": account.get("lifetime_revenue"),
        "outstanding_ar": account.get("outstanding_ar"),
        "score": entry["raw"],
        "deductions": entry["deductions"],
        "blocked_by": entry["hard"] or entry["soft"],
    }
