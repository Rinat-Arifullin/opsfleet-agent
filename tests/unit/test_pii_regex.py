"""Iteration 8a: regex PII scrubber (AC-08.2 regex part, AC-10.7). Synthetic data only."""

import time

import pytest

from opsfleet_agent.guards import pii_regex
from opsfleet_agent.guards.pii_regex import (
    MAX_SCRUB_CHARS,
    TOKENS,
    TRUNCATION_MARKER,
    ScrubResult,
    scrub,
    scrub_for_persistence,
)


def _assert_masked(raw: str, kind: str, text: str | None = None) -> None:
    text = text if text is not None else f"contact: {raw} please"
    result = scrub(text)
    assert raw not in result.text
    assert TOKENS[kind] in result.text
    assert result.findings.get(kind, 0) >= 1


# --- canonical tests ----------------------------------------------------------------


def test_output_filter_redacts_email_phone_address():
    """Regex part of AC-08.2: e-mail and phone. The street-address part is added by the
    NER detector in iteration 8b."""
    answer = (
        "Top customer segment grew 12.5% in 2024. Reach the buyer at "
        "jane.doe@example.com or +1 (555) 010-4477; backup ops-team@example.org, "
        "555-010-9988."
    )
    result = scrub(answer)
    for raw in ("jane.doe@example.com", "ops-team@example.org", "(555) 010-4477", "555-010-9988"):
        assert raw not in result.text
    assert result.findings == {"EMAIL": 2, "PHONE": 2}
    assert "12.5% in 2024" in result.text
    assert result.text.count("<EMAIL>") == 2 and result.text.count("<PHONE>") == 2


def test_user_typed_email_not_persisted():
    """AC-10.7 / R3-M8: a typed e-mail never reaches persistence; only the masked form does.
    State and checkpoints do not exist yet; iteration 14a re-asserts this against the real
    checkpointer, history, summary, trace and router input."""
    persisted: list[str] = []
    msg = "My email is john.smith@example.com, call me on +44 20 7946 0958. Revenue by brand?"
    result = scrub_for_persistence(msg, persisted.append)
    assert len(persisted) == 1
    stored = persisted[0]
    assert "john.smith@example.com" not in stored
    assert "@" not in stored
    assert "7946" not in stored
    assert stored == result.text
    assert "<EMAIL>" in stored and "<PHONE>" in stored
    assert "Revenue by brand?" in stored


def test_persistence_sink_not_called_when_scrub_fails(monkeypatch):
    def boom(_text: str) -> ScrubResult:
        raise RuntimeError("scrubber broken")

    monkeypatch.setattr(pii_regex, "scrub", boom)
    persisted: list[str] = []
    with pytest.raises(RuntimeError):
        scrub_for_persistence("john.smith@example.com", persisted.append)
    assert persisted == []


# --- red team -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "jane.doe@example.com",
        "first.last+tag@sub.example.co.uk",
        "jane @ example.com",
        "jane at example dot com",
        "jane AT example DOT com",
        "jane [at] example [dot] com",
        "jane(at)example(dot)org",
        "jane {at} example {dot} co {dot} uk",
        "jane [at] example.com",
        "jane@example dot com",
        "jane at mail.example dot com",
        "jane＠example.com",  # full-width @
        "jane﹫example.com",  # small commercial at
        "jane＠example．com",  # full-width @ and full stop
        "ja​ne@exa‍mple.com",  # zero-width characters
        # M2: every Cf character and Zs space is folded
        "jane\u2063@example.com",  # invisible separator
        "jane\u2066@example\u2069.com",  # bidi isolates
        "jane\u200e@\u202aexample\u202c.com",  # LRM and bidi embedding
        "jane\u034f@example\u180e.com",  # combining grapheme joiner, Mongolian vowel sep
        "jane\u2007@\u2009example.com",  # figure space, thin space
        # M3: line breaks and tabs around "@"
        "jane\n@example.com",
        "jane@\nexample.com",
        "jane\t@\texample.com",
        # M4: spaced and defanged forms
        "jane @ example . com",
        "jane@ example .com",
        "jane@example[.]com",
        "jane@example(.)com",
        "jane [@] example.com",
        "jane[at]example[.]com",
        "jane@example. com",
        # m1: encoded "@" and "."
        "jane%40example.com",
        "jane%2540example.com",
        "jane&#64;example.com",
        "jane&#064;example.com",
        "jane&#x40;example.com",
        "jane&commat;example.com",
        "jane@example%2Ecom",
        "jane@example&#46;com",
        # optional: plain "at" with a dotted local part
        "jane.doe at example.com",
    ],
)
def test_redteam_email(raw):
    result = scrub(f"contact: {raw} please")
    assert TOKENS["EMAIL"] in result.text
    assert result.findings == {"EMAIL": 1}
    for part in ("jane", "ja", "example"):
        assert part not in result.text


