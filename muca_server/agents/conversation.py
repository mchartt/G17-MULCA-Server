from __future__ import annotations

import asyncio
import binascii
import logging
import time
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from contextlib import suppress
from typing import TYPE_CHECKING, Any
from uuid import UUID

from ..audio import AudioChunk
from ..audio.chunk import DEFAULT_SAMPLE_RATE
from ..audio.pcm import split_float32
from ..cognition.noop import NoCognition
from ..cognition.ports import (
    CognitionPolicy,
    InterventionSignal,
    Utterance,
)
from ..protocol import (
    AgentGuidMessage,
    AgentMessage,
    AssistantTextMessage,
    AudioStreamMessage,
    ConnectAgentMessage,
    CreateAuxiliaryAgentMessage,
    DisconnectMessage,
    EndSession,
    InterruptMessage,
    OutgoingProtocolMessage,
    ParticipantsMessage,
    PlaybackFrameMessage,
    RemoveAuxiliaryAgentMessage,
    StateUpdateMessage,
    TextMessage,
    TriggerResponseMessage,
    UserTextMessage,
)
from .auxiliary_agent import AuxiliaryAgent
from .factory import AgentFactory, DefaultAgentFactory
from .instructions import SPEAKER_CHANGED
from .main_agent import MainAgent, MainAgentEvent, MainAgentState
from .options import AuxiliaryAgentOptions, MainAgentOptions
from .realtime_agent import RealtimeAgent
from .turn import Turn

if TYPE_CHECKING:
    from ..sessions.session_state import SessionState

logger = logging.getLogger(__name__)

NO_SPEAKER = -1

# Stands in for an observer's guid when a turn had nobody to judge it.
NO_OBSERVER = UUID(int=0)

# How long without a client audio frame counts as silence. The Unity client
# sends frames only while somebody is speaking, so silence is an absence.
DEFAULT_SILENCE_TIMEOUT_MS = 400

# How often the conversation re-reads the clock (silence, playback drain).
TICK_MS = 100

# How long a closed spoken turn waits for its transcript to come back.
TRANSCRIPT_WAIT_SECONDS = 3.0

# How long a reply may take to become audible on the client before the
# assistant takes its turn anyway.
PLAYBACK_START_TIMEOUT_SECONDS = 3.0

# Unity's receive loop reads 16 KB per frame; 3000 float32 samples, base64
# encoded, keep a state_update well under that.
MAX_SAMPLES_PER_FRAME = 3000

TurnJob = Callable[[], Awaitable[None]]


def _now_ms() -> int:
    return int(time.time() * 1000)


