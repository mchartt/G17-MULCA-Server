"""Long-term memory by day: the day log and rendering (docs/MEMORY_DESIGN.md)."""

from datetime import datetime

from muca_server.cognition.memory.day_memory import (
    DAY_TZ,
    day_bounds,
    day_log_lines,
    day_of,
    render_summary,
    resolve_period,
)
from muca_server.cognition.memory.records import EpisodicRecord


def at(hour, minute=0):
    return datetime(2026, 10, 14, hour, minute, tzinfo=DAY_TZ).timestamp()


def record(when, content, *, path="none", speaker="Ada", modality="text", reply=None):
    return EpisodicRecord(
        timestamp=when,
        conversation_id="c",
        intervention_path=path,
        speaker=speaker,
        content=content,
        modality=modality,
        assistant_output=reply,
    )


def test_a_day_is_the_groups_calendar_day_in_italy():
    late = datetime(2026, 10, 14, 23, 30, tzinfo=DAY_TZ).timestamp()
    after_midnight = datetime(2026, 10, 15, 0, 30, tzinfo=DAY_TZ).timestamp()

    assert day_of(late) == "2026-10-14"
    assert day_of(after_midnight) == "2026-10-15"
    start, end = day_bounds("2026-10-14")
    assert start <= late < end <= after_midnight


def test_the_day_log_has_every_turn_and_mulcas_replies_and_no_times():
    lines = day_log_lines(
        [
            record(at(10), "hey mulca start", modality="audio", reply="Hi Ada"),
            record(at(10, 1), "", path="untranscribed"),  # noise: left out
            record(at(10, 2), "the deadline is Friday", speaker="Grace"),
        ],
        assistant_name="MULCA",
    )

    assert lines == [
        "Ada (voice): hey mulca start",
        "MULCA (voice): Hi Ada",
        "Grace (text): the deadline is Friday",
    ]


def test_a_long_gap_or_a_call_ending_separates_sessions():
    lines = day_log_lines(
        [
            record(at(10), "morning call"),
            record(at(10, 5), "bye"),
            record(at(10, 20), "back in text"),
            record(at(18), "evening"),
        ],
        assistant_name="MULCA",
        call_ends=[at(10, 6), at(19)],  # the last one separates nothing
    )

    assert lines == [
        "Ada (text): morning call",
        "Ada (text): bye",
        "--- voice call ended ---",
        "Ada (text): back in text",
        "--- break of 7h 40m ---",
        "Ada (text): evening",
    ]


def test_a_summary_renders_narrative_first_then_what_it_adds():
    text = render_summary(
        {
            "narrative": "They chose the weather app.",
            "decisions": [
                {
                    "decision": "build a weather app",
                    "decided_by": ["Ada"],
                    "rationale": "useful",
                    "date": "14 Oct",
                    "changed_on": "",
                }
            ],
            "action_points": [
                {"owner": "Grace", "task": "API link", "status": "open", "raised_on": "14 Oct"},
                {"owner": "Ada", "task": "sketch", "status": "done", "raised_on": "13 Oct"},
            ],
            "knowledge": [{"text": "the API is free", "since": "14 Oct"}],
            "contributions": [{"name": "Ada", "items": ["proposed the app", "sketched it"]}],
            "mulca": ["found the API"],
        },
        heading="Overview:",
    )

    assert text.startswith("Overview:\nThey chose the weather app.")
    assert "- 14 Oct: build a weather app -- decided by Ada; because useful" in text
    assert "Open action points:\n- [Grace] API link (raised 14 Oct)" in text
    assert "Action points done:\n- [Ada] sketch (raised 13 Oct)" in text
    assert "- the API is free (since 14 Oct)" in text
    assert "- Ada: proposed the app; sketched it" in text
    assert "MULCA's part:\n- found the API" in text


def test_a_named_period_becomes_its_days():
    today = "2026-10-15"  # a Thursday
    days = ["2026-10-08", "2026-10-12", "2026-10-14"]

    assert resolve_period("today", today, days) == ("2026-10-15", "2026-10-15")
    assert resolve_period("yesterday", today, days) == ("2026-10-14", "2026-10-14")
    assert resolve_period("last_session", today, days) == ("2026-10-14", "2026-10-14")
    assert resolve_period("this_week", today, days) == ("2026-10-12", "2026-10-15")
    assert resolve_period("last_week", today, days) == ("2026-10-09", "2026-10-15")
    assert resolve_period("2026-10-08", today, days) == ("2026-10-08", "2026-10-08")


def test_all_or_an_unknown_period_is_the_whole_history():
    assert resolve_period("all", "2026-10-15", []) is None
    assert resolve_period("", "2026-10-15", []) is None
    assert resolve_period("before the break", "2026-10-15", []) is None


def test_the_last_session_with_no_earlier_day_is_today():
    assert resolve_period("last_session", "2026-10-15", []) == ("2026-10-15", "2026-10-15")
