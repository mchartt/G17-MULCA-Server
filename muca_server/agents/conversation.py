from __future__ import annotations

import asyncio
import binascii
import logging
import os
import re
import time
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from contextlib import suppress
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID

from ..audio import AudioChunk
from ..audio.chunk import DEFAULT_SAMPLE_RATE
from ..audio.pcm import split_float32
from .voice_control_phrases import (
    DISMISSAL_PATTERN,
    WAKE_ANY_ORDER_PATTERN,
    address_word as _address_word,
    addresses_assistant as _addresses_assistant,
    matches_dismissal as _matches_dismissal,
    matches_wake_any_order as _matches_wake_any_order,
    matches_wake_phrase as _matches_wake_phrase,
    voice_command_v2 as _voice_command_v2,
)
from ..cognition.noop import NoCognition
from ..cognition.ports import (
    CognitionPolicy,
    InterventionSignal,
    Utterance,
)
from ..protocol import (
    AgentGuidMessage,
    AgentMessage,
    ArmListeningMessage,
    AssistantTextMessage,
    AudioStreamMessage,
    ConnectAgentMessage,
    CreateAuxiliaryAgentMessage,
    DisconnectMessage,
    EndSession,
    InterruptMessage,
    ListeningStateMessage,
    OutgoingProtocolMessage,
    ParticipantsMessage,
    PlaybackFrameMessage,
    RawProviderMessage,
    RemoveAuxiliaryAgentMessage,
    SetCostAlertMessage,
    SetModelMessage,
    StateUpdateMessage,
    TextMessage,
    TriggerResponseMessage,
    TurnSkippedMessage,
    UsageMessage,
    UserTextMessage,
)
from .auxiliary_agent import AuxiliaryAgent
from .factory import AgentFactory, DefaultAgentFactory
from .instructions import SPEAKER_CHANGED
from .main_agent import MainAgent, MainAgentEvent, MainAgentState
from .options import AuxiliaryAgentOptions, MainAgentOptions
from .realtime_agent import RealtimeAgent
from .turn import Turn
from ..usage_report import usage_sink

if TYPE_CHECKING:
    from ..sessions.session_state import SessionState
    from ..usage_report import UsageTracker
    from .openai.model_settings import ModelSettings

logger = logging.getLogger(__name__)

# Two separate, mutually exclusive alternatives to "ambient" (every turn
# judged): "toggle" (the button/`/listen`, no auto-close timer -- see
# arm_listening) and "transcribe" (the spoken wake phrase, auto-closes on
# silence -- see _handle_wake_phrase). Kept as two distinct modes, not one
# combined mode with two input methods, so each can be selected (and
# tested, and reasoned about) independently. Both share the exact same
# "only judge inside an open window" gating in _judge() -- this tuple is
# that shared check.
NON_AMBIENT_LISTENING_MODES = ("toggle", "transcribe", "wake")

# "wake_v2" is "wake" with the fixes from the 15-28 Oct exports (see
# Conversation._v2); every "wake" check below applies to it too.
ListeningMode = Literal["ambient", "toggle", "transcribe", "wake", "wake_v2"]

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

# WAKE_PHRASE_PATTERN/_matches_wake_phrase and DISMISSAL_PATTERN/
# _matches_dismissal live in voice_control_phrases.py (imported above),
# not here -- openai/main_agent.py needs them too, to exempt a real wake
# or dismissal attempt from its own low-confidence transcript filter
# before the text ever reaches this module's matching below. See that
# module for the reasoning behind both patterns' own permissiveness.

# Unlike the button/`/listen` (a pure toggle, open until pressed again --
# see arm_listening), a wake phrase only ever opens/extends a window: there
# is no spoken "stop" phrase, so it has to close itself. Restarted on
# every judged turn while open (speaking again extends it), same shape as
# the timer this mode used to always have before it became a pure toggle.
WAKE_PHRASE_SILENCE_MS = 5_000


# Lead-in words that may accompany the trigger itself ("hey mulca") but
# carry no content of their own -- stripped the same as the trigger word
# when deciding whether real content rode along with it, so a bare
# greeting-plus-trigger ("hey mulca") isn't mistaken for a real question.
_WAKE_PHRASE_FILLER = re.compile(
    r"\b(hey|hi|hej|hei|ok|okay|hello|please)\b",
    re.IGNORECASE,
)


def _wake_phrase_has_question(transcript: str) -> bool:
    """
    Whether real content rode along with the wake phrase in the same
    breath ("hey mulca, could you define the UNG diagram" -- not just
    "hey assistant?" alone). Strips every wake-phrase match and lead-in
    filler word out and checks whether anything but stray punctuation is
    left; used to decide whether to give the fixed "Listening."
    acknowledgement alone, or ask the main agent to lead with it and then
    directly answer, same reply.
    """

    # Either order: a bare "start MULCA" has no question either.
    remainder = WAKE_ANY_ORDER_PATTERN.sub("", transcript)
    remainder = _WAKE_PHRASE_FILLER.sub("", remainder)
    return len(remainder.strip(" ,.!?:;-")) > 0


# "wake" mode, voice only: the wake phrase starts an engaged session --
# judged like ambient for the length of a discussion -- instead of
# transcribe's 5 s window. These end it again (_disengage), all silently:
# the client shows the change from the ListeningStateMessage.
#
# A quiet room: nobody, MUCA included, spoke for this long.
ENGAGED_SILENCE_MS = 20_000
# Bounds what one engagement can cost -- by then the interruption
# threshold (below) is at its floor. Naming MULCA mid-session doesn't
# extend it; once it ends, the wake phrase starts a fresh one.
ENGAGED_MAX_MS = 120_000
# The group moved on: this many transcribed turns in a row not addressed
# to MULCA (see _count_addressed_turn). Found live: an engaged session kept
# judging -- and answering -- a discussion that had long stopped being
# addressed to it.
ENGAGED_MAX_UNADDRESSED_TURNS = 4
# Strategies that answer something asked of MULCA, as opposed to ones it
# fires on its own initiative (participation, focus, conflict, tone).
REQUESTED_STRATEGIES = frozenset(
    {"respond_when_addressed", "always_reply", "provide_summary", "remember"}
)

