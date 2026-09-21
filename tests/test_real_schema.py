"""
An end-to-end run of the planner against records shaped exactly like the live
API returns them.

This file exists because of a real bug: the code assumed the account key was
called `id`, the API calls it `account_id`, and nothing caught it until the
pipeline crashed against the live CRM. Everything here uses the true field
names, so that class of mistake fails here instead of in front of a reviewer.

Real shapes, confirmed 20 Sep 2026:
    envelope  {"data": [...], "page": 1, "page_size": 50, "total": 121}
    account   account_id, name, parent_id, parent_name, status, care_type,
              phone, billing_street/city/state/zip, lifetime_revenue (int),
              outstanding_ar (int), chow_current_account, duplicate_of_account,
              note, updated_at, created_by_candidate
    contact   contact_id, account_id, name, title, email, phone, is_active
"""
from config import (
    BELLHAVEN_PARENT_ID,
    CEDAR_TRAIL_PARENT_ID,
    HARBORVIEW_PARENT_ID,
)
from bellhaven.crm_client import ACCOUNT_ID_KEYS, CONTACT_ID_KEYS, _ensure_id, _items_from
from bellhaven.planner import build_plan


def account(account_id, name, parent_id="", **overrides):
    record = {
        "account_id": account_id,
        "name": name,
        "parent_id": parent_id,
        "parent_name": "",
        "status": "Active",
        "care_type": "Assisted Living",
        "phone": "",
        "billing_street": "",
        "billing_city": "",
        "billing_state": "OH",
        "billing_zip": "",
        "lifetime_revenue": 0,
        "outstanding_ar": 0,
        "chow_current_account": "",
        "duplicate_of_account": "",
        "note": "",
        "updated_at": "2026-09-20 13:56:24Z",
        "created_by_candidate": False,
    }
    record.update(overrides)
    return record


def location(name, street, city, zip_code, state="OH", offerings=("Assisted Living",), **kw):
    record = {
        "slug": name.lower().replace(" ", "-"),
        "url": f"https://site/communities/{name.lower().replace(' ', '-')}",
        "name": name,
        "street": street,
        "city": city,
        "state": state,
        "zip": zip_code,
        "offerings": list(offerings),
        "administrator": "Sam Pruitt",
        "phone": "(231) 533-2969",
        "problems": [],
    }
    record.update(kw)
    from bellhaven.normalize import location_key
    record["location_key"] = location_key(record["street"], record["zip"])
    return record


# --------------------------------------------------------- the envelope
def test_the_real_envelope_is_unwrapped():
    payload = {"data": [{"account_id": "001A"}], "page": 1, "page_size": 50, "total": 121}
    assert len(_items_from(payload)) == 1


def test_account_id_and_contact_id_both_become_id():
    accounts = _ensure_id([{"account_id": "001A"}], ACCOUNT_ID_KEYS, "account")
    contacts = _ensure_id(
        [{"contact_id": "003C", "account_id": "001A"}], CONTACT_ID_KEYS, "contact"
    )
    assert accounts[0]["id"] == "001A"
    assert contacts[0]["id"] == "003C"      # not the account it belongs to


# --------------------------------------------------------- the whole plan
ACCOUNTS = _ensure_id(
    [
        account("0015QAPLGS3FVYEEEM", "Bellhaven Senior Living (Parent Account)", care_type=""),
        account("001FJZYHR7MLFMNPLL", "Harborview Care Group (Parent Account)", care_type=""),
        account("001FWSQ30SFW6S7604", "Cedar Trail Communities (Parent Account)", care_type=""),
        # under the wrong parent, revenue but no AR -> plain re-parent
        account("001LIMA", "Bellhaven Crossings of Lima", HARBORVIEW_PARENT_ID,
                billing_street="3115 N Cole St", billing_city="Lima", billing_zip="45801",
                lifetime_revenue=47000, outstanding_ar=0, parent_name="Harborview Care Group"),
        # under the wrong parent, revenue AND AR -> CHOW
        account("001TIFFIN", "Bellhaven of Tiffin", CEDAR_TRAIL_PARENT_ID,
                billing_street="9 Maple Rd", billing_city="Tiffin", billing_zip="44883",
                care_type="Skilled Nursing", lifetime_revenue=88000, outstanding_ar=4200),
        # no parent at all
        account("001FINDLAY", "Bellhaven Meadows of Findlay", "",
                billing_street="1800 N Blanchard St", billing_city="Findlay",
                billing_zip="45840"),
        # two records for one building
        account("001OWOSSO1", "Bellhaven of Owosso", BELLHAVEN_PARENT_ID,
                billing_street="77 Elm St", billing_city="Owosso", billing_state="MI",
                billing_zip="48867", lifetime_revenue=12000),
        account("001OWOSSO2", "Bellhaven of Owosso", BELLHAVEN_PARENT_ID,
                billing_street="77 Elm Street", billing_city="Owosso", billing_state="MI",
                billing_zip="48867"),
        # under Bellhaven but gone from the website
        account("001GHOST", "Bellhaven of Nowhere", BELLHAVEN_PARENT_ID,
                billing_street="1 Vanished Way", billing_city="Elyria", billing_zip="44035"),
        # a name twin in another state under another operator: must never match
        account("001AMBERLY", "Amberly Manor", "001DAAUWV2J3SHQJ34",
                billing_street="12 Pikes Peak Ave", billing_city="Colorado Springs",
                billing_state="CO", billing_zip="80903"),
    ],
    ACCOUNT_ID_KEYS,
    "account",
)

