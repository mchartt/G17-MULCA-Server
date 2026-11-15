"""
Boots MUCA-Server for real, against real OpenAI agents.

The composition root: the one place allowed to pick a task, a persona, and
which strategies are active, mirroring MULCA's old run_discord_bot.py. Every
other module stays generic; nothing outside this file knows what a "general
moderation" task is.

Run with: python -m muca_server.run_server
"""

from __future__ import annotations

import hashlib
import logging
import os
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

import uvicorn
from dotenv import load_dotenv

from .agents import MainAgentOptions
from .agents.conversation import DEFAULT_SILENCE_TIMEOUT_MS
from .agents.openai import (
    OpenAIAgentFactory,
    OpenAIDaySummarizer,
    OpenAIEmbedder,
    OpenAIWebSearcher,
)
from .agents.openai.client_config import build_openai_client
from .agents.openai.day_summarizer import DAILY_SUMMARY_MODEL
from .agents.openai.main_agent import SUMMARY_MODEL
from .agents.openai.model_settings import ModelSettings
from .usage_report import UsageTracker
from .cognition import CoalaPolicy
from .cognition.loader import (
    WAKE_V2_CONFIG_DIR,
    load_persona,
    load_strategy_store,
    load_task,
)
from .cognition.memory.sqlite import (
    SqliteEpisodicStore,
    SqliteSnapshotStore,
    db_path_for,
)
from .cognition.coordination.wiring import build_coordinator_factory
from .cognition.coordination.settings import CoordinatorSettings
from .main import create_app
from .sessions.session_components_factory import DefaultSessionComponentsFactory

logger = logging.getLogger(__name__)

TASK_ID = "general_moderation"
# Group Coordinator task, used instead when MUCA_COORDINATOR_ENABLED is on.
COORDINATOR_TASK_ID = "group_coordinator"
# participation_equality_voice deliberately left out: voice is mention-only
# (respond_when_addressed/provide_summary/remember, all solicited) -- the
# strategy and its voice-only roster/trigger-variable plumbing stay in the
# codebase (cognition/config/strategies/participation_equality_voice.yaml),
# just not active, in case that decision changes again later.
ACTIVE_STRATEGY_IDS = [
    "participation_equality_text",
    "maintain_focus",
    "respond_when_addressed",
    "pleasant_conversation",
    "resolve_conflict",
    "provide_summary",
    "remember",
]
# The unsolicited, background-moderation strategies -- never offered in
# "toggle"/"transcribe"/"wake"'s own catalog (see non_ambient_strategy_
# store below): none of those modes run continuous judgment at all, only
# inside an explicitly-opened window, so there is no ongoing conversation
# for an unprompted strategy to proactively watch in the first place.
# (respond_when_addressed/provide_summary/remember stay in both catalogs:
# all three are solicited, firing only in direct response to being
# addressed -- exactly what a listening window exists for.)
PROACTIVE_STRATEGY_IDS = [
    "participation_equality_text",
    "maintain_focus",
    "pleasant_conversation",
    "resolve_conflict",
]
# Group Coordinator strategies (M3), added to the ambient catalog and to the
# policy's store only when MUCA_COORDINATOR_ENABLED is on. Answered with a
# card (cognition/coordination/proactive.py), never with a reply.
COORDINATOR_STRATEGY_IDS = [
    "cs5_owner_and_date",
    "cs2_delegate_simple_task",
    "cs3_close_decision",
    "cs7_blockers_dependencies",
]
ASSISTANT_NAME = "MULCA"

# "ambient": every turn is judged, as above. "toggle"/
# "transcribe": the intervention agent is only consulted inside a window
# (opened by the button/`/listen`, or a spoken wake phrase, respectively --
# see conversation.py's arm_listening/_handle_wake_phrase/_judge) -- it
# never hears raw audio at all in either, only the main agent's transcript.
# Two separate, mutually exclusive modes, not two input methods for one
# combined mode. "wake" (default): dormant like a closed "transcribe"
# window until the wake phrase, then judged like ambient until the
# session ends (silence, time limit, dismissal, interruptions --
# conversation.py's _disengage).
LISTENING_MODE_ENV_VAR = "MUCA_LISTENING_MODE"

