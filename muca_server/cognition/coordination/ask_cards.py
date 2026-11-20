"""
From a change asked of Cadence in words (ask_action.AskAction) to the card a
member confirms, with the same kinds and buttons as the proactive cards:

    new_task     -> TASK_OWNER   "Owner and date"
    plan         -> PLAN         "Plan: who does what", the whole list at once
    dependency   -> BLOCKER      a possible dependency, "Yes, it depends"
    update_task  -> CHECKIN      one option, e.g. "«Prototipo»: progress 50%"

`action_draft` decides, without writing anything, either the card to create
or the sentence to tell the member instead: it is already in the plan, a card
above already asks it (so "sì, fallo" under a card points to that card), or
the link would close a loop. plan_commands._action_card creates the card.
"""

from __future__ import annotations

from .ask_action import AskAction
from .models import (
    BlockerCertainty,
    BlockerPayload,
    CheckinOption,
    CheckinPayload,
    Evidence,
    PlanPayload,
    ProactiveContext,
    ProjectState,
    ProposalKind,
    TaskAssignmentPayload,
)
from .plan_writes import known_prerequisite, same_title_task, topic_key

DEPENDENCY_STRATEGY = "cs7_blockers_dependencies"
_STATUS_IT = {"todo": "da fare", "doing": "in corso", "done": "fatta", "blocked": "bloccata"}


def action_draft(
    action: AskAction,
    *,
    state: ProjectState,
    open_cards: list,
    members: dict[int, str],
    evidence: list[Evidence],
    it: bool,
):
    """(kind, payload) of the card to create, or the sentence to say instead (a str)."""
    tasks = {t.task_id: t for t in state.tasks}
    above = (
        "C'è già una card qui sopra che propone proprio questo: confermala con il suo pulsante."
        if it else "A card just above already proposes exactly this: confirm it with its button."
    )

    def pending(kind: ProposalKind, same) -> bool:
        return any(c.kind is kind and same(c.payload) for c in open_cards)

    if action.kind == "new_task":
        if same_title_task(action.title, state.tasks):
            return f"«{action.title}» è già nel piano." if it else f"“{action.title}” is already in the plan."
        if pending(ProposalKind.TASK_OWNER, lambda p: topic_key(str(p.get("title", ""))) == topic_key(action.title)):
            return above
        return ProposalKind.TASK_OWNER, TaskAssignmentPayload(
            context=ProactiveContext(strategy_id="cs5_owner_and_date", topic=action.title[:120], evidence=evidence),
            title=action.title,
            owner_id=action.owner_id,
            due_date=action.due_date,
        )

    if action.kind == "plan":
        # Tasks already in the plan are left out; the rest is one card.
        items = [i for i in action.items if not same_title_task(i.title, state.tasks)]
        if not items:
            return "Queste attività sono già nel piano." if it else "These tasks are already in the plan."
        keys = {topic_key(i.title) for i in items}
        if pending(ProposalKind.PLAN, lambda p: keys & {topic_key(str(i.get("title", ""))) for i in p.get("items", [])}):
            return above
        return ProposalKind.PLAN, PlanPayload(items=items)

    if action.kind == "update_task":
        return _update_draft(action, tasks, members, evidence, it, above, pending)

    return _dependency_draft(action, state, tasks, evidence, it, above, pending)


def _update_draft(action: AskAction, tasks: dict, members: dict[int, str], evidence, it: bool, above: str, pending):
    """One task's new owner, date, status or progress, as a check-in card with one option."""
    task = tasks[action.task_id]
    changes = {
        "owner": action.owner_id if action.owner_id not in (None, task.owner_id) else None,
        "due": action.due_date if action.due_date not in (None, task.due_date) else None,
        "status": action.status if action.status not in (None, task.status) else None,
        "progress": action.progress if action.progress not in (None, task.progress) else None,
    }
    if all(v is None for v in changes.values()):
        return f"Il piano registra già così «{task.title}»." if it else f"The plan already shows “{task.title}” like that."
    if pending(ProposalKind.CHECKIN, lambda p: any(o.get("task_id") == task.task_id for o in p.get("options", []))):
        return above
    option = CheckinOption(
        label=update_label(task.title, changes, members, it),
        task_id=task.task_id,
        new_owner_id=changes["owner"],
        new_due_date=changes["due"],
        new_status=changes["status"],
        new_progress=changes["progress"],
    )
    # The card lists what was said (a check-in card's "findings"), so the group sees why.
    said = [f"{e.speaker}: {e.text}"[:300] for e in evidence[-1:]] or [option.label]
    return ProposalKind.CHECKIN, CheckinPayload(findings=said, options=[option])