CONTACTS = _ensure_id(
    [{"contact_id": "003LIMA", "account_id": "001LIMA", "name": "Nadia Sandoval",
      "title": "Executive Director", "email": "", "phone": "", "is_active": True}],
    CONTACT_ID_KEYS,
    "contact",
)

LOCATIONS = [
    location("Bellhaven Crossings of Lima", "3115 N Cole St", "Lima", "45801"),
    location("Bellhaven of Tiffin", "9 Maple Rd", "Tiffin", "44883",
             offerings=["Short-Term Rehabilitation & Nursing"]),
    location("Bellhaven Meadows of Findlay", "1800 N Blanchard St", "Findlay", "45840",
             offerings=["Assisted Living", "Memory Support"]),
    location("Bellhaven of Owosso", "77 Elm St", "Owosso", "48867", state="MI"),
    location("Amberly Manor", "50 Hudson Dr", "Hudson", "44236"),
]


def _plan():
    proposals, matches, _held = build_plan(LOCATIONS, ACCOUNTS, CONTACTS)
    return proposals, matches


def test_the_whole_plan_builds_without_a_keyerror():
    proposals, matches = _plan()
    assert proposals and len(matches) == len(LOCATIONS)


def test_the_corporate_layer_is_created_before_anything_routes_into_it():
    proposals, _ = _plan()
    kinds = [p["type"] for p in proposals]
    assert "create_parent" in kinds and "nest_parent" in kinds


def _by_subject(proposals, subject):
    return [p for p in proposals if p["subject_id"] == subject]


def test_a_harborview_facility_is_left_where_it_is():
    """Lima sits under Harborview. Harborview itself moves under Bellhaven, so
    the facility already rolls up correctly and must not be flattened: the
    Harborview corporate office is who a rep actually calls."""
    proposals, _ = _plan()
    kinds = {p["type"] for p in _by_subject(proposals, "001LIMA")}
    assert "reparent" not in kinds and "chow" not in kinds
    assert kinds <= {"update_fields", "create_contact", "update_contact"}


def test_an_unparented_facility_with_revenue_but_no_ar_reparents_plainly():
    proposals, _ = _plan()
    reparent = [p for p in _by_subject(proposals, "001FINDLAY")
                if p["type"] == "reparent"]
    assert reparent and reparent[0]["target"]["parent_id"] == BELLHAVEN_PARENT_ID


def test_a_cedar_facility_is_held_until_its_parent_exists():
    """Tiffin sits under Cedar Trail, and the Cedar Trail - Bellhaven shell does
    not exist yet, so its ownership change waits rather than guessing an id."""
    _, _, held = build_plan(LOCATIONS, ACCOUNTS, CONTACTS)
    assert [h["account"]["id"] for h in held] == ["001TIFFIN"]


def test_once_the_new_parent_exists_the_cedar_facility_chows_into_it():
    from config import CEDAR_BELLHAVEN_NAME
    accounts = ACCOUNTS + _ensure_id(
        [account("001CEDARBH", CEDAR_BELLHAVEN_NAME, BELLHAVEN_PARENT_ID)],
        ACCOUNT_ID_KEYS, "account")
    proposals, _, held = build_plan(LOCATIONS, accounts, CONTACTS)
    assert not held
    chow = [p for p in proposals
            if p["type"] == "chow" and p["subject_id"] == "001TIFFIN"][0]
    assert chow["target"]["_new_account"]["parent_id"] == "001CEDARBH"
    # the old account is touched nowhere except the CHOW pointer
    assert set(chow["target"]) == {"_new_account", "_new_contact"}


