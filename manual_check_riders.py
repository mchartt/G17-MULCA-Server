"""
Standalone sanity check for the rider-flag feature (flag_offensive_language /
nudge_participation) -- no server, no OpenAI key, no Discord required.

Run: "/c/Users/tmsch/anaconda3/envs/main/python" manual_check_riders.py
"""

import asyncio

from muca_server.cognition import CoalaPolicy, InterventionSignal, Utterance
from muca_server.cognition.memory import InMemoryEpisodicStore
from muca_server.cognition.strategies import StrategyStore
from muca_server.run_server import ACTIVE_STRATEGY_IDS


class FakePort:
    def __init__(self):
        self.instructions: list[str] = []

    @property
    def conversation_id(self) -> str:
        return "manual-check"

    @property
    def is_private(self) -> bool:
        return False

    @property
    def linked_conversation_id(self):
        return None

    def participants(self) -> dict[int, str]:
        return {1: "Ada", 2: "Pietro"}

    def voice_participants(self) -> dict[int, str]:
        return {}

    async def instruct_main_agent(self, directive: str, *, max_tokens=None) -> None:
        self.instructions.append(directive)

    async def tell_observers(self, text: str) -> None:
        pass


def make_utterance(content: str) -> Utterance:
    return Utterance(
        speaker_id=1,
        speaker_name="Ada",
        content=content,
        modality="text",
        timestamp_ms=0,
    )


async def run_scenario(title: str, content: str, signal: InterventionSignal) -> None:
    store = StrategyStore.load_from_dir(
        "muca_server/cognition/config/strategies",
        active_ids=ACTIVE_STRATEGY_IDS,
    )
    policy = CoalaPolicy(strategy_store=store, episodic_store=InMemoryEpisodicStore())
    port = FakePort()
    await policy.start(port)

    await policy.on_utterance(make_utterance(content))
    await policy.on_intervention_signal(object(), signal)

    print(f"--- {title} ---")
    print(f"message: {content!r}")
    print(f"signal: {signal}")
    print("directive sent to main agent:")
    print(port.instructions[0] if port.instructions else "(none -- nothing fired)")
    print()


async def main() -> None:
    await run_scenario(
        "Direct mention + offensive language",
        "MULCA you're useless, can you help us with the plan?",
        InterventionSignal(
            strategy_id="respond_when_addressed",
            prompt_fragment="asked for help with the plan",
            flag_offensive_language=True,
        ),
    )

    await run_scenario(
        "Direct mention + participation nudge",
        "MULCA, what's our next deadline?",
        InterventionSignal(
            strategy_id="respond_when_addressed",
            prompt_fragment="asked about the next deadline",
            nudge_participation=True,
        ),
    )

    await run_scenario(
        "Offensive language standalone (no flag set, strategy_id is the strategy itself)",
        "that comment was out of line",
        InterventionSignal(
            strategy_id="pleasant_conversation",
            prompt_fragment="",
            flag_offensive_language=True,  # should be a no-op: same as primary
        ),
    )


if __name__ == "__main__":
    asyncio.run(main())
