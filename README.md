# Bellhaven Sync

Keeps the CRM's picture of who owns which Bellhaven facility in step with the
Bellhaven website. Proposes changes automatically, writes nothing without a
human approval, and is safe to run every day.

Built for the Clipboard Health analyst assessment.
**New here? Start with [OVERVIEW.md](OVERVIEW.md)**, a plain-language tour.
Setting it up yourself? Follow [IMPLEMENTATION_GUIDE.md](IMPLEMENTATION_GUIDE.md).

## Quick start

```bash
python -m pip install -r requirements.txt
cp .env.example .env            # then paste your token into it
python -m pytest -q             # 54 tests, no network needed
python doctor.py                # which build, which server, what's already in the CRM
python -m bellhaven.snapshot    # save the CRM before touching it
python run_pipeline.py          # propose (never writes)
python -m uvicorn app:app --reload   # review at http://127.0.0.1:8000
python audit.py                 # prove the end state is correct
python export_ledger.py         # optional: CSV for a reviewer in Sheets
```

## How it fits together

```
Website  ──► scraper ──┐
                        ├─► matcher ─► planner ─► ledger ─► review app ─► CRM
CRM (read) ────────────┘              (+ SOP)    (SQLite)   (you click)   (writes)
```

The scheduled job runs everything up to the ledger. It physically cannot write:
`CRMClient` raises on any non-GET request unless constructed with
`allow_writes=True`, which only the executor and the admin screen do.

| File | Job |
| --- | --- |
| `config.py` | Every threshold, id and mapping. Change behaviour here first. |
| `bellhaven/scraper.py` | Crawls the site, parses each detail page **by label**. |
| `bellhaven/normalize.py` | Addresses, names, phones, money into comparable form. |
| `bellhaven/matcher.py` | Scores location against account. Hard vetoes. |
| `bellhaven/planner.py` | Classification and the CHOW SOP. No network, fully testable. |
| `bellhaven/store.py` | SQLite ledger. Fingerprints. Re-run safety. |
| `bellhaven/executor.py` | The only writer. Pre-write drift check, post-write verify. |
| `bellhaven/snapshot.py` | Save and restore CRM state. |
| `app.py` + `templates/` | FastAPI review app. |
| `audit.py` | End-state checks against the live CRM. |
| `export_ledger.py` | One-way CSV of the ledger, for review in Google Sheets. |
| `inspect_api.py` | Read-only dump of the API's real shape and field names. |
| `doctor.py` | Which build is on disk, which app answers on port 8000, and what has already been written to the CRM. |

### Confirmed API schema (20 Sep 2026)

Envelope: `{"data": [...], "page": 1, "page_size": 50, "total": 121}`.
Accounts key off **`account_id`**, contacts off **`contact_id`**; the client
normalizes both to `id` so nothing downstream has to care. Accounts carry a
string `status`; contacts carry a boolean `is_active`. Money fields are plain
integers. `updated_at` is server-set. `created_by_candidate` marks records the
pipeline created, which is what makes a restore able to find them.
| `.github/workflows/daily.yml`, `schedule/crontab.txt` | Schedule config. |

## Design decisions worth knowing

**Address first, name last.** The dataset punishes name matching: the website's
Amberly Manor is in Hudson OH, the CRM's is in Colorado Springs CO under a
different operator; Carlisle and New Carlisle are different towns. Street plus
zip is the identity of a building. Names change on rebrand, and four facilities
here are already renamed.

**Vetoes, not just low scores.** A different state, or a different street number
on the same street, kills a pair outright however similar the names are.

**The SOP is one condition.** Revenue above zero AND AR above zero means the old
account is preserved untouched and a successor is created under Bellhaven, with
`chow_current_account` pointing at it. Everything else is a plain re-parent.
Null money counts as zero; a negative AR is not "greater than zero".

**The corporate layer is built, not flattened.** The brief says outreach often
goes through the corporate office rather than the facility, so the intermediate
operator is operationally useful. Harborview becomes a child of Bellhaven and
keeps its facilities. Cedar Trail is split — a new `Cedar Trail - Bellhaven`
shell under Bellhaven for the communities on the website, the original renamed
`- Independent` for the rest. Facilities with no parent, or parked under an
unrelated operator, go straight to Bellhaven. The website cannot say which
community came from where, so the CRM's existing parent links are treated as
true for membership and only ownership above them is corrected.

**Two veto levels.** Hard means provably different: another state, or a
different zip *and* city. Soft means suspicious but survivable: a stale street
number. A soft veto keeps its score and surfaces as "did you mean this account?"
rather than silently producing a duplicate — which is exactly what one veto
level did to *Union Square Senior Living*.

**Orphans get Needs Review, not Inactive.** A sold facility is still a live
sales target. The website cannot distinguish sold from closed.

**No LLM inside the pipeline.** Non-deterministic output would change proposal
fingerprints run to run, which breaks re-run safety. AI was used to build and
review this code, not to execute it.

## Schedule and re-run safety

`.github/workflows/daily.yml` runs the pipeline every day at 00:00
America/Los_Angeles, using GitHub's `timezone` field so PST/PDT is handled
automatically, plus a **Run workflow** button for manual runs. Each run:

1. checks out the repo, which brings back the ledger at `ledger/ledger.db`,
   the one ledger the app, the pipeline and the scheduled run all share,
2. proposes, and only proposes: the scheduled job has no write access to the CRM,
3. exports the day's proposals as a CSV artifact,
4. commits the updated ledger for tomorrow.

Every proposal carries `sha256(type + subject + what it writes)`. Notes, dates,
scores and labels are excluded, so a decision made today is still recognised
tomorrow. A decided fingerprint is never raised again; a genuine change on the
website produces a new fingerprint and comes back exactly once. A test approves
everything the pipeline proposes, applies it to an in-memory CRM and runs again:
the pipeline settles by the third run, with nothing left to propose.

`schedule/crontab.txt` is the same schedule for a Linux server.

## Known limits

- The API has no DELETE, so restore can revert edits but can only mark created
  accounts Inactive.
- `care_type` holds one value while the website lists several. The reviewer picks
  from a dropdown; the full list goes in the note.
- The Sheets export is one way. The sheet is a window on the ledger, not a
  source of truth: nothing typed there flows back.
- Your machine and the repo each hold a copy of `ledger/ledger.db` until the app
  is hosted with one shared database; upload yours after a review session.