def test_a_chow_never_adds_a_contact_to_the_old_account():
    """Marietta-style case: the website lists an administrator, the old account
    has none. The contact must land on the successor, not on the record billing
    still needs left alone."""
    from config import CEDAR_BELLHAVEN_NAME
    accounts = ACCOUNTS + _ensure_id(
        [account("001CEDARBH", CEDAR_BELLHAVEN_NAME, BELLHAVEN_PARENT_ID)],
        ACCOUNT_ID_KEYS, "account")
    proposals, _, _ = build_plan(LOCATIONS, accounts, CONTACTS)
    assert not [p for p in proposals
                if p["type"] in ("create_contact", "update_contact")
                and p["account_id"] == "001TIFFIN"]
    chow = [p for p in proposals if p["type"] == "chow"][0]
    assert chow["target"]["_new_contact"]["name"] == "Sam Pruitt"


def test_an_account_with_no_parent_moves_to_bellhaven():
    proposals, _ = _plan()
    reparent = [p for p in _by_subject(proposals, "001FINDLAY") if p["type"] == "reparent"][0]
    assert reparent["target"]["parent_id"] == BELLHAVEN_PARENT_ID


def test_the_owosso_pair_resolves_to_one_survivor():
    proposals, _ = _plan()
    dupes = [p for p in proposals if p["type"] == "mark_duplicate"]
    assert len(dupes) == 1
    # the copy with billing history survives, so the other one is the loser
    assert dupes[0]["subject_id"] == "001OWOSSO2"
    assert dupes[0]["target"]["duplicate_of_account"] == "001OWOSSO1"
    assert dupes[0]["target"]["status"] == "Inactive"


def test_a_bellhaven_child_missing_from_the_website_is_flagged():
    proposals, _ = _plan()
    orphan = [p for p in _by_subject(proposals, "001GHOST") if p["type"] == "orphan_review"][0]
    assert orphan["target"]["status"] == "Needs Review"


def test_the_colorado_name_twin_is_never_matched():
    proposals, matches = _plan()
    hudson = matches["50 hudson dr|44236"]
    assert hudson["primary"] is None                    # no match at all
    assert not _by_subject(proposals, "001AMBERLY")     # and it is left alone


def test_a_stale_street_number_offers_the_account_instead_of_creating_one():
    """The Union Square failure, pinned. Same city, state and zip, different
    street number: the account must be offered, not ignored."""
    accounts = ACCOUNTS + _ensure_id(
        [account("001UNION", "Union Square Senior Living", "",
                 billing_street="2210 Union Sq", billing_city="Akron",
                 billing_zip="44305")],
        ACCOUNT_ID_KEYS, "account")
    locations = LOCATIONS + [
        location("Bellhaven at Union Square", "118 Union Sq", "Akron", "44305")]

    proposals, matches, _ = build_plan(locations, accounts, CONTACTS)
    result = matches["118 union sq|44305"]
    assert result["primary"] is None
    assert result["ambiguous"], "the existing account must survive as a candidate"
    assert result["ambiguous"][0]["id"] == "001UNION"

    decision = [p for p in proposals if p["type"] == "match_ambiguous"][0]
    keys = [o["key"] for o in decision["target"]["_options"]]
    assert keys[0] == "001UNION" and keys[-1] == "__create__"


def test_the_candidate_list_is_ranked_by_score_not_by_veto():
    """The original bug: vetoed candidates were all scored 0, so 'nearest' was
    ordered by account id and showed three irrelevant records."""
    accounts = ACCOUNTS + _ensure_id(
        [account("001UNION", "Union Square Senior Living", "",
                 billing_street="2210 Union Sq", billing_city="Akron",
                 billing_zip="44305")],
        ACCOUNT_ID_KEYS, "account")
    locations = [location("Bellhaven at Union Square", "118 Union Sq", "Akron", "44305")]
    _, matches, _ = build_plan(locations, accounts, CONTACTS)
    scores = [c["score"] for c in matches["118 union sq|44305"]["ambiguous"]]
    assert scores == sorted(scores, reverse=True)


