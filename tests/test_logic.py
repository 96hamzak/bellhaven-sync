"""
Tests that run with no network and no CRM token.

Every case here is a real trap from this dataset, not an invented one. If you
change a threshold or a normalization rule during the live demo, run
`pytest -q` afterwards and these will tell you whether you broke something.
"""
import pytest

from bellhaven.matcher import (account_view, hard_veto, location_view,
                               score_pair, soft_veto)
from bellhaven.normalize import (
    location_key, norm_name, norm_phone, norm_street, norm_zip,
    parse_city_state_zip, to_money,
)
from config import BELLHAVEN_PARENT_ID
from bellhaven.planner import care_type_from_offerings, plan_ownership
from bellhaven.store import fingerprint


# ----------------------------------------------------------- normalization
def test_street_abbreviations_and_directionals():
    assert norm_street("1800 N. Blanchard Street") == "1800 n blanchard st"
    assert norm_street("1800 North Blanchard St") == "1800 n blanchard st"


def test_suite_numbers_are_dropped_so_the_building_still_matches():
    assert norm_street("42 Oak Ave Suite 300") == norm_street("42 Oak Avenue")


def test_zip_plus_four_and_city_state_zip():
    assert norm_zip("45840-1234") == "45840"
    assert parse_city_state_zip("Findlay, OH 45840") == ("Findlay", "OH", "45840")
    assert parse_city_state_zip("Grand Rapids, MI 49503-2201") == ("Grand Rapids", "MI", "49503")


def test_money_handles_every_shape_the_crm_might_send():
    assert to_money(47000) == 47000
    assert to_money("$47,000.00") == 47000
    assert to_money(None) == 0
    assert to_money("") == 0
    assert to_money("n/a") == 0            # unparseable digits -> 0, not a crash


def test_phone_normalization():
    assert norm_phone("(231) 533-2969") == "2315332969"
    assert norm_phone("1-231-533-2969") == "2315332969"


def test_renamed_facility_reduces_to_the_same_core():
    # CRM name vs website name for the same building in Ashland, OH
    assert norm_name("Bellhaven Health Care Center of Ashland") == \
           norm_name("Bellhaven Healthcare Centre of Ashland")


def test_location_key_ignores_the_slug():
    assert location_key("1800 N Blanchard St", "45840-9999") == "1800 n blanchard st|45840"


# ------------------------------------------------------------------ vetoes
def _loc(**kw):
    base = {"name": "", "street": "", "city": "", "state": "", "zip": "", "phone": ""}
    return location_view({**base, **kw})


def _acct(**kw):
    base = {"id": "x", "name": "", "billing_street": "", "billing_city": "",
            "billing_state": "", "billing_zip": "", "phone": ""}
    return account_view({**base, **kw})


def test_amberly_manor_trap_different_state_is_hard_vetoed():
    """Website: Amberly Manor, Hudson OH. CRM: Amberly Manor, Colorado Springs CO."""
    loc = _loc(name="Amberly Manor", city="Hudson", state="OH", zip="44236")
    acct = _acct(name="Amberly Manor", billing_city="Colorado Springs",
                 billing_state="CO", billing_zip="80903")
    assert any("different state" in r for r in hard_veto(loc, acct))


def test_carlisle_versus_new_carlisle_is_not_a_match():
    loc = _loc(name="Bellhaven of New Carlisle", street="12 Elm St",
               city="New Carlisle", state="OH", zip="45344")
    acct = _acct(name="Bellhaven of Carlisle", billing_street="88 Elm St",
                 billing_city="Carlisle", billing_state="PA", billing_zip="17013")
    assert hard_veto(loc, acct)


def test_same_street_different_number_is_only_a_SOFT_veto():
    """This is the Union Square lesson. A stale street number must not zero the
    score, or the right account sinks below three irrelevant ones and the
    pipeline proposes creating a duplicate."""
    loc = _loc(street="100 Oak St", city="Marion", state="OH", zip="43302")
    acct = _acct(billing_street="110 Oak St", billing_city="Marion",
                 billing_state="OH", billing_zip="43302")
    assert not hard_veto(loc, acct)
    assert any("street number differs" in r for r in soft_veto(loc, acct))


def test_renamed_facility_at_the_same_address_still_scores_confident():
    loc = _loc(name="Bellhaven Healthcare Centre of Ashland", street="500 Center St",
               city="Ashland", state="OH", zip="44805")
    acct = _acct(name="Bellhaven Health Care Center of Ashland",
                 billing_street="500 Center Street", billing_city="Ashland",
                 billing_state="OH", billing_zip="44805")
    assert not hard_veto(loc, acct) and not soft_veto(loc, acct)
    score, _ = score_pair(loc, acct)
    assert score >= 88


# --------------------------------------------------------------- care type
def test_multiple_website_offerings_collapse_to_one_crm_value():
    best, mapped = care_type_from_offerings(["Assisted Living", "Memory Support"])
    assert mapped == ["Assisted Living", "Memory Care"]
    assert best == "Memory Care"          # higher clinical acuity wins


