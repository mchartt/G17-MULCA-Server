from muca_server.events import DispatchResult, dispatch as dispatch_message
from muca_server.protocol import parse_incoming_message


class RecordingRouter:
    """Satisfies AgentRouterProtocol structurally."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def __getattr__(self, name: str):
        async def record(message) -> None:
            self.calls.append((name, message))

        return record

    @property
    def names(self) -> list[str]:
        return [call[0] for call in self.calls]


async def dispatch(raw: dict) -> tuple[DispatchResult, RecordingRouter]:
    router = RecordingRouter()

    result = await dispatch_message(parse_incoming_message(raw), router)

    return result, router


async def test_audio_stream_is_routed():
    _, router = await dispatch(
        {
            "event": "audio_stream",
            "speaker": 1,
            "audio": "",
            "isNotifying": False,
            "playbackTimeMs": 0,
        }
    )

    assert router.names == ["route_user_audio"]


async def test_playback_frame_is_routed():
    _, router = await dispatch({"event": "playback_frame", "audio": ""})

    assert router.names == ["route_playback_frame"]


async def test_text_message_is_routed():
    _, router = await dispatch({"event": "text_message", "text": "hi"})

    assert router.names == ["route_text"]


async def test_connect_carries_the_target_guid():
    _, router = await dispatch(
        {"event": "connect", "targetAgentGuid": "abc"},
    )

    name, message = router.calls[0]

    assert name == "connect_agent"
    assert message.target_agent_guid == "abc"


async def test_create_auxiliary_agent_carries_the_type():
    _, router = await dispatch(
        {
            "event": "create_auxiliary_agent",
            "auxiliaryAgentType": "intervention_detection",
        }
    )

    name, message = router.calls[0]

    assert name == "create_auxiliary_agent"
    assert message.auxiliary_agent_type == "intervention_detection"


async def test_trigger_response_carries_generation_control():
    _, router = await dispatch(
        {
            "event": "trigger_response",
            "maxTokens": 256,
            "interrupt": False,
            "instructions": "go",
        }
    )

    name, message = router.calls[0]

    assert name == "trigger_response"
    assert message.max_tokens == 256
    assert message.interrupt is False
    assert message.instructions == "go"


async def test_interrupt_is_routed():
    _, router = await dispatch({"event": "interrupt_response"})

    assert router.names == ["interrupt_response"]


async def test_targeted_disconnect_keeps_the_session_open():
    result, router = await dispatch(
        {"event": "disconnect", "targetAgentGuid": "abc"},
    )

    assert result is DispatchResult.CONTINUE
    assert router.names == ["disconnect_agent"]


async def test_untargeted_disconnect_closes_the_session():
    result, router = await dispatch({"event": "disconnect"})

    assert result is DispatchResult.CLOSE_SESSION
    assert router.names == ["disconnect_agent"]
