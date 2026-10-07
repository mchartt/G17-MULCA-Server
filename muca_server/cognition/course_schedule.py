"""
The project deliverables the main agent reminds students of: what is due at
which group's tutoring (config/knowledge/schedule.yaml).

Only the listed deliverables are tracked; later deadlines exist, but are not
the assistant's to announce.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml


def load_schedule(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _day(value: date) -> str:
    return f"{value:%a} {value.day} {value:%b %Y}"


def render_schedule(schedule: dict) -> str:
    """The whole schedule as text, for the main agent's instructions."""

    lines = [f"# {schedule['title']}", schedule["header"], ""]
    lines += [
        f"Tutoring of Group {group}: {slot}."
        for group, slot in schedule["groups"].items()
    ]
    lines += ["", "Events:"]
    lines += [f"- {_day(event['date'])}: {event['what']}" for event in schedule["events"]]
    lines += ["", "Deliverables, each due at a group's tutoring:"]
    for item in schedule["deliverables"]:
        dates = ", ".join(
            f"Group {group} on {_day(due)}" for group, due in item["due"].items()
        )
        lines.append(f"- {item['what']}: {dates}")
    lines += [f"- Then: {schedule['then']}", "", schedule["untracked"]]
    return "\n".join(lines)


def today_in(schedule: dict) -> date:
    return datetime.now(ZoneInfo(schedule["timezone"])).date()


def next_deliverables(schedule: dict, today: date) -> str:
    """What each group has to deliver next, as of `today` (inclusive)."""

    parts = []
    for group in schedule["groups"]:
        upcoming = next(
            (item for item in schedule["deliverables"] if item["due"][group] >= today),
            None,
        )
        parts.append(
            f"Group {group}: {upcoming['what']}, at the tutoring on "
            f"{_day(upcoming['due'][group])}"
            if upcoming is not None
            else f"Group {group}: {schedule['then']} (not tracked further)"
        )
    return "Next deliverables -- " + "; ".join(parts) + "."
