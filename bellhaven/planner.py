"""
Turning "this account matches this community" into "here is exactly what I
propose to change, and here is why".

Nothing in this file touches the network. It takes scraped locations plus a
snapshot of the CRM and returns a list of proposal dictionaries. That makes it
completely testable offline, and it is why a scheduled run can never write.

THE HIERARCHY MODEL. The brief says outreach often goes through the corporate
office rather than the facility, so the corporate layer has to stay visible.
The website cannot tell you which community came from Harborview and which from
Cedar Trail, so the CRM's existing parent links are treated as the source of
truth for membership, and only ownership above them is corrected:

    under Harborview  ->  leave the facility alone; Harborview moves to Bellhaven
    under Cedar Trail ->  move to a new "Cedar Trail - Bellhaven" shell
    anywhere else     ->  Bellhaven directly

Order of operations, and it matters:

  1. Hierarchy       (nest Harborview, create the Cedar Trail - Bellhaven shell)
  2. Matched         (field fixes, re-parenting, CHOW)
  3. Ambiguous       (one good-but-imperfect candidate: link it or create)
  4. Duplicates      (after matching, so the survivor is known)
  5. Missing         (creates)
  6. Contacts        (never on a CHOW'd old account: the SOP says leave it be)
  7. Orphans         (LAST, so a rename reads as a rename, not orphan + create)
"""
from datetime import date

from config import (
    ADMIN_TITLES,
    BELLHAVEN_PARENT_ID,
    CARE_TYPE_PRIORITY,
    CEDAR_BELLHAVEN_NAME,
    CEDAR_INDEPENDENT_NAME,
    CEDAR_TRAIL_PARENT_ID,
    KEEP_CURRENT_PARENT_IDS,
    OFFERING_TO_CARE_TYPE,
    ORPHAN_SCOPE_PARENT_IDS,
    PARENTS_TO_NEST_UNDER_BELLHAVEN,
    SPLIT_PARENTS,
    STATUS_ACTIVE,
    STATUS_INACTIVE,
    STATUS_NEEDS_REVIEW,
)
from bellhaven.normalize import norm_phone, norm_text, to_money, usd

TODAY = date.today().isoformat()
FIELD_LABELS = {
    "name": "Account Name",
    "billing_street": "Billing Street",
    "billing_city": "Billing City",
    "billing_state": "Billing State",
    "billing_zip": "Billing Zip",
    "phone": "Phone",
    "care_type": "Care Type",
    "parent_id": "Parent Account",
    "status": "Status",
    "note": "Note",
    "duplicate_of_account": "Duplicate Of",
    "chow_current_account": "CHOW Current Account",
    "title": "Title",
    "email": "Email",
    "is_active": "Active",
    "account_id": "Account",
}
WEBSITE_FIELDS = ("name", "billing_street", "billing_city", "billing_state",
                  "billing_zip", "phone")


# --------------------------------------------------------------- utilities
def note_line(text):
    return f"[bellhaven-sync {TODAY}] {text}"


def append_note(existing, text):
    """One free-text field, so add a dated line rather than overwrite a human."""
    line = note_line(text)
    existing = (existing or "").strip()
    if line in existing:
        return existing
    return f"{existing}\n{line}".strip()


def care_type_from_offerings(offerings):
    mapped = []
    for offering in offerings or []:
        value = OFFERING_TO_CARE_TYPE.get(norm_text(offering).replace(" and ", " & "))
        if value is None:
            value = OFFERING_TO_CARE_TYPE.get(str(offering).strip().lower())
        if value:
            mapped.append(value)
    mapped = list(dict.fromkeys(mapped))
    if not mapped:
        return None, []
    return max(mapped, key=lambda v: CARE_TYPE_PRIORITY.get(v, 0)), mapped


def sop_requires_chow(account):
    """Revenue AND outstanding AR above zero. Knowable before the target parent
    exists, which is what lets a held account be protected in the meantime."""
    revenue = to_money(account.get("lifetime_revenue")) or 0
    ar = to_money(account.get("outstanding_ar")) or 0
    return revenue > 0 and ar > 0


def find_by_name(accounts, name):
    target = norm_text(name)
    for account in accounts:
        if norm_text(account.get("name")) == target:
            return account
    return None


