"""An admin switching the realtime model, and a session choosing its own."""

import asyncio

import pytest
from fastapi.testclient import TestClient

from muca_server.agents import AuxiliaryAgentOptions, MainAgentOptions
from muca_server.agents.factory import DefaultAgentFactory
from muca_server.agents.openai import OpenAIAgentFactory
from muca_server.agents.openai.model_settings import ModelSettings
from muca_server.cognition.strategies import StrategyStore
from muca_server.main import create_app
from muca_server.protocol import EndSession, parse_incoming_message
from muca_server.sessions import DefaultSessionComponentsFactory

TOKEN = "secret"


def set_model(token=TOKEN, model="gpt-new", include_intervention=False):
    return parse_incoming_message(
        {
            "event": "set_model",
            "adminToken": token,
            "model": model,
            "includeIntervention": include_intervention,
        }
    )


def test_apply_changes_main_only_and_ends_live_sessions(tmp_path):
    settings = ModelSettings(tmp_path / "c.json", admin_token=TOKEN)
    queue = asyncio.Queue()
    settings.watch(queue)
    intervention_before = settings.intervention

    settings.apply(set_model())

    assert settings.main == "gpt-new"
    assert settings.intervention == intervention_before
    assert queue.get_nowait() == EndSession(reason="Model changed")


def test_include_intervention_changes_both_and_survives_a_restart(tmp_path):
    path = tmp_path / "c.json"
    ModelSettings(path, admin_token=TOKEN).apply(
        set_model(include_intervention=True),
    )

    reloaded = ModelSettings(path)

    assert (reloaded.main, reloaded.intervention) == ("gpt-new", "gpt-new")


@pytest.mark.parametrize("admin_token", [TOKEN, None])
def test_wrong_token_or_no_server_token_is_refused(tmp_path, admin_token):
    settings = ModelSettings(tmp_path / "c.json", admin_token=admin_token)
    before = settings.main

    with pytest.raises(PermissionError):
        settings.apply(set_model(token="wrong" if admin_token else ""))

    assert settings.main == before


def test_factory_prefers_the_options_model_over_the_settings(tmp_path):
    settings = ModelSettings(tmp_path / "c.json")
    settings.main = "server-main"
    settings.intervention = "server-intervention"
    factory = OpenAIAgentFactory(
        client=object(),
        strategies=StrategyStore({}),
        main_persona="p",
        intervention_persona="p",
        task_knowledge="t",
        models=settings,
    )

    def models(main_model="", intervention_model=""):
        main = factory.build_main(MainAgentOptions(model=main_model))
        aux = factory.build_auxiliary(
            AuxiliaryAgentOptions.for_type(
                "intervention_detection",
            ).model_copy(update={"model": intervention_model}),
        )
        return main._model, aux._model

    assert models() == ("server-main", "server-intervention")
    assert models("own-main", "own-intervention") == (
        "own-main",
        "own-intervention",
    )


def test_a_session_without_settings_answers_set_model_with_an_error():
    client = TestClient(
        create_app(components_factory=DefaultSessionComponentsFactory()),
    )

    with client.websocket_connect("/") as websocket:
        websocket.send_json({"event": "connect"})
        websocket.send_json(
            {"event": "set_model", "adminToken": "x", "model": "m"},
        )
        reply = websocket.receive_json()

    assert reply["event"] == "error"
    assert "not enabled" in reply["message"]


class RecordingFactory(DefaultAgentFactory):
    def __init__(self) -> None:
        self.models: list[str] = []

    def build_main(self, options):
        self.models.append(options.model)
        return super().build_main(options)

    def build_auxiliary(self, options):
        self.models.append(options.model)
        return super().build_auxiliary(options)


def test_models_in_the_handshake_reach_the_agents_the_session_builds():
    factory = RecordingFactory()
    client = TestClient(
        create_app(
            components_factory=DefaultSessionComponentsFactory(
                agent_factory=factory,
            ),
        ),
    )

    with client.websocket_connect("/") as websocket:
        websocket.send_json(
            {
                "event": "connect",
                "mainModel": "own-main",
                "interventionModel": "own-intervention",
            }
        )
        websocket.send_json(
            {
                "event": "create_auxiliary_agent",
                "auxiliaryAgentType": "intervention_detection",
            }
        )
        # Skip anything else the server sends first, such as a state update.
        while websocket.receive_json()["event"] != "agent_guid":
            pass

    assert factory.models == ["own-main", "own-intervention"]


def test_set_model_as_the_first_frame_needs_no_agents(tmp_path):
    """So an admin can still fix a model that can't connect at all."""

    factory = RecordingFactory()
    settings = ModelSettings(tmp_path / "c.json", admin_token=TOKEN)
    client = TestClient(
        create_app(
            components_factory=DefaultSessionComponentsFactory(
                agent_factory=factory,
                model_settings=settings,
            ),
        ),
    )

    with client.websocket_connect("/") as websocket:
        websocket.send_json(
            {"event": "set_model", "adminToken": "wrong", "model": "gpt-new"},
        )
        assert websocket.receive_json()["event"] == "error"
        assert websocket.receive()["code"] == 1008

    with client.websocket_connect("/") as websocket:
        websocket.send_json(
            {"event": "set_model", "adminToken": TOKEN, "model": "gpt-new"},
        )
        assert websocket.receive()["code"] == 1012

    assert settings.main == "gpt-new"
    assert factory.models == []