# Being talked over right away means MUCA isn't wanted; being heard out
# means it is. A talk-over after T seconds of MUCA's audible reply moves
# the score by the first band T falls under; a reply that finishes
# uninterrupted resets it to 0.
INTERRUPTION_BANDS_MS = ((1_000, 1), (3_000, 0), (5_000, -1))
INTERRUPTION_LONG_REPLY_DELTA = -2
# A backchannel ("mm", "sì") or a cough isn't a rejection: only a
# talk-over that lasted this long, or said this many words, counts.
INTERRUPTION_MIN_MS = 600
INTERRUPTION_MIN_WORDS = 2
# The score that ends the session drops the longer it runs: the start
# value, minus one per step, never below the floor. Half of it (rounded
# up) warns the intervention agent first, once.
INTERRUPTION_THRESHOLD_START = 2
INTERRUPTION_THRESHOLD_STEP_MS = 60_000
INTERRUPTION_THRESHOLD_FLOOR = 1

# "wake" and "wake_v2": saying "MULCA start" again restarts the session
# (the clock, and with it the threshold); MUCA_WAKE_RESTART_ACK=0 drops the
# one-word ack that answers it. The question after a bare wake phrase is
# judged, with a hint if it comes within the follow-up window.
FOLLOWUP_HINT_MS = int(os.environ.get("MUCA_WAKE_FOLLOWUP_WINDOW_MS", 10_000))
RESTART_ACK = os.environ.get("MUCA_WAKE_RESTART_ACK", "1") != "0"
# After MULCA steps back on its own (not dismissed), a turn starting this
# soon that names it opens the session again.
STEP_BACK_GRACE_MS = 5_000
# What "wake" tells the judge about that question, within the window
# ("wake_v2" says it in its turn facts).
FOLLOWUP_HINT_WAKE = (
    "MULCA was just woken up, evaluate whether this turn is what it was "
    "woken up for. If it was, then answer."
)

# "wake_v2" only: a longer session and more talk-overs before it ends.
ENGAGED_MAX_MS_V2 = int(os.environ.get("MUCA_WAKE_V2_MAX_MS", 180_000))
INTERRUPTION_THRESHOLD_START_V2 = int(
    os.environ.get("MUCA_WAKE_V2_INTERRUPTION_START", 3)
)
INTERRUPTION_THRESHOLD_FLOOR_V2 = int(
    os.environ.get("MUCA_WAKE_V2_INTERRUPTION_FLOOR", 2)
)
# How soon after MULCA stops speaking the person it answered counts as
# following up, in the turn facts the observers get.
FOLLOWUP_FACT_MS_V2 = 15_000

# Unity's receive loop reads 16 KB per frame; 3000 float32 samples, base64
# encoded, keep a state_update well under that.
MAX_SAMPLES_PER_FRAME = 3000

TurnJob = Callable[[], Awaitable[None]]

# What the intervention agent sees of an attachment: that it's there and how
# it starts. It only decides whether MULCA speaks; the full text would fill
# its context, which also holds the whole call.
ATTACHMENT_PREVIEW_CHARS = 200


def _with_attachments(text: str, attachments, *, full: bool) -> str:
    """
    A text message with its attachments: in full for MULCA, as a note for
    the observers -- what to do with a file is theirs to say, what's in it
    MULCA's (see the intervention agent's instructions). A cut file says so,
    for MULCA to tell the user.
    """

    parts = [text] if text else []
    for attachment in attachments:
        cut = len(attachment.text) < attachment.chars
        if not attachment.readable:
            parts.append(f"[Attached {attachment.name}: could not be read as text]")
        elif full and cut:
            parts.append(
                f"[Attached {attachment.name} -- only the first "
                f"{len(attachment.text):,} of {attachment.chars:,} characters were "
                "received; the rest is missing. When you answer about this file, "
                f"say that you only read the first {len(attachment.text):,} "
                "characters, and that anything not in them may be in the part "
                "you didn't get:]\n" + attachment.text
            )
        elif full:
            parts.append(
                f"[Attached {attachment.name}, {attachment.chars:,} characters:]\n"
                + attachment.text
            )
        else:
            start = attachment.text[:ATTACHMENT_PREVIEW_CHARS].replace("\n", " ")
            reads = f" (the first {len(attachment.text):,})" if cut else ""
            parts.append(
                f"[Attached {attachment.name}, {attachment.chars:,} characters; "
                f"MULCA reads it{reads}. First words: \"{start}...\"]"
            )
    return "\n\n".join(parts)


# A participant's microphone picking MULCA up from their speakers: suspected
# from the 15-16 Oct exports, not confirmed (the clearest case turned out to
# be someone reading a text reply aloud). A voice turn this soon after a
# reply, made mostly of that reply's word sequences, is logged as an
# echo_suspected session event -- logged only, nothing dropped, so the
# exports can show whether echo happens before anything acts on it.
ECHO_WINDOW_MS = 30_000
ECHO_MIN_WORDS = 4
ECHO_MIN_SHARE = 0.6


def _trigrams(text: str) -> set[tuple[str, ...]]:
    words = re.findall(r"\w+", text.lower())
    return {tuple(words[i : i + 3]) for i in range(len(words) - 2)}


