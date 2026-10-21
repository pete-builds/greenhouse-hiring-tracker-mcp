"""Pure text helpers: HTML to text, salary extraction, remote detection.

Everything here is deterministic and network-free so it can be tested on
fixtures. Connectors call these when an ATS gives prose instead of fields.
"""

from __future__ import annotations

import html as _html
import re

from bs4 import BeautifulSoup

#: Descriptions are stored for search and scoring, not for display. A cap
#: keeps one pathological posting from bloating the database.
MAX_DESCRIPTION_CHARS = 20_000

_CURRENCY_SYMBOLS = {"$": "USD", "£": "GBP", "€": "EUR"}
_CODES = "USD|GBP|EUR|CAD|AUD|CHF|SGD|INR|JPY"

# "$150,000 - $200,000", "$150K–$200K USD", "USD 150,000 to 200,000",
# "$75 - $95 per hour". Group names keep the match readable at the call site.
_RANGE = re.compile(
    rf"(?P<sym>[£$€])?\s*(?P<pre>{_CODES})?\s*(?P<lo>\d[\d,.]*)\s*(?P<lok>[kK])?"
    rf"\s*(?:[—–\-]|to)\s*"
    rf"(?P<sym2>[£$€])?\s*(?P<hi>\d[\d,.]*)\s*(?P<hik>[kK])?\s*(?P<post>{_CODES})?"
    r"(?P<tail>[^.\n]{0,40})",
)
_HOURLY = re.compile(r"\b(per hour|/\s?h(ou)?r|hourly)\b", re.IGNORECASE)
_OTE = re.compile(r"\b(on[- ]target earnings|OTE|total target compensation)\b", re.IGNORECASE)

_REMOTE = re.compile(r"\b(remote|anywhere|distributed|work from home|wfh)\b", re.IGNORECASE)
_NOT_REMOTE = re.compile(r"\b(no[nt]?[- ]remote|on[- ]?site only|in[- ]office only)\b", re.IGNORECASE)
_HYBRID = re.compile(r"\bhybrid\b", re.IGNORECASE)
_ONSITE = re.compile(r"\b(office|on[- ]?site|in[- ]person)\b", re.IGNORECASE)

# Places that are definitely outside the US. Used to tell "Remote - Sweden"
# and "Toronto, Canada" from "Remote - US" and "Austin, TX" when a profile
# wants US roles: a short list of the countries, regions and cities that show
# up on these boards, not a gazetteer.
_NON_US = re.compile(
    r"\b(canada|toronto|vancouver|montreal|mexico|brazil|s[aã]o paulo|argentina|colombia|chile|"
    r"uk|united kingdom|england|scotland|london|ireland|dublin|germany|berlin|munich|france|"
    r"paris|spain|madrid|barcelona|portugal|lisbon|italy|milan|netherlands|amsterdam|belgium|"
    r"brussels|switzerland|zurich|sweden|stockholm|norway|denmark|copenhagen|finland|poland|"
    r"warsaw|czech|prague|austria|vienna|romania|bulgaria|sofia|greece|turkey|istanbul|israel|"
    r"tel aviv|uae|dubai|india|bangalore|bengaluru|hyderabad|pune|mumbai|delhi|singapore|"
    r"japan|tokyo|korea|seoul|china|beijing|shanghai|hong kong|taiwan|australia|sydney|"
    r"melbourne|new zealand|philippines|vietnam|indonesia|emea|apac|latam|europe|eu)\b",
    re.IGNORECASE,
)
_US = re.compile(
    r"(?i:\b(us|usa|u\.s\.a?|united states|americas|north america|"
    r"alabama|alaska|arizona|arkansas|california|colorado|connecticut|delaware|florida|georgia|"
    r"hawaii|idaho|illinois|indiana|iowa|kansas|kentucky|louisiana|maine|maryland|massachusetts|"
    r"michigan|minnesota|mississippi|missouri|montana|nebraska|nevada|new hampshire|new jersey|"
    r"new mexico|new york|north carolina|north dakota|ohio|oklahoma|oregon|pennsylvania|"
    r"rhode island|south carolina|south dakota|tennessee|texas|utah|vermont|virginia|washington|"
    r"west virginia|wisconsin|wyoming|nyc|sf|san francisco|seattle|boston|chicago|austin|denver|"
    r"atlanta|los angeles|miami)\b)"
    r"|,\s*(A[LKZR]|C[AOT]|DE|FL|GA|HI|I[DLNA]|K[SY]|LA|M[EDAINSOT]|N[EVHJMYCD]|O[HKR]|PA|RI|"
    r"S[CD]|T[NX]|UT|V[TA]|W[AVIY]|DC)\b",
)


