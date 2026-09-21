"""
The only place in the project that changes CRM data, and it only ever runs
because a human clicked Approve.

Three safety steps wrap every write:

  BEFORE  re-read the record and confirm it still looks the way it did when the
          proposal was made. If someone changed it meanwhile, write nothing.
  WRITE   send the call(s).
  AFTER   re-read and confirm the change actually landed.

Multi-call paths (CHOW, create-with-contact) create the new record FIRST and
store its id in the ledger immediately, so a retry reuses that id instead of
creating a second permanent duplicate. This API has no delete.
"""
from bellhaven.crm_client import CRMClient
from bellhaven import store

SIMPLE_PATCH_TYPES = {
    "nest_parent", "rename_parent", "reparent", "update_fields",
    "mark_duplicate", "orphan_review",
}


class StaleProposal(RuntimeError):
    pass


def _drift(current, before):
    out = []
    for field, expected in (before or {}).items():
        if field.startswith("_"):
            continue
        live = current.get(field)
        expected = None if expected in ("(none)", "(blank)", "") else expected
        if (live or None) != (expected or None):
            out.append({"field": field, "expected": expected, "found": live})
    return out


def _new_id(record, *keys):
    for key in ("id", *keys):
        if record.get(key):
            return record[key]
    raise RuntimeError(f"Create returned no id: {record}")


def _clean(body):
    return {k: v for k, v in (body or {}).items()
            if not k.startswith("_") and v is not None}


def execute(proposal_id, reviewer_note=None, choice=None):
    proposal = store.get_proposal(proposal_id)
    if proposal is None:
        raise ValueError(f"No proposal {proposal_id}")
    if not store.claim_for_execution(proposal_id):
        raise RuntimeError("Already executing or already decided.")

    client = CRMClient(allow_writes=True)
    try:
        result = _run(client, proposal, choice)
    except Exception as exc:                       # noqa: BLE001
        for call in client.calls:
            store.log_call(proposal_id, call)
        store.set_status(proposal_id, "failed", reviewer_note, {"error": str(exc)})
        raise
    for call in client.calls:
        store.log_call(proposal_id, call)
    store.set_status(proposal_id, "executed", reviewer_note, result)
    return result


def _run(client, proposal, choice):
    kind = proposal["type"]
    target = proposal["target"]
    subject = proposal["subject_id"]

    if kind in ("create_parent",):
        created = client.create_account(_clean(target))
        return {"created_account_id": _new_id(created, "account_id"),
                "name": target.get("name")}

    if kind == "create_account":
        return _create_facility(client, proposal, target, target.get("_new_contact"))

    if kind == "create_contact":
        created = client.create_contact(_clean(target))
        return {"created_contact_id": _new_id(created, "contact_id")}

    if kind == "update_contact":
        client.update_contact(subject, _clean(target))
        return {"updated_contact": subject, "fields": list(_clean(target))}

    if kind == "chow":
        return _run_chow(client, proposal, target["_new_account"],
                         target.get("_new_contact"), proposal["before"])

    if kind == "match_ambiguous":
        return _run_ambiguous(client, proposal, choice)

    if kind == "duplicate_conflict":
        return _run_conflict(client, target)

    if kind in SIMPLE_PATCH_TYPES:
        current = client.get_account(subject)
        drift = _drift(current, proposal["before"])
        if drift:
            raise StaleProposal(f"Account changed since this was proposed: {drift}")
        client.update_account(subject, _clean(target))
        after = client.get_account(subject)
        return {"verified": {f: after.get(f) for f in target if not f.startswith("_")}}

    raise RuntimeError(f"No executor branch for proposal type {kind!r}")


def _create_facility(client, proposal, body, contact_body):
    previous = proposal.get("result") or {}
    account_id = previous.get("created_account_id")
    if not account_id:
        created = client.create_account(_clean(body))
        account_id = _new_id(created, "account_id")
        store.set_status(proposal["id"], "executing", None,
                         {"created_account_id": account_id})
    result = {"created_account_id": account_id}
    if contact_body:
        contact = client.create_contact({**_clean(contact_body), "account_id": account_id})
        result["created_contact_id"] = _new_id(contact, "contact_id")
    return result


