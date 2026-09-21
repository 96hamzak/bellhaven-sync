"""
All the knobs live here, in one file, on purpose.

In the live demo you will almost certainly be asked to change something.
Changing a threshold, a routing rule or a list should mean editing THIS file
and nothing else.
"""
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# Bump this when you take a new build. run_pipeline.py prints it, so you can
# always tell whether the files on disk are the ones you think they are.
BUILD = "2026-09-21h"

# ---------------------------------------------------------------- endpoints
SITE_BASE = "https://analyst-assessment-production.up.railway.app"
API_BASE = f"{SITE_BASE}/api/v1"
API_TOKEN = os.getenv("CRM_API_TOKEN", "")

# Read-only CRM browser. Used to deep-link every account from the review app,
# so a reviewer can always open the real record in one click.
CRM_BROWSER_BASE = f"{SITE_BASE}/crm/{API_TOKEN}/accounts"


def crm_link(account_id):
    return f"{CRM_BROWSER_BASE}/{account_id}" if account_id else ""


# ---------------------------------------------------------------- parent ids
BELLHAVEN_PARENT_ID = "0015QAPLGS3FVYEEEM"
HARBORVIEW_PARENT_ID = "001FJZYHR7MLFMNPLL"
CEDAR_TRAIL_PARENT_ID = "001FWSQ30SFW6S7604"

OTHER_PARENT_IDS = {
    "00139TNDS8HNLUZ5A6": "Stonebridge Eldercare",
    "001DAAUWV2J3SHQJ34": "Juniper Point Healthcare",
    "001YRHHXQ5HJ0TCL2U": "Millstone Health Partners",
}

# ------------------------------------------------------------- hierarchy
# The brief says outreach often goes through the corporate office rather than
# the facility, so the CORPORATE LAYER has to stay visible, not be flattened.
# That is why facilities are not all pointed straight at Bellhaven.
#
# The website cannot tell you which community came from Harborview and which
# from Cedar Trail. The CRM's existing parent links can, so the CRM hierarchy
# is treated as the source of truth for MEMBERSHIP, and we only correct
# OWNERSHIP above it.
#
#   Bellhaven Senior Living
#   |-- Harborview Care Group ............ whole family acquired 2025
#   |     `-- its facilities ............. unchanged, they ride along
#   |-- Cedar Trail - Bellhaven .......... NEW: the select communities acquired 2026
#   |     `-- Cedar Trail facilities listed on the Bellhaven website
#   `-- facilities with no parent, or parked under an unrelated operator
#
#   Cedar Trail - Independent ............ the original record, NOT under Bellhaven
#         `-- Cedar Trail facilities absent from the Bellhaven website

# Parent shells that become children of Bellhaven, keeping their own children.
PARENTS_TO_NEST_UNDER_BELLHAVEN = [HARBORVIEW_PARENT_ID]

# Parents only partly acquired: split into an acquired half and an independent
# half, because one record cannot honestly represent both.
CEDAR_BELLHAVEN_NAME = "Cedar Trail Communities - Bellhaven (Parent Account)"
CEDAR_INDEPENDENT_NAME = "Cedar Trail Communities - Independent (Parent Account)"
SPLIT_PARENTS = {CEDAR_TRAIL_PARENT_ID: CEDAR_BELLHAVEN_NAME}

# Where a website facility should end up, given where it sits today:
#   under Harborview   -> leave it; Harborview itself moves under Bellhaven
#   under Cedar Trail  -> the new Cedar Trail - Bellhaven shell
#   anywhere else      -> Bellhaven directly
KEEP_CURRENT_PARENT_IDS = [HARBORVIEW_PARENT_ID]

# Whose children get flagged when they vanish from the website. Harborview's
# children are deliberately excluded: the About page says the whole family
# joined, so absence from the Bellhaven site does not imply a sale.
# Cedar Trail - Bellhaven is added at runtime once that account exists.
ORPHAN_SCOPE_PARENT_IDS = [BELLHAVEN_PARENT_ID]

# ---------------------------------------------------------------- matching
CONFIDENT_SCORE = 88      # clean match, safe to bulk approve
PROBABLE_SCORE = 60       # worth showing a reviewer as the primary match
AMBIGUOUS_SCORE = 45      # good enough to offer as "did you mean this account?"
STATES_IN_FOOTPRINT = {"OH", "MI", "IN", "PA"}

# Duplicates that disagree about their parent are never resolved automatically:
# there is no way to tell which operator really owns the building, so every copy
# is flagged Needs Review with a note listing the others. With this True, a copy
# with NO parent also counts as disagreeing. Set False to treat a blank parent
# as "no opinion", so a blank copy and a parented copy resolve normally.
BLANK_PARENT_CONFLICTS = True

# ---------------------------------------------------------------- care type
OFFERING_TO_CARE_TYPE = {
    "assisted living": "Assisted Living",
    "memory support": "Memory Care",
    "memory care": "Memory Care",
    "short-term rehabilitation & nursing": "Skilled Nursing",
    "short term rehabilitation & nursing": "Skilled Nursing",
    "skilled nursing": "Skilled Nursing",
    "independent living": "Independent Living",
}
CARE_TYPE_CHOICES = [
    "Assisted Living",
    "Memory Care",
    "Skilled Nursing",
    "Independent Living",
]
CARE_TYPE_PRIORITY = {
    "Skilled Nursing": 3,
    "Memory Care": 2,
    "Assisted Living": 1,
    "Independent Living": 0,
}

# ---------------------------------------------------------------- statuses
STATUS_ACTIVE = "Active"
STATUS_INACTIVE = "Inactive"
STATUS_NEEDS_REVIEW = "Needs Review"

# Confirmed against the live API on 20 Sep 2026:
#   accounts  -> account_id, updated_at, status (string), created_by_candidate
#   contacts  -> contact_id, account_id, is_active (bool), title
# Money fields come back as plain integers, not formatted strings.
# The website labels its contact "Administrator"; CRM titles vary.
ADMIN_TITLES = ("administrator", "executive director", "ed", "admin")

# ---------------------------------------------------------------- safety
MIN_EXPECTED_LOCATIONS = 30
REQUEST_TIMEOUT = 20
REQUEST_RETRIES = 3
POLITE_DELAY_SECONDS = 0.3

# Snapshots are taken by hand, from the Tools tab or `python -m bellhaven.snapshot`.
# Set this True if you ever want every pipeline run to save one first (capped at
# one per calendar day). Off by default: a scheduled job quietly writing files is
# a surprise, and deciding when you have a restore point is the reviewer's call.
AUTO_SNAPSHOT = False

# ---------------------------------------------------------------- paths
ROOT = Path(__file__).parent
DB_PATH = ROOT / "data" / "ledger.db"
SNAPSHOT_DIR = ROOT / "data" / "snapshots"
RAW_DIR = ROOT / "data" / "raw"
ACCOUNT_INDEX = ROOT / "data" / "accounts_index.json"
for _d in (DB_PATH.parent, SNAPSHOT_DIR, RAW_DIR):
    _d.mkdir(parents=True, exist_ok=True)
