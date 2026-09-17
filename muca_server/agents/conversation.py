from __future__ import annotations

import asyncio
import binascii
import logging
import time
from collections.abc import Coroutine, Mapping
from contextlib import suppress
from typing import TYPE_CHECKING, Any
from uuid import UUID

from ..audio import AudioChunk
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


def _now_ms() -> int:
    return int(time.time() * 1000)


class Conversation:
    """
    Orchestrates a realtime multi-agent conversation and delegates the
    assistant's work to a MainAgent.

    This class assumes the client reports the user whose turn it is right now,
    that two users never hold the turn at the same time, and that backchannels
    ("um", "hmm") are already filtered out. It also assumes that if a user and
    the assistant speak over each other, the assistant is interrupted, either
    by its own provider or by this class.

    Only the main agent speaks to the users, so only its audio travels back to
    the client. Auxiliary agents observe: they receive user audio, and the
    assistant's audio only once the users have actually heard it.
    """

    def __init__(
        self,
        *,
        state: SessionState,
        outbound_queue: asyncio.Queue[
            OutgoingProtocolMessage
        ],
        main_agent_options: MainAgentOptions,
        agent_factory: AgentFactory | None = None,
        policy: CognitionPolicy | None = None,
    ) -> None:
        self._state = state
        self._outbound_queue = outbound_queue
        self._main_agent_options = main_agent_options
        self._agent_factory = (
            agent_factory or DefaultAgentFactory()
        )
        self._policy = policy or NoCognition()

        self._conversation_id = str(state.session_id)
        self._is_private = False
        self._linked_conversation_id: str | None = None

        self._main_agent: MainAgent | None = None
        self._auxiliary_agents: dict[UUID, AuxiliaryAgent] = {}

        self.turns: list[Turn] = []
        self.main_agent_state = MainAgentState.INACTIVE

        # Everyone present, including those who have not spoken yet.
        self._participants: dict[int, str] = {}

        # Deltas of the reply being streamed, until it completes.
        self._assistant_reply: list[str] = []

        # What the client reports about its own playback of the main
        # agent's audio: the server cannot see it directly.
        self._assistant_notifying = asyncio.Event()
        self._assistant_silent = asyncio.Event()
        self._assistant_silent.set()

        self._user_voice_activity_detected = False
        self._tasks: set[asyncio.Task[None]] = set()

    # ------------------------------------------------------------------
    # Conversation state, derived from the turns rather than tracked
    # ------------------------------------------------------------------

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
    def linked_conversation_id(self) -> str | None:
        return self._linked_conversation_id

    def participants(self) -> dict[int, str]:
        return dict(self._participants)

    async def instruct_main_agent(self, directive: str) -> None:
        if self._main_agent is None:
            return

        await self._main_agent.trigger_response(
            instructions=directive,
        )

    async def tell_observers(self, text: str) -> None:
        for agent in list(self._auxiliary_agents.values()):
            await agent.send_text(text)

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
        await self._policy.start(self)

        # The client's handshake is its request to connect the main agent,
        # and the runtime consumes that message before dispatching begins.
        await self._connect_main_agent()

    async def close(self) -> None:
        with suppress(Exception):
            await self._policy.close()

        for task in self._tasks:
            task.cancel()

        if self._tasks:
            await asyncio.gather(
                *self._tasks,
                return_exceptions=True,
            )

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

    # ------------------------------------------------------------------
    # Incoming client messages
    # ------------------------------------------------------------------

    async def route_user_audio(
        self,
        message: AudioStreamMessage,
    ) -> None:
        """
        Forward one frame of user audio, then re-read who is speaking.

        Each of these frames is one tick of the conversation: the client
        pushes them continuously, whether or not anyone is talking.
        """

        self._report_assistant_playback(
            is_notifying=message.is_notifying,
        )
        self._state.playback_time_ms = message.playback_time_ms

        chunk = self._decode(
            message.audio,
            speaker=message.speaker,
        )

        if chunk is not None:
            if self._main_agent is not None:
                await self._main_agent.send_audio_chunk(chunk)

            for agent in list(self._auxiliary_agents.values()):
                if agent.listens_to_user(message.speaker):
                    await agent.send_audio_chunk(chunk)

        # A frame with no audio means nobody is speaking.
        await self._read_speaker(
            message.speaker if chunk is not None else None,
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
        Take one user's text utterance as a complete turn.

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

        consult_observers = await self._policy.on_utterance(
            Utterance(
                speaker_id=message.speaker,
                speaker_name=label,
                content=message.text,
                timestamp_ms=timestamp,
                modality="text",
            )
        )

        if consult_observers:
            self._ask_observers_to_consider(message.speaker)

        # Serializes text turns end to end (judge -> act -> reply), so a
        # second utterance can never be attributed to the wrong decision --
        # found from a real duplicate/missing episodic-log entry, not
        # hypothetically. Text only: this call site is only reachable from
        # route_user_text, since audio never calls on_utterance in the
        # first place (see _ask_observers_to_consider below) -- audio's own
        # self-interrupt reflex handles overlap instead.
        await self._policy.wait_for_turn_to_finish()

    async def route_participants(
        self,
        message: ParticipantsMessage,
    ) -> None:
        self._participants = {
            participant.id: participant.name
            for participant in message.participants
        }

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

        await agent.interrupt_response()

    async def connect_agent(
        self,
        message: ConnectAgentMessage,
    ) -> None:
        if not message.target_agent_guid:
            if message.conversation_id:
                self._conversation_id = message.conversation_id

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

        self._auxiliary_agents[agent.guid] = agent

        await agent.connect()
        self._state.connected_agents.add(str(agent.guid))

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
    # Turn taking
    # ------------------------------------------------------------------

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
            # The main agent cannot see playback, so these come from here.
            if current_speaker is not None:
                await self._handle_main_agent_event(
                    MainAgentEvent.AGENT_SPEECH_INTERRUPTED,
                )
            elif not self._assistant_notifying.is_set():
                await self._handle_main_agent_event(
                    MainAgentEvent.AGENT_SPEECH_STOPPED,
                )

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

        # Goes straight to the observers, bypassing CoalaPolicy.on_utterance
        # entirely -- spoken turns are not appended to working memory, get
        # no trigger variables, no forced-intervention check, and no
        # episodic log entry, unlike text.
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
        logger.debug("Main agent event %s", event)

        match event:
            case MainAgentEvent.AGENT_SPEECH_STARTED:
                self._spawn(self._begin_assistant_turn())

            case MainAgentEvent.AGENT_SPEECH_STOPPED:
                self._spawn(self._end_assistant_turn())

            case MainAgentEvent.AGENT_SPEECH_INTERRUPTED:
                await self._interrupt_assistant_turn()

            case MainAgentEvent.USER_SPEECH_STARTED:
                if not self._provider_vad():
                    return

                self._user_voice_activity_detected = True
                self.main_agent_state = MainAgentState.LISTENING

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

        # A user is still mid-turn, so the assistant should not have started.
        if self._user_is_speaking() and not interrupts_itself:
            await self._main_agent.interrupt_response()
            await self._clear_client_audio()
            return

        await self._assistant_notifying.wait()

        # A user started before the assistant became audible: it never really
        # took a turn.
        if self._user_is_speaking():
            if not interrupts_itself:
                await self._main_agent.interrupt_response()

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

    async def _end_assistant_turn(self) -> None:
        if self._main_agent is None:
            return

        await self._assistant_silent.wait()

        await self._tell_observers(
            f"MAIN ASSISTANT ({self._main_agent.name}) stopped speaking."
        )

        self._close_assistant_turn(_now_ms())
        self.main_agent_state = MainAgentState.INACTIVE

        await self._send(
            StateUpdateMessage.response_completed(
                current_speaker=self.current_speaker,
                last_speaker=self.last_speaker,
            )
        )

    async def _interrupt_assistant_turn(self) -> None:
        if self._main_agent is None:
            return

        await self._tell_observers(
            f"MAIN ASSISTANT ({self._main_agent.name}) stopped speaking but "
            "it did not finish saying what it wanted to say because it was "
            "interrupted."
        )

        if not self._main_agent.capabilities.interrupt:
            await self._main_agent.interrupt_response()

        self._close_assistant_turn(_now_ms())
        self.main_agent_state = MainAgentState.INACTIVE

        # Audio is generated faster than it is played, so the client is still
        # holding speech that is no longer wanted.
        await self._clear_client_audio()

    async def _handle_main_agent_audio(
        self,
        chunk: AudioChunk,
    ) -> None:
        await self._send(
            StateUpdateMessage.audio_chunk(
                current_speaker=self.current_speaker,
                last_speaker=self.last_speaker,
                encoded_audio=chunk.to_base64(),
            )
        )

    async def _handle_main_agent_text(
        self,
        text: str,
        is_final: bool,
    ) -> None:
        await self._send(
            AssistantTextMessage(text=text, is_final=is_final),
        )

        if text:
            self._assistant_reply.append(text)

        if is_final:
            reply = "".join(self._assistant_reply)
            self._assistant_reply.clear()

            # Observers otherwise only ever hear that MULCA spoke (the
            # generic "started/stopped speaking" notices below), never what
            # it actually said -- a blind spot found while auditing history
            # symmetry between the main and intervention agents.
            if reply and self._main_agent is not None:
                spoken = f"{self._main_agent.name}: {reply}"
                for agent in list(self._auxiliary_agents.values()):
                    if agent.listen_to_assistant:
                        await agent.send_text(spoken, role="user")

            await self._policy.on_assistant_output(
                reply,
                interrupted=False,
            )

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
            self._main_agent.on_message = (
                self._handle_main_agent_message
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
        return bool(self.turns) and self.turns[-1].is_open

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
                turn.end_time = timestamp
                return

    def _username(self, user_id: int) -> str:
        if user_id == self._main_agent_options.agent_id:
            return self._main_agent_options.name

        return self._participants.get(
            user_id,
            f"User {user_id}",
        )

    def _report_assistant_playback(
        self,
        *,
        is_notifying: bool,
    ) -> None:
        if is_notifying:
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
        message: OutgoingProtocolMessage,
    ) -> None:
        self._state.current_speaker = self.current_speaker
        self._state.last_speaker = self.last_speaker
        self._state.is_answering = self.is_answering
        self._state.assistant_spoke_last = (
            self.assistant_spoke_last
        )

        await self._outbound_queue.put(message)

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
