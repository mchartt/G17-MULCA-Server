import asyncio
from uuid import uuid4

from muca_server.agents import (
    AgentCapabilities,
    AuxiliaryAgent,
    AuxiliaryAgentOptions,
    Conversation,
    MainAgentEvent,
    MainAgentOptions,
    MainAgentState,
)
from muca_server.agents.turn import Turn
from muca_server.audio import AudioChunk
from muca_server.protocol import parse_incoming_message
from muca_server.sessions import SessionState


def make_conversation() -> tuple[SessionState, Conversation, asyncio.Queue]:
    state = SessionState(session_id=uuid4())
    state.is_connected = True
    outbound: asyncio.Queue = asyncio.Queue()

    conversation = Conversation(
        state=state,
        outbound_queue=outbound,
        main_agent_options=MainAgentOptions(),
    )

    return state, conversation, outbound


def audio_stream(
    *,
    speaker: int,
    audio: str = "AAAAAA==",
    is_notifying: bool = False,
    playback_time_ms: int = 0,
):
    return parse_incoming_message(
        {
            "event": "audio_stream",
            "speaker": speaker,
            "audio": audio,
            "isNotifying": is_notifying,
            "playbackTimeMs": playback_time_ms,
        }
    )


def create_auxiliary(agent_type: str = "intervention_detection"):
    return parse_incoming_message(
        {
            "event": "create_auxiliary_agent",
            "auxiliaryAgentType": agent_type,
        }
    )


def make_auxiliary(**options) -> AuxiliaryAgent:
    return AuxiliaryAgent(options=AuxiliaryAgentOptions(**options))


def record_audio(agent) -> list:
    received: list = []

    async def send(chunk) -> None:
        received.append(chunk)

    agent._send_audio_chunk = send

    return received


def record_text(agent) -> list:
    received: list = []

    async def send(text: str, *, role: str = "system") -> None:
        received.append(text)

    agent._send_text = send

    return received


def record_triggers(agent) -> list:
    calls: list = []

    async def trigger(**kwargs) -> None:
        calls.append(kwargs)

    agent._trigger_response = trigger

    return calls


async def drain(outbound: asyncio.Queue) -> list:
    messages = []
    while not outbound.empty():
        messages.append(outbound.get_nowait())

    return messages


# ----------------------------------------------------------------------
# Agents
# ----------------------------------------------------------------------


def test_an_auxiliary_agent_without_filters_listens_to_everyone():
    agent = make_auxiliary(listened_users=None)

    assert agent.listens_to_user(0)
    assert agent.listens_to_user(7)


def test_an_auxiliary_agent_with_filters_listens_to_those_users_only():
    agent = make_auxiliary(listened_users=[1])

    assert agent.listens_to_user(1)
    assert not agent.listens_to_user(2)


def test_each_auxiliary_agent_gets_its_own_guid():
    assert make_auxiliary().guid != make_auxiliary().guid


async def test_an_agent_drops_audio_until_it_is_connected():
    agent = make_auxiliary()
    sent = record_audio(agent)

    await agent.send_audio_chunk(AudioChunk(data=b"\x00" * 4))
    assert sent == []

    await agent.connect()
    await agent.send_audio_chunk(AudioChunk(data=b"\x00" * 4))
    assert len(sent) == 1


# ----------------------------------------------------------------------
# Session: connection and agent management
# ----------------------------------------------------------------------


async def test_start_connects_the_main_agent():
    state, conversation, _ = make_conversation()

    await conversation.start()

    assert conversation.main_agent is not None
    assert conversation.main_agent.is_connected
    assert state.active_agent == "ASSISTANT"


async def test_an_empty_target_resolves_to_the_main_agent():
    _, conversation, _ = make_conversation()
    await conversation.start()

    assert conversation._resolve(None) is conversation.main_agent
    assert conversation._resolve("") is conversation.main_agent


async def test_an_unusable_target_resolves_to_nothing():
    _, conversation, _ = make_conversation()
    await conversation.start()

    assert conversation._resolve(str(uuid4())) is None
    assert conversation._resolve("not-a-guid") is None


async def test_creating_an_auxiliary_agent_announces_its_guid():
    state, conversation, outbound = make_conversation()
    await conversation.start()

    await conversation.create_auxiliary_agent(create_auxiliary())

    guid = next(iter(conversation.auxiliary_agents))
    announcements = [
        message
        for message in await drain(outbound)
        if message.event == "agent_guid"
    ]

    assert len(announcements) == 1
    assert announcements[0].agent_guid == str(guid)
    assert str(guid) in state.connected_agents


