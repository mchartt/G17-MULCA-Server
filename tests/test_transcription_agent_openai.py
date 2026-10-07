# TranscriptionAgentOpenAI is not wired into conversation.py yet -- see the
# "NOT CURRENTLY WIRED IN" note at the top of transcription_agent.py. If
# that follow-up is dropped, this file and the class it tests are both
# safe to delete together.

from openai.types.realtime.conversation_item_input_audio_transcription_completed_event import (
    ConversationItemInputAudioTranscriptionCompletedEvent,
)
from openai.types.realtime.conversation_item_input_audio_transcription_failed_event import (
    ConversationItemInputAudioTranscriptionFailedEvent,
)

from muca_server.agents.openai.transcription_agent import TranscriptionAgentOpenAI


def make_agent(**kwargs) -> TranscriptionAgentOpenAI:
    return TranscriptionAgentOpenAI(client=object(), **kwargs)


def completed_event(item_id: str, transcript: str):
    return ConversationItemInputAudioTranscriptionCompletedEvent.model_construct(
        type="conversation.item.input_audio_transcription.completed",
        content_index=0,
        event_id="e1",
        item_id=item_id,
        transcript=transcript,
        usage=None,
    )


def failed_event(item_id: str):
    return ConversationItemInputAudioTranscriptionFailedEvent.model_construct(
        type="conversation.item.input_audio_transcription.failed",
        content_index=0,
        event_id="e2",
        item_id=item_id,
        error=None,
    )


def test_session_config_requests_a_transcription_session_not_a_conversational_one():
    agent = make_agent(prompt="English or Italian.")

    config = agent._session_config()

    assert config["type"] == "transcription"
    assert config["audio"]["input"]["transcription"]["model"] == "gpt-4o-transcribe"
    assert config["audio"]["input"]["transcription"]["prompt"] == "English or Italian."
    # No model/response-generation fields at all -- this session type can't
    # produce a billed response, unlike MainAgentOpenAI/InterventionAgentOpenAI.
    assert "output_modalities" not in config
    assert "tools" not in config


async def test_completed_transcripts_reach_the_on_transcript_hook():
    agent = make_agent()
    received = []

    async def record(item_id, transcript):
        received.append((item_id, transcript))

    agent.on_transcript = record

    await agent._interpret(completed_event("item1", "hello there"))

    assert received == [("item1", "hello there")]


async def test_a_failed_transcription_does_not_call_on_transcript_or_raise():
    agent = make_agent()
    received = []

    async def record(item_id, transcript):
        received.append((item_id, transcript))

    agent.on_transcript = record

    await agent._interpret(failed_event("item1"))

    assert received == []


async def test_send_audio_chunk_is_a_no_op_before_connecting():
    from muca_server.audio import AudioChunk

    agent = make_agent()

    # No connection yet: must not raise (mirrors MainAgentOpenAI's own
    # _send_audio_chunk guard).
    await agent._send_audio_chunk(AudioChunk(data=b"", sample_rate=24_000))