def corporate_layer(accounts):
    """Parent shells that represent Bellhaven ownership."""
    ids = {BELLHAVEN_PARENT_ID, *PARENTS_TO_NEST_UNDER_BELLHAVEN}
    for name in SPLIT_PARENTS.values():
        shell = find_by_name(accounts, name)
        if shell:
            ids.add(shell["id"])
    return ids


def route_parent(account, split_shell_ids):
    """
    Where should this facility's parent point, and why?
    Returns (target_parent_id or None if blocked, human explanation).
    """
    current = account.get("parent_id") or ""
    # Already in the acquired half of a split parent: that IS its correct home.
    # Without this, the run after a facility is routed here would propose
    # pulling it straight up to Bellhaven, erasing the corporate office.
    if current and current in split_shell_ids.values():
        return current, (
            f"already under {account.get('parent_name') or current}, which sits "
            "under Bellhaven; it stays there"
        )
    if current in KEEP_CURRENT_PARENT_IDS:
        return current, (
            f"stays under {account.get('parent_name') or current}, which is itself "
            "being re-parented to Bellhaven"
        )
    if current in SPLIT_PARENTS:
        shell_id = split_shell_ids.get(current)
        if not shell_id:
            return None, "waiting on the Cedar Trail - Bellhaven parent account"
        return shell_id, (
            "listed on the Bellhaven website, so it is one of the Cedar Trail "
            "communities that joined Bellhaven"
        )
    return BELLHAVEN_PARENT_ID, "listed on the Bellhaven website"


def _proposal(kind, subject_id, subject_label, before, target, evidence,
              tier="probable", editable=None, care_type_options=None,
              account_id=None, subject_kind="account"):
    return {
        "type": kind,
        "subject_id": subject_id,
        "subject_label": subject_label,
        "subject_kind": subject_kind,
        "account_id": account_id if account_id is not None else subject_id,
        "before": before,
        "target": target,
        "evidence": evidence,
        "tier": tier,
        "editable": editable or [k for k in target if not k.startswith("_")],
        "care_type_options": care_type_options or [],
    }


def _website_patch(location):
    return {
        "name": location["name"],
        "billing_street": location["street"],
        "billing_city": location["city"],
        "billing_state": location["state"],
        "billing_zip": location["zip"],
        "phone": location["phone"],
    }


def _new_account_body(location, parent_id, note):
    care_type, _ = care_type_from_offerings(location["offerings"])
    body = _website_patch(location)
    body.update({
        "parent_id": parent_id,
        "status": STATUS_ACTIVE,
        "care_type": care_type,
        "note": note,
    })
    return body


def _admin_contact_body(location):
    admin = (location.get("administrator") or "").strip()
    if not admin:
        return None
    return {
        "name": admin,
        "title": "Administrator",
        "is_active": True,
        "phone": location.get("phone") or "",
    }