def test_parent_shells_only_ever_get_hierarchy_proposals():
    """A shell is not a facility, so it must never pick up an address, a care
    type or a duplicate flag from a website community."""
    proposals, _ = _plan()
    allowed = {"nest_parent", "rename_parent"}
    assert not _by_subject(proposals, BELLHAVEN_PARENT_ID)
    for shell in (HARBORVIEW_PARENT_ID, CEDAR_TRAIL_PARENT_ID):
        assert {p["type"] for p in _by_subject(proposals, shell)} <= allowed


def test_contacts_use_is_active_not_status():
    """The account status field is a string; the contact one is a boolean."""
    proposals, _ = _plan()
    creates = [p for p in proposals if p["type"] == "create_contact"]
    assert creates, "expected at least one contact create"
    body = creates[0]["target"]
    assert body["is_active"] is True
    assert "status" not in body


def test_an_executive_director_counts_as_the_administrator_contact():
    """Lima's CRM contact is titled Executive Director, not Administrator, so it
    should be updated rather than duplicated with a second admin record."""
    proposals, _ = _plan()
    lima_contacts = [
        p for p in proposals
        if p["type"] in ("create_contact", "update_contact")
        and (p["subject_id"] == "003LIMA" or p["subject_id"] == "001LIMA")
    ]
    assert [p["type"] for p in lima_contacts] == ["update_contact"]


def test_money_arrives_as_plain_integers():
    """The browser shows '$47,000'; the API sends 47000. Both must work."""
    from bellhaven.normalize import to_money
    assert to_money(47000) == to_money("$47,000") == 47000


# ------------------------------------------- the old CHOW account is untouchable
def _with_cedar_shell():
    from config import CEDAR_BELLHAVEN_NAME
    return ACCOUNTS + _ensure_id(
        [account("001CEDARBH", CEDAR_BELLHAVEN_NAME, BELLHAVEN_PARENT_ID)],
        ACCOUNT_ID_KEYS, "account")


def test_a_chow_account_gets_no_field_updates_either():
    """
    The SOP: leave the existing account EXACTLY as it is. Not just its parent,
    and not just its contacts: its name, address, phone and care type too. The
    website's values belong on the successor, which already carries them.
    """
    proposals, _, _ = build_plan(LOCATIONS, _with_cedar_shell(), CONTACTS)
    on_old = [p["type"] for p in proposals
              if p["account_id"] == "001TIFFIN" or p["subject_id"] == "001TIFFIN"]
    assert on_old == ["chow"], f"old CHOW account also got: {on_old}"


def test_a_held_account_that_will_chow_gets_nothing_in_the_meantime():
    """
    Before the Cedar Trail - Bellhaven shell exists, Tiffin's ownership change is
    held. But its SOP outcome is already knowable (revenue AND AR above zero), so
    nothing may be proposed on it now either: a field fix approved on run 1 would
    be a violation the moment run 2 proposes the CHOW.
    """
    proposals, _, held = build_plan(LOCATIONS, ACCOUNTS, CONTACTS)
    assert [h["account"]["id"] for h in held] == ["001TIFFIN"]
    on_old = [p["type"] for p in proposals
              if p["account_id"] == "001TIFFIN" or p["subject_id"] == "001TIFFIN"]
    assert on_old == [], f"held CHOW-bound account still got: {on_old}"


def test_a_held_account_that_will_simply_reparent_still_gets_its_fixes():
    """The protection must not over-reach: a Cedar facility with no AR re-parents
    in place, so fixing its phone number now is harmless and should not wait."""
    from bellhaven.normalize import location_key
    accounts = ACCOUNTS + _ensure_id(
        [account("001MARION", "Bellhaven of Marion", CEDAR_TRAIL_PARENT_ID,
                 billing_street="40 Oak St", billing_city="Marion",
                 billing_zip="43302", lifetime_revenue=5000, outstanding_ar=0)],
        ACCOUNT_ID_KEYS, "account")
    locations = LOCATIONS + [location("Bellhaven of Marion", "40 Oak St", "Marion", "43302")]
    proposals, _, held = build_plan(locations, accounts, CONTACTS)
    assert "001MARION" in [h["account"]["id"] for h in held]
    kinds = {p["type"] for p in proposals if p["account_id"] == "001MARION"}
    assert "update_fields" in kinds