@pytest.mark.parametrize(
    "raw",
    [
        "+1 (555) 010-4477",
        "+1-555-010-4477",
        "+15550104477",
        "+44 20 7946 0958",
        "+44 (0) 20 7946 0958",
        "0044 20 7946 0958",
        "+7 (495) 010-44-77",
        "(555) 010-4477",
        "555-010-4477",
        "555.010.4477",
        "555 010 4477",
        "1-555-010-4477",
        "020 7946 0958",
        "＋1 555 010 4477",  # full-width plus
        "555–010–4477",  # en dashes
        # M1: international numbers with 6-8 digit groups
        "+49 30 1234567",
        "0049 30 1234567",
        "+44 7700 900123",
        "+44 20 79460958",
        "+7 495 0104477",
        "+1 555 0104477",
        "+49 (0)30 1234567",
        # M2: Zs spaces fold to ASCII space
        "555\u2007010\u20074477",  # figure space
        "+44\u300020\u20097946\u205f0958",  # ideographic, thin, medium math space
        # M3: tabs, line breaks and spaced hyphens between groups
        "+1\t555\t010\t4477",
        "555\n010\n4477",
        "555\r\n010\r\n4477",
        "555 - 010 - 4477",
        "555\u2028010\u20284477",  # line separator
    ],
)
def test_redteam_phone_with_separators(raw):
    _assert_masked(raw, "PHONE")


@pytest.mark.parametrize(
    "text",
    [
        "phone: 5550104477",
        "Tel. 5550104477",
        "WhatsApp 5550104477",
        "Phone:\n5550104477",  # m3: one line break after the keyword
        "cell 5550104477",
        "Mobile: 5550104477",
        "contact number 5550104477",
        "Contact  number:\n 555 0104477",
    ],
)
def test_redteam_phone_after_keyword(text):
    result = scrub(text)
    assert "5550104477" not in result.text
    assert result.findings == {"PHONE": 1}


@pytest.mark.parametrize(
    "raw, kind",
    [
        ("4242 4242 4242 4242", "CARD"),
        ("4242-4242-4242-4242", "CARD"),
        ("4242.4242.4242.4242", "CARD"),
        ("4242424242424242", "CARD"),
        ("5555 5555 5555 4444", "CARD"),
        ("3782 822463 10005", "CARD"),
        ("378282246310005", "CARD"),
        ("4111 1111 1111 1111", "CARD"),
        ("４２４２ 4242 4242 4242", "CARD"),  # full-width digits
        ("4242-4242-4242-4241", "ID"),  # card-shaped, Luhn-invalid
        ("1234567890123", "ID"),  # 13 contiguous digits
        ("12345678901234567890123", "ID"),  # longer than any PAN
        ("123-45-6789", "ID"),  # SSN shape
        ("123 45 6789", "ID"),  # m4: SSN with spaces
        ("123.45.6789", "ID"),  # m4: SSN with dots
        ("4242\u20074242\u20074242\u20074242", "CARD"),  # M2: figure spaces
        ("4242\u200b4242\u20604242\ufeff4242", "CARD"),  # M2: zero-width glue
        ("4242\n4242\n4242\n4242", "CARD"),  # M3: line breaks
        ("4242\t4242\t4242\t4242", "CARD"),  # M3: tabs
        ("4242 - 4242 - 4242 - 4242", "CARD"),  # M3: spaced hyphens
    ],
)
def test_redteam_card_like_digit_groups(raw, kind):
    _assert_masked(raw, kind)


