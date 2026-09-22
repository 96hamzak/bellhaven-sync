# Bellhaven Sync — step by step

Read this top to bottom. Do not skip ahead. Anything in a grey box is meant to
be copied and pasted into a terminal exactly as written.

Total time if nothing goes wrong: **about 90 minutes**, of which roughly 40 is
you reading proposals and deciding.

---

## Before you start: two warnings

**1. This CRM has no undo.** The API has GET, POST and PATCH. It has no DELETE
and no reset. Every write is permanent, and the graders look at your final data.
Step 6 takes a snapshot before you write anything. Do not skip it.

**2. Your token is a password.** It is already written into `.env.example` as a
placeholder. You put the real one in `.env`, which is git-ignored, so it never
reaches GitHub. Never paste it into a code file.

---

## Step 0 — What this thing actually does

Five pieces, in a line:

```
Website  ──►  Scraper  ──┐
                          ├──►  Matcher  ──►  Planner  ──►  Ledger  ──►  Review app  ──►  CRM
CRM (read) ──────────────┘                    (+ SOP)      (SQLite)     (you click)      (writes)
```

* The **scraper** reads all 35 Bellhaven communities off the public website.
* The **matcher** works out which CRM account is which building.
* The **planner** decides what should change, and applies the CHOW rule.
* The **ledger** is a small database file that remembers every decision, so a
  second run never asks you the same question twice.
* The **review app** is a web page on your own machine. Nothing reaches the CRM
  until you click Approve.

The scheduled job runs everything up to the ledger. It cannot write. That is
enforced in code: the API client refuses anything other than GET unless it is
built with `allow_writes=True`, which only the review app and the admin screen
do.

---

## Step 1 — Install Python and check it works

You said Python is already installed. Confirm it.

Open a terminal (Windows: press Start, type `cmd`, hit Enter. Mac: press
Cmd+Space, type `terminal`, hit Enter) and run:

```
python --version
```

You want **3.10 or higher**. If you get "command not found", try `python3
--version` instead, and use `python3` everywhere below.

If neither works, install Python from <https://python.org/downloads> and tick
**"Add Python to PATH"** on the first screen of the installer. That tickbox is
the single most common thing people miss.

---

## Step 2 — Put the project on your machine

You do not have Git installed and you do not need it.

1. Unzip the `bellhaven-sync-<build>.zip` somewhere easy to reach, for example
   your Desktop.
2. In the terminal, move into the folder. Type `cd ` (with a space), then drag
   the unzipped folder from your file browser onto the terminal window and press
   Enter. That pastes the path for you.
3. Confirm you are in the right place:

```
dir        (Windows)
ls         (Mac)
```

You should see `app.py`, `run_pipeline.py`, `config.py` and a folder called
`bellhaven`.

---

## Step 2b — Moving to a newer build (do this every time)

Old screens after an update almost always mean an old copy is still running.
Follow this exactly.

1. **Close every terminal window.** Every one. This stops any review app still
   running from an old folder. If you skip this, the browser keeps talking to
   the old app and nothing you extract makes any difference.
2. **Find the newest zip.** Windows names repeat downloads
   `bellhaven-sync (1).zip`, `bellhaven-sync (2).zip` and so on. In Downloads,
   sort by *Date modified* and take the top one.
3. **Extract it.** Right-click the zip, *Extract All*, and accept whatever
   destination Windows suggests. Every zip from build `2026-09-21d` onwards
   contains a folder named after its build, so the code always lands in a folder
   like `...\bellhaven-sync-2026-09-21c\` that cannot be confused with, or
   merged into, an older one. Windows sometimes nests it one level deeper; the
   folder you want is whichever one directly contains `app.py`.
4. **Copy three things across from your old folder:** the `.env` file, the
   `ledger` folder (every decision you have made), and `data\snapshots` (your
   restore points). Use Copy and Paste rather than dragging: in Windows, dragging
   between folders on the same drive MOVES a file instead of copying it.
5. **Open one new terminal** and `cd` into the inner folder.
6. **Run the doctor:**

```
python doctor.py
```

Every line should say `[OK ]`. It checks that all the files come from the same
build, that nothing stale is holding port 8000, and — most important — what has
**already been written to the CRM**, read from the CRM itself.

7. **Start the app.** The terminal prints which build and which folder it is
   serving:

```
  Bellhaven Sync build 2026-09-21d
  serving from C:\Users\...\bellhaven-sync-2026-09-21d