# ------------------------------------------------------------ 1. hierarchy
def plan_hierarchy(accounts):
    """
    Two corporate-layer corrections, plus the Cedar Trail split.

    Harborview: the whole family joined Bellhaven in 2025, so the shell becomes
    a child of Bellhaven and its facilities ride along untouched.

    Cedar Trail: only SELECT communities joined in 2026, so one record cannot
    honestly represent both halves. We create a second shell under Bellhaven for
    the acquired half and rename the original to say plainly that it is not
    Bellhaven's. Facilities are routed to whichever half they belong to.
    """
    out = []
    by_id = {a["id"]: a for a in accounts}

    for parent_id in PARENTS_TO_NEST_UNDER_BELLHAVEN:
        parent = by_id.get(parent_id)
        if not parent or parent.get("parent_id") == BELLHAVEN_PARENT_ID:
            continue
        target = {
            "parent_id": BELLHAVEN_PARENT_ID,
            "note": append_note(
                parent.get("note"),
                "About page: the Harborview family of communities joined Bellhaven "
                "in 2025. Recording Bellhaven as the corporate parent; the "
                "facilities below keep reporting to Harborview.",
            ),
        }
        out.append(_proposal(
            "nest_parent", parent_id, parent.get("name", parent_id),
            {"parent_id": parent.get("parent_id") or "(none)", "note": parent.get("note")},
            target,
            {"source": "About page",
             "quote": "In 2025 we welcomed the Harborview Care Group family of communities.",
             "effect": "Outreach for these facilities now routes through Bellhaven "
                       "corporate, while the Harborview office stays visible."},
            tier="manual", editable=["note"],
        ))

    for original_id, new_name in SPLIT_PARENTS.items():
        original = by_id.get(original_id)
        if not original:
            continue
        if find_by_name(accounts, new_name) is None:
            body = {
                "name": new_name,
                "parent_id": BELLHAVEN_PARENT_ID,
                "status": STATUS_ACTIVE,
                "note": note_line(
                    "About page: select Cedar Trail communities joined Bellhaven in "
                    "2026. This shell holds that acquired half. Cedar Trail "
                    "communities absent from the Bellhaven website stay under the "
                    "original, independent Cedar Trail record."
                ),
            }
            out.append(_proposal(
                "create_parent", f"new::{new_name}", new_name, {}, body,
                {"source": "About page",
                 "quote": "in 2026 we expanded further with select communities joining us from Cedar Trail.",
                 "effect": "Approve this FIRST, then re-run the pipeline. Cedar Trail "
                           "facilities cannot be routed until this account exists."},
                tier="manual",
            ))

        if original.get("name") != CEDAR_INDEPENDENT_NAME:
            target = {
                "name": CEDAR_INDEPENDENT_NAME,
                "note": append_note(
                    original.get("note"),
                    "Renamed to separate the independent Cedar Trail communities from "
                    f"those acquired by Bellhaven, which now sit under {new_name}.",
                ),
            }
            out.append(_proposal(
                "rename_parent", original_id, original.get("name", original_id),
                {"name": original.get("name"), "note": original.get("note")},
                target,
                {"effect": "Makes it unambiguous which Cedar Trail communities a rep "
                           "should approach through Bellhaven corporate.",
                 "optional": "Reject this if you would rather leave the original name alone; "
                             "nothing else depends on it."},
                tier="manual",
            ))
    return out


# ------------------------------------------------- 2. matched locations
def plan_field_fixes(match):
    account, location = match["primary"], match["location"]
    before, target = {}, {}

    for field, website_value in _website_patch(location).items():
        current = (account.get(field) or "").strip()
        if field == "phone":
            if website_value and norm_phone(website_value) != norm_phone(current):
                before[field], target[field] = current, website_value
            continue
        if website_value and current != website_value:
            before[field], target[field] = current, website_value

    preferred, all_mapped = care_type_from_offerings(location["offerings"])
    if preferred and (account.get("care_type") or "") not in all_mapped:
        before["care_type"] = account.get("care_type") or "(blank)"
        target["care_type"] = preferred

    if not target:
        return None

    changed = ", ".join(FIELD_LABELS.get(f, f) for f in target)
    target["note"] = append_note(
        account.get("note"), f"Synced from {location['url']}: {changed}."
    )
    return _proposal(
        "update_fields", account["id"], account.get("name", account["id"]),
        before, target,
        {"website": location,
         "match_score": match["score"],
         "deductions": match["deductions"],
         "offerings_on_site": location["offerings"],
         "care_types_mapped": all_mapped},
        tier=match["tier"],
        editable=[f for f in target if f != "note"] + ["note"],
        care_type_options=all_mapped if "care_type" in target else [],
    )