# ------------------------------------------------------------ steady state
# Every earlier bug of this kind lived in the run AFTER approval: tests checked
# the first move and never looked at what the next run does with the result.
# These tests approve everything the pipeline proposes, apply it to an
# in-memory CRM, and run again. A correct pipeline has nothing left to say.

import itertools as _itertools

# One counter for the whole session: a real CRM never reuses an id, and an
# earlier version of this harness restarted it per run and overwrote records.
_SIM_IDS = _itertools.count(1)


def _apply(accounts, contacts, proposals):
    """Execute every proposal against an in-memory copy of the CRM, the way the
    executor would against the real one, choosing each decision's default."""
    accts = {a["id"]: dict(a) for a in accounts}
    cons = [dict(c) for c in contacts]
    counter = _SIM_IDS

    def clean(body):
        return {k: v for k, v in (body or {}).items() if not k.startswith("_")}

    def new_account(body):
        new_id = f"001SIM{next(counter):05d}"
        record = account(new_id, body.get("name", ""), body.get("parent_id", ""))
        record.update(clean(body))
        record["id"] = record["account_id"] = new_id
        accts[new_id] = record
        return new_id

    def new_contact(body, account_id):
        new_id = f"003SIM{next(counter):05d}"
        cons.append({**clean(body), "account_id": account_id,
                     "contact_id": new_id, "id": new_id})

    def chow(old_id, new_body, contact_body):
        new_id = new_account(new_body)
        if contact_body:
            new_contact(contact_body, new_id)
        accts[old_id]["chow_current_account"] = new_id

    for p in proposals:
        kind, target = p["type"], p["target"]
        if kind == "create_parent":
            new_account(target)
        elif kind == "create_account":
            created = new_account(target)
            if target.get("_new_contact"):
                new_contact(target["_new_contact"], created)
        elif kind == "chow":
            chow(p["subject_id"], target["_new_account"], target.get("_new_contact"))
        elif kind == "create_contact":
            new_contact(target, target["account_id"])
        elif kind == "update_contact":
            for c in cons:
                if c["id"] == p["subject_id"]:
                    c.update(clean(target))
        elif kind == "match_ambiguous":
            option = target["_options"][0]
            if option["kind"] == "link":
                accts[option["key"]].update(clean(option["patch"]))
            elif option["kind"] == "chow":
                chow(option["key"], option["new_account"], option.get("new_contact"))
            else:
                created = new_account(option["new_account"])
                if option.get("new_contact"):
                    new_contact(option["new_contact"], created)
        elif kind == "duplicate_conflict":
            for member in target["_members"]:
                accts[member["id"]]["status"] = target["status"]
        elif kind != "needs_human":
            accts[p["subject_id"]].update(clean(target))

    for a in accts.values():                      # the API derives parent_name
        a["parent_name"] = accts.get(a.get("parent_id") or "", {}).get("name", "")
    return list(accts.values()), cons


def _world():
    accounts = ACCOUNTS + _ensure_id(
        [account("001MARION", "Bellhaven of Marion", CEDAR_TRAIL_PARENT_ID,
                 billing_street="40 Oak St", billing_city="Marion",
                 billing_zip="43302", lifetime_revenue=5000, outstanding_ar=0)],
        ACCOUNT_ID_KEYS, "account")
    locations = LOCATIONS + [location("Bellhaven of Marion", "40 Oak St", "Marion", "43302")]
    return accounts, CONTACTS, locations


def test_a_facility_already_in_the_cedar_bellhaven_shell_stays_there():
    """
    The Marietta bug. A facility routed into Cedar Trail - Bellhaven must not be
    proposed to move again on the next run. Pointing it straight at Bellhaven
    would erase the corporate office the whole three-tier model exists to keep.
    """
    accounts, contacts, locations = _world()
    for run in range(2):                          # run 1 creates the shell, run 2 routes
        proposals, _, _ = build_plan(locations, accounts, contacts)
        accounts, contacts = _apply(accounts, contacts, proposals)

    proposals, _, _ = build_plan(locations, accounts, contacts)
    moves = [(p["subject_label"], p["target"].get("parent_id"))
             for p in proposals if p["type"] in ("reparent", "chow")]
    assert moves == [], f"facilities proposed to move out of their correct parent: {moves}"


