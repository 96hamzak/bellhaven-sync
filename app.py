#!/usr/bin/env python3
"""
The review app.

    python -m uvicorn app:app --reload      then open http://127.0.0.1:8000

Every page is a plain server-rendered form. You click, the browser sends one
request, the server does one thing and sends one page back. No hidden reruns,
which is what you want when the buttons write to a CRM that has no undo.

  /pending   the queue, one collapsed line per change, filterable
  /decided   everything already ruled on, searchable
  /admin     every account with its contacts, directly editable
  /tools     snapshot, restore, CSV export, clear local history
"""
import json
import subprocess
import sys

from fastapi import FastAPI, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from config import (
    ACCOUNT_INDEX,
    BUILD,
    CARE_TYPE_CHOICES,
    ROOT,
    crm_link,
)
from bellhaven import snapshot, store
from bellhaven.crm_client import CRMClient
from bellhaven.executor import execute
from bellhaven.normalize import usd
from bellhaven.planner import FIELD_LABELS
from export_ledger import export as export_ledger_csv

app = FastAPI(title=f"Bellhaven Sync (build {BUILD})")
templates = Jinja2Templates(directory=str(ROOT / "templates"))
templates.env.filters["pretty"] = lambda v: json.dumps(v, indent=2, default=str)
templates.env.globals["crm_link"] = crm_link
templates.env.filters["money"] = usd

ACCOUNT_FIELDS = [
    "name", "parent_id", "status", "care_type", "phone",
    "billing_street", "billing_city", "billing_state", "billing_zip",
    "lifetime_revenue", "outstanding_ar",
    "chow_current_account", "duplicate_of_account", "note",
]
CONTACT_FIELDS = ["name", "title", "email", "phone", "is_active"]

TYPE_LABELS = {
    "create_parent": "Create parent account",
    "nest_parent": "Move parent under Bellhaven",
    "rename_parent": "Rename parent account",
    "chow": "Change of ownership",
    "reparent": "Change parent",
    "match_ambiguous": "Needs a decision",
    "duplicate_conflict": "Duplicates under different owners",
    "create_account": "Create account",
    "mark_duplicate": "Mark duplicate",
    "orphan_review": "Flag for review",
    "update_fields": "Update fields",
    "create_contact": "Add contact",
    "update_contact": "Update contact",
    "needs_human": "Blocked",
}


