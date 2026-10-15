"""
The OpenAI-specific parsing logic in InterventionAgentOpenAI._interpret.

Built from real SDK event models (via model_construct, to avoid supplying
every unrelated required field) rather than plain dicts, so a schema change
in the SDK would break these tests the same way it would break the real
code.
"""

import json

from openai.types.realtime.realtime_error_event import RealtimeErrorEvent
from openai.types.realtime.response_done_event import ResponseDoneEvent
from openai.types.realtime.response_function_call_arguments_done_event import (
    ResponseFunctionCallArgumentsDoneEvent,
)

from muca_server.agents.openai.intervention_agent import (
    TOOL_NAME,
    InterventionAgentOpenAI,
)
from muca_server.agents.options import AuxiliaryAgentOptions
from muca_server.cognition import InterventionSignal
from muca_server.cognition.strategies import Strategy, StrategyStore


def make_agent() -> InterventionAgentOpenAI:
    strategies = StrategyStore(
        {
            "respond_when_addressed": Strategy(
                id="respond_when_addressed",
                trigger_description="",
                prompt_template="{prompt_fragment}",
            ),
        }
    )

    return InterventionAgentOpenAI(
        options=AuxiliaryAgentOptions(),
        client=object(),  # never called directly by _interpret
        strategies=strategies,
        persona="p",
        task_knowledge="t",
    )


def tool_call_event(
    *,
    arguments: dict,
    call_id: str = "call_1",
    name: str = TOOL_NAME,
) -> ResponseFunctionCallArgumentsDoneEvent:
    return ResponseFunctionCallArgumentsDoneEvent.model_construct(
        type="response.function_call_arguments.done",
        call_id=call_id,
        name=name,
        arguments=json.dumps(arguments),
        event_id="e1",
        item_id="i1",
        output_index=0,
        response_id="r1",
    )


def response_done_event() -> ResponseDoneEvent:
    return ResponseDoneEvent.model_construct(
        type="response.done",
        event_id="e2",
        response=None,
    )


def error_event() -> RealtimeErrorEvent:
    return RealtimeErrorEvent.model_construct(
        type="error",
        event_id="e3",
        error={"message": "boom", "type": "server_error"},
    )


async def record_decisions(agent: InterventionAgentOpenAI) -> list:
    decisions = []

    async def record(guid, signal) -> None:
        decisions.append(signal)

    agent.on_decision = record
    return decisions


async def test_a_tool_call_then_response_done_yields_a_signal():
    agent = make_agent()
    decisions = await record_decisions(agent)

    await agent._interpret(
        tool_call_event(
            arguments={
                "strategy_id": "respond_when_addressed",
                "prompt_fragment": "they asked directly",
                "needs_history": False,
                "needs_web_search": False,
            }
        )
    )
    await agent._interpret(response_done_event())

    assert decisions == [
        InterventionSignal(
            strategy_id="respond_when_addressed",
            prompt_fragment="they asked directly",
        )
    ]


async def test_needs_web_search_carries_its_own_query():
    agent = make_agent()
    decisions = await record_decisions(agent)

    await agent._interpret(
        tool_call_event(
            arguments={
                "strategy_id": "respond_when_addressed",
                "prompt_fragment": "asked about the weather",
                "needs_history": False,
                "needs_web_search": True,
                "web_search_query": "weather in Rome today",
            }
        )
    )
    await agent._interpret(response_done_event())

    assert decisions == [
        InterventionSignal(
            strategy_id="respond_when_addressed",
            prompt_fragment="asked about the weather",
            needs_web_search=True,
            web_search_query="weather in Rome today",
        )
    ]


def test_the_tool_definition_includes_web_search_query():
    agent = make_agent()

    properties = agent._tool_definition()["parameters"]["properties"]

    assert properties["web_search_query"]["type"] == "string"


def test_the_history_period_is_read_from_the_tool_call():
    agent = make_agent()

    signal = agent._parse_signal(
        '{"strategy_id": "provide_summary", "prompt_fragment": "", '
        '"history_period": "today"}'
    )
    assert signal.history_period == "today"
    assert "history_period" in agent._tool_definition()["parameters"]["properties"]

    # Left out: the whole history, as before.
    signal = agent._parse_signal('{"strategy_id": "provide_summary", "prompt_fragment": ""}')
    assert signal.history_period == "all"


async def test_response_done_with_no_tool_call_means_no_intervention():
    agent = make_agent()
    decisions = await record_decisions(agent)

    await agent._interpret(response_done_event())

    assert decisions == [None]


async def test_an_unrelated_tool_call_is_ignored():
    agent = make_agent()
    decisions = await record_decisions(agent)

    await agent._interpret(
        tool_call_event(
            name="some_other_tool",
            arguments={"strategy_id": "respond_when_addressed"},
        )
    )
    await agent._interpret(response_done_event())

    assert decisions == [None]


async def test_a_provider_error_does_not_hang_and_reports_no_signal():
    """
    MULCA's decide() had no error handling at all: a provider error meant
    the `async for` loop waited forever for a response.done that would
    never arrive. This is the fix.
    """

    agent = make_agent()
    decisions = await record_decisions(agent)
    agent._response_active = True  # a judgement is in flight

    await agent._interpret(error_event())

    assert decisions == [None]


async def test_an_error_with_no_judgement_in_flight_reports_nothing():
    """
    An unrelated error (a rejected item, say) must not report a decision
    nobody asked for, nor drop the one still being made.
    """

    agent = make_agent()
    decisions = await record_decisions(agent)

    await agent._interpret(error_event())

    assert decisions == []


async def test_a_late_cancel_error_does_not_clobber_the_pending_signal():
    agent = make_agent()
    decisions = await record_decisions(agent)
    agent._response_active = True
    agent._pending_signal = "kept"

    await agent._interpret(
        RealtimeErrorEvent.model_construct(
            type="error",
            event_id="e5",
            error={
                "message": "no active response",
                "type": "invalid_request_error",
                "code": "response_cancel_not_active",
            },
        )
    )

    assert decisions == []
    assert agent._pending_signal == "kept"


async def test_malformed_tool_arguments_are_dropped_not_raised():
    agent = make_agent()
    decisions = await record_decisions(agent)

    await agent._interpret(
        ResponseFunctionCallArgumentsDoneEvent.model_construct(
            type="response.function_call_arguments.done",
            call_id="call_1",
            name=TOOL_NAME,
            arguments="not json",
            event_id="e1",
            item_id="i1",
            output_index=0,
            response_id="r1",
        )
    )
    await agent._interpret(response_done_event())

    assert decisions == [None]


class FakeResponse:
    def __init__(self) -> None:
        self.created = 0
        self.cancelled = 0

    async def create(self, **kwargs) -> None:
        self.created += 1

    async def cancel(self) -> None:
        self.cancelled += 1


class FakeConnection:
    def __init__(self) -> None:
        self.response = FakeResponse()


async def test_turns_during_a_judgement_are_judged_once_after_it_not_cancelled():
    """
    Cancelling a running judgement for a newer turn was billed in full,
    uncached, cut its tool call short and raced the next response.create.
    """

    agent = make_agent()
    decisions = await record_decisions(agent)
    agent._connection = FakeConnection()
    responses = agent._connection.response

    for _ in range(3):
        await agent.trigger_response()
    assert (responses.created, responses.cancelled) == (1, 0)

    await agent._interpret(response_done_event())
    assert responses.created == 2

    await agent._interpret(response_done_event())
    assert responses.created == 2
    assert decisions == [None, None]
    assert not agent._response_active