def test_approve_everything_and_the_next_run_proposes_nothing():
    """
    The re-run requirement at the level of the DATA, not just the fingerprints:
    once every proposal is approved, the CRM agrees with the website and the
    pipeline has nothing left to change.
    """
    accounts, contacts, locations = _world()
    history = []
    for run in range(4):
        proposals, _, held = build_plan(locations, accounts, contacts)
        history.append(sorted(p["type"] for p in proposals))
        if not proposals and not held:
            break
        accounts, contacts = _apply(accounts, contacts, proposals)

    assert history[-1] == [], (
        "pipeline never settled. Proposals per run: "
        + " | ".join(f"run {i + 1}: {h}" for i, h in enumerate(history)))
    assert len(history) <= 3, f"took {len(history)} runs to settle: {history}"


# ----------------------------------------------- duplicates, owners disagree
MONROE = dict(billing_street="750 Stewart Rd", billing_city="Monroe",
              billing_state="MI", billing_zip="48162")


def _monroe_world():
    """The real case: one building, three accounts, three different parents,
    three different phones, and a website administrator nobody has on file."""
    accounts = ACCOUNTS + _ensure_id([
        account("001U1750VLVJAGG1S5", "Bellhaven Gardens of Monroe", BELLHAVEN_PARENT_ID,
                phone="(734) 555-0101", **MONROE),
        account("001CEDARMONROE", "Cedar Trail of Monroe", CEDAR_TRAIL_PARENT_ID,
                phone="(734) 555-0102", **MONROE),
        account("00159PL81N38KM4FHM", "Monroe Gardens Care Center", HARBORVIEW_PARENT_ID,
                phone="(734) 555-0103", **MONROE),
    ], ACCOUNT_ID_KEYS, "account")
    contacts = CONTACTS + _ensure_id(
        [{"contact_id": "003ELAINE", "account_id": "001U1750VLVJAGG1S5",
          "name": "Elaine Bogle", "title": "Administrator", "email": "", "phone": "",
          "is_active": True}], CONTACT_ID_KEYS, "contact")
    locations = LOCATIONS + [location(
        "Bellhaven Gardens of Monroe", "750 Stewart Rd", "Monroe", "48162", state="MI",
        administrator="Phil Mabry", phone="(814) 412-2218")]
    return accounts, contacts, locations


MONROE_IDS = {"001U1750VLVJAGG1S5", "001CEDARMONROE", "00159PL81N38KM4FHM"}


def test_copies_under_different_parents_are_flagged_not_resolved():
    accounts, contacts, locations = _monroe_world()
    proposals, _, _ = build_plan(locations, accounts, contacts)

    conflicts = [p for p in proposals if p["type"] == "duplicate_conflict"]
    assert len(conflicts) == 1
    assert {m["id"] for m in conflicts[0]["target"]["_members"]} == MONROE_IDS
    assert conflicts[0]["target"]["status"] == "Needs Review"

    # and nothing else touches any of the three: no survivor, no move, no fix
    touching = [(p["type"], p["subject_label"]) for p in proposals
                if p["type"] != "duplicate_conflict"
                and ({p["subject_id"], p.get("account_id")} & MONROE_IDS)]
    assert touching == [], f"decided something it cannot know: {touching}"


def test_the_conflict_card_shows_every_copy_with_its_contacts():
    accounts, contacts, locations = _monroe_world()
    proposals, _, _ = build_plan(locations, accounts, contacts)
    card = [p for p in proposals if p["type"] == "duplicate_conflict"][0]
    members = {m["name"]: m for m in card["evidence"]["members"]}
    assert set(members) == {"Bellhaven Gardens of Monroe", "Cedar Trail of Monroe",
                            "Monroe Gardens Care Center"}
    assert members["Bellhaven Gardens of Monroe"]["contacts"] == ["Elaine Bogle (Administrator)"]
    assert "Phil Mabry" in card["target"]["note"]


def test_a_flagged_group_is_quiet_on_the_next_run():
    accounts, contacts, locations = _monroe_world()
    for _ in range(2):
        proposals, _, _ = build_plan(locations, accounts, contacts)
        accounts, contacts = _apply(accounts, contacts, proposals)
    proposals, _, _ = build_plan(locations, accounts, contacts)
    assert not [p for p in proposals if {p["subject_id"], p.get("account_id")} & MONROE_IDS
                or p["type"] == "duplicate_conflict"]


