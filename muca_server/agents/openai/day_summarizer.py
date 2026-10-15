"""
Long-term memory summaries on Chat Completions (docs/MEMORY_DESIGN.md):
one per day from that day's log, and an overview from the day summaries.
Structured output, no word limit -- length follows content.
"""

from __future__ import annotations

import json
import logging

from openai import AsyncOpenAI

from ...cognition.memory.day_memory import day_heading, render_summary
from ...usage_report import UsageTracker
from .main_agent import SUMMARY_MODEL, record_cost

logger = logging.getLogger(__name__)

# The once-a-day summaries (a finished day, the overview) are few, and
# what the group's memory is built from -- quality over price. "Today so
# far" is replaced by the day's own summary anyway, so it stays on the
# cheaper SUMMARY_MODEL. MUCA_DAILY_SUMMARY_MODEL overrides this.
DAILY_SUMMARY_MODEL = "gpt-5"

_DAY_PROMPT = """\
You are writing the memory of one day of a student group's project work, \
for an assistant (MULCA) that supports the group and will rely on this to \
remember what happened. The log below is everything said that day, in text \
and voice, by the students and by MULCA, in order. Lines starting with \
"---" mark a break between sessions.

Write `narrative` first: a concrete account of the day -- what the group \
worked on, what they decided and produced, and how they got there. Then \
fill in the fields, each capturing what the narrative may leave out:
- decisions: what was decided, by whom, and why
- action_points: tasks still to do or done today, each with an owner
- conclusions: results or answers the group reached
- knowledge: facts and findings gained, including from MULCA or a web search
- contributions: per person, what they specifically proposed, built, argued \
or decided
- decision_making: how each decision came about -- who proposed it, \
objections, how it was settled
- process_notes: how the work went -- phases, where they got stuck, \
conflicts, changes of direction
- mulca: what MULCA was asked or contributed, and whether it helped

Rules:
- Always state the content, never just the topic: "agreed the deadline is \
Fri 24 Oct", not "discussed the deadline". Keep names, numbers, titles, \
dates and links.
- Only include what is in the log. Do not infer goals, invent action points \
or describe what the group "should" do.
- Contributions are per person and specific: what they proposed, built, \
argued or decided -- not a role label. Someone who barely contributed reads \
as barely contributing.
- Every action point has an owner: a name, "group", or "unassigned".
- MULCA's contributions go in `mulca`, not in `contributions`.
- What MULCA says about itself -- what it can or can't do, remember or access -- is not knowledge or a conclusion: leave it out (its abilities change). Only what it contributed to the group's work counts.
- Ignore lines that are garbled or meaningless; they are likely \
transcription noise.
- Leave a field empty rather than pad it. Write in English, whatever \
language was spoken."""

_OVERVIEW_PROMPT = """\
Below are the day summaries of a student group's project so far, each with \
its date. Write the group's current memory of the whole project, for an \
assistant (MULCA) that supports the group and will rely on it.

Same fields as a day summary, now with dates (write dates like "14 Oct"):
- decisions: keep every decision with the date it was made. If one was \
later changed, keep both: set changed_on and say "changed from A to B".
- action_points: one current list; drop items a later day marks done. Each \
keeps its owner and the date it was raised.
- conclusions / knowledge: the current state, with the date each was \
established (`since`); note anything a later day overturned.
- contributions: per person across the whole project, dating the key ones.
- narrative: how the project developed -- a line per active day, then \
phases, turning points and recurring problems.
- mulca: how the group has been using MULCA, and whether it helped.

Rules:
- Work only from the day summaries given; don't carry over anything they \
don't contain, and don't infer goals or invent action points.
- Always state the content, never just the topic. Keep names, numbers, \
titles and dates.
- Contributions are per person and specific, not role labels.
- What MULCA said about itself -- what it can or can't do, remember or access -- is not knowledge or a conclusion: leave it out.
- Leave a field empty rather than pad it. Write in English."""


def _strings() -> dict:
    return {"type": "array", "items": {"type": "string"}}


def _objects(properties: dict) -> dict:
    return {
        "type": "array",
        "items": {
            "type": "object",
            "properties": properties,
            "required": list(properties),
            "additionalProperties": False,
        },
    }


def _schema(name: str, *, dated: bool) -> dict:
    string = {"type": "string"}
    decision = {"decision": string, "decided_by": _strings(), "rationale": string}
    action = {
        "owner": string,
        "task": string,
        "status": {"type": "string", "enum": ["open", "done"]},
    }
    if dated:
        decision |= {"date": string, "changed_on": string}
        action |= {"raised_on": string}
    established = (
        _objects({"text": string, "since": string}) if dated else _strings()
    )
    properties = {
        "narrative": string,
        "decisions": _objects(decision),
        "action_points": _objects(action),
        "conclusions": established,
        "knowledge": established,
        "contributions": _objects({"name": string, "items": _strings()}),
        "decision_making": _strings(),
        "process_notes": _strings(),
        "mulca": _strings(),
    }
    return {
        "name": name,
        "schema": {
            "type": "object",
            "properties": properties,
            "required": list(properties),
            "additionalProperties": False,
        },
        "strict": True,
    }


_DAY_SCHEMA = _schema("day_summary", dated=False)
_OVERVIEW_SCHEMA = _schema("overview", dated=True)


class OpenAIDaySummarizer:
    """Satisfies cognition.ports.DaySummarizer structurally."""

    def __init__(
        self,
        client: AsyncOpenAI,
        *,
        daily_model: str = DAILY_SUMMARY_MODEL,
        model: str = SUMMARY_MODEL,
        usage_tracker: UsageTracker | None = None,
    ) -> None:
        self._client = client
        self._daily_model = daily_model
        self._model = model
        self._usage_tracker = usage_tracker

    async def summarize_day(self, *, day: str, log: str, final: bool) -> dict:
        return await self._complete(
            _DAY_PROMPT,
            f"Day: {day_heading(day)}\n\nLog:\n{log}",
            _DAY_SCHEMA,
            daily=final,
        )

    async def build_overview(self, *, days: list[tuple[str, dict]]) -> dict:
        rendered = "\n\n".join(
            render_summary(summary, heading=f"=== {day_heading(day)} ===")
            for day, summary in days
        )
        return await self._complete(
            _OVERVIEW_PROMPT,
            rendered,
            _OVERVIEW_SCHEMA,
            daily=True,
        )

    async def _complete(
        self,
        system: str,
        user: str,
        schema: dict,
        *,
        daily: bool,
    ) -> dict:
        models = [self._daily_model, self._model] if daily else [self._model]
        for i, model in enumerate(models):
            try:
                return await self._call(model, system, user, schema)
            except Exception:
                if i == len(models) - 1:
                    raise
                # e.g. this key has no access to the stronger model: the
                # memory still gets written, on the usual one.
                logger.warning(
                    "Summary on %s failed; falling back to %s",
                    model,
                    models[i + 1],
                    exc_info=True,
                )
        raise AssertionError("unreachable")

    async def _call(self, model: str, system: str, user: str, schema: dict) -> dict:
        response = await self._client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            response_format={"type": "json_schema", "json_schema": schema},
        )

        usage = getattr(response, "usage", None)
        if usage is not None:
            details = getattr(usage, "prompt_tokens_details", None)
            cached = getattr(details, "cached_tokens", 0) or 0
            record_cost(
                "summary",
                model,
                (usage.prompt_tokens - cached, cached, 0, 0, usage.completion_tokens, 0),
                tracker=self._usage_tracker,
            )

        return json.loads(response.choices[0].message.content)