def plan_ownership(match, split_shell_ids):
    """
    The SOP. The brief says this matters as much as the matching.

        revenue > 0 AND outstanding AR > 0  ->  CHOW: preserve the old account
        anything else                       ->  re-parent in place
    """
    account, location = match["primary"], match["location"]
    target_parent, why = route_parent(account, split_shell_ids)

    if target_parent is None:
        return {"_held": True, "account": account, "location": location, "reason": why}
    if account.get("parent_id") == target_parent:
        return None

    revenue = to_money(account.get("lifetime_revenue"))
    ar = to_money(account.get("outstanding_ar"))
    unparseable = revenue is None or ar is None
    revenue, ar = revenue or 0.0, ar or 0.0

    sop = {
        "lifetime_revenue": account.get("lifetime_revenue"),
        "outstanding_ar": account.get("outstanding_ar"),
        "current_parent_id": account.get("parent_id") or "(none)",
        "current_parent_name": account.get("parent_name") or "(none)",
        "target_parent_id": target_parent,
        "routing": why,
    }
    evidence = {"website": location, "match_score": match["score"],
                "deductions": match["deductions"], "sop": sop}

    if unparseable:
        sop["branch"] = "Blocked: a money field could not be read as a number."
        return _proposal("needs_human", account["id"], account.get("name", ""),
                         {"parent_id": account.get("parent_id")}, {}, evidence,
                         tier="manual", editable=[])

    if revenue > 0 and ar > 0:
        sop["branch"] = ("CHOW. Revenue and outstanding AR are both above zero, so "
                         "billing needs the old account preserved exactly as it is.")
        note = note_line(
            f"Change of ownership. Successor to account {account['id']} "
            f"({account.get('name')}), which retains billing history "
            f"(revenue {usd(account.get('lifetime_revenue'))}, AR {usd(account.get('outstanding_ar'))})."
        )
        target = {"_new_account": _new_account_body(location, target_parent, note)}
        contact = _admin_contact_body(location)
        if contact:
            target["_new_contact"] = contact
            sop["contact_note"] = (
                f"The website lists {contact['name']} as administrator. That contact "
                "goes on the NEW account only. The SOP says leave the old account "
                "exactly as it is, so nothing is added to it."
            )
        return _proposal("chow", account["id"], account.get("name", account["id"]),
                         {"parent_id": account.get("parent_id") or "(none)",
                          "chow_current_account": account.get("chow_current_account") or ""},
                         target, evidence, tier=match["tier"], editable=["_new_account"])

    sop["branch"] = (f"Re-parent in place. Revenue {usd(revenue)}, AR {usd(ar)}; the SOP only "
                     "preserves the old account when BOTH are above zero.")
    target = {
        "parent_id": target_parent,
        "note": append_note(account.get("note"), f"Re-parented: {why}. "
                            f"SOP check: revenue {usd(revenue)}, AR {usd(ar)}."),
    }
    return _proposal("reparent", account["id"], account.get("name", account["id"]),
                     {"parent_id": account.get("parent_id") or "(none)",
                      "note": account.get("note")},
                     target, evidence, tier=match["tier"], editable=["note"])


# ------------------------------------------------------------ 3. ambiguous
def plan_ambiguous(match, split_shell_ids):
    """
    One or more accounts look like this building but something is off, usually a
    stale street number. Rather than silently creating a duplicate, put the
    choice in front of a reviewer: link one of these, or create a new account.

    This exists because the first version proposed creating
    "Bellhaven at Union Square" while "Union Square Senior Living" sat in the
    CRM at the same city, state and zip.
    """
    location = match["location"]
    options = []

    for candidate in match["ambiguous"]:
        revenue = to_money(candidate.get("lifetime_revenue")) or 0
        ar = to_money(candidate.get("outstanding_ar")) or 0
        fake_account = {"id": candidate["id"], "parent_id": candidate.get("parent_id"),
                        "parent_name": candidate.get("parent_name")}
        target_parent, why = route_parent(fake_account, split_shell_ids)
        chow = revenue > 0 and ar > 0 and target_parent not in (None, candidate.get("parent_id"))

        option = {
            "key": candidate["id"],
            "kind": "chow" if chow else "link",
            "label": f"{candidate['name']} - {candidate.get('city')}, {candidate.get('state')}",
            "candidate": candidate,
            "routing": why,
            "sop": ("CHOW: this account has revenue and AR above zero, so it is "
                    "preserved and a successor is created."
                    if chow else
                    f"Re-parent in place (revenue {usd(revenue)}, AR {usd(ar)})."),
        }
        if chow:
            option["new_account"] = _new_account_body(
                location, target_parent,
                note_line(f"Change of ownership. Successor to {candidate['id']}."))
            option["new_contact"] = _admin_contact_body(location)
        else:
            patch = _website_patch(location)
            if target_parent and target_parent != candidate.get("parent_id"):
                patch["parent_id"] = target_parent
            care_type, _ = care_type_from_offerings(location["offerings"])
            if care_type and candidate.get("care_type") != care_type:
                patch["care_type"] = care_type
            patch["note"] = note_line(
                f"Linked to {location['url']} after reviewer confirmation. "
                f"Address and name refreshed from the website.")
            option["patch"] = patch
        options.append(option)

    options.append({
        "key": "__create__",
        "kind": "create",
        "label": f"None of these - create a new account for {location['name']}",
        "candidate": None,
        "routing": "listed on the Bellhaven website",
        "sop": "New accounts carry no billing history, so the SOP does not apply.",
        "new_account": _new_account_body(
            location, BELLHAVEN_PARENT_ID,
            note_line(f"Created from {location['url']}. Care offerings listed: "
                      f"{', '.join(location['offerings']) or 'none'}.")),
        "new_contact": _admin_contact_body(location),
    })

    return _proposal(
        "match_ambiguous", location["location_key"], location["name"],
        {}, {"_options": options, "_choice": options[0]["key"]},
        {"website": location,
         "why": "At least one CRM account is close but not a clean match. Pick one, "
                "or create a new account.",
         "rejected": match["rejected"]},
        tier="manual", editable=[],
        account_id=options[0]["key"] if options[0]["key"] != "__create__" else None,
        subject_kind="location",
    )


