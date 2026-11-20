"""
What Cadence may propose when a member asks it, in words, to change the plan
(command "ask"): the `action` of its answer, checked here against the plan
before it becomes a card. Cadence never changes the plan itself: every action
is a pending card a member confirms (plan_commands._action_card).

    new_task     one new task, maybe with owner and date   -> task_owner card
    plan         several new tasks at once                 -> plan card
    dependency   one task waits on another                 -> blocker card
    update_task  owner, date, status or progress of a task -> check-in card, one option

update_task and plan were added after the 20/11 test space: asked "segna il
prototipo al 50%" or "crea queste tre task", Cadence answered "non posso"
because it had no card for them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from .models import MAX_TITLE, PlanItem, Task, TaskStatus

MAX_PLAN_ITEMS = 8
_OPT_ID = {"type": ["integer", "null"]}
_OPT_TEXT = {"type": ["string", "null"]}

# The `action` field of ASK_SCHEMA (strict: every key is always present).
ACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": ["none", "new_task", "plan", "dependency", "update_task"]},
        "task_id": _OPT_TEXT,
        "title": {"type": "string"},
        "owner_id": _OPT_ID,
        "due_date": _OPT_TEXT,
        "status": {"type": ["string", "null"], "enum": [*(s.value for s in TaskStatus), None]},
        "progress": _OPT_ID,
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"title": {"type": "string"}, "owner_id": _OPT_ID, "due_date": _OPT_TEXT},
                "required": ["title", "owner_id", "due_date"],
                "additionalProperties": False,
            },
        },
        "depends_on_task_id": _OPT_TEXT,
        "depends_on_title": {"type": "string"},
        "depends_on_owner_id": _OPT_ID,
    },
    "required": ["kind", "task_id", "title", "owner_id", "due_date", "status", "progress", "items",
                 "depends_on_task_id", "depends_on_title", "depends_on_owner_id"],
    "additionalProperties": False,
}


@dataclass(frozen=True, slots=True)
class AskAction:
    """A plan change a member asked Cadence for, checked against the plan; it becomes a card."""

    kind: str  # "new_task" | "plan" | "dependency" | "update_task"
    task_id: str | None
    title: str
    owner_id: int | None = None
    due_date: date | None = None
    depends_on_task_id: str | None = None
    depends_on_title: str = ""
    depends_on_owner_id: int | None = None
    status: TaskStatus | None = None  # update_task only
    progress: int | None = None  # update_task only, 0-100
    items: tuple[PlanItem, ...] = ()  # plan only


def _title(value: Any) -> str:
    return " ".join(str(value or "").split())[:MAX_TITLE]


def _due(value: Any, today: date | None) -> date | None:
    due = date.fromisoformat(value) if value else None
    if due is not None and today is not None and due < today:
        raise ValueError("due date in the past")
    return due


def _owner(value: Any, members: dict[int, str]) -> int | None:
    if value is not None and value not in members:
        raise ValueError("owner is not a member")
    return value


def ask_action(data: dict, tasks: dict[str, Task], members: dict[int, str], today: date | None) -> AskAction | None:
    """The `action` of an ask answer, checked; None when it asks for no change. Raises on a bad one (retry)."""
    kind = data.get("kind") or "none"
    if kind == "none":
        return None
    title = _title(data.get("title"))
    owner = _owner(data.get("owner_id"), members)
    _owner(data.get("depends_on_owner_id"), members)
    for key in ("task_id", "depends_on_task_id"):
        if data.get(key) is not None and data[key] not in tasks:
            raise ValueError(f"{key} is not a plan task")
    if kind == "new_task":
        if not title:
            raise ValueError("a new task needs a title")
        return AskAction("new_task", None, title, owner_id=owner, due_date=_due(data.get("due_date"), today))
    if kind == "plan":
        return _plan(data.get("items") or [], members, today)
    if kind == "update_task":
        return _update(data, tasks, owner, today)
    if kind == "dependency":
        return _dependency(data, tasks, title)
    raise ValueError("unknown action")


def _plan(raw: list, members: dict[int, str], today: date | None) -> AskAction:
    """Several new tasks the member listed, each with its owner and date if said."""
    items = tuple(
        PlanItem(title=_title(i.get("title")), owner_id=_owner(i.get("owner_id"), members), due_date=_due(i.get("due_date"), today))
        for i in raw[:MAX_PLAN_ITEMS]
        if _title(i.get("title"))
    )
    if not items:
        raise ValueError("a plan needs at least one task")
    return AskAction("plan", None, items[0].title, items=items)


def _update(data: dict, tasks: dict[str, Task], owner: int | None, today: date | None) -> AskAction:
    """New owner, date, status or progress of one plan task: at least one of them."""
    task_id = data.get("task_id")
    if task_id is None:
        raise ValueError("an update needs a plan task")
    progress = data.get("progress")
    if progress is not None and not (isinstance(progress, int) and 0 <= progress <= 100):
        raise ValueError("progress is 0-100")
    status = TaskStatus(data["status"]) if data.get("status") else None
    due = _due(data.get("due_date"), today)
    if owner is None and due is None and status is None and progress is None:
        raise ValueError("an update changes nothing")
    return AskAction("update_task", task_id, tasks[task_id].title, owner_id=owner, due_date=due, status=status, progress=progress)


def _dependency(data: dict, tasks: dict[str, Task], title: str) -> AskAction:
    task_id, depends_on = data.get("task_id"), data.get("depends_on_task_id")
    depends_title = _title(data.get("depends_on_title"))
    if task_id is not None:
        title = tasks[task_id].title
    if not title or not (depends_on or depends_title):
        raise ValueError("a dependency needs both sides")
    if task_id is not None and task_id == depends_on:
        raise ValueError("a task cannot wait on itself")
    if depends_on is not None:
        depends_title = tasks[depends_on].title
    return AskAction(
        "dependency",
        task_id,
        title,
        depends_on_task_id=depends_on,
        depends_on_title=depends_title,
        depends_on_owner_id=None if depends_on is not None else data.get("depends_on_owner_id"),
    )