```

8. **In the browser, press Ctrl+F5.** The build number appears in the header
   next to *Bellhaven Sync*. If it does not, you are still looking at an old app.

You can also open <http://127.0.0.1:8000/version> at any time. It names the build
and folder answering. A `{"detail":"Not Found"}` there means an old build is
running.

---

## Step 3 — Install the libraries

```
python -m pip install -r requirements.txt
```

This downloads nine free packages. It takes a minute or two. Warnings in yellow
are fine. Red text saying `ERROR` is not; if that happens, try:

```
python -m pip install --user -r requirements.txt
```

---

## Step 4 — Add your token

In the project folder, make a copy of `.env.example` and name the copy `.env`
(just `.env`, no other extension). Open it in any text editor and replace the
placeholder with the personal token from your assessment page, so it reads
like this (with your token, not these letters):

```
CRM_API_TOKEN=bh_paste_your_own_token_here
```

Save it. Your token only ever lives in `.env`, which is never uploaded: anyone
holding it can change your CRM copy. Then confirm the CRM can hear you:

```
python -c "from bellhaven.crm_client import CRMClient; print(CRMClient().me())"
```

You should see a short line of JSON with your name in it. If you see
"No CRM token", the file is named wrong — Windows loves to save it as
`.env.txt`. Turn on "show file extensions" in Explorer and rename it.

---

## Step 5 — Run the tests

```
python -m pytest -q
```

Expected: **28 passed**. These run with no internet and no token. They check the
matching traps in this dataset and all six branches of the CHOW rule. Run them
again any time you change something.

---

## Step 5a — Check you are running the build you think you are

```
python -c "import config; print(config.BUILD)"
```

Every build has a stamp, and `run_pipeline.py` prints it on its first line. If a
fix does not seem to have taken effect, check this before anything else — a file
copied into the wrong folder looks identical to a bug.

---

## Step 5b — Look at what the API actually returns

```
python inspect_api.py
```

Read-only. It prints the envelope shape, every field name on a real account and
a real contact, the valid `status` and `care_type` values, and the list of parent
accounts with their ids. Thirty seconds now saves you guessing later, and it is
the first thing to run whenever anything downstream complains about a field.

---

## Step 6 — Snapshot the CRM (do not skip)

```
python -m bellhaven.snapshot
```

Writes every account and contact to `data/snapshots/`. Nothing is automatic —
the pipeline never takes one for you, it only warns on its snapshot line if none
exists yet. Take one now, and again before any large batch of approvals.

The limit is honest: field edits revert cleanly, but accounts the pipeline
*created* cannot be deleted by this API and are marked Inactive instead. So a
missing snapshot hurts most on `create` approvals.

---

## Step 7 — Run the pipeline

```
python run_pipeline.py
```

It will print the communities as it scrapes them, then a summary like:

```
Generated this run:
  create_account       1
  mark_duplicate       1
  orphan_review        2
  reparent             3
  update_fields        7