class Conversation:
    """
    Orchestrates a realtime multi-agent conversation and delegates the
    assistant's work to a MainAgent.

    The server-side port of the Unity package's SpeechToSpeechSession. It
    assumes the client reports the user whose turn it is right now, that two
    users never hold the turn at the same time, and that backchannels ("um",
    "hmm") are already filtered out. If a user and the assistant speak over
    each other, the assistant is interrupted, either by its own provider or
    by this class.

    Only the main agent speaks to the users, so only its audio travels back
    to the client. Auxiliary agents observe: they receive user audio, and the
    assistant's audio only once the users have actually heard it.

    Whole turns -- a text message, or a spoken turn once its transcript is
    back -- go through cognition one at a time, on a queue of their own, so
    the socket keeps being read (audio, interruptions) while a turn is being
    judged and answered.
    """

    def __init__(
        self,
        *,
        state: SessionState,
        outbound_queue: asyncio.Queue[
            OutgoingProtocolMessage | EndSession
        ],
        main_agent_options: MainAgentOptions,
        agent_factory: AgentFactory | None = None,
        policy: CognitionPolicy | None = None,
        silence_timeout_ms: int = DEFAULT_SILENCE_TIMEOUT_MS,
    ) -> None:
        self._state = state
        self._outbound_queue = outbound_queue
        self._main_agent_options = main_agent_options
        self._agent_factory = (
            agent_factory or DefaultAgentFactory()
        )
        self._policy = policy or NoCognition()
        self._silence_timeout_ms = silence_timeout_ms

        self._conversation_id = str(state.session_id)
        self._has_conversation_id = False
        self._is_private = False
        self._linked_conversation_id: str | None = None

        self._main_agent: MainAgent | None = None
        self._auxiliary_agents: dict[UUID, AuxiliaryAgent] = {}

        self.turns: list[Turn] = []
        self.main_agent_state = MainAgentState.INACTIVE

        # Everyone present in text, including those who have not spoken yet.
        self._participants: dict[int, str] = {}

        # Everyone present in the voice call, tracked completely separately
        # from the text roster above -- who's in one is not reliably who's
        # in the other. See route_participants/voice_participants().
        self._voice_participants: dict[int, str] = {}

        # Deltas of the reply being streamed, until it completes.
        self._assistant_reply: list[str] = []

        # What the client reports about its own playback of the main
        # agent's audio: the server cannot see it directly. When the client
        # stops reporting, playback is estimated from the audio sent.
        self._client_notifying = False
        self._last_client_frame_ms: int | None = None
        self._assistant_audio_first_ms: int | None = None
        self._assistant_audio_sent_ms = 0
        self._assistant_notifying = asyncio.Event()
        self._assistant_silent = asyncio.Event()
        self._assistant_silent.set()

        # The assistant's turn: generated in full, and already being closed.
        self._assistant_generation_done = False
        self._assistant_turn_ending = False

        self._user_voice_activity_detected = False
        self._last_voice_frame_ms = 0
        self._last_client_speaker = NO_SPEAKER

        # Spoken input, as the main agent's provider transcribes it: who was
        # speaking when each input item started, and what it said.
        self._item_speakers: dict[str, int] = {}
        self._transcripts: dict[str, str] = {}
        self._transcript_arrived = asyncio.Event()

        # Whole turns, handled strictly one after the other.
        self._turn_queue: asyncio.Queue[TurnJob] = asyncio.Queue()
        self._turn_modality: str | None = None

        self._tasks: set[asyncio.Task[None]] = set()
        self._background: list[asyncio.Task[None]] = []
        self._closed = False

        # Set once a provider connection was lost and this session is on
        # its way out (see _handle_agent_connection_lost).
        self._ending = False
        self._policy_closed = False

    # ------------------------------------------------------------------
    # ConversationPort — what the cognition policy may ask of us
    # ------------------------------------------------------------------

    @property
    def conversation_id(self) -> str:
        return self._conversation_id

    @property
    def is_private(self) -> bool:
        return self._is_private

    @property
    def is_ephemeral(self) -> bool:
        """
        No client-given conversation id: this session is not a continuation
        of anything, so nothing about it is worth keeping.
        """

        return not self._has_conversation_id

    @property
    def linked_conversation_id(self) -> str | None:
        return self._linked_conversation_id

    def participants(self) -> dict[int, str]:
        return dict(self._participants)

    def voice_participants(self) -> dict[int, str]:
        return dict(self._voice_participants)

    async def instruct_main_agent(self, directive: str) -> None:
        if self._main_agent is None:
            return

        # Answer in the medium the turn arrived in: a text message gets a
        # text reply, a spoken turn a spoken one.
        await self._main_agent.trigger_response(
            instructions=directive,
            output_modality=self._turn_modality,
        )

    async def tell_observers(self, text: str) -> None:
        for agent in list(self._auxiliary_agents.values()):
            await agent.send_text(text)

    # ------------------------------------------------------------------
    # Conversation state, derived from the turns rather than tracked
    # ------------------------------------------------------------------

    @property
    def main_agent(self) -> MainAgent | None:
        return self._main_agent

    @property
    def auxiliary_agents(
        self,
    ) -> Mapping[UUID, AuxiliaryAgent]:
        return self._auxiliary_agents

    @property
    def current_speaker(self) -> int:
        """Who holds the turn right now, or -1 when nobody does."""

        if not self.turns or not self.turns[-1].is_open:
            return NO_SPEAKER

        return self.turns[-1].speaker_id

    @property
    def last_speaker(self) -> int:
        """The last user to speak, ignoring the assistant."""

        for turn in reversed(self.turns):
            if turn.speaker_id > self._main_agent_options.agent_id:
                return turn.speaker_id

        return NO_SPEAKER

    @property
    def assistant_spoke_last(self) -> bool:
        return (
            bool(self.turns)
            and self.turns[-1].speaker_id
            == self._main_agent_options.agent_id
        )

    @property
    def is_answering(self) -> bool:
        return self.main_agent_state is MainAgentState.ANSWERING

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        self._background = [
            asyncio.create_task(self._run_turns()),
            asyncio.create_task(self._run_clock()),
        ]

        await self._policy.start(self)

        # The client's handshake already connected the main agent; this
        # only matters for a conversation started without one.
        await self._connect_main_agent()

    async def close(self) -> None:
        # Closed by an explicit disconnect and again by the runtime.
        if self._closed:
            return

        self._closed = True

        # Already closed (memory saved) when a lost provider connection
        # ended this session; saving again now, possibly after the client's
        # next session has loaded that memory, could only overwrite it.
        if not self._policy_closed:
            self._policy_closed = True
            with suppress(Exception):
                await self._policy.close()

        for task in [*self._background, *self._tasks]:
            task.cancel()

        await asyncio.gather(
            *self._background,
            *self._tasks,
            return_exceptions=True,
        )

        self._background.clear()
        self._tasks.clear()

        if self._main_agent is not None:
            with suppress(Exception):
                await self._main_agent.disconnect()

        for agent in list(self._auxiliary_agents.values()):
            with suppress(Exception):
                await agent.disconnect()

        self._auxiliary_agents.clear()
        self.main_agent_state = MainAgentState.INACTIVE
        self._state.connected_agents.clear()
        self._state.active_agent = None

    async def wait_until_idle(self) -> None:
        """Wait until every queued turn has been fully handled."""

        await self._turn_queue.join()

    # ------------------------------------------------------------------
    # Incoming client messages
    # ------------------------------------------------------------------

    async def route_user_audio(
        self,
        message: AudioStreamMessage,
    ) -> None:
        """
        Forward one frame of user audio, then re-read who is speaking.

        Each frame is one tick of the conversation. A frame with no audio (or
        with speaker -1) means nobody is speaking; so does a long enough gap
        between frames, since some clients only send while someone talks.
        """

        now = _now_ms()

        self._report_assistant_playback(
            is_notifying=message.is_notifying,
        )
        self._state.playback_time_ms = message.playback_time_ms
        if self._main_agent is not None:
            self._main_agent.playback_time_ms = message.playback_time_ms

        chunk = self._decode(
            message.audio,
            speaker=message.speaker,
        )

        if chunk is not None:
            self._last_voice_frame_ms = now
            if message.speaker != NO_SPEAKER:
                self._last_client_speaker = message.speaker

            if self._main_agent is not None:
                await self._main_agent.send_audio_chunk(chunk)

            for agent in list(self._auxiliary_agents.values()):
                if agent.listens_to_user(message.speaker):
                    await agent.send_audio_chunk(chunk)

        await self._read_speaker(
            message.speaker
            if chunk is not None and message.speaker != NO_SPEAKER
            else None,
        )

    async def route_playback_frame(
        self,
        message: PlaybackFrameMessage,
    ) -> None:
        """
        Forward assistant audio to the auxiliary agents.

        The client echoes each frame as it plays it, so the observers hear the
        assistant at the same pace the users do rather than as it is generated.
        """

        chunk = self._decode(message.audio)
        if chunk is None:
            return

        if self.main_agent_state is not MainAgentState.ANSWERING:
            return

        for agent in list(self._auxiliary_agents.values()):
            if agent.listen_to_assistant:
                await agent.send_audio_chunk(chunk)

    async def route_text(self, message: TextMessage) -> None:
        agent = self._resolve(message.target_agent_guid)
        if agent is None:
            return

        await agent.send_text(message.text, role=message.role)

    async def route_user_text(
        self,
        message: UserTextMessage,
    ) -> None:
        """
        Queue one user's text utterance as a complete turn.

        Returns as soon as the turn is queued; `wait_until_idle` waits for it
        to be judged and answered.
        """

        self._enqueue_turn(lambda: self._take_text_turn(message))

    async def route_participants(
        self,
        message: ParticipantsMessage,
    ) -> None:
        roster = {
            participant.id: participant.name
            for participant in message.participants
        }
        if message.modality == "audio":
            self._voice_participants = roster
        else:
            self._participants = roster

    async def trigger_response(
        self,
        message: TriggerResponseMessage,
    ) -> None:
        agent = self._resolve(message.target_agent_guid)
        if agent is None:
            return

        await agent.trigger_response(
            max_tokens=message.max_tokens,
            interrupt=message.interrupt,
            instructions=message.instructions,
        )

    async def interrupt_response(
        self,
        message: InterruptMessage,
    ) -> None:
        agent = self._resolve(message.target_agent_guid)
        if agent is None:
            return

        if agent is self._main_agent:
            await self._main_agent.stop_speaking()
            return

        await agent.interrupt_response()

    async def connect_agent(
        self,
        message: ConnectAgentMessage,
    ) -> None:
        if not message.target_agent_guid:
            if message.conversation_id:
                self._conversation_id = message.conversation_id
                self._has_conversation_id = True

            self._is_private = message.is_dm
            self._linked_conversation_id = message.linked_conversation_id

            await self._connect_main_agent()
            return

        agent = self._resolve(message.target_agent_guid)
        if agent is None:
            return

        await agent.connect()
        self._state.connected_agents.add(
            message.target_agent_guid,
        )

    async def disconnect_agent(
        self,
        message: DisconnectMessage,
    ) -> None:
        """Disconnect one auxiliary agent, or the whole session."""

        if not message.target_agent_guid:
            await self.close()
            return

        agent = self._resolve(message.target_agent_guid)
        if agent is None:
            return

        await agent.disconnect()
        self._state.connected_agents.discard(
            message.target_agent_guid,
        )

    async def create_auxiliary_agent(
        self,
        message: CreateAuxiliaryAgentMessage,
    ) -> None:
        options = AuxiliaryAgentOptions.for_type(
            message.auxiliary_agent_type,
            # Otherwise the intervention agent always thought it was
            # watching a group channel, even inside a DM where every message
            # is unambiguously addressed to it.
            is_dm=self._is_private,
        )

        agent = self._agent_factory.build_auxiliary(options)
        agent.on_message = self._auxiliary_message_handler(
            agent.guid,
        )
        agent.on_decision = self._handle_intervention_signal
        agent.on_connection_lost = self._handle_agent_connection_lost

        await agent.connect()

        # Registered only once connected, so a failed connection leaves
        # nothing half-built behind.
        self._auxiliary_agents[agent.guid] = agent
        self._state.connected_agents.add(str(agent.guid))

        # Catches this one agent up on whatever's already in WM it was
        # never live for -- after a restart (WM restored from a snapshot
        # before any observer existed to see it happen), or any time a new
        # observer connects mid-conversation. A targeted send to just this
        # agent, not a broadcast via tell_observers: an observer that's
        # already been running this whole time already saw everything live
        # and doesn't need re-telling.
        catchup = self._policy.observer_catchup_text()
        if catchup:
            await agent.send_text(catchup, role="user")

        await self._send(
            AgentGuidMessage(agent_guid=str(agent.guid)),
        )

    async def remove_auxiliary_agent(
        self,
        message: RemoveAuxiliaryAgentMessage,
    ) -> None:
        if not message.target_agent_guid:
            logger.warning(
                "Ignoring auxiliary agent removal without a target guid"
            )
            return

        agent = self._resolve(message.target_agent_guid)
        if not isinstance(agent, AuxiliaryAgent):
            return

        self._auxiliary_agents.pop(agent.guid, None)
        self._state.connected_agents.discard(
            message.target_agent_guid,
        )

        with suppress(Exception):
            await agent.disconnect()

    # ------------------------------------------------------------------
    # Whole turns, through cognition
    # ------------------------------------------------------------------

    def _enqueue_turn(self, job: TurnJob) -> None:
        if self._ending:
            # The session is closing; its successor takes new turns.
            return

        self._turn_queue.put_nowait(job)

    async def _run_turns(self) -> None:
        while True:
            job = await self._turn_queue.get()

            try:
                await job()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "A turn in conversation %s failed",
                    self._conversation_id,
                )
            finally:
                self._turn_modality = None
                self._turn_queue.task_done()

    async def _take_text_turn(self, message: UserTextMessage) -> None:
        """
        One user's text utterance, taken as a complete turn.

        Unlike audio, which arrives as a stream of frames the server has to
        read a speaker out of, a text message is a whole turn the moment it
        arrives: it opens and closes in one step.

        Ordering invariant: the raw utterance is forwarded to observers here,
        *before* `self._policy.on_utterance(...)` runs below. That call is
        what appends this utterance to working memory and computes trigger
        variables from it, and `CoalaPolicy.on_utterance` then sends those
        variables to the same observers as a second message. So an observer
        always sees (this turn's utterance, then this turn's variables) in
        that order, both before it's ever asked to decide anything -- reorder
        either side of this and an observer would be asked to judge before
        seeing one half of this turn's information.
        """

        self._turn_modality = "text"

        if message.speaker_name:
            self._participants[message.speaker] = message.speaker_name

        label = self._username(message.speaker)
        spoken = f"{label}: {message.text}"

        if self._main_agent is not None:
            await self._main_agent.send_text(spoken, role="user")

        for agent in list(self._auxiliary_agents.values()):
            if agent.listens_to_user(message.speaker):
                await agent.send_text(spoken, role="user")

        timestamp = _now_ms()
        self.turns.append(
            Turn(
                start_time=timestamp,
                end_time=timestamp,
                speaker_id=message.speaker,
                speaker_name=label,
            )
        )

        await self._send_state_update()

        await self._judge(
            Utterance(
                speaker_id=message.speaker,
                speaker_name=label,
                content=message.text,
                timestamp_ms=timestamp,
                modality="text",
            )
        )

    async def _take_voice_turn(
        self,
        speaker: int,
        ended_at: int,
    ) -> None:
        """
        A spoken turn that just ended, once the provider has transcribed it.

        The main agent and the observers have already heard the audio; this
        is what gives cognition the words (history, trigger variables, the
        episodic log), exactly as a text message does.
        """

        self._turn_modality = "audio"

        transcript = await self._collect_transcript(speaker)
        # debug, not info: this is diagnostic detail for investigating the
        # turn/cooldown machinery, not something worth seeing on every
        # single spoken turn by default -- set MUCA_LOG_LEVEL=DEBUG to see
        # these (and the rest of the [voice-turn]-tagged lines) again.
        logger.debug(
            "[voice-turn] %s _take_voice_turn transcript for %s: %r",
            self._conversation_id,
            self._username(speaker),
            transcript,
        )
        if not transcript:
            # Nothing understood (noise, or transcription failed): the
            # observers can still judge from what they heard. Logged too --
            # every other outcome (including "decided not to act") gets an
            # audit row; this one shouldn't be the silent exception.
            logger.debug(
                "No transcript for %s's turn, asking observers anyway",
                self._username(speaker),
            )
            await self._policy.on_untranscribed_turn(
                speaker,
                self._username(speaker),
                ended_at,
            )
            self._ask_observers_to_consider(speaker)
            return

        await self._judge(
            Utterance(
                speaker_id=speaker,
                speaker_name=self._username(speaker),
                content=transcript,
                timestamp_ms=ended_at,
                modality="audio",
            )
        )

    async def _judge(self, utterance: Utterance) -> None:
        consult_observers = await self._policy.on_utterance(utterance)

        if consult_observers:
            if self._listening_to_user(utterance.speaker_id):
                self._ask_observers_to_consider(utterance.speaker_id)
            else:
                # Nobody to judge this turn: it resolves as "say nothing"
                # right away, rather than holding every later turn until
                # the policy's timeout.
                await self._policy.on_intervention_signal(
                    NO_OBSERVER,
                    None,
                )

        # Serializes turns end to end (judge -> act -> reply), so a second
        # utterance can never be attributed to the wrong decision -- found
        # from a real duplicate/missing episodic-log entry, not
        # hypothetically.
        await self._policy.wait_for_turn_to_finish()

    async def _collect_transcript(self, speaker: int) -> str:
        """
        What the provider transcribed of `speaker`'s input items, waiting
        (briefly) for the ones still being transcribed.
        """

        loop = asyncio.get_running_loop()
        deadline = loop.time() + TRANSCRIPT_WAIT_SECONDS
        texts: list[str] = []

        while True:
            for item_id, item_speaker in list(self._item_speakers.items()):
                if item_speaker == speaker and item_id in self._transcripts:
                    del self._item_speakers[item_id]
                    text = self._transcripts.pop(item_id)
                    if text:
                        texts.append(text)

            pending = [
                item_id
                for item_id, item_speaker in self._item_speakers.items()
                if item_speaker == speaker
            ]

            remaining = deadline - loop.time()
            if (texts and not pending) or remaining <= 0:
                # A transcript that never came is not waited for again.
                for item_id in pending:
                    self._item_speakers.pop(item_id, None)

                return " ".join(texts)

            self._transcript_arrived.clear()
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(
                    self._transcript_arrived.wait(),
                    timeout=remaining,
                )

    # ------------------------------------------------------------------
    # Turn taking
    # ------------------------------------------------------------------

    async def _run_clock(self) -> None:
        """
        Re-read the conversation every tick, for what no message announces:
        a client that went quiet (silence), and playback that drained.
        """

        silence = AudioChunk(
            data=bytes(4 * DEFAULT_SAMPLE_RATE * TICK_MS // 1000),
        )

        while True:
            await asyncio.sleep(TICK_MS / 1000)

            try:
                now = _now_ms()
                self._refresh_playback()

                # Keep the provider's audio clock running while its VAD
                # still hears speech: a client that stops sending when
                # nobody talks would otherwise leave the provider waiting
                # forever for the trailing silence that ends the utterance.
                if (
                    self._user_voice_activity_detected
                    and now - self._last_voice_frame_ms >= TICK_MS
                ):
                    await self._send_to_listeners(silence)

                stale = (
                    self._last_client_frame_ms is None
                    or now - self._last_client_frame_ms
                    > self._silence_timeout_ms
                )
                if stale:
                    await self._read_speaker(None)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Conversation clock tick failed")

    async def _send_to_listeners(self, chunk: AudioChunk) -> None:
        if self._main_agent is not None:
            await self._main_agent.send_audio_chunk(chunk)

        for agent in list(self._auxiliary_agents.values()):
            await agent.send_audio_chunk(chunk)

    async def _read_speaker(
        self,
        speaker: int | None,
    ) -> None:
        if self._main_agent is None:
            return

        last_turn = self.turns[-1] if self.turns else None
        old_speaker = (
            last_turn.speaker_id
            if last_turn is not None and last_turn.is_open
            else None
        )

        # With provider VAD, only believe the client once the provider has
        # confirmed that somebody is speaking.
        current_speaker = (
            speaker
            if (
                not self._main_agent.capabilities.vad
                or self._user_voice_activity_detected
            )
            else None
        )

        if (
            self._user_voice_activity_detected
            and current_speaker is None
        ):
            current_speaker = old_speaker

        if (
            old_speaker is not None
            and old_speaker == self._main_agent.agent_id
        ):
            # debug: fires on every tick while the assistant is speaking --
            # the noisiest of these by far. MUCA_LOG_LEVEL=DEBUG to see it.
            logger.debug(
                "[voice-turn] %s _read_speaker: assistant was speaking, "
                "incoming speaker=%s (raw=%s) -> %s",
                self._conversation_id,
                current_speaker,
                speaker,
                "interrupting"
                if current_speaker is not None
                else (
                    "ending (can_stop)"
                    if self._assistant_can_stop()
                    else "waiting"
                ),
            )

            # The main agent cannot see playback, so these come from here.
            if current_speaker is not None:
                await self._handle_main_agent_event(
                    MainAgentEvent.AGENT_SPEECH_INTERRUPTED,
                )
            elif self._assistant_can_stop():
                self._spawn(self._end_assistant_turn())

            # Wait for the assistant's turn to close before moving on.
            return

        if old_speaker != current_speaker:
            await self._on_speaker_changed(
                old_speaker,
                current_speaker,
            )

    async def _on_speaker_changed(
        self,
        old_speaker: int | None,
        current_speaker: int | None,
    ) -> None:
        logger.debug(
            "Speaker changed from %s to %s",
            old_speaker,
            current_speaker,
        )

        timestamp = _now_ms()
        main_agent_text = ""
        auxiliary_text: dict[UUID, str] = {}

        if old_speaker is not None:
            if self.turns:
                self.turns[-1].end_time = timestamp

            line = f"{self._username(old_speaker)} stopped speaking.\n"
            main_agent_text += line

            for agent in self._listening_to_user(old_speaker):
                auxiliary_text[agent.guid] = (
                    auxiliary_text.get(agent.guid, "") + line
                )

        if current_speaker is not None:
            self.turns.append(
                Turn(
                    start_time=timestamp,
                    speaker_id=current_speaker,
                    speaker_name=self._username(current_speaker),
                )
            )

            line = f"{self._username(current_speaker)} started speaking.\n"
            main_agent_text += line

            for agent in self._listening_to_user(current_speaker):
                auxiliary_text[agent.guid] = (
                    auxiliary_text.get(agent.guid, "") + line
                )

        for guid, text in auxiliary_text.items():
            agent = self._auxiliary_agents.get(guid)
            if agent is not None:
                await agent.send_text(SPEAKER_CHANGED + text)

        if self._main_agent is not None and main_agent_text:
            await self._main_agent.send_text(main_agent_text)

        await self._send_state_update()

        if old_speaker is None:
            return

        # Ignore user to user transitions.
        if current_speaker is not None:
            return

        if self._provider_vad():
            # The provider transcribes speech: the turn goes through
            # cognition like a text message, once its words are back.
            #
            # Debugging a reported cooldown bug (a strategy refiring far
            # more often than cooldown_messages should allow): every one of
            # these is a complete turn for cognition purposes (one
            # increment of total_message_count, one cooldown check). If the
            # provider's VAD ever reports a false "stopped speaking" mid
            # sentence (a natural pause exceeding its silence threshold)
            # followed by "started" again for the same continuous
            # utterance, one human sentence becomes several of these here
            # -- which would silently consume a whole cooldown window
            # within a single sentence. This count is exactly what would
            # show that. debug: MUCA_LOG_LEVEL=DEBUG to see it.
            logger.debug(
                "[voice-turn] %s enqueuing a voice turn for %s "
                "(old_speaker=%s -> current_speaker=None)",
                self._conversation_id,
                self._username(old_speaker),
                old_speaker,
            )
            self._enqueue_turn(
                lambda: self._take_voice_turn(old_speaker, timestamp),
            )
        else:
            # No words will ever come for it; the observers judge the audio.
            self._ask_observers_to_consider(old_speaker)

    def _ask_observers_to_consider(self, speaker: int) -> None:
        """Somebody finished speaking: let the observers decide to step in."""

        for agent in self._listening_to_user(speaker):
            self._spawn(agent.trigger_response())

    # ------------------------------------------------------------------
    # Main agent callbacks
    # ------------------------------------------------------------------

    async def _handle_main_agent_event(
        self,
        event: MainAgentEvent,
    ) -> None:
        # debug: fires on every MainAgentEvent (several times per turn) --
        # MUCA_LOG_LEVEL=DEBUG to see it when actively investigating the
        # barge-in/interrupt path specifically.
        logger.debug(
            "[voice-turn] %s event=%s turn_ending=%s state=%s "
            "generation_done=%s silent=%s",
            self._conversation_id,
            event,
            self._assistant_turn_ending,
            self.main_agent_state,
            self._assistant_generation_done,
            self._assistant_silent.is_set(),
        )

        match event:
            case MainAgentEvent.AGENT_SPEECH_STARTED:
                self._assistant_generation_done = False
                self._assistant_turn_ending = False
                self._assistant_audio_first_ms = None
                self._assistant_audio_sent_ms = 0
                self._spawn(self._begin_assistant_turn())

            case MainAgentEvent.AGENT_SPEECH_STOPPED:
                # Generation is over; the turn ends once playback drains
                # (checked here and on every tick from then on).
                self._assistant_generation_done = True
                if self._assistant_turn_open() and self._assistant_can_stop():
                    self._spawn(self._end_assistant_turn())

            case MainAgentEvent.AGENT_SPEECH_INTERRUPTED:
                await self._interrupt_assistant_turn()

            case MainAgentEvent.USER_SPEECH_STARTED:
                if not self._provider_vad():
                    return

                self._user_voice_activity_detected = True
                self.main_agent_state = MainAgentState.LISTENING

                item_id = self._main_agent.last_input_item_id
                if item_id:
                    self._item_speakers[item_id] = self._voice_speaker_now()

            case MainAgentEvent.USER_SPEECH_STOPPED:
                if not self._provider_vad():
                    return

                self._user_voice_activity_detected = False
                self.main_agent_state = MainAgentState.INACTIVE

    async def _begin_assistant_turn(self) -> None:
        if self._main_agent is None:
            return

        interrupts_itself = (
            self._main_agent.capabilities.interrupt
        )
        logger.debug(
            "[voice-turn] %s _begin_assistant_turn interrupts_itself=%s "
            "user_speaking=%s open_turns=%d",
            self._conversation_id,
            interrupts_itself,
            self._user_is_speaking(),
            sum(1 for t in self.turns if t.is_open),
        )

        # A user is still mid-turn, so the assistant should not have started.
        if self._user_is_speaking() and not interrupts_itself:
            logger.debug(
                "[voice-turn] %s _begin_assistant_turn: bailing, user "
                "already speaking",
                self._conversation_id,
            )
            await self._main_agent.stop_speaking()
            await self._clear_client_audio()
            return

        try:
            await asyncio.wait_for(
                self._assistant_notifying.wait(),
                timeout=PLAYBACK_START_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.info(
                "The client never reported playing the reply; the "
                "assistant takes its turn anyway",
            )

        # A user started before the assistant became audible: it never really
        # took a turn.
        if self._user_is_speaking():
            if not interrupts_itself:
                await self._main_agent.stop_speaking()

            await self._clear_client_audio()
            await self._tell_observers(
                f"MAIN ASSISTANT ({self._main_agent.name}) was about to "
                "speak but someone else spoke."
            )
            return

        self.turns.append(
            Turn(
                start_time=_now_ms(),
                speaker_id=self._main_agent.agent_id,
                speaker_name=self._main_agent.name,
            )
        )

        await self._tell_observers(
            f"MAIN ASSISTANT ({self._main_agent.name}) started speaking."
        )

        self.main_agent_state = MainAgentState.ANSWERING
        await self._send_state_update()

        # A short reply may have finished generating (and even playing)
        # before the turn was taken.
        if self._assistant_can_stop():
            self._spawn(self._end_assistant_turn())

    async def _end_assistant_turn(self) -> None:
        if self._main_agent is None or self._assistant_turn_ending:
            logger.debug(
                "[voice-turn] %s _end_assistant_turn: bailing early "
                "(main_agent=%s already_ending=%s) -- a call that returns "
                "here instead of proceeding never clears the turn, which "
                "is how a stuck turn would show up in these logs",
                self._conversation_id,
                self._main_agent is not None,
                self._assistant_turn_ending,
            )
            return

        self._assistant_turn_ending = True
        wait_started = _now_ms()

        await self._assistant_silent.wait()

        logger.debug(
            "[voice-turn] %s _end_assistant_turn: silent after %dms wait, "
            "closing turn",
            self._conversation_id,
            _now_ms() - wait_started,
        )

        await self._tell_observers(
            f"MAIN ASSISTANT ({self._main_agent.name}) stopped speaking."
        )

        self._close_assistant_turn(_now_ms())
        self.main_agent_state = MainAgentState.INACTIVE
        self._reset_assistant_audio()

        await self._send(
            StateUpdateMessage.response_completed(
                current_speaker=self.current_speaker,
                last_speaker=self.last_speaker,
            )
        )

    async def _interrupt_assistant_turn(self) -> None:
        if self._main_agent is None:
            return

        logger.debug(
            "[voice-turn] %s _interrupt_assistant_turn: interrupts_itself=%s",
            self._conversation_id,
            self._main_agent.capabilities.interrupt,
        )

        self._assistant_turn_ending = True

        await self._tell_observers(
            f"MAIN ASSISTANT ({self._main_agent.name}) stopped speaking but "
            "it did not finish saying what it wanted to say because it was "
            "interrupted."
        )

        if not self._main_agent.capabilities.interrupt:
            await self._main_agent.stop_speaking()

        self._close_assistant_turn(_now_ms())
        self.main_agent_state = MainAgentState.INACTIVE
        self._reset_assistant_audio()

        # Audio is generated faster than it is played, so the client is still
        # holding speech that is no longer wanted.
        await self._clear_client_audio()

    async def _handle_main_agent_audio(
        self,
        chunk: AudioChunk,
    ) -> None:
        if self._assistant_audio_first_ms is None:
            self._assistant_audio_first_ms = _now_ms()
        self._assistant_audio_sent_ms += chunk.duration_ms
        self._refresh_playback()

        for frame in split_float32(chunk.data, MAX_SAMPLES_PER_FRAME):
            await self._send(
                StateUpdateMessage.audio_chunk(
                    current_speaker=self.current_speaker,
                    last_speaker=self.last_speaker,
                    encoded_audio=AudioChunk(data=frame).to_base64(),
                    is_answering=self.is_answering,
                    assistant_spoke_last=self.assistant_spoke_last,
                )
            )

    async def _handle_main_agent_text(
        self,
        text: str,
        is_final: bool,
        modality: str = "text",
        interrupted: bool = False,
    ) -> None:
        await self._send(
            AssistantTextMessage(
                text=text,
                is_final=is_final,
                modality=modality,
                interrupted=interrupted and is_final,
            ),
        )

        if text:
            self._assistant_reply.append(text)

        if is_final:
            reply = "".join(self._assistant_reply)
            self._assistant_reply.clear()

            # Observers otherwise only ever hear that MULCA spoke (the
            # generic "started/stopped speaking" notices below), never what
            # it actually said -- a blind spot found while auditing history
            # symmetry between the main and intervention agents. A spoken
            # reply they have already heard, through playback frames.
            if (
                reply
                and modality == "text"
                and self._main_agent is not None
            ):
                spoken = f"{self._main_agent.name}: {reply}"
                for agent in list(self._auxiliary_agents.values()):
                    if agent.listen_to_assistant:
                        await agent.send_text(spoken, role="user")

            await self._policy.on_assistant_output(
                reply,
                interrupted=interrupted,
            )

    async def _handle_transcript(
        self,
        item_id: str,
        transcript: str,
    ) -> None:
        # Speech the provider heard without raising speech_started for it
        # (it happens) still belongs to someone: the last person who spoke.
        self._item_speakers.setdefault(item_id, self._voice_speaker_now())
        self._transcripts[item_id] = transcript.strip()
        self._transcript_arrived.set()

    async def _handle_intervention_signal(
        self,
        agent_guid: UUID,
        signal: InterventionSignal | None,
    ) -> None:
        await self._policy.on_intervention_signal(
            agent_guid,
            signal,
        )

    async def _handle_main_agent_message(
        self,
        raw: str,
    ) -> None:
        await self._send(AgentMessage(text=raw))

    def _auxiliary_message_handler(self, guid: UUID):
        async def handle(raw: str) -> None:
            await self._send(
                AgentMessage(agent_guid=str(guid), text=raw),
            )

        return handle

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _connect_main_agent(self) -> None:
        if self._main_agent is None:
            self._main_agent = self._agent_factory.build_main(
                self._main_agent_options,
            )
            self._main_agent.on_event = (
                self._handle_main_agent_event
            )
            self._main_agent.on_audio = (
                self._handle_main_agent_audio
            )
            self._main_agent.on_text = (
                self._handle_main_agent_text
            )
            self._main_agent.on_transcript = (
                self._handle_transcript
            )
            self._main_agent.on_message = (
                self._handle_main_agent_message
            )
            self._main_agent.on_connection_lost = (
                self._handle_agent_connection_lost
            )

        await self._main_agent.connect()
        self._state.active_agent = self._main_agent.name

    def _resolve(
        self,
        target_agent_guid: str | None,
    ) -> RealtimeAgent | None:
        """An empty or missing guid addresses the main agent."""

        if not target_agent_guid:
            return self._main_agent

        try:
            guid = UUID(target_agent_guid)
        except ValueError:
            logger.warning(
                "Ignoring message for malformed agent guid %r",
                target_agent_guid,
            )
            return None

        agent = self._auxiliary_agents.get(guid)
        if agent is None:
            logger.warning(
                "Ignoring message for unknown agent %s",
                guid,
            )

        return agent

    def _listening_to_user(
        self,
        user_id: int,
    ) -> list[AuxiliaryAgent]:
        return [
            agent
            for agent in self._auxiliary_agents.values()
            if agent.listens_to_user(user_id)
        ]

    async def _tell_observers(self, text: str) -> None:
        for agent in list(self._auxiliary_agents.values()):
            if agent.listen_to_assistant:
                await agent.send_text(SPEAKER_CHANGED + text)

    def _user_is_speaking(self) -> bool:
        return (
            bool(self.turns)
            and self.turns[-1].is_open
            and not self._assistant_turn_open()
        )

    def _assistant_turn_open(self) -> bool:
        return (
            bool(self.turns)
            and self.turns[-1].is_open
            and self.turns[-1].speaker_id == self._main_agent_options.agent_id
        )

    def _assistant_can_stop(self) -> bool:
        """Generated in full, and no longer audible on the client."""

        return (
            self._assistant_generation_done
            and not self._assistant_turn_ending
            and self._assistant_silent.is_set()
        )

    def _voice_speaker_now(self) -> int:
        """Who is speaking according to the client, or who spoke last."""

        recent = (
            _now_ms() - self._last_voice_frame_ms
            <= self._silence_timeout_ms + TICK_MS
        )
        if recent and self._last_client_speaker != NO_SPEAKER:
            return self._last_client_speaker

        if self._user_is_speaking():
            return self.turns[-1].speaker_id

        return self.last_speaker

    def _provider_vad(self) -> bool:
        return (
            self._main_agent is not None
            and self._main_agent.capabilities.vad
        )

    def _close_assistant_turn(self, timestamp: int) -> None:
        if self._main_agent is None:
            return

        for turn in reversed(self.turns):
            if turn.speaker_id == self._main_agent.agent_id:
                if turn.is_open:
                    turn.end_time = timestamp
                return

    def _reset_assistant_audio(self) -> None:
        self._assistant_generation_done = False
        self._assistant_audio_first_ms = None
        self._assistant_audio_sent_ms = 0

    def _username(self, user_id: int) -> str:
        if user_id == self._main_agent_options.agent_id:
            return self._main_agent_options.name

        if user_id in self._participants:
            return self._participants[user_id]

        return self._voice_participants.get(
            user_id,
            f"User {user_id}",
        )

    def _report_assistant_playback(
        self,
        *,
        is_notifying: bool,
    ) -> None:
        self._client_notifying = is_notifying
        self._last_client_frame_ms = _now_ms()
        self._refresh_playback()

    def _refresh_playback(self) -> None:
        """
        Whether the assistant is audible on the client right now.

        Taken from the client while it keeps reporting; estimated from the
        audio sent so far once it goes quiet (a client that only sends frames
        while someone talks).
        """

        now = _now_ms()
        reporting = (
            self._last_client_frame_ms is not None
            and now - self._last_client_frame_ms <= self._silence_timeout_ms
        )

        if reporting:
            audible = self._client_notifying
        else:
            audible = (
                self._assistant_audio_first_ms is not None
                and now
                < self._assistant_audio_first_ms
                + self._assistant_audio_sent_ms
            )

        if audible:
            self._assistant_silent.clear()
            self._assistant_notifying.set()
        else:
            self._assistant_notifying.clear()
            self._assistant_silent.set()

    async def _clear_client_audio(self) -> None:
        await self._send(
            StateUpdateMessage.interrupted(
                current_speaker=self.current_speaker,
                last_speaker=self.last_speaker,
            )
        )

    async def _send_state_update(self) -> None:
        await self._send(
            StateUpdateMessage(
                is_connected=self._state.is_connected,
                current_speaker=self.current_speaker,
                last_speaker=self.last_speaker,
                is_answering=self.is_answering,
                assistant_spoke_last=self.assistant_spoke_last,
                clear_audio_queue=False,
            )
        )

    async def _send(
        self,
        message: OutgoingProtocolMessage | EndSession,
    ) -> None:
        self._state.current_speaker = self.current_speaker
        self._state.last_speaker = self.last_speaker
        self._state.is_answering = self.is_answering
        self._state.assistant_spoke_last = (
            self.assistant_spoke_last
        )

        await self._outbound_queue.put(message)

    async def _handle_agent_connection_lost(
        self,
        agent: RealtimeAgent,
    ) -> None:
        """
        A provider connection ended on its own -- OpenAI closes every
        Realtime session after 60 minutes. Nothing in this session works
        without it, so end the whole session and let the client reopen: a
        fresh session reconnects the agents and reloads the memory saved
        here, the same path a server restart takes.

        Runs inside the lost agent's own receive task, which close() would
        cancel -- so it only asks the runtime to close the websocket (an
        EndSession on the outbound queue), it doesn't close anything itself.
        """

        if self._closed or self._ending:
            return

        self._ending = True
        logger.info(
            "%s's provider ended the realtime session of %s; ending this "
            "session so the client reopens it",
            type(agent).__name__,
            self._conversation_id,
        )

        # Saved before the socket closes, not after: the client reopens as
        # soon as it sees the close, and its new session loads this memory.
        self._policy_closed = True
        with suppress(Exception):
            await self._policy.close()

        await self._send(EndSession())

    def _spawn(
        self,
        coroutine: Coroutine[Any, Any, None],
    ) -> None:
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _decode(
        self,
        encoded: str,
        *,
        speaker: int = NO_SPEAKER,
    ) -> AudioChunk | None:
        if not encoded:
            return None

        try:
            chunk = AudioChunk.from_base64(
                encoded,
                speaker=speaker,
            )
        except (binascii.Error, ValueError):
            logger.warning(
                "Dropping malformed audio frame from speaker %d",
                speaker,
            )
            return None

        return chunk or None