def _run_chow(client, proposal, new_account, new_contact, before):
    """
    SOP: the old account keeps its billing history and is NOT re-parented.
    A successor is created under the correct parent, and the old account's
    chow_current_account points at it.

    The website's administrator contact goes on the NEW account only. Adding a
    contact to the old record would violate "leave the existing account exactly
    as it is", and the person works for the facility under its new owner.
    """
    old_id = proposal["subject_id"]
    previous = proposal.get("result") or {}
    new_id = previous.get("new_account_id")

    old = client.get_account(old_id)
    drift = _drift(old, {"parent_id": before.get("parent_id")})
    if drift:
        raise StaleProposal(f"Old account changed since this was proposed: {drift}")

    if not new_id:
        created = client.create_account(_clean(new_account))
        new_id = _new_id(created, "account_id")
        store.set_status(proposal["id"], "executing", None, {"new_account_id": new_id})

    contact_id = previous.get("new_contact_id")
    if new_contact and not contact_id:
        contact = client.create_contact({**_clean(new_contact), "account_id": new_id})
        contact_id = _new_id(contact, "contact_id")
        store.set_status(proposal["id"], "executing", None,
                         {"new_account_id": new_id, "new_contact_id": contact_id})

    # The ONLY field touched on the old account.
    client.update_account(old_id, {"chow_current_account": new_id})

    old_after = client.get_account(old_id)
    new_after = client.get_account(new_id)
    return {
        "new_account_id": new_id,
        "new_contact_id": contact_id,
        "old_parent_unchanged": old_after.get("parent_id") == before.get("parent_id"),
        "chow_pointer_set": old_after.get("chow_current_account") == new_id,
        "new_account_parent": new_after.get("parent_id"),
        "old_account_untouched_except_chow": True,
    }


def _append_line(existing, line):
    existing = (existing or "").strip()
    if not line or line in existing:
        return existing
    return f"{existing}\n{line}".strip()


def _run_conflict(client, target):
    """
    Flag every copy of a building whose owners disagree.

    Reads ALL of them first and writes NONE if any has moved since the proposal
    was made: a group flagged halfway would be worse than one left alone.
    """
    members = target["_members"]
    live = {m["id"]: client.get_account(m["id"]) for m in members}
    drift = [
        {"account": m["id"], "expected_parent": m["parent_id"] or None,
         "found_parent": live[m["id"]].get("parent_id") or None}
        for m in members
        if (live[m["id"]].get("parent_id") or None) != (m["parent_id"] or None)
    ]
    if drift:
        raise StaleProposal(f"A copy was re-parented since this was proposed: {drift}")

    for m in members:
        client.update_account(m["id"], {
            "status": target["status"],
            "note": _append_line(live[m["id"]].get("note"), target.get("note")),
        })
    after = {m["id"]: client.get_account(m["id"]).get("status") for m in members}
    return {"flagged": after, "all_flagged": all(s == target["status"] for s in after.values())}


def _run_ambiguous(client, proposal, choice):
    """The reviewer picked one of the candidate accounts, or chose to create."""
    options = proposal["target"]["_options"]
    key = choice or proposal["target"].get("_choice")
    option = next((o for o in options if o["key"] == key), None)
    if option is None:
        raise RuntimeError(f"No option {key!r} on proposal {proposal['id']}")

    if option["kind"] == "create":
        result = _create_facility(client, proposal, option["new_account"],
                                  option.get("new_contact"))
        result["decision"] = "created a new account"
        return result

    if option["kind"] == "chow":
        result = _run_chow(client, proposal, option["new_account"],
                           option.get("new_contact"),
                           {"parent_id": option["candidate"].get("parent_id")})
        result["decision"] = f"CHOW against existing account {option['key']}"
        return result

    account_id = option["key"]
    client.update_account(account_id, _clean(option["patch"]))
    after = client.get_account(account_id)
    return {
        "decision": f"linked to existing account {account_id}",
        "linked_account_id": account_id,
        "verified": {f: after.get(f) for f in option["patch"]},
    }
