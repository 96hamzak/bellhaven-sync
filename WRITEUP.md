# Writeup

**Matching: address first, name last.** Records are normalised first: suffixes and directions abbreviated (Street → St), suites dropped, zip+4 cut to five digits, phones to digits, and brand and care words stripped from names, so a rename compares on what is distinctive. Each website community is scored against every CRM account: street 50 (exact, or scaled by similarity), zip 20, city 10, phone 10, name 10. A hard veto (different state, or different zip and city) means a different building: never matched. A soft veto (different street number) keeps the score but caps it below confident, so a reviewer chooses link or create. 88+ is confident; below that a person decides, and cards show only what was deducted. Parent accounts and records already marked duplicate or CHOW'd never match. That handles two Amberly Manors in different states, four renames and Union Square's stale street number.

**Rules**
- Field drift: update name, address, phone, care type (several site offerings map to one CRM value; the rest go in the note).
- Wrong parent: keep the corporate office visible. Harborview facilities stay, and Harborview moves under Bellhaven. Cedar Trail facilities move to a new Cedar Trail – Bellhaven parent; the original is renamed Independent. Anything else goes to Bellhaven.
- SOP: revenue and AR both above zero means CHOW. The old account is untouched except `chow_current_account`; a successor and the site's administrator go under the right parent. Otherwise, re-parent in place.
- Duplicates under one parent: keep the copy with billing, then contacts, then Active; the rest get `duplicate_of_account` and Inactive. Under different parents: all flagged Needs Review, no guess.
- Missing from the CRM: create, or link-or-create for near matches. Under Bellhaven but gone from the site: Needs Review with a dated note.

**Re-run safety: fingerprints.** Each proposal is fingerprinted: a SHA-256 hash of its type, the record it targets and the exact values it would write. Notes, dates and scores are left out on purpose; when the dated note was included, every rejection returned the next day. Fingerprints sit in a unique column of the ledger, which is committed to the repo, so the daily midnight-Pacific run starts from every earlier decision. Each run, an approved or rejected fingerprint is skipped for good, a pending one is refreshed rather than duplicated, and one no longer generated expires. An item returns only if what it would write genuinely changes. Nothing writes without approval, and a test approves everything, applies it to an in-memory CRM and confirms the next run proposes nothing.

**AI.** Built with Claude as a pair programmer: I set the rules and checked every change against live data; it wrote the code and tests.

**Next.** A hosted version (web app, Postgres, login) so the team reviews in one place; a better UI with run stats; then auto-approval for repeatable low-risk fixes once decisions accumulate, and an AI layer that drafts recommendations for judgment calls.