```

**Nothing has been written to the CRM yet.** This command is safe to run as many
times as you like.

If it says `ABORTED. Only N locations found` — good. That is the safety gate
doing its job. It means the scrape came back short, and rather than proposing
"not on the website" for thirty healthy facilities, it stopped. Check the
website loads in your browser and run it again.

---

## Step 8 — Open the review app

```
python -m uvicorn app:app --reload
```

Leave that terminal window running, and open <http://127.0.0.1:8000> in your
browser. To stop the app later, click the terminal and press `Ctrl+C`.

You get four tabs:

| Tab | What it is for |
| --- | --- |
| **Pending** | The queue. One card per proposed change. |
| **Decided** | Everything already ruled on. Three filters narrow each other: group, then the exact action, then the outcome. Opens on *Executed*, which is what actually changed the CRM; *Expired* items were superseded and never written. |
| **Admin** | Every account as a collapsed row. Expand for all fields including parent, revenue and AR, plus the contacts on that account, all editable. Writes immediately. |
| **Tools** | Snapshot, restore, and clearing local history. |

Each pending card shows you, in order: what kind of change it is, how confident
the match is, the website evidence, the SOP numbers if ownership is moving, a
field-by-field diff, and the exact API calls that clicking Approve will send.
Every proposed value is an editable box. Change anything before approving and
your version is what gets written.

---

## Step 9 — Review and approve

This is the part that is actually graded. The queue shows one collapsed line per
change — type, account, confidence — and expands when you click. Filter chips
across the top give you **CHOW only**, **Accounts only**, **Contacts only**,
**Hierarchy**, **Duplicates**, **Orphans** and **Needs a decision**.

Items are grouped by consequence and alphabetical by account within each group,
whichever run produced them. Work down it:

| # | Group | Why it sits here |
| --- | --- | --- |
| 1 | Create parent account | nothing can route into a parent that does not exist |
| 2 | Move parent under Bellhaven | the Harborview layer |
| 3 | Rename parent account | Cedar Trail's independent half |
| 4 | Change of ownership (CHOW) | the SOP, the part graded hardest |
| 5 | Change parent | plain re-parents |
| 6 | Needs a decision | link an existing account, or create |
| 7 | Create account | permanent, so after the decisions that might avoid one |
| 8 | Mark duplicate | needs the survivor settled first |
| 9 | Flag for review | orphans, last of the account-level work |
| 10 | Update fields | cosmetic |
| 11 | Update contact | |
| 12 | Add contact | |

**1. Hierarchy first, and it needs two runs.** You will see *Create parent
account* for `Cedar Trail Communities - Bellhaven`. Approve it, then press **Run
pipeline** again. Cedar Trail facilities cannot be routed until that account has
a real id, so the pipeline holds them and names them in the run output rather
than guessing. Also here: moving Harborview under Bellhaven, and renaming the
original Cedar Trail record to `- Independent` so nobody confuses the two halves.

**2. Needs a decision.** These are the ones where an account looks close but not
identical — usually a stale street number. You get radio buttons: link to the
existing account, or create a new one. Each option shows its own address, score,
revenue, AR and a CRM link, plus what would happen if you pick it. Check the
candidate in the CRM before choosing; creating a duplicate is permanent.

**3. CHOW and re-parenting.** Read the ownership panel: revenue, AR, current
parent, proposed parent, and the rule applied.

> Revenue above zero **and** AR above zero → CHOW: leave the old account alone,
> create a successor, point `chow_current_account` at it.
> Anything else → change the parent in place.

On a CHOW, the website's administrator contact goes on the **new** account only.
The old record is touched nowhere except the CHOW pointer, because the SOP says
leave it exactly as it is — so you will never see an *Update fields* or *Add
contact* item for it. That holds even on the first run, while a Cedar Trail
facility is still waiting on its new parent: if its revenue and AR are both above
zero, nothing at all is proposed on it until the CHOW can be.
`python audit.py` proves this at the end, field by field against your earliest
snapshot.

**4. Duplicates.** Two kinds of card:

* *Mark duplicate* — every copy sits under the **same** parent, so the pipeline
  picks a survivor: billing history first, then contacts, then Active, then
  whichever looks most like the website. The account id is only ever the last
  resort. If the card warns that both copies carry billing history, think before
  clicking.
* *Duplicates under different owners* — the copies sit under **different**
  parents (Monroe has three: Bellhaven, Harborview and Cedar Trail). Nothing in
  the data says who really owns the building, so the pipeline picks nothing and
  moves nothing. Approving flags every copy *Needs Review* with a note naming the
  others. To resolve one later, mark the wrong copies as duplicates of the right
  one in the Admin tab; the next run carries on from the one left standing.
  A copy with no parent counts as disagreeing; set `BLANK_PARENT_CONFLICTS = False`
  in `config.py` to change that.

**5. Field updates.** Name, address and care-type corrections. There is a bulk
button for the confident ones — read five, then bulk-approve the rest.

**6. Orphans.** Bellhaven children no longer on the website. Default is *Needs
Review* with a dated note, not *Inactive*: a sold facility is still someone's
sales target, and the website cannot tell you which happened.

**Things you can do on any card:** rename the account while approving something
else, edit any proposed value before it is written, type a parent id and see its
name resolve next to it, and open the record in the CRM in one click.

**A care-type note.** The website lists several offerings; the CRM holds one
value from a list of four. The card shows every offering and lets you pick the
single CRM value from a dropdown. The full list goes into the note.

**On match scores.** Only deductions are shown. "90/100 — phone not on file in
CRM (−10)" tells you what is missing; listing what matched would not.

## Step 10 — Prove the data is right

```
python audit.py
```

This re-reads the live CRM and the live website and checks six things, including
"no account with revenue and AR above zero was re-parented". Every line must say
PASS. This is the single best thing to have on screen in your demo.

---

## Step 11 — Prove a second run is safe

```
python run_pipeline.py
```

You want `new: 0`. Refresh the Pending tab; it should still be empty. That is
the re-run requirement in the brief, demonstrated rather than asserted.

---

## Step 11b — Optional: hand the queue to a non-technical reviewer

```
python export_ledger.py
```

That writes `data/ledger_export.csv`: one row per proposal, with what changed,
why, the SOP branch, the website link and your decision, all in plain English.
There is also a **Download ledger CSV** button on the Tools tab.

To put it in a sheet: open <https://sheets.new>, then **File → Import → Upload**,
drag the CSV in, choose *Replace current sheet*, and click Import data.

This is one way on purpose. The sheet is a window on the ledger, never a source
of truth — nothing typed there comes back. If a reviewer spots something wrong,
they tell you the proposal id and you fix it in the app.

---

## Step 12 — Put it on GitHub and schedule it for midnight Pacific

No Git needed; everything happens in the browser.

**Prepare the folder**

1. Your decisions already live in `ledger\ledger.db`, the one ledger the app,
   the pipeline and the scheduled run all use. Uploading the `ledger` folder is
   what lets the scheduled run recognise everything you have decided.
2. Never upload `.env` (your token) or the `data` folder (snapshots and local
   files).

**Create the repository**

3. Open <https://github.com/new>, name it `bellhaven-sync`, choose **Public**,
   leave "Add a README" unticked, click **Create repository**.
4. Click **uploading an existing file**. Select everything in the project folder,
   Ctrl+click `.env` and `data` to deselect them, drag the rest in, and click
   **Commit changes**.
5. Check the file list shows `.github`, `bellhaven`, `ledger`, `templates` and
   `tests`, and **no** `.env`. If `.github` is missing, click **Add file → Create
   new file**, type `.github/workflows/daily.yml` as the name (the slashes make
   the folders), paste the file's contents, and commit.

**Give the scheduled run access**

6. **Settings → Secrets and variables → Actions → New repository secret**.
   Name `CRM_API_TOKEN`, value your token, **Add secret**. Secrets are encrypted
   and hidden from logs, even in a public repository.
7. **Settings → Actions → General → Workflow permissions**: choose **Read and
   write permissions**, **Save**. This lets the run commit its ledger back.

**Test it, then leave it**

8. Open the **Actions** tab (enable workflows if GitHub asks), choose *Bellhaven
   CRM sync*, click **Run workflow**. After about two minutes you get a green tick.
9. Open the run. The pipeline step should end with `new: 0` and a count of items
   already decided; a new commit, *chore: ledger after scheduled run*, appears on
   the code page; and the run's **run-evidence** download holds the day's CSV.
10. Click **Run workflow** again. `new: 0` again is re-run safety, demonstrated.

From now on it runs by itself every day at 00:00 Los Angeles time, which is
12:00 in Pakistan in summer and 13:00 in winter. GitHub sometimes starts
scheduled runs a few minutes late, and emails you if one fails, for example
when the scrape safety gate aborts. The daily ledger commit counts as activity,
which stops GitHub disabling the schedule after 60 quiet days.

**Your daily routine.** The midnight run commits what it found to the repo's
copy of `ledger/ledger.db`. To bring that onto your machine, press **Fetch from
GitHub** on the Tools tab (or run `python sync_ledger.py`), then work through the
queue. There is no need to run the pipeline yourself. The fetch merges rather than
overwrites: any decision you made here always wins, even one you never uploaded,
and your ledger is backed up first.

After a session in which you **rejected** anything, upload your `ledger` folder to
the repo (**Add file → Upload files**, drag it in, commit), so the scheduled run
stops reporting it. Approvals need no upload: they are already in the CRM.

In a public repo the ledger is public too, which is fine for this fictional
sandbox but not for real CRM data. A hosted version with one private database
removes both the fetch and the upload.

---

## Step 13 — Write the submission

`WRITEUP.md` holds the submission writeup, and `OVERVIEW.md` the plain-language
version for anyone opening the project for the first time.
Rewrite it in your own words — the graders are reading for your judgement, not
for polished prose. Then fill in the three submission fields on the assessment
page: repo link, honest time spent, and the writeup.

On time: count the planning, not just the typing. If it took you five hours,
write five hours. The brief says a fast sound submission beats a slow perfect
one, which means they are checking whether your estimate is honest, not whether
it is impressive.

---

## If something goes wrong

| What you see | What it means | What to do |
| --- | --- | --- |
| Screens unchanged after updating | An old app is still running, or you are in the old folder | Close every terminal, then follow step 2b. `python doctor.py` names the cause |
| `/version` says Not Found | An app older than build 2026-09-21c is answering | Close every terminal, or run the PowerShell line the doctor prints |
| Doctor: "moved OUT of Harborview" | The old build flattened some facilities before you switched | Send the doctor output to Claude before approving anything else |
| `No CRM token` | `.env` missing or misnamed | Check for `.env.txt` on Windows |
| `KeyError: 'id'` | You are on an old build | Check the build stamp (step 5a). The current build normalizes `account_id` to `id` automatically |
| `has no recognisable id field` | The API renamed the primary key again | `python inspect_api.py`, then add the name to `ACCOUNT_ID_KEYS` in `bellhaven/crm_client.py` |
| Any "missing field" complaint | The schema differs from what was assumed | `python inspect_api.py` prints every field name on a real record |
| `ABORTED. Only N locations found` | Scrape came back short | Open the website in a browser; rerun |
| `no care offerings found` next to a community | The page layout changed | Open `data/raw/community_<slug>.html` and look at the markup around the words "Care Offerings" |
| `Account changed since this was proposed` | Someone edited it after the proposal | Re-run the pipeline to refresh, then approve |
| `Already executing or already decided` | You double-clicked Approve | Expected. The guard worked. Refresh the page |
| A proposal you want back | You rejected it | It returns only if the evidence changes; or use the Admin tab |

**To undo everything:** Tools tab → Restore (dry run first, then type YES) →
Clear history. Remember: accounts the pipeline *created* cannot be deleted by
this API. Restore marks them Inactive with a note instead.

---

## Preparing for the 45-minute demo

They will ask you to make a small live change. Almost everything tunable lives
in `config.py`, on purpose. Practise these three, with the AI switched off:

1. **Change a threshold.** `CONFIDENT_SCORE` from 88 to 92, rerun, watch
   proposals move from confident to probable.
   Or flatten the hierarchy: empty `PARENTS_TO_NEST_UNDER_BELLHAVEN` and
   `SPLIT_PARENTS` in `config.py`, rerun, and every facility routes straight to
   Bellhaven. One line, visible effect, and it shows the corporate layer was a
   design choice rather than an accident.
2. **Add a normalization rule.** Add `"pike": "pk"` to `SUFFIXES` in
   `bellhaven/normalize.py`, then `pytest -q`.
3. **Add a field to the evidence panel.** One line in `templates/pending.html`.

Be ready to answer these in one sentence each:

* *Why address-first matching?* Because the dataset is built to punish name
  matching. The website's "Amberly Manor" is in Hudson, OH; the CRM's is in
  Colorado Springs, CO, under a different operator. Carlisle and New Carlisle
  are different towns. A building is identified by where it stands.
* *What is a veto?* Not a low score — a provable difference. Different state,
  or different street number on the same street, kills the pair outright
  regardless of how similar the names are.
* *How is a re-run safe?* Every proposal has a fingerprint made of its type,
  its subject and the exact values to be written. Decided fingerprints are never
  raised again. Scores and timestamps are deliberately excluded, because they
  change every run and would break it.
* *Why no LLM inside the pipeline?* Non-deterministic output would change the
  fingerprints between runs, which destroys re-run safety. AI was used to build
  and check the system, not to run it.
* *Why three tiers instead of pointing everything at Bellhaven?* The brief says
  outreach often goes through the corporate office rather than the facility. A
  rep working a former Harborview building needs to know that office still
  exists and that Bellhaven now sits above it. Flattening would delete the fact
  the brief says the team needs.
* *How do you know which community came from Harborview and which from Cedar
  Trail?* You cannot tell from the website. The CRM's existing parent links are
  treated as true for membership, and only the ownership layer above them is
  corrected. That assumption is stated in the writeup.
* *What went wrong with Union Square, and what did you change?* A single veto
  level zeroed the score of an account with a stale street number, which sorted
  it below three irrelevant records and produced a "create new account"
  proposal. Now a bad street number is a soft veto that keeps its score, ranking
  is by raw score, and the reviewer gets an explicit choice between linking the
  existing account and creating a new one.
* *Why "Needs Review" for orphans rather than "Inactive"?* A sold facility is
  still a live sales target. The website cannot tell you whether a missing
  facility was sold or closed, and the brief gives us a status that means
  exactly "a human should look at this".
