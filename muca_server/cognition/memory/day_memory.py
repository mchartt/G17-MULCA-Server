"""
Long-term memory by day (docs/MEMORY_DESIGN.md): what a day's log looks like
to the summarizer, and how a summary reads to the agents.

Every summary is generated from the stored log, never rewritten from a
previous summary -- the management cycle this replaces rewrote one running
summary every 24 messages, which kept it at ~250 words however much had
happened, and drifted it into abstraction.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .records import EpisodicRecord

# Where the groups are: a "day" is their calendar day, not the server's.
DAY_TZ = ZoneInfo("Europe/Rome")

# A gap this long within a day reads as a new session to the summarizer.
SESSION_BREAK_SECONDS = 30 * 60

# Rows that hold nothing anybody said.
_NOT_SPOKEN = ("management_cycle", "untranscribed")


def day_of(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, DAY_TZ).date().isoformat()


def day_bounds(day: str) -> tuple[float, float]:
    """[start, end) of a day, as Unix timestamps."""

    start = datetime.combine(date.fromisoformat(day), time(), DAY_TZ)
    return start.timestamp(), (start + timedelta(days=1)).timestamp()


def _duration(seconds: float) -> str:
    minutes = int(seconds // 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m" if hours else f"{minutes}m"


def day_log_lines(
    records: list[EpisodicRecord],
    *,
    assistant_name: str,
    call_ends: list[float] = (),
) -> list[str]:
    """
    One line per turn -- the speaker's, then MULCA's reply if it gave one --
    in order, with no per-turn times: the day is the timestamp. A separator
    marks a long gap or a voice call ending, so two sessions on one day
    aren't read as one conversation.
    """

    entries: list[tuple[float, int, str]] = []
    for record in records:
        if record.intervention_path in _NOT_SPOKEN or not record.content:
            continue
        channel = "voice" if record.modality == "audio" else "text"
        entries.append(
            (record.timestamp, 0, f"{record.speaker} ({channel}): {record.content}")
        )
        if record.assistant_output:
            entries.append(
                (
                    record.timestamp,
                    1,
                    f"{assistant_name} ({channel}): {record.assistant_output}",
                )
            )
    for ended in call_ends:
        entries.append((ended, 2, "--- voice call ended ---"))
    entries.sort(key=lambda entry: (entry[0], entry[1]))

    lines: list[str] = []
    previous: float | None = None
    for timestamp, kind, text in entries:
        if kind == 2:
            if lines and lines[-1] != text:
                lines.append(text)
            continue
        if previous is not None and timestamp - previous >= SESSION_BREAK_SECONDS:
            lines.append(f"--- break of {_duration(timestamp - previous)} ---")
        lines.append(text)
        previous = timestamp
    # A call ending after the last thing said separates nothing.
    while lines and lines[-1] == "--- voice call ended ---":
        lines.pop()
    return lines


def estimate_tokens(text: str) -> int:
    return len(text) // 4


def _bullets(items: list[str]) -> str:
    return "".join(f"- {item}\n" for item in items if item)


def render_summary(summary: dict, *, heading: str) -> str:
    """A day summary, "today so far" or the overview, for an agent to read."""

    parts = [f"{heading}\n{summary.get('narrative', '').strip()}\n"]

    decisions = []
    for d in summary.get("decisions", []):
        line = d.get("decision", "")
        when = d.get("date")
        if when:
            line = f"{when}: {line}"
        if d.get("changed_on"):
            line += f" (changed on {d['changed_on']})"
        by = ", ".join(d.get("decided_by", []))
        if by:
            line += f" -- decided by {by}"
        if d.get("rationale"):
            line += f"; because {d['rationale']}"
        decisions.append(line)
    if decisions:
        parts.append("Decisions:\n" + _bullets(decisions))

    open_points, done_points = [], []
    for a in summary.get("action_points", []):
        line = f"[{a.get('owner', 'unassigned')}] {a.get('task', '')}"
        if a.get("raised_on"):
            line += f" (raised {a['raised_on']})"
        (done_points if a.get("status") == "done" else open_points).append(line)
    if open_points:
        parts.append("Open action points:\n" + _bullets(open_points))
    if done_points:
        parts.append("Action points done:\n" + _bullets(done_points))

    for key, title in (("conclusions", "Conclusions"), ("knowledge", "Knowledge gained")):
        items = []
        for item in summary.get(key, []):
            if isinstance(item, dict):
                text = item.get("text", "")
                if item.get("since"):
                    text = f"{text} (since {item['since']})"
                items.append(text)
            else:
                items.append(item)
        if items:
            parts.append(f"{title}:\n" + _bullets(items))

    contributions = [
        f"{c.get('name', '?')}: " + "; ".join(c.get("items", []))
        for c in summary.get("contributions", [])
        if c.get("items")
    ]
    if contributions:
        parts.append("Contributions:\n" + _bullets(contributions))

    for key, title in (
        ("decision_making", "How decisions were made"),
        ("process_notes", "How the process went"),
        ("mulca", "MULCA's part"),
    ):
        if summary.get(key):
            parts.append(f"{title}:\n" + _bullets(summary[key]))

    return "\n".join(parts).strip()


def day_heading(day: str) -> str:
    return f"{date.fromisoformat(day):%A %d %B %Y}"


# What the intervention agent may name as the period a question is about
# (InterventionSignal.history_period); a date as YYYY-MM-DD too. It names
# the period, the code does the calendar: date arithmetic and "which day
# was the last session" are where a small model slips.
HISTORY_PERIODS = (
    "all",
    "today",
    "yesterday",
    "last_session",
    "this_week",
    "last_week",
)


def resolve_period(
    period: str,
    today: str,
    days: list[str],
) -> tuple[str, str] | None:
    """
    The days (first, last; ISO, inclusive) a history question is about, or
    None for everything -- also for anything unrecognised, which keeps the
    old whole-history behaviour rather than guessing.

    days: the days before today with a summary, i.e. with activity.
    """

    period = (period or "all").strip().lower()
    current = date.fromisoformat(today)
    if period == "today":
        return today, today
    if period == "yesterday":
        day = (current - timedelta(days=1)).isoformat()
        return day, day
    if period == "last_session":
        earlier = sorted(d for d in days if d < today)
        return (earlier[-1], earlier[-1]) if earlier else (today, today)
    if period == "this_week":
        monday = current - timedelta(days=current.weekday())
        return monday.isoformat(), today
    if period == "last_week":
        # The last seven days, today included -- not the previous calendar
        # week: found live, "last week" on a Thursday meant the past week,
        # and Mon-Sun left out the session the day before.
        return (current - timedelta(days=6)).isoformat(), today
    try:
        day = date.fromisoformat(period).isoformat()
    except ValueError:
        return None
    return day, day


def period_label(first: str, last: str) -> str:
    if first == last:
        return day_heading(first)
    return f"{day_heading(first)} to {day_heading(last)}"