def us_status(location: str | None) -> str:
    """"us", "non_us", "mixed", or "unknown" for a location string."""
    if not location:
        return "unknown"
    us = bool(_US.search(location))
    non_us = bool(_NON_US.search(location))
    if us and non_us:
        return "mixed"
    if us:
        return "us"
    return "non_us" if non_us else "unknown"


def html_to_text(raw: str | None) -> str:
    """Strip HTML to whitespace-normalised text, decoding entity-encoded input.

    Greenhouse's per-job endpoint returns `&lt;div&gt;` while its list endpoint
    returns real tags; decoding once when only entities are present handles
    both without double-decoding literal ampersands.
    """
    if not raw:
        return ""
    if "&lt;" in raw and "<" not in raw[:200]:
        raw = _html.unescape(raw)
    text = BeautifulSoup(raw, "html.parser").get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text)[:MAX_DESCRIPTION_CHARS]


def _amount(num: str, k: str | None) -> float | None:
    cleaned = num.replace(",", "")
    # "120.000" is a European thousands separator, "120.5" is a decimal.
    if re.fullmatch(r"\d{1,3}(\.\d{3})+", cleaned):
        cleaned = cleaned.replace(".", "")
    try:
        value = float(cleaned)
    except ValueError:
        return None
    return value * 1000 if k else value


def parse_salary(text: str | None) -> dict | None:
    """Find the first plausible pay range in free text.

    Returns ``{min, max, currency, period, comp_type, evidence}`` with amounts
    in whole annual units (hourly ranges are annualised at 2,080 hours), or
    None. ``evidence`` is a window around the match, not the head of the text,
    so a caller quoting it quotes the sentence that holds the numbers.

    A range qualifies only when it carries a currency marker and lands in a
    salary-shaped band. That is what keeps "10-15 years" and "2-3 days a week"
    out of the comp column.
    """
    if not text:
        return None
    for m in _RANGE.finditer(text):
        sym = m.group("sym") or m.group("sym2")
        code = m.group("pre") or m.group("post")
        if not sym and not code:
            continue
        lo = _amount(m.group("lo"), m.group("lok"))
        hi = _amount(m.group("hi"), m.group("hik") or m.group("lok"))
        if lo is None or hi is None:
            continue
        # "$150-200K": the K on the high end applies to both.
        if m.group("hik") and not m.group("lok") and lo < 1000:
            lo *= 1000
        if hi < lo:
            continue
        window = text[max(0, m.start() - 120) : m.end() + 120]
        period = "hour" if _HOURLY.search(m.group("tail") or "") or hi < 500 else "year"
        if period == "hour":
            if hi > 500:
                continue
            lo, hi = lo * 2080, hi * 2080
        elif lo < 15_000 or hi > 5_000_000:
            continue
        return {
            "min": int(lo),
            "max": int(hi),
            "currency": code or _CURRENCY_SYMBOLS.get(sym or "$", "USD"),
            "period": period,
            "comp_type": "ote" if _OTE.search(window) else "base",
            "evidence": window.strip(),
        }
    return None


def detect_remote(*fields: str | None) -> bool | None:
    """True when any field says remote, False when one says on-site only.

    None means the posting did not say, which scoring treats differently from
    a definite no: an unstated location is a question for the screen call,
    not a reason to drop the role.
    """
    joined = " ".join(f for f in fields if f)
    if not joined:
        return None
    if _NOT_REMOTE.search(joined):
        return False
    if _REMOTE.search(joined):
        return True
    # "Hybrid - San Francisco", "SF Office; NYC Office": a stated workplace
    # that is not remote is a definite no.
    if _HYBRID.search(joined) or _ONSITE.search(joined):
        return False
    return None