async def test_a_newly_connected_observer_gets_caught_up_on_existing_history():
    """
    After a restart, WM's history is restored before any observer exists to
    have seen it live -- a freshly connecting observer gets a one-time
    catchup message instead, so it isn't judging blind. Targeted at just
    this agent, not broadcast: see Conversation.create_auxiliary_agent.
    """

    class CatchupPolicy:
        async def start(self, port) -> None:
            pass

        async def close(self) -> None:
            pass

        async def on_utterance(self, utterance) -> bool:
            return True

        async def on_intervention_signal(self, agent_guid, signal) -> None:
            pass

        async def on_assistant_output(self, text, *, interrupted) -> None:
            pass

        async def wait_for_turn_to_finish(self) -> None:
            pass

        def observer_catchup_text(self) -> str:
            return "Ada: the deadline is friday"

    state = SessionState(session_id=uuid4())
    state.is_connected = True
    conversation = Conversation(
        state=state,
        outbound_queue=asyncio.Queue(),
        main_agent_options=MainAgentOptions(),
        policy=CatchupPolicy(),
    )
    await conversation.start()

    received: list[str] = []
    original_send_text = AuxiliaryAgent._send_text

    async def recording_send_text(self, text: str, *, role: str = "system") -> None:
        received.append(text)
        await original_send_text(self, text, role=role)

    AuxiliaryAgent._send_text = recording_send_text
    try:
        await conversation.create_auxiliary_agent(create_auxiliary())
    finally:
        AuxiliaryAgent._send_text = original_send_text

    assert received == ["Ada: the deadline is friday"]


async def test_removing_an_auxiliary_agent_forgets_it():
    state, conversation, _ = make_conversation()
    await conversation.start()
    await conversation.create_auxiliary_agent(create_auxiliary())

    guid = next(iter(conversation.auxiliary_agents))
    await conversation.remove_auxiliary_agent(
        parse_incoming_message(
            {
                "event": "remove_auxiliary_agent",
                "targetAgentGuid": str(guid),
            }
        )
    )

    assert guid not in conversation.auxiliary_agents
    assert str(guid) not in state.connected_agents


async def test_unknown_targets_never_raise():
    _, conversation, _ = make_conversation()
    await conversation.start()

    stranger = str(uuid4())

    await conversation.route_text(
        parse_incoming_message(
            {
                "event": "text_message",
                "text": "hi",
                "targetAgentGuid": stranger,
            }
        )
    )
    await conversation.interrupt_response(
        parse_incoming_message(
            {"event": "interrupt", "targetAgentGuid": stranger},
        )
    )
    await conversation.remove_auxiliary_agent(
        parse_incoming_message({"event": "remove_auxiliary_agent"}),
    )


# ----------------------------------------------------------------------
# Session: audio routing
# ----------------------------------------------------------------------


async def test_user_audio_reaches_the_main_agent_and_its_listeners():
    _, conversation, _ = make_conversation()
    await conversation.start()
    await conversation.create_auxiliary_agent(create_auxiliary())

    main_received = record_audio(conversation.main_agent)

    auxiliary = next(iter(conversation.auxiliary_agents.values()))
    auxiliary.listened_users = [1]
    auxiliary_received = record_audio(auxiliary)

    await conversation.route_user_audio(audio_stream(speaker=1))
    await conversation.route_user_audio(audio_stream(speaker=2))

    assert len(main_received) == 2
    assert len(auxiliary_received) == 1


async def test_playback_audio_only_reaches_observers_while_answering():
    _, conversation, _ = make_conversation()
    await conversation.start()
    await conversation.create_auxiliary_agent(create_auxiliary())

    auxiliary = next(iter(conversation.auxiliary_agents.values()))
    received = record_audio(auxiliary)

    playback = parse_incoming_message(
        {"event": "playback_frame", "audio": "AAAAAA=="},
    )

    await conversation.route_playback_frame(playback)
    assert received == []

    conversation.main_agent_state = MainAgentState.ANSWERING
    await conversation.route_playback_frame(playback)
    assert len(received) == 1


async def test_the_assistants_own_reply_reaches_observers_listening_to_it():
    _, conversation, _ = make_conversation()
    await conversation.start()
    await conversation.create_auxiliary_agent(create_auxiliary())

    auxiliary = next(iter(conversation.auxiliary_agents.values()))
    assert auxiliary.listen_to_assistant
    received = record_text(auxiliary)

    await conversation._handle_main_agent_text("Hello there.", True)

    assert received == [f"{conversation.main_agent.name}: Hello there."]


async def test_playback_position_is_recorded_for_truncation():
    state, conversation, _ = make_conversation()
    await conversation.start()

    await conversation.route_user_audio(
        audio_stream(speaker=1, is_notifying=True, playback_time_ms=14250),
    )

    assert state.playback_time_ms == 14250
    assert conversation._assistant_notifying.is_set()


async def test_malformed_audio_is_dropped_rather_than_raised():
    _, conversation, _ = make_conversation()
    await conversation.start()

    await conversation.route_user_audio(
        audio_stream(speaker=1, audio="not base64!!"),
    )