# ------------------------------------------------------------- 4. duplicates
def plan_duplicates(match):
    out = []
    survivor = match["primary"]
    for loser in match["duplicates"]:
        if loser.get("duplicate_of_account"):
            continue
        revenue = to_money(loser.get("lifetime_revenue")) or 0
        ar = to_money(loser.get("outstanding_ar")) or 0
        survivor_billing = ((to_money(survivor.get("lifetime_revenue")) or 0) > 0
                            or (to_money(survivor.get("outstanding_ar")) or 0) > 0)
        both_have_billing = (revenue > 0 or ar > 0) and survivor_billing
        target = {
            "duplicate_of_account": survivor["id"],
            "status": STATUS_INACTIVE,
            "note": append_note(
                loser.get("note"),
                f"Duplicate of {survivor['id']} ({survivor.get('name')}), same building "
                f"at {match['location']['street']}, {match['location']['city']}. "
                "Marked Inactive; this API has no merge or delete."),
        }
        out.append(_proposal(
            "mark_duplicate", loser["id"], loser.get("name", loser["id"]),
            {"duplicate_of_account": loser.get("duplicate_of_account") or "(none)",
             "status": loser.get("status")},
            target,
            {"website": match["location"],
             "survivor": {"id": survivor["id"], "name": survivor.get("name"),
                          "lifetime_revenue": survivor.get("lifetime_revenue"),
                          "outstanding_ar": survivor.get("outstanding_ar"),
                          "status": survivor.get("status")},
             "loser_billing": {"lifetime_revenue": loser.get("lifetime_revenue"),
                               "outstanding_ar": loser.get("outstanding_ar")},
             "both_have_billing": both_have_billing,
             "survivor_rule": "billing history, then already in the Bellhaven family, "
                              "then Active, then most complete, then lowest id"},
            tier="manual" if both_have_billing else match["tier"],
            editable=["note", "status"]))
    return out