def test_short_term_rehab_maps_to_skilled_nursing():
    best, _ = care_type_from_offerings(["Short-Term Rehabilitation & Nursing"])
    assert best == "Skilled Nursing"


# --------------------------------------------------------------------- SOP
def _match(revenue, ar, parent="001OTHERPARENT0000"):
    return {
        "primary": {
            "id": "001TEST", "name": "Test Facility", "parent_id": parent,
            "lifetime_revenue": revenue, "outstanding_ar": ar, "note": "",
            "parent_name": "Some Operator",
        },
        "location": {
            "name": "Test Facility", "street": "1 Main St", "city": "Lima",
            "state": "OH", "zip": "45801", "phone": "(419) 555-0100",
            "administrator": "Rosa Dietz",
            "offerings": ["Assisted Living"], "url": "https://example/x",
            "location_key": "1 main st|45801",
        },
        "score": 95, "tier": "confident", "deductions": [],
        "ambiguous": [], "rejected": [], "duplicates": [],
    }


@pytest.mark.parametrize(
    "revenue, ar, expected",
    [
        (47000, 12000, "chow"),       # both above zero -> preserve the old account
        (47000, 0, "reparent"),       # revenue but no AR -> simple re-parent
        (0, 12000, "reparent"),       # AR but no revenue -> literal SOP, re-parent
        (0, 0, "reparent"),
        (None, None, "reparent"),     # nulls count as zero
        (47000, -500, "reparent"),    # a credit balance is not "greater than zero"
    ],
)
def test_sop_branches(revenue, ar, expected):
    assert plan_ownership(_match(revenue, ar), {})["type"] == expected


def test_no_ownership_proposal_when_the_parent_is_already_correct():
    assert plan_ownership(_match(1, 1, parent=BELLHAVEN_PARENT_ID), {}) is None


def test_chow_carries_the_contact_to_the_new_account_only():
    """The SOP says leave the existing account exactly as it is. The website's
    administrator therefore belongs to the successor, not the old record."""
    proposal = plan_ownership(_match(47000, 12000), {})
    assert proposal["type"] == "chow"
    assert set(proposal["target"]) == {"_new_account", "_new_contact"}
    assert proposal["target"]["_new_contact"]["is_active"] is True


# ------------------------------------------------------------ re-run safety
def test_identical_proposals_share_a_fingerprint():
    a = {"type": "reparent", "subject_id": "001X", "target": {"parent_id": "001P"}}
    b = {"type": "reparent", "subject_id": "001X", "target": {"parent_id": "001P"}}
    assert fingerprint(a) == fingerprint(b)


def test_changed_evidence_produces_a_new_fingerprint():
    a = {"type": "update_fields", "subject_id": "001X", "target": {"name": "Old Name"}}
    b = {"type": "update_fields", "subject_id": "001X", "target": {"name": "New Name"}}
    assert fingerprint(a) != fingerprint(b)


def test_scores_and_timestamps_stay_out_of_the_fingerprint():
    """Otherwise every run would produce new fingerprints and nothing would
    ever stay decided."""
    a = {"type": "reparent", "subject_id": "001X", "target": {"parent_id": "001P"},
         "evidence": {"score": 91}}
    b = {"type": "reparent", "subject_id": "001X", "target": {"parent_id": "001P"},
         "evidence": {"score": 94}}
    assert fingerprint(a) == fingerprint(b)


# ------------------------------------------------- parent shells stay untouched
def test_the_corporate_layer_is_built_not_flattened():
    """Harborview becomes a child of Bellhaven, and Cedar Trail is split so one
    record does not have to represent both an acquired and an independent half."""
    from config import CEDAR_BELLHAVEN_NAME, CEDAR_TRAIL_PARENT_ID, HARBORVIEW_PARENT_ID
    from bellhaven.planner import plan_hierarchy

    accounts = [
        {"id": HARBORVIEW_PARENT_ID, "name": "Harborview Care Group (Parent Account)",
         "parent_id": "", "note": ""},
        {"id": CEDAR_TRAIL_PARENT_ID, "name": "Cedar Trail Communities (Parent Account)",
         "parent_id": "", "note": ""},
    ]
    kinds = {p["type"] for p in plan_hierarchy(accounts)}
    assert kinds == {"nest_parent", "create_parent", "rename_parent"}

    created = [p for p in plan_hierarchy(accounts) if p["type"] == "create_parent"][0]
    assert created["target"]["name"] == CEDAR_BELLHAVEN_NAME
    assert created["target"]["parent_id"] == BELLHAVEN_PARENT_ID