def test_long_digit_run_glued_to_letters_is_masked():
    result = scrub("ref SKU12345678901234 shipped")
    assert "12345678901234" not in result.text
    assert result.findings == {"ID": 1}


# --- negative cases: ordinary analytics text survives ----------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Revenue in 2024 was $1,234.56 million, up 12.5% from 2023.",
        "Orders: 48213; returned: 1,204 (2.5%).",
        "Between 2024-01-15 and 2024-03-31T23:59:59 the AOV was $59.99.",
        "Largest aggregate 999999999999 (12 digits) and 1,234,567,890,123 formatted.",
        "SKU-12345, SKU 8812-A, product id 40213, order 1029384.",
        "Price band 250-1000 and 10-20 items; Q3-2024 vs Q4-2024.",
        "Years 2021, 2022, 2023 and 2024.",
        "Years 2021 2022 2023 2024 side by side.",
        "Revenue at thelook.com grew 5% at 10.5 per order.",
        "Traffic source Email drove 3,402 sessions; look at brand Allegra K.",
        "Ratio 0.0123456789 and -12.75% margin; +12.5% YoY.",
        "Version 1.2.3 and port 8080 at 10:30.",
        "Top categories: Jeans 12 345, Tops 9 876.",
        # separator widening (M3) and new keywords (m3) must not eat ordinary text
        "Year\n2021\n2022\n2023\n2024",
        "Years\t2021\t2022\t2023\t2024",
        "Revenue:\n1,234.56\n2,345.67\n3,456.78",
        "Orders by month\nJan 1204\nFeb 1310\nMar 998",
        "Price bands 10 - 20, 20 - 50 and 50 - 100.",
        "Mobile orders in 2023: 1,204; cell 4 of the grid.",
        "Contact number of orders: 1,204.",
        "Growth +12.5% (2023) and +3.2% (2024).",
        "Revenue at thelook.com, Q4 at thelook.com, 5.5 at thelook.com.",
        "Data from thelook_ecommerce.orders at bigquery-public-data.thelook_ecommerce.",
        "Reach ops@acme. Thanks for asking.",
        "Sold 3 @ 5. Then 4 @ 6.",
        "Encoded percent 15%, 40% and 64 items; R&D spend 46%.",
        "Version 1.2.3, IP 10.0.0.1, ratio 123.45 and 6789 units.",
    ],
)
def test_analytics_text_not_over_masked(text):
    result = scrub(text)
    assert result.text == text
    assert result.findings == {}
    assert not result.redacted


def test_business_digit_groups_masked_as_phone_by_design():
    """m6, documented trade-off (fail safe): bare 3-3-4 digit groups are phone-shaped, so
    business identifiers written that way are over-masked as <PHONE> rather than risk a
    leaked phone number. The same holds for a one-column list split by line breaks."""
    assert scrub("IDs 123 456 7890").text == "IDs <PHONE>"
    assert scrub("Counts\n312\n455\n1203").text == "Counts\n<PHONE>"


def test_dotted_local_part_at_domain_masked_by_design():
    """Optional red-team fix, documented trade-off: "<a.b> at <domain.tld>" is masked even
    when it is a table-qualified column, because it is indistinguishable from an address."""
    assert scrub("orders.status at thelook.com").text == "<EMAIL>"


def test_keyword_cell_over_masks_year_pairs_by_design():
    """m3 trade-off: a new keyword followed by 7+ digits is a phone, even two years."""
    assert scrub("Cell phones 2023 2024").text == "Cell phones <PHONE>"


def test_international_phone_over_15_digits_left_to_other_patterns():
    # E.164 caps at 15 digits; longer "+" sequences fall through to the card/ID rules.
    result = scrub("+1 2345 6789 0123 4567")
    assert "<PHONE>" not in result.text