# Anchored to this package's own location, not the current working
# directory -- a relative default here would mean restarting the server
# from a different shell/cwd silently creates a second, empty database
# instead of finding the one from before. Beside the package rather than
# inside it, so conversation data never lands among the source (or in
# site-packages). Set MUCA_DB_DIR for a real deployment.
DEFAULT_DB_DIR = Path(__file__).resolve().parent.parent / "data"


def build_app():
    load_dotenv()

    listening_mode = os.environ.get(LISTENING_MODE_ENV_VAR, "wake")
    if listening_mode not in ("ambient", "toggle", "transcribe", "wake", "wake_v2"):
        raise ValueError(
            f"{LISTENING_MODE_ENV_VAR} must be 'ambient', 'toggle', "
            f"'transcribe', 'wake' or 'wake_v2', got {listening_mode!r}"
        )

    # This is the server-wide default only -- a link can still override it
    # per-conversation via ConnectAgentMessage.listening_mode (/mulca link
    # mode:...). Logged because it's read once at startup from the
    # environment: a running process never picks up a later .env edit.
    logger.info("Default listening mode: %s", listening_mode)

    # Two separate catalogs, not one store that always includes
    # always_reply: which strategies an intervention agent's catalog
    # contains is a structured-output enum it picks strategy_id from (see
    # InterventionAgentOpenAI._tool_definition), not just prose -- an
    # ambient conversation's own agent must never have always_reply (or any
    # "toggle"/"transcribe"-only strategy) in its catalog at all, so the
    # model is structurally unable to select it, not merely asked not to by
    # a trigger_description it might misjudge. OpenAIAgentFactory picks
    # between these two per conversation, from that conversation's own
    # resolved listening_mode (server default, or its own handshake
    # override) -- never from this process's own env var alone.
    # OpenAI as before, or Azure OpenAI when AZURE_OPENAI_ENDPOINT and
    # AZURE_OPENAI_API_KEY are both set (agents/openai/client_config.py).
    client = build_openai_client(os.environ)
    coordinator_settings = CoordinatorSettings.from_env(os.environ)
    logger.info("Group Coordinator: %s", "on" if coordinator_settings.enabled else "off")
    coordinator_ids = COORDINATOR_STRATEGY_IDS if coordinator_settings.enabled else []
    strategy_store = load_strategy_store(ACTIVE_STRATEGY_IDS + coordinator_ids)
    non_ambient_strategy_store = load_strategy_store(
        [sid for sid in ACTIVE_STRATEGY_IDS if sid not in PROACTIVE_STRATEGY_IDS]
        + ["always_reply"]
    )
    # CoalaPolicy's own store is different from the two above: it is never
    # exposed to a model as a selectable catalog (that's strictly the
    # intervention agent's own, mode-filtered one -- see
    # OpenAIAgentFactory.build_auxiliary), only used internally for
    # cooldown/modality vetoes and the always_reply forced-fallback lookup
    # (CoalaPolicy.on_intervention_signal's self._strategies.get(...)
    # calls). It needs every strategy any conversation might legitimately
    # reference, in any mode, all the time -- not a per-conversation
    # subset, since CoalaPolicy is built once per conversation by
    # policy_factory below, before that conversation's own listening_mode
    # handshake override has even arrived (create_conversation runs at
    # session start; ConnectAgentMessage arrives after). Leaving a
    # PROACTIVE_STRATEGY_IDS member or always_reply out of this store for
    # some conversations would make a legitimate strategy_id the model's
    # own (correctly filtered) catalog just gave it resolve to nothing here.
    policy_strategy_store = load_strategy_store(
        ACTIVE_STRATEGY_IDS + coordinator_ids + ["always_reply"]
    )
    main_persona = load_persona("main_default")
    intervention_persona = load_persona("intervention_default")
    # "wake_v2": the ambient catalog with its own respond_when_addressed,
    # provide_summary and remember, and its own intervention persona.
    wake_v2_strategy_store = load_strategy_store(
        ACTIVE_STRATEGY_IDS,
        overrides_dir=WAKE_V2_CONFIG_DIR,
    )
    wake_v2_intervention_persona = load_persona(
        "intervention_default",
        config_dir=WAKE_V2_CONFIG_DIR,
    )
    task_id = COORDINATOR_TASK_ID if coordinator_settings.enabled else TASK_ID
    task_knowledge = load_task(task_id)
    # Which prompt a reply came from, for the logs: changes with any edit to
    # the persona or the task. A client's session.update replaces both (and
    # is logged per session).
    logger.info(
        "Main prompt version %s (persona main_default, task %s)",
        hashlib.sha256(
            "\n".join([main_persona, task_knowledge]).encode()
        ).hexdigest()[:12],
        task_id,
    )

    # One SQLite file per conversation_id, not one shared file -- e.g. each
    # of a deployment's Discord channels (channel/instance scoping) gets
    # its own database. The directory is what's fixed; the
    # filename is deterministically derived from conversation_id, so there's
    # nothing per-channel to separately configure here (that lives entirely
    # in the Discord-specific client, e.g. mulca_discord's instances.yaml --
    # this file never sees a channel id, only whatever conversation_id it's
    # handed).
    db_dir = os.environ.get("MUCA_DB_DIR", str(DEFAULT_DB_DIR))
    os.makedirs(db_dir, exist_ok=True)

    # The realtime models, which an admin can change at runtime when
    # MUCA_ADMIN_TOKEN is set. The leading underscore keeps the file apart
    # from the per-conversation databases.
    model_settings = ModelSettings(
        Path(db_dir) / "_runtime_config.json",
        admin_token=os.environ.get("MUCA_ADMIN_TOKEN"),
    )

    # This process's own running spend -- the real deployment's one and
    # only MUCA-Server (see usage_report.py's module docstring), so this is
    # already the deployment-wide total, broken down by agent type and by
    # instance (bot/team). MUCA_COST_ALERT_WEBHOOK_URL/
    # _THRESHOLD_STEP_USD are both optional starting points -- unset either
    # and usage is still tracked and persisted, just never posted anywhere.
    # An admin can also set/change both live, with no restart or Swarmpit
    # edit, via /mulca costalert (the set_cost_alert message), gated on the
    # same MUCA_ADMIN_TOKEN as /mulca model.
    threshold_step_env = os.environ.get(
        "MUCA_COST_ALERT_THRESHOLD_STEP_USD", ""
    ).strip()
    usage_tracker = UsageTracker(
        Path(db_dir) / "_usage_report.json",
        server_name=os.environ.get("MUCA_SERVER_NAME"),
        webhook_url=os.environ.get("MUCA_COST_ALERT_WEBHOOK_URL"),
        threshold_step_usd=(
            float(threshold_step_env) if threshold_step_env else None
        ),
        admin_token=os.environ.get("MUCA_ADMIN_TOKEN"),
    )

    agent_factory = OpenAIAgentFactory(
        client=client,
        strategies=strategy_store,
        non_ambient_strategies=non_ambient_strategy_store,
        wake_v2_strategies=wake_v2_strategy_store,
        main_persona=main_persona,
        intervention_persona=intervention_persona,
        wake_v2_intervention_persona=wake_v2_intervention_persona,
        task_knowledge=task_knowledge,
        models=model_settings,
        usage_tracker=usage_tracker,
    )

    # Shared across every conversation's store: one client, same as the
    # main/summarizer agents reuse, not a new one per channel.
    embedder = OpenAIEmbedder(client, usage_tracker=usage_tracker)

    def store_factory(conversation_id: str):
        path = db_path_for(conversation_id, base_dir=db_dir)
        return SqliteEpisodicStore(path, embedder=embedder), SqliteSnapshotStore(path)

    # Long-term memory (docs/MEMORY_DESIGN.md): without it, nothing is
    # summarized and the agents only ever know today. The once-a-day
    # summaries run on a stronger model, MUCA_DAILY_SUMMARY_MODEL to
    # override. MUCA_SUMMARY_MODEL overrides the everyday summary model
    # (default unchanged); on Azure it is the deployment name.
    day_summarizer = OpenAIDaySummarizer(
        client,
        daily_model=os.environ.get("MUCA_DAILY_SUMMARY_MODEL") or DAILY_SUMMARY_MODEL,
        model=os.environ.get("MUCA_SUMMARY_MODEL") or SUMMARY_MODEL,
        usage_tracker=usage_tracker,
    )
    web_searcher = OpenAIWebSearcher(client, usage_tracker=usage_tracker)

    def policy_factory(context) -> CoalaPolicy:
        return CoalaPolicy(
            strategy_store=policy_strategy_store,
            store_factory=store_factory,
            day_summarizer=day_summarizer,
            embedder=embedder,
            web_searcher=web_searcher,
            assistant_name=ASSISTANT_NAME,
        )

    components_factory = DefaultSessionComponentsFactory(
        agent_factory=agent_factory,
        policy_factory=policy_factory,
        # Without this, MainAgentOptions.name defaults to "ASSISTANT" -- the
        # same missing-name bug as CoalaPolicy's assistant_name above, just
        # in the Conversation layer: the intervention agent's own
        # turn-tracking markers ("MAIN ASSISTANT (...) started speaking")
        # would say "ASSISTANT" instead of "MULCA".
        main_agent_options=MainAgentOptions(name=ASSISTANT_NAME),
        model_settings=model_settings,
        usage_tracker=usage_tracker,
        silence_timeout_ms=int(
            os.environ.get(
                "MUCA_SILENCE_TIMEOUT_MS",
                DEFAULT_SILENCE_TIMEOUT_MS,
            )
        ),
        listening_mode=listening_mode,
        # None when the coordinator is off: sessions are built exactly as before.
        coordinator_factory=build_coordinator_factory(
            coordinator_settings,
            client=client,
            db_dir=db_dir,
            usage_tracker=usage_tracker,
        ),
    )

    return create_app(
        components_factory=components_factory,
        db_dir=db_dir,
        admin_token=os.environ.get("MUCA_ADMIN_TOKEN"),
    )