# ----------------------------------------------------------------------
# Session: turn taking
# ----------------------------------------------------------------------


async def test_a_speaking_user_opens_a_turn():
    _, conversation, _ = make_conversation()
    await conversation.start()

    await conversation.route_user_audio(audio_stream(speaker=1))

    assert conversation.current_speaker == 1
    assert len(conversation.turns) == 1
    assert conversation.turns[0].is_open


async def test_silence_closes_the_turn_and_asks_observers_to_step_in():
    _, conversation, _ = make_conversation()
    await conversation.start()
    await conversation.create_auxiliary_agent(create_auxiliary())

    auxiliary = next(iter(conversation.auxiliary_agents.values()))
    triggers = record_triggers(auxiliary)

    await conversation.route_user_audio(audio_stream(speaker=1))
    await conversation.route_user_audio(audio_stream(speaker=1, audio=""))

    assert conversation.current_speaker == -1
    assert conversation.last_speaker == 1
    assert not conversation.turns[-1].is_open

    await asyncio.sleep(0)
    assert triggers


async def test_a_user_to_user_change_does_not_ask_observers_to_step_in():
    _, conversation, _ = make_conversation()
    await conversation.start()
    await conversation.create_auxiliary_agent(create_auxiliary())

    auxiliary = next(iter(conversation.auxiliary_agents.values()))
    triggers = record_triggers(auxiliary)

    await conversation.route_user_audio(audio_stream(speaker=1))
    await conversation.route_user_audio(audio_stream(speaker=2))

    assert triggers == []
    assert conversation.current_speaker == 2


async def test_the_assistant_takes_a_turn_once_the_client_plays_it():
    _, conversation, _ = make_conversation()
    await conversation.start()

    conversation._report_assistant_playback(is_notifying=True)

    await conversation._handle_main_agent_event(
        MainAgentEvent.AGENT_SPEECH_STARTED,
    )
    await asyncio.sleep(0)

    assert conversation.main_agent_state is MainAgentState.ANSWERING
    assert conversation.assistant_spoke_last
    assert conversation.is_answering


async def test_the_assistant_gives_up_its_turn_if_a_user_is_speaking():
    _, conversation, outbound = make_conversation()
    await conversation.start()

    await conversation.route_user_audio(audio_stream(speaker=1))

    await conversation._handle_main_agent_event(
        MainAgentEvent.AGENT_SPEECH_STARTED,
    )
    await asyncio.sleep(0)

    assert conversation.main_agent_state is not MainAgentState.ANSWERING
    assert conversation.current_speaker == 1

    cleared = [
        message
        for message in await drain(outbound)
        if getattr(message, "clear_audio_queue", False)
    ]
    assert cleared


async def test_an_interruption_clears_the_clients_audio():
    _, conversation, outbound = make_conversation()
    await conversation.start()
    conversation.main_agent_state = MainAgentState.ANSWERING
    conversation.turns.append(
        Turn(start_time=0, speaker_id=0, speaker_name="ASSISTANT"),
    )

    await conversation._handle_main_agent_event(
        MainAgentEvent.AGENT_SPEECH_INTERRUPTED,
    )

    assert conversation.main_agent_state is MainAgentState.INACTIVE
    assert not conversation.turns[-1].is_open

    cleared = [
        message
        for message in await drain(outbound)
        if getattr(message, "clear_audio_queue", False)
    ]
    assert cleared


async def test_main_agent_audio_reaches_the_client():
    _, conversation, outbound = make_conversation()
    await conversation.start()

    await conversation._handle_main_agent_audio(
        AudioChunk(data=b"\x00\x00\x80\x3f"),
    )

    messages = await drain(outbound)

    assert len(messages) == 1
    assert messages[0].event == "state_update"
    assert messages[0].audio == "AACAPw=="


async def test_agent_messages_are_tagged_with_their_agent():
    _, conversation, outbound = make_conversation()
    await conversation.start()
    await conversation.create_auxiliary_agent(create_auxiliary())
    await drain(outbound)

    guid = next(iter(conversation.auxiliary_agents))
    auxiliary = conversation.auxiliary_agents[guid]

    await conversation._handle_main_agent_message('{"type":"a"}')
    await auxiliary._handle_provider_message('{"type":"b"}')

    messages = await drain(outbound)

    assert messages[0].agent_guid is None
    assert messages[1].agent_guid == str(guid)


async def test_provider_vad_holds_the_turn_until_speech_is_confirmed():
    _, conversation, _ = make_conversation()
    await conversation.start()
    conversation.main_agent.capabilities = AgentCapabilities(vad=True)

    await conversation.route_user_audio(audio_stream(speaker=1))
    assert conversation.current_speaker == -1

    await conversation._handle_main_agent_event(
        MainAgentEvent.USER_SPEECH_STARTED,
    )
    await conversation.route_user_audio(audio_stream(speaker=1))

    assert conversation.current_speaker == 1