# --------------------------------------------------- 4b. owners disagree
def plan_duplicate_conflict(match, contacts_by_account, names=None):
    """
    Several copies of one building, sitting under DIFFERENT parents.

    Real example from this CRM: 750 Stewart Rd, Monroe has three accounts, one
    under Bellhaven, one under Harborview, one under Cedar Trail, each with a
    different phone number, and a website administrator that matches none of
    their contacts. Nothing in the data says which operator really owns it, and
    ownership is the one thing the sales team needs to be right.

    So the pipeline makes no decision at all: no survivor, no re-parenting, no
    field fixes, no contacts. Every copy is flagged Needs Review with a note
    naming the others. Once a human marks the wrong copies as duplicates, the
    next run sees one account and carries on normally from there.
    """
    location = match["location"]
    names = names or {}
    group = []
    for m in sorted(match["group"], key=lambda m: m["id"]):
        # Say "no parent" only when there genuinely is none. A missing name with
        # a real parent id resolves to the name, or at worst shows the id.
        parent_id = m.get("parent_id") or ""
        label = (m.get("parent_name") or names.get(parent_id) or parent_id) if parent_id else "no parent"
        group.append({**m, "parent_label": label})
    to_flag = [m for m in group if m.get("status") != STATUS_NEEDS_REVIEW]
    if not to_flag:
        return None                       # already flagged; waiting on a human

    listing = "; ".join(f"{m['id']} {m['name']} (parent: {m['parent_label']})" for m in group)
    note = note_line(
        f"{len(group)} accounts share {location['street']}, {location['city']} under "
        f"different parents: {listing}. The website lists it as {location['name']}, "
        f"administrator {location.get('administrator') or 'not listed'}, phone "
        f"{location.get('phone') or 'not listed'}. Confirm which record is real and "
        "which operator owns it, then mark the others as duplicates of it.")

    members = []
    for m in group:
        attached = contacts_by_account.get(m["id"], [])
        members.append({**m, "contacts": [
            f"{c.get('name')} ({c.get('title') or 'no title'})" for c in attached]})

    return _proposal(
        "duplicate_conflict", location["location_key"],
        f"{location['name']} ({len(group)} accounts)",
        {"status": ", ".join(sorted({m.get("status") or "(blank)" for m in to_flag}))},
        {"status": STATUS_NEEDS_REVIEW, "note": note,
         "_members": [{"id": m["id"], "parent_id": m.get("parent_id") or ""}
                      for m in to_flag]},
        {"website": location, "members": members,
         "why": f"These {len(group)} accounts are the same building but sit under "
                "different parents. There is no reliable way to tell which operator "
                "owns it, so nothing is merged, moved or updated. All of them are "
                "flagged for a person to decide."},
        tier="manual", editable=["status", "note"], account_id=None,
        subject_kind="location")


# ----------------------------------------------------------------- 5. creates
def plan_create(match):
    location = match["location"]
    _, mapped = care_type_from_offerings(location["offerings"])
    body = _new_account_body(
        location, BELLHAVEN_PARENT_ID,
        note_line(f"Created from {location['url']}. Care offerings listed on the "
                  f"website: {', '.join(location['offerings']) or 'none listed'}."))
    target = {**body}
    contact = _admin_contact_body(location)
    if contact:
        target["_new_contact"] = contact
    return _proposal(
        "create_account", location["location_key"], location["name"], {}, target,
        {"website": location,
         "rejected": match["rejected"],
         "why": "No CRM account came close enough to offer as a link."},
        tier="manual", care_type_options=mapped, account_id=None,
        subject_kind="location")


# ---------------------------------------------------------------- 6. contacts
def plan_contact(match, contacts_by_account, skip_account_ids):
    """
    Never touches an account that is about to be CHOW'd. The SOP says leave the
    old account exactly as it is, and the website's administrator belongs to the
    facility under its new owner, so that contact is created as part of the CHOW
    itself rather than bolted onto the record billing still needs.
    """
    account, location = match["primary"], match["location"]
    if account["id"] in skip_account_ids:
        return None
    body = _admin_contact_body(location)
    if body is None:
        return None

    existing = contacts_by_account.get(account["id"], [])
    administrators = [c for c in existing
                      if any(t in (c.get("title") or "").lower() for t in ADMIN_TITLES)]

    if administrators:
        contact = administrators[0]
        if norm_text(contact.get("name")) == norm_text(body["name"]):
            return None
        return _proposal(
            "update_contact", contact["id"],
            f"{account.get('name')} \u00b7 {contact.get('name')} -> {body['name']}",
            {"name": contact.get("name"), "title": contact.get("title")},
            {"name": body["name"]},
            {"website": location,
             "account": {"id": account["id"], "name": account.get("name")},
             "why": f"The website lists {body['name']} as administrator; the CRM "
                    f"contact titled {contact.get('title')} says "
                    f"{contact.get('name')}."},
            tier="probable", account_id=account["id"], subject_kind="contact")

    return _proposal(
        "create_contact", account["id"],
        f"{account.get('name')} \u00b7 add {body['name']}", {},
        {**body, "account_id": account["id"]},
        {"website": location,
         "account": {"id": account["id"], "name": account.get("name")},
         "why": "The website names an administrator this account has no contact for."},
        tier="probable", account_id=account["id"], subject_kind="contact")