def test_space_separated_luhn_invalid_groups_are_kept_by_design():
    # Documented trade-off: a space-separated non-Luhn sequence is treated as a number list.
    text = "4242 4242 4242 4241"
    assert scrub(text).text == text


# --- properties ---------------------------------------------------------------------


def test_findings_hold_counts_only_never_values():
    result = scrub("a@example.com b@example.com 4242 4242 4242 4242 +1 555 010 4477")
    assert dict(result.findings) == {"EMAIL": 2, "CARD": 1, "PHONE": 1}
    assert all(isinstance(v, int) for v in result.findings.values())
    assert "example" not in repr(result.findings)


def test_findings_are_immutable():
    result = scrub("a@example.com")
    with pytest.raises(TypeError):
        result.findings["EMAIL"] = 0  # type: ignore[index]


@pytest.mark.parametrize(
    "text",
    [
        "jane@example.com, +44 20 7946 0958, 4242 4242 4242 4242, 123-45-6789",
        "nothing to see in 2024",
    ],
)
def test_deterministic_and_idempotent(text):
    once = scrub(text)
    assert scrub(text) == once
    again = scrub(once.text)
    assert again.text == once.text
    assert again.findings == {}


def test_empty_and_type():
    assert scrub("") == ScrubResult(text="", findings={}, truncated=False)
    with pytest.raises(TypeError):
        scrub(None)  # type: ignore[arg-type]


def test_input_over_cap_is_truncated_and_scrubbed():
    tail_email = "late@example.com"
    text = "x " * (MAX_SCRUB_CHARS // 2 - 10) + "jane@example.com " + tail_email * 100
    result = scrub(text)
    assert result.truncated
    assert result.text.endswith(TRUNCATION_MARKER)
    assert "jane@example.com" not in result.text
    assert "late@" not in result.text and "example.com" not in result.text
    assert len(result.text) <= MAX_SCRUB_CHARS + len(TRUNCATION_MARKER)


def test_truncation_drops_bisected_trailing_token():
    """m5: a value cut in half at the cap no longer matches any pattern, so the trailing
    partial token is dropped rather than returned."""
    email = "jane.doe@example.com"
    prefix = "x " * ((MAX_SCRUB_CHARS - 12) // 2)
    text = prefix + email + " tail" * 10
    assert (prefix + email)[:MAX_SCRUB_CHARS].endswith(" jane.doe@exa")  # bisected
    result = scrub(text)
    assert result.truncated
    assert "jane" not in result.text and "@" not in result.text
    assert result.text == prefix + TRUNCATION_MARKER


def test_truncation_without_whitespace_drops_everything():
    result = scrub("a" * (MAX_SCRUB_CHARS + 1))
    assert result.truncated
    assert result.text == TRUNCATION_MARKER


@pytest.mark.parametrize(
    "unit",
    [
        "a",
        "a@",
        "a.",
        "1",
        "1 ",
        "1-",
        "+1 ",
        "+1(1)",
        "00 1 ",
        "4242 ",
        "a at b dot ",
        "a [at] b [dot] ",
        "x@y.",
        "phone ",
        "phone: 1 2 3 4 5 6 ",
        "​",
        # patterns widened in the second review
        "\t",
        "\n",
        "1\t",
        "1\n",
        "1 - ",
        "1\n\n",
        "+1\t",
        "+1 1234567 ",
        "+1 (1)",
        "0049 ",
        "[",
        "[.]",
        "a [.] ",
        "a [@] b [.] ",
        "a(.)",
        "@ ",
        " @ ",
        "a @\n",
        "a @ b . ",
        "a.b at ",
        "a.b at c.",
        "a . ",
        "%40",
        "%2540",
        "&#64;",
        "&#x40;",
        "phone\n",
        "cell \n",
        "123 45 ",
        "1.1.",
        "\u2066",
        "\u2007",
    ],
)
def test_adversarial_input_finishes_fast(unit):
    text = unit * (MAX_SCRUB_CHARS // len(unit))
    start = time.perf_counter()
    scrub(text)
    assert time.perf_counter() - start < 1.0
