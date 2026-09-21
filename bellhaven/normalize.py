"""
Turning messy human text into something two records can be compared on.

Nothing here ever gets written to the CRM. We keep the raw value for writing
and the normalized value for matching. That distinction matters: the CRM should
end up holding "1800 N Blanchard St", not "1800 n blanchard st".
"""
import re

SUFFIXES = {
    "street": "st", "str": "st", "st": "st",
    "avenue": "ave", "av": "ave", "ave": "ave",
    "road": "rd", "rd": "rd",
    "drive": "dr", "dr": "dr",
    "boulevard": "blvd", "blvd": "blvd",
    "lane": "ln", "ln": "ln",
    "court": "ct", "ct": "ct",
    "circle": "cir", "cir": "cir",
    "parkway": "pkwy", "pkwy": "pkwy",
    "place": "pl", "pl": "pl",
    "terrace": "ter", "ter": "ter",
    "highway": "hwy", "hwy": "hwy",
    "trail": "trl", "trl": "trl",
    "way": "way",
}
DIRECTIONALS = {
    "north": "n", "south": "s", "east": "e", "west": "w",
    "northeast": "ne", "northwest": "nw",
    "southeast": "se", "southwest": "sw",
}
UNIT_WORDS = {"suite", "ste", "apt", "unit", "#", "bldg", "building", "floor", "fl"}

# Words that carry no distinguishing information in a facility name.
NAME_STOPWORDS = {
    "bellhaven", "senior", "living", "the", "of", "at", "and", "a",
    "care", "center", "centre", "healthcare", "health",
    "rehabilitation", "rehab", "nursing", "community", "communities",
    "group", "llc", "inc", "parent", "account",
}

STATE_NAMES = {
    "ohio": "OH", "michigan": "MI", "indiana": "IN", "pennsylvania": "PA",
    "colorado": "CO", "illinois": "IL", "kentucky": "KY", "west virginia": "WV",
    "new york": "NY", "wisconsin": "WI",
}


def _clean(text):
    if text is None:
        return ""
    return re.sub(r"\s+", " ", str(text)).strip()


def norm_text(text):
    """Lowercase, strip punctuation, collapse spaces."""
    t = _clean(text).lower()
    t = re.sub(r"[^\w\s#&]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def norm_street(street):
    """
    '1800 N. Blanchard Street, Suite 3'  ->  '1800 n blanchard st'

    Unit/suite parts are dropped on purpose: two records for the same building
    should match even when one of them records a suite number.
    """
    t = norm_text(street)
    if not t:
        return ""
    words, out = t.split(), []
    for w in words:
        if w in UNIT_WORDS:
            break                      # everything after 'suite' is unit detail
        out.append(DIRECTIONALS.get(w, SUFFIXES.get(w, w)))
    return " ".join(out).strip()


def street_number(street):
    """The leading house number, or '' if there isn't one."""
    m = re.match(r"^\s*(\d+)", _clean(street))
    return m.group(1) if m else ""


def norm_zip(value):
    """'45840-1234' -> '45840'. Anything that isn't 5 digits returns ''."""
    digits = re.sub(r"\D", "", _clean(value))
    return digits[:5] if len(digits) >= 5 else ""


def norm_state(value):
    v = _clean(value)
    if len(v) == 2:
        return v.upper()
    return STATE_NAMES.get(v.lower(), v.upper()[:2])


def norm_city(value):
    return norm_text(value)


def norm_phone(value):
    """'(231) 533-2969' -> '2315332969'. US 1- prefix is dropped."""
    digits = re.sub(r"\D", "", _clean(value))
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits if len(digits) == 10 else ""


def norm_name(value):
    """Strip the brand and the generic care words so 'Bellhaven Healthcare
    Centre of Ashland' and 'Bellhaven Health Care Center of Ashland' both
    reduce to 'ashland'."""
    words = [w for w in norm_text(value).split() if w not in NAME_STOPWORDS]
    return " ".join(words)


def parse_city_state_zip(line):
    """
    'Findlay, OH 45840' -> ('Findlay', 'OH', '45840')
    Tolerates zip+4 and a missing comma.
    """
    line = _clean(line)
    m = re.match(r"^(.*?),?\s*([A-Za-z]{2})\.?\s+(\d{5})(?:-\d{4})?$", line)
    if m:
        return _clean(m.group(1)), m.group(2).upper(), m.group(3)
    return line, "", ""


def to_money(value):
    """
    The CRM may send 47000, 47000.0, '47000', '$47,000.00' or null.
    All of them have to become a number we can compare to zero.
    Returns None only when the value is genuinely unparseable.
    """
    if value is None or value == "":
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    cleaned = re.sub(r"[^\d.\-]", "", str(value))
    if cleaned in ("", "-", "."):
        return 0.0
    try:
        return float(cleaned)
    except ValueError:
        return None


def location_key(street, zip_code):
    """
    The stable identity of a physical building.

    Street + zip, not the URL slug and not the name. A facility can be renamed
    and its web address can change; the building stays where it is.
    """
    return f"{norm_street(street)}|{norm_zip(zip_code)}"



def usd(value):
    """
    47000 -> $47,000   3800.5 -> $3,800.50   -500 -> -$500   None -> $0

    For display and notes only. Anything that is not a number is shown exactly
    as it came, so a garbage value in the CRM is visible instead of becoming $0.
    """
    if value is None or value == "":
        return "$0"
    try:
        amount = float(str(value).replace("$", "").replace(",", ""))
        whole = amount == int(amount)
    except (ValueError, OverflowError):
        return str(value)
    sign = "-" if amount < 0 else ""
    text = f"{abs(amount):,.0f}" if whole else f"{abs(amount):,.2f}"
    return f"{sign}${text}"