def main() -> None:
    host = os.environ.get("MUCA_SERVER_HOST", "0.0.0.0")
    port = int(os.environ.get("MUCA_SERVER_PORT", "8000"))
    # Root stays at INFO always -- a DEBUG root would also unleash
    # httpx/openai/websockets' own verbose internals, not just ours.
    # MUCA_LOG_LEVEL only ever controls the muca_server logger tree
    # specifically (every logger here is logging.getLogger(__name__),
    # under that namespace).
    #
    # Defaults to DEBUG, not INFO: a plain run with nothing special set is
    # a human at a terminal actively watching it, who wants the
    # [voice-turn]-tagged diagnostic detail. Set MUCA_LOG_LEVEL=INFO
    # explicitly for a quiet run (e.g. scripted/live-verification runs that
    # don't need per-tick detail).
    #
    # Also written to daily files, 14 days kept, under MUCA_LOG_DIR (by
    # default beside the databases, so on the deployment's data volume).
    load_dotenv()
    log_dir = Path(
        os.environ.get("MUCA_LOG_DIR")
        or Path(os.environ.get("MUCA_DB_DIR", str(DEFAULT_DB_DIR))) / "logs"
    )
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level="INFO",
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            TimedRotatingFileHandler(
                log_dir / "muca-server.log",
                when="midnight",
                backupCount=14,
                encoding="utf-8",
            ),
        ],
    )
    logging.getLogger("muca_server").setLevel(
        os.environ.get("MUCA_LOG_LEVEL", "DEBUG"),
    )
    # Its INFO line per request prints full URLs, the cost-alert webhook's
    # secret token included.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    uvicorn.run(build_app(), host=host, port=port)


if __name__ == "__main__":
    main()