def update_label(title: str, changes: dict, members: dict[int, str], it: bool) -> str:
    """The option's button, in the group's language: “Prototipo”: progress 50%, owner Bruno."""
    parts = []
    if changes["progress"] is not None:
        parts.append(f"{'avanzamento' if it else 'progress'} {changes['progress']}%")
    if changes["status"] is not None:
        status = changes["status"].value
        parts.append(f"stato: {_STATUS_IT[status]}" if it else f"status: {status}")
    if changes["owner"] is not None:
        parts.append(f"{'assegnata a' if it else 'owner'} {members.get(changes['owner'], 'a member')}")
    if changes["due"] is not None:
        parts.append(f"{'entro il' if it else 'due'} {changes['due'].strftime('%d/%m' if it else '%d %b')}")
    return (f"«{title[:120]}»: " if it else f"“{title[:120]}”: ") + ", ".join(parts)


def _dependency_draft(action: AskAction, state: ProjectState, tasks: dict, evidence, it: bool, above: str, pending):
    waiting = tasks.get(action.task_id) if action.task_id else None
    depends_on_id = action.depends_on_task_id or same_title_task(action.depends_on_title, state.tasks, besides=action.task_id)
    depends_title = tasks[depends_on_id].title if depends_on_id else action.depends_on_title
    recorded = (depends_on_id in waiting.depends_on if depends_on_id else
                known_prerequisite(waiting, action.depends_on_title, action.depends_on_owner_id, state.tasks)) if waiting else None
    if recorded:
        shown = tasks[recorded].title if recorded in tasks else depends_title
        return (f"Il piano registra già che «{waiting.title}» aspetta «{shown}»." if it
                else f"The plan already records that “{waiting.title}” waits on “{shown}”.")
    if waiting is not None and depends_on_id and reaches(tasks, depends_on_id, waiting.task_id):
        return (
            f"«{depends_title}» aspetta già «{waiting.title}»: collegarle anche al contrario creerebbe un giro "
            "chiuso, quindi ho lasciato il piano com'è." if it else
            f"“{depends_title}” already waits on “{waiting.title}”: linking them the other way too "
            "would make a loop, so I left the plan as it is."
        )
    if pending(ProposalKind.BLOCKER, lambda p: (action.task_id and p.get("task_id") == action.task_id)
               or topic_key(str(p.get("title", ""))) == topic_key(action.title)):
        return above
    return ProposalKind.BLOCKER, BlockerPayload(
        context=ProactiveContext(strategy_id=DEPENDENCY_STRATEGY, topic=action.title[:120], evidence=evidence),
        task_id=action.task_id,
        title=action.title,
        depends_on_task_id=depends_on_id,
        depends_on_title=depends_title,
        depends_on_owner_id=None if depends_on_id else action.depends_on_owner_id,
        question="",
        certainty=BlockerCertainty.POSSIBLE,
    )


def reaches(tasks: dict, start: str, target: str) -> bool:
    """True when `start` already waits, through any tasks, on `target`."""
    to_visit, seen = [start], set()
    while to_visit:
        current = to_visit.pop()
        if current == target:
            return True
        if current not in seen and current in tasks:
            seen.add(current)
            to_visit.extend(tasks[current].depends_on)
    return False


def card_line(card, members: dict[int, str]) -> str:
    """One open card in a few words, for Cadence's context (so it never drafts the same change twice)."""
    p = card.payload
    if card.kind is ProposalKind.BLOCKER:
        by = members.get(p.get("depends_on_owner_id")) if p.get("depends_on_owner_id") else None
        return f"{card.kind.value}: “{p.get('title', '')}” waits on “{p.get('depends_on_title', '')}”" + (f" (delivered by {by})" if by else "")
    if card.kind is ProposalKind.TASK_OWNER:
        return f"new task: “{p.get('title', '')}”" + (f" for {members.get(p.get('owner_id'))}" if p.get("owner_id") else "")
    if card.kind is ProposalKind.PLAN:
        return ("new tasks: " + ", ".join(f"“{i.get('title', '')}”" for i in p.get("items", [])))[:200]
    if card.kind is ProposalKind.CHECKIN:
        return ("task changes: " + "; ".join(str(o.get("label", "")) for o in p.get("options", [])))[:200]
    return f"{card.kind.value}: {p.get('question') or p.get('title') or ''}"[:200]
