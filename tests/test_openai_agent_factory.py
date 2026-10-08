"""
OpenAIAgentFactory.build_auxiliary picks the intervention agent's own
strategy catalog from the conversation's resolved listening_mode, not from
one catalog shared by every conversation. This matters because that
catalog becomes a structured-output enum the model picks strategy_id from
(InterventionAgentOpenAI._tool_definition) -- a strategy absent from it is
structurally impossible for the model to select, not merely discouraged
by a trigger_description it might misjudge. An ambient conversation's own
agent must never have always_reply in its catalog at all.
"""

from muca_server.agents import AuxiliaryAgentOptions
from muca_server.agents.openai import OpenAIAgentFactory
from muca_server.agents.openai.model_settings import ModelSettings
from muca_server.cognition.strategies import Strategy, StrategyStore

AMBIENT_STRATEGIES = StrategyStore(
    {
        "respond_when_addressed": Strategy(
            id="respond_when_addressed",
            trigger_description="addressed directly",
            prompt_template="Answer them. {prompt_fragment}",
            modalities=frozenset({"text", "audio"}),
        ),
    }
)

NON_AMBIENT_STRATEGIES = StrategyStore(
    {
        "respond_when_addressed": AMBIENT_STRATEGIES.get("respond_when_addressed"),
        "always_reply": Strategy(
            id="always_reply",
            trigger_description="toggle/transcribe fallback",
            prompt_template="Respond directly. {prompt_fragment}",
            modalities=frozenset({"audio"}),
        ),
    }
)


def make_factory(tmp_path) -> OpenAIAgentFactory:
    return OpenAIAgentFactory(
        client=object(),
        strategies=AMBIENT_STRATEGIES,
        non_ambient_strategies=NON_AMBIENT_STRATEGIES,
        main_persona="p",
        intervention_persona="p",
        task_knowledge="t",
        models=ModelSettings(tmp_path / "c.json"),
    )


def test_an_ambient_conversations_agent_never_has_always_reply_in_its_catalog(
    tmp_path,
):
    factory = make_factory(tmp_path)

    aux = factory.build_auxiliary(
        AuxiliaryAgentOptions.for_type(
            "intervention_detection",
        ).model_copy(update={"listening_mode": "ambient"}),
    )

    assert "always_reply" not in aux._strategies.ids()


def test_a_wake_conversations_agent_gets_the_ambient_catalog(tmp_path):
    # An engaged "wake" session is judged like ambient; only the follow-up
    # to a bare wake phrase is forced, by the policy, not by the model.
    factory = make_factory(tmp_path)

    aux = factory.build_auxiliary(
        AuxiliaryAgentOptions.for_type(
            "intervention_detection",
        ).model_copy(update={"listening_mode": "wake"}),
    )

    assert "always_reply" not in aux._strategies.ids()


def test_a_toggle_conversations_agent_has_always_reply_in_its_catalog(tmp_path):
    factory = make_factory(tmp_path)

    aux = factory.build_auxiliary(
        AuxiliaryAgentOptions.for_type(
            "intervention_detection",
        ).model_copy(update={"listening_mode": "toggle"}),
    )

    assert "always_reply" in aux._strategies.ids()


def test_a_transcribe_conversations_agent_has_always_reply_in_its_catalog(tmp_path):
    factory = make_factory(tmp_path)

    aux = factory.build_auxiliary(
        AuxiliaryAgentOptions.for_type(
            "intervention_detection",
        ).model_copy(update={"listening_mode": "transcribe"}),
    )

    assert "always_reply" in aux._strategies.ids()


def test_without_a_non_ambient_store_the_same_catalog_is_used_for_both(tmp_path):
    # Callers that don't care about the distinction (e.g. older tests) get
    # today's behavior back: one shared catalog, same as before this split.
    factory = OpenAIAgentFactory(
        client=object(),
        strategies=AMBIENT_STRATEGIES,
        main_persona="p",
        intervention_persona="p",
        task_knowledge="t",
        models=ModelSettings(tmp_path / "c.json"),
    )

    aux = factory.build_auxiliary(
        AuxiliaryAgentOptions.for_type(
            "intervention_detection",
        ).model_copy(update={"listening_mode": "toggle"}),
    )

    assert aux._strategies is AMBIENT_STRATEGIES
