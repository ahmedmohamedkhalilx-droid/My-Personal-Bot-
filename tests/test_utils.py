from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from bot.config import Contacts, load_contacts
from bot.utils import (
    chunk_message,
    html_to_text,
    matches_filters,
    parse_duration,
    parse_when,
    resolve_recipients,
    split_args,
)

CONTACTS = Contacts(
    groups={
        "team": {"emails": [], "whatsapp": [], "members": ["sara", "omar"]},
        "family": {"emails": ["mom@x.com"], "whatsapp": ["+20 100 111 2223"], "members": []},
    },
    people={
        "sara": {"email": "sara@x.com", "whatsapp": "+201001234567"},
        "omar": {"email": "omar@x.com", "whatsapp": ""},
    },
)


def test_split_args_keeps_extra_pipes_in_body():
    assert split_args("team | Hi | a | b", 3) == ["team", "Hi", "a | b"]
    assert split_args("team | hello", 2) == ["team", "hello"]


def test_resolve_groups_people_and_raw_values():
    r = resolve_recipients("team, family, new@y.com, +20 111 222 3333, nobody", CONTACTS)
    assert r.emails == ["sara@x.com", "omar@x.com", "mom@x.com", "new@y.com"]
    assert r.phones == ["201001234567", "201001112223", "201112223333"]
    assert r.unknown == ["nobody"]


def test_resolve_dedupes():
    r = resolve_recipients("sara, team, SARA@x.com", CONTACTS)
    assert r.emails == ["sara@x.com", "omar@x.com"]


@pytest.mark.parametrize("text,expected", [
    (None, timedelta(hours=1)),
    ("90m", timedelta(minutes=90)),
    ("1h30m", timedelta(minutes=90)),
    ("2 hours", timedelta(hours=2)),
    ("1.5h", timedelta(minutes=90)),
    ("45", timedelta(minutes=45)),
])
def test_parse_duration(text, expected):
    assert parse_duration(text) == expected


def test_parse_duration_all_day_and_bad():
    assert parse_duration("all day") is None
    with pytest.raises(ValueError):
        parse_duration("forever")


def test_parse_when_relative():
    tz = "Africa/Cairo"
    now = datetime(2026, 11, 17, 10, 0, tzinfo=ZoneInfo(tz))
    dt = parse_when("tomorrow 3pm", tz, now=now)
    assert (dt.year, dt.month, dt.day, dt.hour) == (2026, 11, 18, 15)
    assert dt.utcoffset() is not None
    with pytest.raises(ValueError):
        parse_when("blorp", tz)


def test_html_to_text():
    assert html_to_text("<p>Hello&nbsp;<b>class</b></p><p>Quiz on Monday</p>") == "Hello class\n\nQuiz on Monday"


def test_filters():
    assert matches_filters("a@b.com", "hi", "", [], [])
    assert matches_filters("Registrar <reg@aucegypt.edu>", "x", "", ["aucegypt.edu"], [])
    assert matches_filters("x@y.com", "Important ANNOUNCEMENT", "", [], ["announcement"])
    assert not matches_filters("x@y.com", "lunch?", "", ["aucegypt.edu"], ["announcement"])


def test_chunk_message():
    text = "\n".join(["x" * 100] * 100)
    chunks = chunk_message(text, limit=1000)
    assert all(len(c) <= 1000 for c in chunks)
    assert "\n".join(chunks) == text


def test_load_contacts(tmp_path):
    f = tmp_path / "c.yaml"
    f.write_text("contacts:\n  Sara:\n    email: s@x.com\n    whatsapp: 2010\ngroups:\n  Team:\n    members: [sara]\n")
    c = load_contacts(f)
    assert c.people["sara"] == {"email": "s@x.com", "whatsapp": "2010"}
    assert c.groups["team"]["members"] == ["sara"]
    assert load_contacts(tmp_path / "missing.yaml").people == {}
