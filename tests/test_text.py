import pytest

from hub.text import detect_remote, html_to_text, parse_salary, us_status


@pytest.mark.parametrize(
    ("text", "lo", "hi", "period"),
    [
        ("The base range is $150,000 - $200,000 USD per year.", 150_000, 200_000, "year"),
        ("Pay: $150K–$195K plus equity", 150_000, 195_000, "year"),
        ("Compensation $150-200K", 150_000, 200_000, "year"),
        ("USD 151,000 to 196,000 base", 151_000, 196_000, "year"),
        ("€120.000 – €140.000 per annum", 120_000, 140_000, "year"),
        ("$95 - $100 per hour on contract", 197_600, 208_000, "hour"),
    ],
)
def test_parse_salary_ranges(text, lo, hi, period):
    got = parse_salary(text)
    assert got and (got["min"], got["max"], got["period"]) == (lo, hi, period)


@pytest.mark.parametrize(
    "text",
    [
        "10-15 years of experience",
        "Work 2-3 days a week in office",
        "Series B, 50-100 employees",
        "",
    ],
)
def test_parse_salary_ignores_ranges_that_are_not_pay(text):
    assert parse_salary(text) is None


def test_salary_evidence_is_the_sentence_with_the_numbers():
    """The old parser returned the first 500 chars, which on a long posting
    did not contain the salary it reported."""
    text = "About us. " * 200 + "The salary range is $290,000 - $435,000 USD." + " More." * 50
    got = parse_salary(text)
    assert "$290,000" in got["evidence"]


def test_ote_is_flagged():
    assert parse_salary("OTE $200,000 - $260,000 USD")["comp_type"] == "ote"


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        (("Remote - US",), True),
        (("San Francisco, CA",), None),
        (("Hybrid - New York",), False),
        (("New York", "on-site only"), False),
        ((None, ""), None),
    ],
)
def test_detect_remote(fields, expected):
    assert detect_remote(*fields) is expected


def test_html_to_text_decodes_entity_encoded_html():
    assert html_to_text("&lt;p&gt;Hello &amp;amp; welcome&lt;/p&gt;") == "Hello & welcome"
    assert html_to_text(None) == ""


@pytest.mark.parametrize(
    ("location", "status"),
    [
        ("Remote - United States", "us"),
        ("San Francisco, CA", "us"),
        ("US, TX, Remote", "us"),
        ("Remote - Sweden", "non_us"),
        ("Toronto, Canada", "non_us"),
        ("Dublin, IE", "non_us"),
        ("Remote, Canada; Remote, US", "mixed"),
        ("2 Locations", "unknown"),
        (None, "unknown"),
    ],
)
def test_us_status(location, status):
    assert us_status(location) == status
