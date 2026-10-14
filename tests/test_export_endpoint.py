"""GET /export: one conversation's episodic log as CSV, admin only."""

from fastapi.testclient import TestClient

from muca_server.cognition.memory.records import EpisodicRecord
from muca_server.cognition.memory.sqlite import SqliteEpisodicStore, db_path_for
from muca_server.main import create_app


async def logged(tmp_path, conversation_id="channel:1"):
    store = SqliteEpisodicStore(db_path_for(conversation_id, base_dir=tmp_path))
    await store.log(
        EpisodicRecord(
            timestamp=1.0,
            conversation_id=conversation_id,
            intervention_path="none",
            speaker="Ada",
            content="hello",
        )
    )


async def test_the_admin_gets_the_csv(tmp_path):
    await logged(tmp_path)
    client = TestClient(create_app(db_dir=tmp_path, admin_token="secret"))

    response = client.get(
        "/export",
        params={"conversation_id": "channel:1"},
        headers={"X-Admin-Token": "secret"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "Ada,hello" in response.text


async def test_a_wrong_or_missing_token_is_refused(tmp_path):
    await logged(tmp_path)
    for token, configured in [("wrong", "secret"), ("", "secret"), ("", None)]:
        client = TestClient(create_app(db_dir=tmp_path, admin_token=configured))
        response = client.get(
            "/export",
            params={"conversation_id": "channel:1"},
            headers={"X-Admin-Token": token},
        )
        assert response.status_code == 403


async def test_an_unknown_conversation_is_404_and_paths_stay_inside(tmp_path):
    await logged(tmp_path)
    client = TestClient(create_app(db_dir=tmp_path, admin_token="secret"))

    for conversation_id in ("channel:2", "../channel_1"):
        response = client.get(
            "/export",
            params={"conversation_id": conversation_id},
            headers={"X-Admin-Token": "secret"},
        )
        assert response.status_code == 404