def _is_echo(transcript: str, replies, now_ms: int) -> bool:
    """Whether `transcript` mostly repeats a reply MULCA just gave."""

    if len(re.findall(r"\w+", transcript)) < ECHO_MIN_WORDS:
        return False
    heard = _trigrams(transcript)
    for said_at, reply in replies:
        if now_ms - said_at > ECHO_WINDOW_MS:
            continue
        shared = heard & _trigrams(reply)
        if len(shared) >= ECHO_MIN_SHARE * len(heard):
            return True
    return False


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
        model_settings: ModelSettings | None = None,
        usage_tracker: UsageTracker | None = None,
        listening_mode: ListeningMode = "ambient",
    ) -> None:
        self._state = state
        self._outbound_queue = outbound_queue
        self._model_settings = model_settings
        self._usage_tracker = usage_tracker
        # "toggle"/"transcribe" only; ambient (the default) is entirely
        # unaffected. "toggle" (the button/`/listen`) has no auto-close
        # timer -- found live that one alongside manual open/close fought
        # the manual control. "transcribe" (the wake phrase) is different:
        # there's no spoken "stop", so it needs one (WAKE_PHRASE_SILENCE_MS)
        # to close itself.
        self._v2 = listening_mode == "wake_v2"
        self._listening_mode = "wake" if self._v2 else listening_mode
        self._listening_window_open = False
        self._wake_phrase_timer: asyncio.Task[None] | None = None
        # "wake" only: _listening_window_open doubles as "engaged". When
        # it started, the last time anyone spoke, and the interruption
        # score that can end it (see INTERRUPTION_BANDS_MS).
        self._engaged_since_ms = 0
        self._last_speech_ms = 0
        self._interruption_score = 0
        self._warned_about_interruptions = False
        self._unaddressed_turns = 0
        # How far into MUCA's audible reply it was talked over, until the
        # interrupting turn closes and shows whether it counts.
        self._pending_interruption_ms: int | None = None
        # Who said a bare wake phrase: their next turn is the question it
        # was said for, so it always gets a reply.
        self._followup_speaker: int | None = None
        # Whose turn is being handled, and whose MULCA last answered.
        self._turn_speaker: int | None = None
        self._last_answered_speaker: int | None = None
        self._mulca_last_answered_ms = 0
        # "wake": the reply now playing answers a turn that named MULCA.
        self._restart_when_heard_out = False
        # "wake": when MULCA last stepped back on its own, or 0.
        self._stepped_back_ms = 0
        if model_settings is not None:
            model_settings.watch(outbound_queue)
        # This session's own models, from the handshake; empty means the
        # server's.
        self._intervention_model = ""
        self._main_agent_options = main_agent_options.model_copy(
            update={"listening_mode": listening_mode},
        )
        self._agent_factory = (
            agent_factory or DefaultAgentFactory()
        )
        self._policy = policy or NoCognition()
        self._silence_timeout_ms = silence_timeout_ms

        self._conversation_id = str(state.session_id)
        self._has_conversation_id = False
        self._is_private = False
        self._linked_conversation_id: str | None = None
        # Which bot/team this session's cost should be attributed to in
        # usage_report.py; falls back to conversation_id if the client never
        # sent one (see ConnectAgentMessage.bot_name).
        self._instance_label = self._conversation_id

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
        # Whether this session has had the voice roster yet: the first one
        # may describe a call already under way, not one starting.
        self._voice_roster_seen = False

        # Deltas of the reply being streamed, until it completes.
        self._assistant_reply: list[str] = []
        # (when, text) of MULCA's latest replies, for _is_echo.
        self._recent_replies: list[tuple[int, str]] = []

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

    async def instruct_main_agent(
        self,
        directive: str,
        *,
        max_tokens: int | None = None,
    ) -> None:
        if self._main_agent is None:
            return

        logger.info(
            "%s asking the main agent for a %s reply (%d-char directive)%s",
            self._conversation_id,
            self._turn_modality or "default",
            len(directive),
            f", max_tokens={max_tokens}" if max_tokens is not None else "",
        )

        # Answer in the medium the turn arrived in: a text message gets a
        # text reply, a spoken turn a spoken one.
        await self._main_agent.trigger_response(
            instructions=directive,
            output_modality=self._turn_modality,
            max_tokens=max_tokens,
        )

    async def tell_observers(self, text: str) -> None:
        for agent in list(self._auxiliary_agents.values()):
            await agent.send_text(text)

    async def no_reply(self) -> None:
        await self._send(TurnSkippedMessage())

    async def reset_agent_contexts(self, observer_memory: str) -> None:
        """
        Between turns, so nothing is cleared mid-reply: every agent's live
        session starts over; the observers get observer_memory straight
        away, the main agent its memory with its next reply.
        """

        self._enqueue_turn(lambda: self._reset_agent_contexts(observer_memory))

    async def _reset_agent_contexts(self, observer_memory: str) -> None:
        if self._main_agent is not None:
            with suppress(Exception):
                await self._main_agent.clear_context()
        for agent in list(self._auxiliary_agents.values()):
            with suppress(Exception):
                await agent.clear_context()
                if observer_memory:
                    await agent.send_text(observer_memory, role="user")
        logger.info("%s agents' sessions renewed from memory", self._conversation_id)

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
        logger.info(
            "%s conversation closed, cost $%.4f",
            self._conversation_id,
            sum(
                agent.cost_usd
                for agent in [self._main_agent, *self._auxiliary_agents.values()]
                if agent is not None
            ),
        )

        # Already closed (memory saved) when a lost provider connection
        # ended this session; saving again now, possibly after the client's
        # next session has loaded that memory, could only overwrite it.
        if not self._policy_closed:
            await self._log_session_cut_off()
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

            # toggle/transcribe: the intervention agent never hears raw
            # audio at all, only the already-transcribed text
            # _take_voice_turn sends it -- the whole point is that it never judges
            # from an "impression" of audio it passively heard, only from a
            # literal transcript. The main agent above is unaffected: it
            # still needs audio for turn-taking/VAD -- see the "NOT
            # CURRENTLY WIRED IN" note in transcription_agent.py for why
            # that part isn't also changed yet. "wake" hears it only while
            # engaged, i.e. while it judges like ambient.
            if self._listening_mode == "ambient" or self._engaged():
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

        if self._wake_dormant():
            return

        for agent in list(self._auxiliary_agents.values()):
            if agent.listen_to_assistant:
                await agent.send_audio_chunk(chunk)

    async def route_text(self, message: TextMessage) -> None:
        agent = self._resolve(message.target_agent_guid)
        if agent is None:
            return

        await agent.send_text(message.text, role=message.role)

    async def update_session(self, message: RawProviderMessage) -> None:
        if message.target_agent_guid or self._main_agent is None:
            logger.info(
                "%s ignoring a session.update not for the main agent",
                self._conversation_id,
            )
            return

        await self._main_agent.update_session(
            message.payload.get("session") or {},
        )

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
            previous = self._voice_participants
            self._voice_participants = roster
            await self._log_voice_roster_change(previous, roster)
        else:
            self._participants = roster

    async def _log_voice_roster_change(
        self,
        previous: dict[int, str],
        roster: dict[int, str],
    ) -> None:
        names = ", ".join(sorted(roster.values()))
        if not self._voice_roster_seen:
            self._voice_roster_seen = True
            if roster:
                await self._session_event(
                    "voice_call_ongoing",
                    f"already in the call at connect: {names}",
                )
            return

        if not previous and roster:
            await self._session_event("voice_call_started", names)
            return
        if previous and not roster:
            await self._session_event(
                "voice_call_ended",
                ", ".join(sorted(previous.values())),
            )
            return

        for user_id in roster.keys() - previous.keys():
            await self._session_event("voice_joined", speaker=roster[user_id])
        for user_id in previous.keys() - roster.keys():
            await self._session_event("voice_left", speaker=previous[user_id])

    async def _session_event(
        self,
        event: str,
        detail: str = "",
        *,
        speaker: str = "",
        **data: Any,
    ) -> None:
        logger.info(
            "%s session event %s%s%s",
            self._conversation_id,
            event,
            f" ({speaker})" if speaker else "",
            f": {detail}" if detail else "",
        )
        # Analysis only: failing to log must never disturb the conversation.
        try:
            await self._policy.on_session_event(
                event,
                detail,
                speaker=speaker,
                data=data,
            )
        except Exception:
            logger.warning(
                "%s could not log session event %s",
                self._conversation_id,
                event,
                exc_info=True,
            )

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

            self._instance_label = message.bot_name or self._conversation_id
            # Every provider call this session makes from here on reports
            # its cost to this client (see usage_report.usage_sink).
            usage_sink.set((self._instance_label, self._send_usage))
            self._is_private = message.is_dm
            self._linked_conversation_id = message.linked_conversation_id
            self._intervention_model = message.intervention_model
            if message.listening_mode is not None:
                self._v2 = message.listening_mode == "wake_v2"
                self._listening_mode = "wake" if self._v2 else message.listening_mode
                self._main_agent_options = self._main_agent_options.model_copy(
                    update={"listening_mode": message.listening_mode},
                )
            if message.main_model:
                self._main_agent_options = (
                    self._main_agent_options.model_copy(
                        update={"model": message.main_model},
                    )
                )

            await self._connect_main_agent()
            return

        agent = self._resolve(message.target_agent_guid)
        if agent is None:
            return

        await agent.connect()
        self._state.connected_agents.add(
            message.target_agent_guid,
        )

    async def set_model(self, message: SetModelMessage) -> None:
        if self._model_settings is None:
            raise RuntimeError("Model switching is not enabled")

        # Ends every live session, this one included.
        self._model_settings.apply(message)

    async def set_cost_alert(self, message: SetCostAlertMessage) -> None:
        if self._usage_tracker is None:
            raise RuntimeError("Usage tracking is not enabled")

        # Unlike set_model, nothing here needs any session to restart.
        self._usage_tracker.apply(
            admin_token=message.admin_token,
            webhook_url=message.webhook_url,
            threshold_usd=message.threshold_usd,
        )

    async def arm_listening(self, message: ArmListeningMessage) -> None:
        """
        "toggle" mode only: a participant pressed the button -- a toggle,
        same button for "start talking" and "stop talking", open until
        pressed again (no auto-close timer: found live that one alongside
        manual control just fought it, closing a window mid-conversation
        well before the next press). A no-op in ambient or "transcribe"
        mode -- "transcribe" arms exclusively via the spoken wake phrase
        instead; these are two separate, mutually exclusive modes, not two
        input methods for one combined mode.

        The server is the sole source of truth for which of the two a
        given press means (the button itself carries no state -- every
        press sends the exact same message), and reports the result back
        via ListeningStateMessage so the client can show it accurately
        rather than guessing or claiming a fixed direction.
        """

        if self._listening_mode != "toggle":
            return

        self._listening_window_open = not self._listening_window_open
        logger.info(
            "%s listening window %s (press)",
            self._conversation_id,
            "opened" if self._listening_window_open else "closed",
        )
        await self._session_event(
            "listen_opened" if self._listening_window_open else "listen_closed",
            "button",
        )
        await self._send(ListeningStateMessage(open=self._listening_window_open))

    async def _handle_wake_phrase(
        self,
        speaker: int,
        *,
        has_question: bool = True,
        trigger: str = "",
    ) -> None:
        """
        "transcribe" mode only, voice only: a wake phrase (WAKE_PHRASE_
        PATTERN) opens (or extends) the listening window entirely
        hands-free, no UI or text channel involved. Unlike "toggle" mode's
        button, there's no spoken "stop" -- it closes itself after
        WAKE_PHRASE_SILENCE_MS of silence instead.

        "wake" mode instead starts (or restarts) an engaged session, which
        only _disengage ends.
        """

        if self._listening_mode == "wake":
            await self._engage(speaker, has_question=has_question, trigger=trigger)
            return

        self._listening_window_open = True
        self._cancel_wake_phrase_timer()
        task = asyncio.create_task(self._close_window_after_wake_phrase_silence())
        self._wake_phrase_timer = task
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        logger.info(
            "%s listening window opened (wake phrase, %s)",
            self._conversation_id,
            self._username(speaker),
        )
        await self._session_event(
            "listen_opened",
            trigger,
            speaker=self._username(speaker),
        )
        await self._send(ListeningStateMessage(open=True))

    def _cancel_wake_phrase_timer(self) -> None:
        if self._wake_phrase_timer is not None:
            self._wake_phrase_timer.cancel()
            self._wake_phrase_timer = None

    async def _close_window_after_wake_phrase_silence(self) -> None:
        try:
            await asyncio.sleep(WAKE_PHRASE_SILENCE_MS / 1000)
        except asyncio.CancelledError:
            return

        self._listening_window_open = False
        logger.info(
            "%s listening window closed (%dms silence after wake phrase)",
            self._conversation_id,
            WAKE_PHRASE_SILENCE_MS,
        )
        await self._session_event("listen_closed", "silence")
        await self._send(ListeningStateMessage(open=False))

    def _engaged(self) -> bool:
        return self._listening_mode == "wake" and self._listening_window_open

    def _wake_dormant(self) -> bool:
        return self._listening_mode == "wake" and not self._listening_window_open

    async def _engage(
        self,
        speaker: int,
        *,
        has_question: bool,
        trigger: str = "",
    ) -> None:
        """
        "wake" mode: start the session, or restart it if already engaged --
        saying the name again is the strongest sign MUCA is still wanted.
        """

        was_engaged = self._listening_window_open
        now = _now_ms()
        self._listening_window_open = True
        self._engaged_since_ms = now
        self._last_speech_ms = now
        self._interruption_score = 0
        self._warned_about_interruptions = False
        self._unaddressed_turns = 0
        self._pending_interruption_ms = None
        # A bare "hey MUCA": the question comes in their next turn.
        self._followup_speaker = None if has_question else speaker
        logger.info(
            "%s engaged session %s (wake phrase, %s)",
            self._conversation_id,
            "restarted" if was_engaged else "started",
            self._username(speaker),
        )
        await self._session_event(
            "wake_restarted" if was_engaged else "wake_started",
            trigger,
            speaker=self._username(speaker),
        )
        if not was_engaged:
            await self._send(ListeningStateMessage(open=True))

    async def _disengage(self, reason: str) -> None:
        # An explicit stop gets no step-back grace, and cancels one left by
        # an earlier step back on MULCA's own.
        explicit = reason in ("dismissed", "dismissed in text", "session closed")
        if explicit:
            self._stepped_back_ms = 0
        if not self._listening_window_open:
            return

        self._listening_window_open = False
        self._followup_speaker = None
        self._pending_interruption_ms = None
        if not explicit:
            self._stepped_back_ms = _now_ms()
        duration_s = (_now_ms() - self._engaged_since_ms) // 1000
        logger.info(
            "%s engaged session ended (%s) after %ds, interruption score %d",
            self._conversation_id,
            reason,
            duration_s,
            self._interruption_score,
        )
        await self._session_event(
            "wake_ended",
            reason,
            reason=reason,
            duration_s=duration_s,
            interruption_score=self._interruption_score,
        )
        self._interruption_score = 0
        self._warned_about_interruptions = False
        await self._send(ListeningStateMessage(open=False))

    async def _check_engaged_session(self, now: int) -> None:
        """Every tick: a quiet room, or the time limit, ends the session."""

        if (
            self._user_is_speaking()
            or self._user_voice_activity_detected
            or self.main_agent_state is MainAgentState.ANSWERING
        ):
            # Also keeps a reply that is playing from being cut by the cap.
            self._last_speech_ms = now
            return

        if now - self._last_speech_ms >= ENGAGED_SILENCE_MS:
            await self._disengage("silence")
        elif now - self._engaged_since_ms >= (
            ENGAGED_MAX_MS_V2 if self._v2 else ENGAGED_MAX_MS
        ):
            await self._disengage("time limit")

    def _interruption_threshold(self, now: int) -> int:
        elapsed_steps = (
            now - self._engaged_since_ms
        ) // INTERRUPTION_THRESHOLD_STEP_MS
        if self._v2:
            return max(
                INTERRUPTION_THRESHOLD_FLOOR_V2,
                INTERRUPTION_THRESHOLD_START_V2 - elapsed_steps,
            )
        return max(
            INTERRUPTION_THRESHOLD_FLOOR,
            INTERRUPTION_THRESHOLD_START - elapsed_steps,
        )

    async def _settle_interruption(
        self,
        speaker: int,
        ended_at: int,
        transcript: str,
    ) -> None:
        """
        The turn that talked over MUCA just closed: score it, unless it was
        only a backchannel or a cough.
        """

        heard_ms = self._pending_interruption_ms
        self._pending_interruption_ms = None
        if heard_ms is None or not self._engaged():
            return

        turn = next(
            (
                t
                for t in reversed(self.turns)
                if t.speaker_id == speaker and t.end_time == ended_at
            ),
            None,
        )
        lasted_ms = ended_at - turn.start_time if turn is not None else 0
        if (
            lasted_ms < INTERRUPTION_MIN_MS
            and len(transcript.split()) < INTERRUPTION_MIN_WORDS
        ):
            return

        delta = next(
            (d for limit, d in INTERRUPTION_BANDS_MS if heard_ms < limit),
            INTERRUPTION_LONG_REPLY_DELTA,
        )
        self._interruption_score = max(0, self._interruption_score + delta)

        threshold = self._interruption_threshold(_now_ms())
        logger.info(
            "%s talked over after %dms: interruption score %d/%d",
            self._conversation_id,
            heard_ms,
            self._interruption_score,
            threshold,
        )
        await self._session_event(
            "wake_talked_over",
            f"after {heard_ms}ms, score {self._interruption_score}/{threshold}",
            speaker=self._username(speaker),
            heard_ms=heard_ms,
            score=self._interruption_score,
            threshold=threshold,
        )
        if self._interruption_score >= threshold:
            await self._disengage("interruptions")
        elif (
            self._interruption_score >= -(-threshold // 2)
            and not self._warned_about_interruptions
        ):
            self._warned_about_interruptions = True
            await self._tell_observers(
                "You've been cut off repeatedly -- only intervene in a "
                "clear pause or when directly addressed."
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
        options.model = self._intervention_model
        # This link's own resolved mode (server default, or its handshake
        # override -- already applied by the time this runs), not a guess:
        # decides which strategy catalog this agent is built with (see
        # OpenAIAgentFactory.build_auxiliary).
        options.listening_mode = "wake_v2" if self._v2 else self._listening_mode

        agent = self._agent_factory.build_auxiliary(options)
        agent.on_message = self._auxiliary_message_handler(
            agent.guid,
        )
        agent.on_decision = self._handle_intervention_signal
        agent.on_connection_lost = self._handle_agent_connection_lost
        agent.on_context_full = getattr(self._policy, "on_context_full", None)
        agent.log_label = self._conversation_id
        agent.instance_label = self._instance_label

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

        # Nothing to answer. Found live (21-24 Oct): empty text turns got
        # "I can't see content in that message since it's blank."
        if not message.text.strip() and not message.attachments:
            logger.info(
                "%s ignored an empty text turn from %s",
                self._conversation_id,
                message.speaker,
            )
            return

        self._turn_modality = "text"
        self._turn_speaker = message.speaker
        logger.info(
            "%s text turn from %s (%d chars)",
            self._conversation_id,
            message.speaker,
            len(message.text),
        )

        if message.speaker_name:
            self._participants[message.speaker] = message.speaker_name

        # A typed "MULCA stop" ends the voice session too. Found live (group
        # 6, 15 Oct): "@MULCA stop" got "I'm stepping back" in the chat while
        # MULCA kept talking in the call. Named, unlike a spoken dismissal: a
        # typed "thanks" is far likelier to be meant for someone else.
        # Also during the step-back grace, which it cancels.
        if (
            (self._engaged() or self._stepped_back_ms)
            and (message.mentions_assistant or _addresses_assistant(message.text))
            and _matches_dismissal(message.text)
        ):
            await self._disengage("dismissed in text")

        label = self._username(message.speaker)
        # MULCA, the log and the memory get attachments in full; the
        # observers a note of each.
        content = _with_attachments(message.text, message.attachments, full=True)
        noted = _with_attachments(message.text, message.attachments, full=False)

        if self._main_agent is not None:
            await self._main_agent.send_text(f"{label}: {content}", role="user")

        for agent in list(self._auxiliary_agents.values()):
            if agent.listens_to_user(message.speaker):
                await agent.send_text(f"{label}: {noted}", role="user")

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
                content=content,
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
        self._turn_speaker = speaker

        # Snapshot now, before the slow transcript wait below -- not after
        # it. Found live: pressing /listen while a just-finished utterance's
        # transcript was still being collected made that utterance (spoken
        # *before* the press) count as inside the window by the time
        # _judge() finally ran for it, since that check used to happen only
        # after this wait. This keeps the window's open/closed state tied
        # to when the turn actually ended, not to whatever the server's
        # processing lag happened to be.
        in_open_window = (
            self._listening_mode in NON_AMBIENT_LISTENING_MODES
            and self._listening_window_open
        )
        is_followup = (
            self._engaged() and self._followup_speaker == speaker
        )

        transcript = await self._collect_transcript(speaker)
        logger.info(
            "%s voice turn attributed to speaker %s by the ASR (%d-char "
            "transcript)",
            self._conversation_id,
            speaker,
            len(transcript),
        )
        # debug, not info: this is diagnostic detail for investigating the
        # turn/cooldown machinery, not something worth seeing on every
        # single spoken turn by default -- set MUCA_LOG_LEVEL=DEBUG to see
        # these (and the rest of the [voice-turn]-tagged lines) again.
        #
        # Worded as the ASR's own claim, not a fact -- found live, a
        # transcript it attributed to a real speaker turned out to be
        # hallucinated (plausible-sounding "helpful assistant" boilerplate
        # text the model invents during near-silent/low-signal audio, a
        # known ASR failure mode, unrelated to any specific prompt text),
        # not anything that speaker actually said. The speaker_id here is
        # only ever "whichever speaker's audio window this was," not
        # independent confirmation of who said what was transcribed.
        logger.debug(
            "[voice-turn] %s ASR transcript (unverified) for speaker %s: %r",
            self._conversation_id,
            self._username(speaker),
            transcript,
        )
        if self._listening_mode == "wake":
            await self._settle_interruption(speaker, ended_at, transcript)
            # "wake_v2": the turn that just ended the session (talked over
            # once too often, or past the time limit while it was being
            # transcribed) is not answered. Found in the 15-28 Oct exports:
            # 43 replies came seconds after the session closed.
            if self._v2 and not self._engaged():
                in_open_window = False
                is_followup = False

        if transcript and _is_echo(transcript, self._recent_replies, _now_ms()):
            await self._session_event(
                "echo_suspected",
                transcript[:300],
                speaker=self._username(speaker),
            )

        if not transcript:
            # Nothing understood (noise, or transcription failed) --
            # explicitly told, rather than asking the observers to judge
            # blind from nothing but whatever they made of the raw audio
            # themselves. Found live: without this, ordinary room noise
            # (even someone typing) was getting judged as worth replying to.
            logger.debug(
                "No transcript for %s's turn, asking observers anyway",
                self._username(speaker),
            )
            await self._policy.on_untranscribed_turn(
                speaker,
                self._username(speaker),
                ended_at,
            )
            await self._tell_observers(
                f"{self._username(speaker)}'s turn had no clear transcript "
                "(likely noise, not speech) -- nothing intelligible was "
                "said, so there is nothing to react to."
            )
            # Only ambient judges noise. Found live (group 10, 14 Oct): while
            # MULCA was awake, each noise turn was judged with no words to
            # judge, so the newest thing in the observer's context -- MULCA's
            # own "MULCA here..." -- read as being addressed, and it answered
            # itself, again and again (16 replies in one session).
            if self._listening_mode not in NON_AMBIENT_LISTENING_MODES:
                self._ask_observers_to_consider(speaker)
            return

        # "wake_v2": wake, dismissal and hold, whichever is said last.
        command = _voice_command_v2(transcript) if self._v2 else None
        # "wake" takes "start MULCA" too.
        is_wake_phrase = (
            command == "wake"
            if self._v2
            else _matches_wake_any_order(transcript)
            if self._listening_mode == "wake"
            else self._listening_mode == "transcribe"
            and _matches_wake_phrase(transcript)
        )
        followup_hint = (
            is_followup and self._mulca_quiet_for_ms() <= FOLLOWUP_HINT_MS
        )
        engaged_turn = False
        named = False
        wake_restart = False

        # "wake": MULCA stepped back on its own a moment ago, and this turn,
        # started since, names it (and is no command): it steps back in, and
        # the turn is judged as one said to it.
        started_at = next(
            (
                t.start_time
                for t in reversed(self.turns)
                if t.speaker_id == speaker and t.end_time == ended_at
            ),
            ended_at,
        )
        if (
            self._listening_mode == "wake"
            and (
                command is None
                if self._v2
                else not (is_wake_phrase or _matches_dismissal(transcript))
            )
            and not self._engaged()
            and self._stepped_back_ms
            and 0 <= started_at - self._stepped_back_ms <= STEP_BACK_GRACE_MS
            and _addresses_assistant(transcript)
        ):
            await self._engage(speaker, has_question=True, trigger=transcript)
            in_open_window = True

        # "wake": a dismissal wins over the wake phrase -- it ends an
        # engaged session unanswered, and never starts one.
        if self._listening_mode == "wake" and (
            command == "dismiss" if self._v2 else _matches_dismissal(transcript)
        ):
            await self._disengage("dismissed")
            is_wake_phrase = False
            is_followup = False
            in_open_window = False
        elif command == "hold":
            # "wake_v2": stop talking, keep the session, don't answer this.
            if self._assistant_turn_open():
                await self._interrupt_assistant_turn()
            is_followup = False
            in_open_window = False
        elif self._listening_mode == "wake" and in_open_window:
            engaged_turn = True
            named = is_wake_phrase or is_followup or _addresses_assistant(transcript)
            if (
                is_wake_phrase
                and RESTART_ACK
                and not _wake_phrase_has_question(transcript)
            ):
                # A bare "MULCA start" restarts the session and gets a
                # one-word ack (_handle_wake_phrase, below). Found in the
                # 15-28 Oct exports: 447 replies were acks to it.
                wake_restart = True
            elif is_wake_phrase:
                # Restarted all the same; a question with it is judged as
                # below, a bare one gets nothing.
                await self._engage(
                    speaker,
                    has_question=_wake_phrase_has_question(transcript),
                    trigger=transcript,
                )
                in_open_window = _wake_phrase_has_question(transcript)
                is_followup = False
                is_wake_phrase = False
            else:
                # Already engaged, naming MULCA is just an addressed turn:
                # the judge decides (respond_when_addressed), nothing is
                # forced and nothing restarts. Found live: every passing
                # mention forced a reply and reset the time limit and the
                # interruption score, so the session never ended.
                is_wake_phrase = False

        wake_phrase_has_question = is_wake_phrase and _wake_phrase_has_question(
            transcript
        )
        judge_wake_question = (
            wake_phrase_has_question and self._listening_mode != "wake"
        )
        if is_followup:
            self._followup_speaker = None
        if is_wake_phrase:
            await self._handle_wake_phrase(
                speaker,
                has_question=wake_phrase_has_question,
                trigger=transcript,
            )

        # Found live: unlike a text message (_take_text_turn, above), a
        # voice turn's transcript was never actually forwarded to the
        # observers as explicit text -- they only ever "heard" the raw
        # audio live and saw the small trigger-variables note. Applying a
        # strict lexical rule (e.g. respond_when_addressed's "contains the
        # literal word 'you'") reliably needs the literal transcript to
        # check against, not a reconstruction from memory of raw audio --
        # confirmed live: identical content judged correctly over text but
        # not over voice. Same ordering invariant as _take_text_turn: this
        # must reach observers before _judge -> on_utterance's
        # trigger-variables note, so they see (utterance, then variables),
        # never the reverse.
        #
        # Also found live: the main agent was never included here either,
        # unlike _take_text_turn (which sends it to both) -- it only ever
        # heard the raw audio, with no durable "who said what" text record
        # the way a text turn gives it, so it had nothing to summarize
        # per-person from and no general sense of who is in the
        # conversation beyond whatever a single turn's own directive
        # happens to mention. Included now, same tagged line, same pattern.
        spoken = f"{self._username(speaker)}: {transcript}"
        if self._main_agent is not None:
            await self._main_agent.send_text(spoken, role="user")
        # "wake_v2": the observers also get what they'd otherwise have to
        # infer about this turn (see the wake_v2 intervention persona);
        # "wake", only that it may be what MULCA was woken for.
        observed = (
            f"{spoken}\n{self._turn_facts(speaker, transcript, followup_hint)}"
            if self._v2
            else f"{spoken}\n{FOLLOWUP_HINT_WAKE}"
            if followup_hint
            else spoken
        )
        for agent in list(self._auxiliary_agents.values()):
            if agent.listens_to_user(speaker):
                await agent.send_text(observed, role="user")

        await self._judge(
            Utterance(
                speaker_id=speaker,
                speaker_name=self._username(speaker),
                content=transcript,
                timestamp_ms=ended_at,
                modality="audio",
                # "wake" judges an engaged turn like ambient, the question
                # after a bare wake phrase too (with a hint, above): 140 of
                # 168 forced replies to it were wrong (15-28 Oct exports).
                always_reply=(
                    self._listening_mode != "wake" and in_open_window
                ),
                is_wake_phrase=is_wake_phrase,
                wake_phrase_has_question=wake_phrase_has_question,
                judge_wake_question=judge_wake_question,
                wake_v2=self._v2,
                wake_restart=wake_restart,
            ),
            # judge_wake_question: in_open_window was snapshotted before
            # _handle_wake_phrase (above) opened the window for this very
            # utterance -- a real question riding along with the wake
            # phrase itself must still reach the observer in "transcribe"/
            # "toggle" (see policy.py's on_utterance), not be treated as
            # "outside any window" just because the window technically
            # didn't exist yet at the top of this function. "wake" is
            # unaffected: judge_wake_question is always False there.
            in_window=in_open_window or is_followup or judge_wake_question,
        )

        if engaged_turn and self._engaged():
            # An answer to a turn that said MULCA's name, once heard out in
            # full, restarts the session (_end_assistant_turn).
            self._restart_when_heard_out = (
                _addresses_assistant(transcript)
                and self._policy.last_turn_strategy() is not None
            )
            await self._count_addressed_turn(named)

    def _mulca_quiet_for_ms(self) -> int:
        """
        How long since MULCA last answered (in text or voice) or stopped
        speaking: 0 while it speaks.
        """

        if self._assistant_turn_open():
            return 0
        return _now_ms() - self._mulca_last_answered_ms

    def _turn_facts(
        self,
        speaker: int,
        transcript: str,
        followup_hint: bool,
    ) -> str:
        """
        "wake_v2": a spoken turn's facts, for the observers. Found in the
        15-28 Oct exports: 634 of 764 wrong replies came on turns that
        neither named MULCA nor followed up on its answer.
        """

        word = _address_word(transcript)
        quiet_ms = self._mulca_quiet_for_ms()
        answered = self._last_answered_speaker
        followup = answered == speaker and quiet_ms <= FOLLOWUP_FACT_MS_V2
        facts = [
            f"speaker={self._username(speaker)}",
            f"words={len(transcript.split())}",
            f'names MULCA=yes ("{word}")' if word else "names MULCA=no",
            "follow-up of MULCA's last answer="
            + ("yes" if followup else "no")
            + (
                f" (MULCA last answered {self._username(answered)}, "
                f"{quiet_ms // 1000} s ago)"
                if answered is not None
                else " (MULCA hasn't answered anyone yet)"
            ),
            f"session open for {(_now_ms() - self._engaged_since_ms) // 1000} s"
            if self._engaged()
            else "session not open",
        ]
        if followup_hint:
            facts.append(
                "they just woke MULCA with a bare wake phrase, so this is "
                "probably what they woke it for"
            )
        return "Turn facts: " + "; ".join(facts) + "."

    async def _count_addressed_turn(self, named: bool) -> None:
        """
        "wake", after an engaged turn is decided: was it talking to MULCA?
        Its name, or the intervention agent's own judgement -- it chose to
        answer something asked of MULCA (REQUESTED_STRATEGIES), which also
        catches a follow-up without the name. Interventions on MULCA's own
        initiative don't count, so its eagerness can't keep it awake.
        """

        strategy_id = self._policy.last_turn_strategy()
        if named or strategy_id in REQUESTED_STRATEGIES:
            self._unaddressed_turns = 0
            return

        self._unaddressed_turns += 1
        if self._unaddressed_turns >= ENGAGED_MAX_UNADDRESSED_TURNS:
            await self._disengage("not addressed")

    async def _judge(
        self,
        utterance: Utterance,
        *,
        in_window: bool | None = None,
    ) -> None:
        consult_observers = await self._policy.on_utterance(utterance)

        # "toggle"/"transcribe": outside an open window, resolve exactly
        # like "nobody is listening" below -- the utterance still updates
        # wm history/the episodic log via on_intervention_signal, it just
        # never actually reaches the intervention agent to be judged. Voice
        # only: this is a voice-UX concept (the cost/accuracy problem it
        # fixes was continuous audio judgment), so a text message or DM on
        # a non-ambient link is judged normally, same as always.
        #
        # utterance.always_reply (not self._listening_window_open directly)
        # is the source of truth here -- it was snapshotted in
        # _take_voice_turn before the slow transcript-collection wait, not
        # after. Re-reading live state here would let a /listen press that
        # happens *during* that wait retroactively count an
        # already-finished utterance as inside the window, even though it
        # was said before the press -- confirmed live. "wake" passes that
        # snapshot as in_window instead: its engaged turns are inside the
        # window without always_reply.
        if in_window is None:
            in_window = utterance.always_reply
        if (
            consult_observers
            and self._listening_mode in NON_AMBIENT_LISTENING_MODES
            and utterance.modality == "audio"
            and not in_window
        ):
            await self._policy.on_intervention_signal(NO_OBSERVER, None)
            consult_observers = False

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

                if self._engaged():
                    await self._check_engaged_session(now)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Conversation clock tick failed")

    async def _send_to_listeners(self, chunk: AudioChunk) -> None:
        if self._main_agent is not None:
            await self._main_agent.send_audio_chunk(chunk)

        # A dormant "wake" observer never judges: audio would only be
        # context it pays for at its next judgement.
        if self._wake_dormant():
            return

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

        observers = self._listening_to_user(speaker)
        logger.info(
            "%s asking %d observer(s) to judge %s's turn",
            self._conversation_id,
            len(observers),
            speaker,
        )
        for agent in observers:
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

        # Heard out in full: MUCA is wanted after all.
        self._interruption_score = 0
        self._warned_about_interruptions = False
        # "wake": and if it answered someone who named MULCA, the session
        # restarts, as with "MULCA start" (the clock and the threshold).
        if self._restart_when_heard_out and self._engaged():
            self._engaged_since_ms = _now_ms()
            logger.info(
                "%s engaged session restarted (answer heard out)",
                self._conversation_id,
            )
        self._restart_when_heard_out = False

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
        self._restart_when_heard_out = False

        # Scored once the interrupting turn closes (_settle_interruption):
        # only then is it known whether it was a real talk-over.
        now = _now_ms()
        if self._engaged() and self._assistant_turn_open():
            self._pending_interruption_ms = now - self.turns[-1].start_time

        # This fires whenever the user starts talking while the assistant's
        # reply is still audible on the client, even if generation (and
        # _handle_main_agent_text's is_final text, already sent to
        # observers) completed first -- a reply is usually fully generated
        # long before it finishes playing (see stop_speaking's comment on
        # the same gap). Telling observers it "did not finish" in that case
        # is simply false, and it was observed misleading the intervention
        # agent's own judgment of whether the next message is a reply to
        # MULCA's (already-complete) immediately preceding message.
        if self._assistant_generation_done:
            await self._tell_observers(
                f"MAIN ASSISTANT ({self._main_agent.name}) stopped speaking "
                "(its reply had already finished generating; the user just "
                "started talking again before playback had fully drained)."
            )
        else:
            await self._tell_observers(
                f"MAIN ASSISTANT ({self._main_agent.name}) stopped speaking "
                "but it did not finish saying what it wanted to say because "
                "it was interrupted."
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
            if reply:
                self._last_answered_speaker = self._turn_speaker
                self._mulca_last_answered_ms = _now_ms()
                self._recent_replies = self._recent_replies[-4:] + [(_now_ms(), reply)]

            # Observers otherwise only ever hear that MULCA spoke (the
            # generic "started/stopped speaking" notices below), never what
            # it actually said -- a blind spot found while auditing history
            # symmetry between the main and intervention agents. This used to
            # be gated on modality == "text", but a voice-modality response's
            # spoken words stream in as modality == "audio" (the
            # response.output_audio_transcript.delta branch in
            # main_agent.py), so that gate silently dropped MULCA's own
            # voice replies from every observer's transcript -- the
            # intervention agent never had a literal "MULCA: ..." to judge a
            # reply against in voice mode (e.g. respond_when_addressed's
            # "reply to your own immediately preceding message" case), even
            # though it has already heard the audio itself through playback
            # frames.
            if reply and self._main_agent is not None:
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
            self._main_agent.on_context_full = getattr(
                self._policy, "on_context_full", None
            )

        self._main_agent.log_label = self._conversation_id
        self._main_agent.instance_label = self._instance_label
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

        self._mulca_last_answered_ms = timestamp
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

    async def _send_usage(self, kind: str, tokens: tuple, cost: float) -> None:
        fresh_text, cached_text, fresh_audio, cached_audio, out_text, out_audio = tokens
        await self._send(
            UsageMessage(
                agent=kind,
                fresh_text=fresh_text,
                cached_text=cached_text,
                fresh_audio=fresh_audio,
                cached_audio=cached_audio,
                out_text=out_text,
                out_audio=out_audio,
                cost_usd=cost,
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
        await self._log_session_cut_off()
        self._policy_closed = True
        with suppress(Exception):
            await self._policy.close()

        await self._send(EndSession())

    async def _log_session_cut_off(self) -> None:
        """
        Logged while the policy can still write them: a wake session or a
        call cut off by the connection, not by anyone in it. The next
        connection starts with voice_call_ongoing.
        """

        if self._engaged():
            await self._disengage("session closed")
        if self._voice_participants:
            await self._session_event(
                "voice_session_closed",
                ", ".join(sorted(self._voice_participants.values())),
            )

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
