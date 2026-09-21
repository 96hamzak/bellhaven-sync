"""
The only file in the project that is allowed to talk to the CRM.

It is read-only unless you construct it with allow_writes=True, which only the
executor and the admin screen do. That single flag is the reason a scheduled
run can never change your graded data by accident.

API surface, read from the live OpenAPI schema on 20 Sep 2026:
    GET    /accounts            q, city, state, zip, street, parent_id, page, page_size
    POST   /accounts
    GET    /accounts/{id}
    PATCH  /accounts/{id}
    GET    /contacts            account_id, q, page, page_size
    POST   /contacts
    GET    /contacts/{id}
    PATCH  /contacts/{id}
    GET    /me
There is no DELETE and no reset endpoint. Writes are permanent.
"""
import time

import requests

from config import API_BASE, API_TOKEN, REQUEST_RETRIES, REQUEST_TIMEOUT


class CRMWriteBlocked(RuntimeError):
    """Raised when something tries to write through a read-only client."""


class CRMClient:
    def __init__(self, token=None, allow_writes=False):
        self.token = token or API_TOKEN
        if not self.token:
            raise RuntimeError(
                "No CRM token. Copy .env.example to .env and put your token in it."
            )
        self.allow_writes = allow_writes
        self.calls = []          # every request/response, for the evidence trail

    # ------------------------------------------------------------- plumbing
    def _request(self, method, path, **kwargs):
        if method != "GET" and not self.allow_writes:
            raise CRMWriteBlocked(f"{method} {path} blocked: this client is read-only")

        url = f"{API_BASE}{path}"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }
        last_error = None
        for attempt in range(REQUEST_RETRIES):
            try:
                response = requests.request(
                    method, url, headers=headers, timeout=REQUEST_TIMEOUT, **kwargs
                )
                if response.status_code == 429:           # rate limited
                    time.sleep(2 * (attempt + 1))
                    continue
                self.calls.append(
                    {
                        "method": method,
                        "url": url,
                        "body": kwargs.get("json"),
                        "status": response.status_code,
                        "response": _safe_json(response),
                    }
                )
                response.raise_for_status()
                return _safe_json(response)
            except requests.RequestException as exc:      # noqa: PERF203
                last_error = exc
                time.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"{method} {url} failed: {last_error}")

    def _paged(self, path, params):
        """Walk every page and return one flat list."""
        out, page = [], 1
        while True:
            params = {**params, "page": page, "page_size": 200}
            payload = self._request("GET", path, params=params)
            items = _items_from(payload)
            out.extend(items)
            if len(items) < 200:
                break
            page += 1
            if page > 50:                                 # runaway guard
                break
        return out

    # ------------------------------------------------------------- reads
    def me(self):
        return self._request("GET", "/me")

    def list_accounts(self, **filters):
        return _ensure_id(self._paged("/accounts", filters), ACCOUNT_ID_KEYS, "account")

    def get_account(self, account_id):
        return _ensure_id(
            [self._request("GET", f"/accounts/{account_id}")], ACCOUNT_ID_KEYS, "account"
        )[0]

    def list_contacts(self, account_id=""):
        rows = self._paged("/contacts", {"account_id": account_id} if account_id else {})
        return _ensure_id(rows, CONTACT_ID_KEYS, "contact")

    # ------------------------------------------------------------- writes
    def update_account(self, account_id, fields):
        return self._request("PATCH", f"/accounts/{account_id}", json=fields)

    def create_account(self, fields):
        return self._request("POST", "/accounts", json=fields)

    def update_contact(self, contact_id, fields):
        return self._request("PATCH", f"/contacts/{contact_id}", json=fields)

    def create_contact(self, fields):
        return self._request("POST", "/contacts", json=fields)


def _safe_json(response):
    try:
        return response.json()
    except ValueError:
        return {"_raw": response.text[:2000]}


def _items_from(payload):
    """The API may return a bare list or wrap it. Handle both, plus one level
    of nesting, and fall back to 'whichever value is a list of objects'."""
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in ("items", "data", "results", "accounts", "contacts", "records", "rows"):
        value = payload.get(key)
        if isinstance(value, list):
            return value
        if isinstance(value, dict):                      # one level deeper
            nested = _items_from(value)
            if nested:
                return nested
    for value in payload.values():                       # last resort
        if isinstance(value, list) and value and isinstance(value[0], dict):
            return value
    return []


# Whatever this API calls the primary key, everything downstream relies on "id".
# Normalising here means the rest of the project never has to care.
ACCOUNT_ID_KEYS = ("id", "account_id", "accountId", "Id", "ID", "_id", "uuid", "record_id", "pk")
CONTACT_ID_KEYS = ("id", "contact_id", "contactId", "Id", "ID", "_id", "uuid", "record_id", "pk")


def _ensure_id(records, candidates, label):
    """
    Guarantee every record has an "id" key, or fail with something you can act
    on. A bare KeyError three files downstream tells you nothing; this tells you
    the record's actual field names.
    """
    out = []
    for position, record in enumerate(records):
        if not isinstance(record, dict):
            raise RuntimeError(
                f"{label} #{position} is a {type(record).__name__}, not an object: "
                f"{str(record)[:200]}\nRun  python inspect_api.py  to see what the "
                "API is actually returning."
            )
        if record.get("id") not in (None, ""):
            out.append(record)
            continue
        for key in candidates:
            if record.get(key) not in (None, ""):
                out.append({**record, "id": record[key]})
                break
        else:
            raise RuntimeError(
                f"{label} #{position} has no recognisable id field.\n"
                f"  Fields present : {sorted(record)}\n"
                f"  Fields tried   : {list(candidates)}\n"
                "Fix: add the right name to ACCOUNT_ID_KEYS / CONTACT_ID_KEYS in "
                "bellhaven/crm_client.py.\n"
                "Or run  python inspect_api.py  to see the raw record."
            )
    return out