def test_hierarchy_work_is_not_repeated_once_it_is_done():
    from config import CEDAR_BELLHAVEN_NAME, CEDAR_INDEPENDENT_NAME, CEDAR_TRAIL_PARENT_ID, HARBORVIEW_PARENT_ID
    from bellhaven.planner import plan_hierarchy

    done = [
        {"id": HARBORVIEW_PARENT_ID, "name": "Harborview Care Group (Parent Account)",
         "parent_id": BELLHAVEN_PARENT_ID, "note": ""},
        {"id": CEDAR_TRAIL_PARENT_ID, "name": CEDAR_INDEPENDENT_NAME,
         "parent_id": "", "note": ""},
        {"id": "001NEWCEDAR", "name": CEDAR_BELLHAVEN_NAME,
         "parent_id": BELLHAVEN_PARENT_ID, "note": ""},
    ]
    assert plan_hierarchy(done) == []


def test_a_harborview_facility_keeps_its_parent():
    """It rides along under Harborview, which is itself moving to Bellhaven.
    Flattening it would erase the corporate office a rep has to call."""
    from config import HARBORVIEW_PARENT_ID
    assert plan_ownership(_match(0, 0, parent=HARBORVIEW_PARENT_ID), {}) is None


def test_a_cedar_facility_routes_to_the_bellhaven_half():
    from config import CEDAR_TRAIL_PARENT_ID
    proposal = plan_ownership(_match(0, 0, parent=CEDAR_TRAIL_PARENT_ID),
                              {CEDAR_TRAIL_PARENT_ID: "001NEWCEDAR"})
    assert proposal["target"]["parent_id"] == "001NEWCEDAR"


def test_a_cedar_facility_is_held_until_the_new_parent_exists():
    from config import CEDAR_TRAIL_PARENT_ID
    held = plan_ownership(_match(0, 0, parent=CEDAR_TRAIL_PARENT_ID), {})
    assert held["_held"] is True


def test_an_unrelated_operator_routes_straight_to_bellhaven():
    proposal = plan_ownership(_match(0, 0, parent="00139TNDS8HNLUZ5A6"), {})
    assert proposal["target"]["parent_id"] == BELLHAVEN_PARENT_ID


# ------------------------------------------------------ upgrading old ledgers
def test_a_ledger_from_an_older_build_is_upgraded_and_its_stale_rows_expire(tmp_path, monkeypatch):
    """
    Two real failure modes when moving between builds:
      * the old table lacks columns, so the first insert crashes
      * old pending rows with no last_seen_run never expire, because in SQL
        NULL != 5 is not true, and a stale proposal sits in the queue forever
    """
    import sqlite3
    from bellhaven import store

    db = tmp_path / "ledger.db"
    monkeypatch.setattr(store, "DB_PATH", db)
    with sqlite3.connect(db) as conn:
        conn.executescript("""
        CREATE TABLE proposals (id INTEGER PRIMARY KEY AUTOINCREMENT,
          fingerprint TEXT NOT NULL UNIQUE, run_id INTEGER, last_seen_run INTEGER,
          type TEXT NOT NULL, subject_id TEXT NOT NULL, subject_label TEXT, tier TEXT,
          before_json TEXT, target_json TEXT, calls_json TEXT, evidence_json TEXT,
          editable_json TEXT, status TEXT NOT NULL DEFAULT 'pending', reviewer_note TEXT,
          result_json TEXT, created_at TEXT, decided_at TEXT);
        INSERT INTO proposals (fingerprint, type, subject_id, status)
          VALUES ('old', 'reparent', '001LIMA', 'pending');
        """)

    store.init()
    row = store.list_proposals("pending")[0]
    assert row["subject_kind"] == "account" and row["account_id"] == "001LIMA"

    run = store.start_run()
    fresh = {"type": "update_fields", "subject_id": "001X", "subject_label": "X",
             "before": {}, "target": {"name": "Y"}, "evidence": {}, "tier": "confident"}
    assert store.upsert_proposals(run, [fresh]) == (1, 0, 0)
    assert store.expire_stale(run) == 1
    assert [p["subject_id"] for p in store.list_proposals("pending")] == ["001X"]



def test_a_rejection_survives_the_next_days_run():
    from bellhaven import planner
    from tests.test_real_schema import ACCOUNTS, CONTACTS, LOCATIONS

    original = planner.TODAY
    try:
        planner.TODAY = "2026-09-21"
        today = {fingerprint(p) for p in planner.build_plan(LOCATIONS, ACCOUNTS, CONTACTS)[0]}
        planner.TODAY = "2026-09-22"
        tomorrow = {fingerprint(p) for p in planner.build_plan(LOCATIONS, ACCOUNTS, CONTACTS)[0]}
    finally:
        planner.TODAY = original
    assert today == tomorrow


def test_the_note_never_decides_identity_but_the_values_do():
    same = {"type": "reparent", "subject_id": "001X",
            "target": {"parent_id": "001P", "note": "[bellhaven-sync 2026-09-21] a"}}
    reworded = {"type": "reparent", "subject_id": "001X",
                "target": {"parent_id": "001P", "note": "[bellhaven-sync 2026-09-22] b"}}
    different = {"type": "reparent", "subject_id": "001X",
                 "target": {"parent_id": "001Q", "note": "[bellhaven-sync 2026-09-21] a"}}
    assert fingerprint(same) == fingerprint(reworded)
    assert fingerprint(same) != fingerprint(different)