def test_once_a_human_picks_the_real_one_the_pipeline_carries_on_from_it():
    """Resolve it the way the card says: mark the wrong copies as duplicates of
    the right one. The next run sees one account and treats it normally."""
    accounts, contacts, locations = _monroe_world()
    for a in accounts:
        if a["id"] in ("001CEDARMONROE", "00159PL81N38KM4FHM"):
            a.update(duplicate_of_account="001U1750VLVJAGG1S5", status="Inactive")
    proposals, matches, _ = build_plan(locations, accounts, contacts)
    kinds = {p["type"] for p in proposals if p.get("account_id") == "001U1750VLVJAGG1S5"
             or p["subject_id"] == "003ELAINE"}
    assert "duplicate_conflict" not in {p["type"] for p in proposals}
    assert kinds == {"update_fields", "update_contact"}   # phone, then Elaine -> Phil


def test_same_parent_copies_keep_the_one_with_billing_even_at_a_lower_score():
    """Survivor is chosen across ALL copies, not among those tied on score: a
    copy finance references must never be retired for a better phone match."""
    accounts = ACCOUNTS + _ensure_id([
        account("001BILLED", "Bellhaven of Zanesville", BELLHAVEN_PARENT_ID,
                billing_street="9 Elm St", billing_city="Zanesville", billing_zip="43701",
                phone="", lifetime_revenue=20000),
        account("001PHONE", "Bellhaven of Zanesville", BELLHAVEN_PARENT_ID,
                billing_street="9 Elm St", billing_city="Zanesville", billing_zip="43701",
                phone="(231) 533-2969"),
    ], ACCOUNT_ID_KEYS, "account")
    locations = LOCATIONS + [location("Bellhaven of Zanesville", "9 Elm St", "Zanesville", "43701")]
    proposals, _, _ = build_plan(locations, accounts, CONTACTS)
    dupes = [p for p in proposals if p["type"] == "mark_duplicate"
             and p["subject_id"] in ("001BILLED", "001PHONE")]
    assert [(d["subject_id"], d["target"]["duplicate_of_account"]) for d in dupes] == \
        [("001PHONE", "001BILLED")]


def test_contacts_break_a_tie_before_the_account_id_does():
    accounts = ACCOUNTS + _ensure_id([
        account("001AAA", "Bellhaven of Wooster", BELLHAVEN_PARENT_ID,
                billing_street="5 Oak St", billing_city="Wooster", billing_zip="44691"),
        account("001ZZZ", "Bellhaven of Wooster", BELLHAVEN_PARENT_ID,
                billing_street="5 Oak St", billing_city="Wooster", billing_zip="44691"),
    ], ACCOUNT_ID_KEYS, "account")
    contacts = CONTACTS + _ensure_id(
        [{"contact_id": "003W", "account_id": "001ZZZ", "name": "Ann Lee",
          "title": "Administrator", "email": "", "phone": "", "is_active": True}],
        CONTACT_ID_KEYS, "contact")
    locations = LOCATIONS + [location("Bellhaven of Wooster", "5 Oak St", "Wooster", "44691")]
    proposals, _, _ = build_plan(locations, accounts, contacts)
    dupe = [p for p in proposals if p["type"] == "mark_duplicate"
            and p["subject_id"] in ("001AAA", "001ZZZ")][0]
    assert dupe["subject_id"] == "001AAA"          # the lower id loses to contacts
    assert dupe["target"]["duplicate_of_account"] == "001ZZZ"


def test_a_copy_with_no_parent_counts_as_disagreeing_unless_configured_not_to():
    import bellhaven.matcher as matcher
    pair = [{"parent_id": BELLHAVEN_PARENT_ID}, {"parent_id": ""}]
    assert matcher.parents_disagree(pair) is True
    original = matcher.BLANK_PARENT_CONFLICTS
    try:
        matcher.BLANK_PARENT_CONFLICTS = False
        assert matcher.parents_disagree(pair) is False
    finally:
        matcher.BLANK_PARENT_CONFLICTS = original


def test_the_note_names_the_real_parent_even_when_parent_name_is_blank():
    """It once said 'parent: none' for an account sitting under Harborview."""
    accounts, contacts, locations = _monroe_world()
    card = [p for p in build_plan(locations, accounts, contacts)[0]
            if p["type"] == "duplicate_conflict"][0]
    note = card["target"]["note"]
    assert "Monroe Gardens Care Center (parent: Harborview Care Group (Parent Account))" in note
    assert "parent: none" not in note and "no parent" not in note
