# Bellhaven Sync, in plain words

## The problem

Clipboard Health sells to care facilities. When a facility belongs to a larger
company, a sale usually has to go through that company's head office, so the
sales team needs to know who owns every facility. Owners change all the time,
and the CRM, the database the sales team works from, quietly falls out of date.

## What this does

Every night it reads the public website of one operator, Bellhaven Senior
Living, which lists every community Bellhaven runs. It compares that list with
the CRM, and wherever the two disagree it writes a suggested fix. For example,
"this facility should sit under Bellhaven, not Harborview", or "these two records
are the same building". It never changes the CRM by itself: a person approves
every change.

## A day in the life

1. **Midnight, Pacific time:** GitHub starts the job automatically.
2. It reads the 35 communities on Bellhaven's website: name, address, services,
   administrator and phone number.
3. It reads the CRM. It can only look; it has no permission to change anything.
4. It matches each community to its CRM record and writes a list of suggested
   fixes, each with the evidence behind it.
5. A reviewer clicks **Fetch from GitHub** to bring the night's findings onto
   their machine, then opens the review queue: one line per suggestion. Clicking
   a line shows what the website says, what the CRM says, and why the change is
   suggested. They approve it, edit it, or reject it.
6. Only approved changes are written to the CRM.

## How it decides

- **Same building means same address,** not same name. Names change when a
  facility is rebranded, and the same name can exist in two different states.
- **The corporate office stays visible.** Bellhaven bought the whole Harborview
  group, so Harborview's facilities stay under Harborview, which now sits under
  Bellhaven. It bought only some of Cedar Trail's, so Cedar Trail is split into
  the part Bellhaven owns and the part it does not.
- **The billing rule.** If a facility that is changing owner still has money
  owed to Clipboard, its old record is left exactly as it is for the billing
  team. A new record is created under the new owner and linked to the old one.
  This is called a change of ownership, or CHOW.
- **Duplicate records.** If the same building appears twice under the same
  owner, the copy with billing history or contacts is kept and the others are
  retired. If the copies sit under *different* owners, nobody can tell which is
  right, so all of them are flagged for a person to decide.
- **Gone from the website** means flagged for review, not deleted. A facility
  that was sold is still someone's customer.
- **When in doubt, it asks.** A close-but-imperfect match is shown as a choice,
  never guessed.

## Why running it every day is safe

It keeps a record of every suggestion and every decision, called the ledger.
Each suggestion gets an ID made from exactly what it would change, so the same
suggestion always gets the same ID. Once something has been approved or
rejected, that ID is never suggested again, unless the website itself changes
in a way that would alter the fix.

## What is in the folder

| Where | What it does |
| --- | --- |
| `run_pipeline.py` | The nightly job: read, compare, suggest |
| `app.py` and `templates/` | The review app a person uses to decide |
| `sync_ledger.py` | Brings the nightly run's findings from GitHub onto your machine |
| `bellhaven/` | The logic: reading the site, matching, the rules, the ledger |
| `ledger/ledger.db` | The memory of every suggestion and decision |
| `.github/workflows/daily.yml` | The midnight schedule |
| `audit.py` | A final check that the CRM ended up correct |
| `tests/` | Automatic checks that the rules behave as described |

## Try it

```
python -m pip install -r requirements.txt
python run_pipeline.py
python -m uvicorn app:app --reload
```

Then open <http://127.0.0.1:8000>. You need a CRM token in a `.env` file first;
[IMPLEMENTATION_GUIDE.md](IMPLEMENTATION_GUIDE.md) walks through it step by step.