def account_names():
    """
    id -> name, for resolving a parent id to something readable.

    Read from the index run_pipeline.py writes. Deliberately never falls back to
    the network: a page render must not hang on the CRM. Until the pipeline has
    run once in a folder, parent ids simply show as "unknown id".
    """
    try:
        return json.loads(ACCOUNT_INDEX.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def refresh_account_names(accounts):
    """Keep the index current whenever we already hold the full account list,
    so a successor created by a CHOW resolves by name without another run."""
    try:
        ACCOUNT_INDEX.write_text(
            json.dumps({a["id"]: a.get("name", "") for a in accounts}), encoding="utf-8")
    except OSError:
        pass


def _page(request, template, **context):
    return templates.TemplateResponse(
        request, template,
        {"counts": store.counts(), "groups": store.group_counts(),
         "field_labels": FIELD_LABELS, "type_labels": TYPE_LABELS,
         "care_types": CARE_TYPE_CHOICES, "build": BUILD, **context},
    )


@app.on_event("startup")
def _startup():
    store.init()
    # Printed in the terminal, so you can see WHICH folder this server is
    # serving. A stale server from an old folder is the classic reason for
    # "I updated the files but the screens did not change".
    print(f"\n  Bellhaven Sync build {BUILD}")
    print(f"  serving from {ROOT}\n")


@app.get("/version")
def version():
    """Which build and which folder is answering? A 404 here means you are
    talking to a server older than build 2026-09-21c."""
    return {"build": BUILD, "folder": str(ROOT), "templates": str(ROOT / "templates")}


@app.get("/", response_class=HTMLResponse)
def home():
    return RedirectResponse("/pending", status_code=303)


# ------------------------------------------------------------------ queue
@app.get("/pending", response_class=HTMLResponse)
def pending(request: Request, msg: str = "", group: str = "all", q: str = ""):
    return _page(request, "pending.html",
                 proposals=store.list_proposals("pending", group, q),
                 names=account_names(), group=group, q=q, msg=msg)


@app.get("/decided", response_class=HTMLResponse)
def decided(request: Request, q: str = "", group: str = "all", kind: str = "",
            outcome: str = "executed", msg: str = ""):
    """
    Three filters, each narrowing the last: the group (Hierarchy, CHOW only...),
    the exact action inside it, then the outcome. Defaults to Executed, because
    that is what actually changed the CRM; expired items are superseded noise.
    """
    in_group = store.list_proposals("decided", group, q)
    kinds = {}
    for p in in_group:
        kinds[p["type"]] = kinds.get(p["type"], 0) + 1
    if kind not in kinds:
        kind = ""
    in_kind = [p for p in in_group if not kind or p["type"] == kind]
    outcomes = {o: sum(1 for p in in_kind if p["status"] == o)
                for o in store.DECIDED_OUTCOMES}
    shown = [p for p in in_kind if outcome == "all" or p["status"] == outcome]
    return _page(request, "decided.html", proposals=shown, group=group, q=q,
                 kind=kind, kinds=kinds, outcome=outcome, outcomes=outcomes,
                 total=len(in_kind), msg=msg)


@app.post("/proposals/{proposal_id}/approve")
async def approve(proposal_id: int, request: Request):
    form = await request.form()
    proposal = store.get_proposal(proposal_id)
    if proposal is None:
        return RedirectResponse("/pending?msg=No+such+proposal", status_code=303)

    target = dict(proposal["target"])
    for key, value in form.items():
        if key.startswith("target__"):
            field = key[len("target__"):]
            if field in target and str(value) != str(target.get(field)):
                target[field] = value
        elif key.startswith("newacct__") and "_new_account" in target:
            target["_new_account"][key[len("newacct__"):]] = value

    # Rename the account while approving something else, without a second trip.
    extra_name = (form.get("extra__name") or "").strip()
    if extra_name and extra_name != (proposal["subject_label"] or "").strip():
        if proposal["subject_kind"] == "account" and proposal["type"] != "chow":
            target["name"] = extra_name

    choice = form.get("choice") or None
    if choice:
        target["_choice"] = choice
    store.override_target(proposal_id, target)

    try:
        result = execute(proposal_id, reviewer_note=form.get("reviewer_note") or None,
                         choice=choice)
        message = f"#{proposal_id} done: {json.dumps(result, default=str)[:150]}"
    except Exception as exc:                            # noqa: BLE001
        message = f"#{proposal_id} FAILED: {exc}"
    back = f"/pending?group={form.get('group', 'all')}&msg={message[:400]}"
    return RedirectResponse(back, status_code=303)


@app.post("/proposals/{proposal_id}/reject")
async def reject(proposal_id: int, reviewer_note: str = Form(""), group: str = Form("all")):
    store.set_status(proposal_id, "rejected", reviewer_note or "rejected by reviewer")
    return RedirectResponse(
        f"/pending?group={group}&msg=Rejected+%23{proposal_id}", status_code=303)


@app.post("/bulk-approve")
async def bulk_approve(tier: str = Form("confident"), group: str = Form("all")):
    """Confident field fixes and re-parents only. Never CHOWs or creates."""
    allowed = {"update_fields", "reparent"}
    done, failed = 0, 0
    for proposal in store.list_proposals("pending"):
        if proposal["tier"] != tier or proposal["type"] not in allowed:
            continue
        try:
            execute(proposal["id"], reviewer_note=f"bulk approved ({tier})")
            done += 1
        except Exception:                               # noqa: BLE001
            failed += 1
    return RedirectResponse(
        f"/pending?group={group}&msg=Bulk+approved+{done}+item(s),+{failed}+failed",
        status_code=303)


@app.post("/run")
def run_now(group: str = Form("all")):
    result = subprocess.run([sys.executable, "run_pipeline.py"], cwd=str(ROOT),
                            capture_output=True, text=True, timeout=900)
    tail = (result.stdout or result.stderr).strip().splitlines()[-7:]
    return RedirectResponse(f"/pending?group={group}&msg={' | '.join(tail)[:400]}",
                            status_code=303)


# ------------------------------------------------------------------- admin
@app.get("/admin", response_class=HTMLResponse)
def admin(request: Request, q: str = "", msg: str = ""):
    client = CRMClient()
    accounts = client.list_accounts(q=q) if q else client.list_accounts()
    contacts = client.list_contacts()
    by_account = {}
    for contact in contacts:
        by_account.setdefault(contact.get("account_id"), []).append(contact)
    if not q:
        refresh_account_names(accounts)
    accounts.sort(key=lambda a: (a.get("name") or ""))
    return _page(request, "admin.html", accounts=accounts, contacts=by_account,
                 fields=ACCOUNT_FIELDS, contact_fields=CONTACT_FIELDS,
                 names=account_names(), q=q, msg=msg)


@app.post("/admin/push")
async def admin_push(request: Request):
    """Direct write, outside the proposal flow, for judgement calls a pipeline
    should not be making. Logged like everything else."""
    form = await request.form()
    account_id = form.get("account_id")
    body = {key[3:]: value for key, value in form.items()
            if key.startswith("f__") and value != form.get("orig__" + key[3:], "")}
    if not account_id or not body:
        return RedirectResponse("/admin?msg=Nothing+changed", status_code=303)
    client = CRMClient(allow_writes=True)
    client.update_account(account_id, body)
    for call in client.calls:
        store.log_call(None, call)
    return RedirectResponse(
        f"/admin?msg=Pushed+{'+'.join(body)}+to+{account_id}", status_code=303)


@app.post("/admin/contact")
async def admin_contact(request: Request):
    form = await request.form()
    contact_id = form.get("contact_id")
    account_id = form.get("account_id")
    body = {key[3:]: value for key, value in form.items()
            if key.startswith("c__") and value != form.get("corig__" + key[3:], "")}
    if "is_active" in body:
        body["is_active"] = str(body["is_active"]).lower() in ("true", "1", "yes", "on")
    if not body:
        return RedirectResponse("/admin?msg=Nothing+changed", status_code=303)

    client = CRMClient(allow_writes=True)
    if contact_id:
        client.update_contact(contact_id, body)
        note = f"Updated contact {contact_id}"
    else:
        client.create_contact({**body, "account_id": account_id})
        note = f"Added a contact to {account_id}"
    for call in client.calls:
        store.log_call(None, call)
    return RedirectResponse(f"/admin?msg={note.replace(' ', '+')}", status_code=303)


# ------------------------------------------------------------------- tools
@app.get("/tools", response_class=HTMLResponse)
def tools(request: Request, msg: str = "", preview: str = ""):
    return _page(request, "tools.html", snapshots=snapshot.list_snapshots(),
                 msg=msg, preview=preview)


@app.post("/tools/snapshot")
def take_snapshot():
    path, accounts, contacts = snapshot.save()
    return RedirectResponse(
        f"/tools?msg=Saved+{accounts}+accounts+and+{contacts}+contacts+to+{path.name}",
        status_code=303)


@app.post("/tools/restore")
def do_restore(confirm: str = Form("")):
    result = snapshot.restore(dry_run=confirm != "YES")
    if confirm != "YES":
        return RedirectResponse(
            f"/tools?preview={json.dumps(result['changes'], default=str)[:6000]}"
            "&msg=Dry+run+only.+Type+YES+to+apply.", status_code=303)
    return RedirectResponse(
        f"/tools?msg=Restored+{len(result['changes'])}+account(s)", status_code=303)


@app.post("/tools/reset-history")
def reset_history(confirm: str = Form("")):
    if confirm != "YES":
        return RedirectResponse("/tools?msg=Type+YES+to+confirm", status_code=303)
    store.reset_history()
    return RedirectResponse(
        "/tools?msg=Local+run+history+cleared", status_code=303)


@app.post("/tools/export")
def export_csv():
    """One-way CSV of the whole ledger, for a reviewer who lives in Sheets."""
    path, count = export_ledger_csv()
    return FileResponse(path, filename="bellhaven_ledger.csv", media_type="text/csv",
                        headers={"X-Rows": str(count)})