# ----------------------------------------------------------------- 7. orphans
def plan_orphans(accounts, matched_ids, scope_parent_ids):
    """
    Bellhaven children that no longer appear on the website.

    Default is Needs Review, not Inactive. A facility that was sold is still a
    live sales target for someone; a closed one is not, and the website cannot
    tell you which. Harborview's children are out of scope on purpose: the
    About page says the whole family joined, so absence from the Bellhaven site
    does not imply a divestiture there.
    """
    out = []
    for account in accounts:
        if account.get("parent_id") not in scope_parent_ids:
            continue
        if account["id"] in matched_ids:
            continue
        if str(account.get("name", "")).endswith("(Parent Account)"):
            continue
        if account.get("duplicate_of_account") or account.get("chow_current_account"):
            continue
        if account.get("status") == STATUS_NEEDS_REVIEW:
            continue
        target = {
            "status": STATUS_NEEDS_REVIEW,
            "note": append_note(
                account.get("note"),
                f"Not listed on the Bellhaven website as of {TODAY}. Possibly sold, "
                "closed or rebranded. Verify the current owner before outreach."),
        }
        out.append(_proposal(
            "orphan_review", account["id"], account.get("name", account["id"]),
            {"status": account.get("status"), "note": account.get("note")}, target,
            {"why": "Parented to Bellhaven but matched no community on the website.",
             "account": {"id": account["id"], "city": account.get("billing_city"),
                         "state": account.get("billing_state"),
                         "street": account.get("billing_street"),
                         "parent_name": account.get("parent_name"),
                         "lifetime_revenue": account.get("lifetime_revenue"),
                         "outstanding_ar": account.get("outstanding_ar")}},
            tier="manual", editable=["status", "note"]))
    return out


# --------------------------------------------------------------------- entry
def build_plan(locations, accounts, contacts):
    """Returns (proposals, matches, held)."""
    from bellhaven.matcher import match_all

    contacts_by_account = {}
    for contact in contacts:
        contacts_by_account.setdefault(contact.get("account_id"), []).append(contact)

    split_shell_ids = {}
    for original_id, new_name in SPLIT_PARENTS.items():
        shell = find_by_name(accounts, new_name)
        if shell:
            split_shell_ids[original_id] = shell["id"]

    family = corporate_layer(accounts)
    contact_counts = {aid: len(cs) for aid, cs in contacts_by_account.items()}
    matches = match_all(locations, accounts, family, contact_counts)

    proposals = list(plan_hierarchy(accounts))
    held, matched_ids, chow_subjects = [], set(), set()

    for match in matches.values():
        if match["primary"] is None:
            proposals.append(
                plan_ambiguous(match, split_shell_ids) if match["ambiguous"]
                else plan_create(match))
            continue

        matched_ids.add(match["primary"]["id"])
        matched_ids.update(d["id"] for d in match["duplicates"])

        # Copies under different parents: decide nothing, flag them all.
        if match["conflict"]:
            conflict = plan_duplicate_conflict(
                match, contacts_by_account, {a["id"]: a.get("name", "") for a in accounts})
            if conflict:
                proposals.append(conflict)
            continue

        account = match["primary"]
        ownership = plan_ownership(match, split_shell_ids)
        if ownership and ownership.get("_held"):
            # Its parent cannot be proposed yet, but its SOP outcome is already
            # knowable. If it will be a CHOW, protect it now: a field fix
            # approved on this run would be a violation on the next.
            if sop_requires_chow(account):
                chow_subjects.add(account["id"])
                ownership["reason"] += (
                    "; it will be a CHOW, so nothing else is proposed on it until then")
            held.append(ownership)
            ownership = None
        if ownership:
            proposals.append(ownership)
            if ownership["type"] == "chow":
                chow_subjects.add(account["id"])

        # SOP: the old account in a CHOW is left EXACTLY as it is. Not its
        # parent, not its fields, not its contacts. The website's values and the
        # administrator go on the successor, which the CHOW proposal creates.
        if account["id"] not in chow_subjects:
            for maybe in (plan_field_fixes(match),
                          plan_contact(match, contacts_by_account, chow_subjects)):
                if maybe:
                    proposals.append(maybe)
        proposals.extend(plan_duplicates(match))

    scope = list(ORPHAN_SCOPE_PARENT_IDS) + list(split_shell_ids.values())
    proposals.extend(plan_orphans(accounts, matched_ids, scope))
    return [p for p in proposals if p], matches, held
