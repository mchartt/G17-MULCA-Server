import hmac
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Response, WebSocket
from starlette.concurrency import run_in_threadpool

from .sessions import SessionComponentsFactory, SessionRuntimeFactory


def create_app(
    components_factory: SessionComponentsFactory | None = None,
    *,
    db_dir: str | Path | None = None,
    admin_token: str | None = None,
) -> FastAPI:
    """
    Build the server.

    Passing a components factory is how a deployment chooses its agents and
    cognition, and how a test runs the real server against agents that reach
    no provider.
    """

    app = FastAPI()
    runtime_factory = SessionRuntimeFactory(
        components_factory=components_factory,
    )

    @app.get("/")
    async def root():
        return {"status": "server running"}

    @app.get("/export")
    async def export(
        conversation_id: str,
        since: float | None = None,
        x_admin_token: str = Header(default=""),
    ) -> Response:
        """
        One conversation's episodic log as CSV (/mulca export). Gated on
        MUCA_ADMIN_TOKEN like /mulca model; off entirely without one.
        """

        # Here, not at the top: sqlite needs the optional "storage" extra
        # (aiosqlite), and the Discord bot imports muca_server without it.
        from .cognition.memory.sqlite import db_path_for, export_episodic_csv

        if not admin_token or not hmac.compare_digest(
            x_admin_token.encode(),
            admin_token.encode(),
        ):
            raise HTTPException(status_code=403, detail="bad admin token")
        if db_dir is None:
            raise HTTPException(status_code=404, detail="no database directory")

        # db_path_for reduces the id to a safe file name: no path tricks.
        path = db_path_for(conversation_id, base_dir=db_dir)
        if not path.is_file():
            raise HTTPException(status_code=404, detail="no log for that conversation")

        body = await run_in_threadpool(export_episodic_csv, path, since=since)
        return Response(content=body, media_type="text/csv; charset=utf-8")

    @app.delete("/conversation")
    async def delete_conversation(
        conversation_id: str,
        x_admin_token: str = Header(default=""),
    ) -> dict:
        """
        Group Coordinator (project rule 10, "delete all the group's data on
        confirmation"): remove one conversation's SQLite file, i.e. its
        memory, episodic log and project state. Gated on MUCA_ADMIN_TOKEN
        like /export; off entirely without one. The client closes the
        group's session first, so nothing writes the file again.
        """

        from .cognition.memory.sqlite import db_path_for

        if not admin_token or not hmac.compare_digest(
            x_admin_token.encode(),
            admin_token.encode(),
        ):
            raise HTTPException(status_code=403, detail="bad admin token")
        if db_dir is None:
            raise HTTPException(status_code=404, detail="no database directory")

        path = db_path_for(conversation_id, base_dir=db_dir)
        removed = []
        # SQLite may leave its journal files next to the database.
        for candidate in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm"), path.with_name(path.name + "-journal")):
            if candidate.is_file():
                await run_in_threadpool(candidate.unlink)
                removed.append(candidate.name)
        # A deleted web space takes its links with it, and a deleted channel
        # its own link (coordination/links.py): no stale "this group" left.
        from .cognition.coordination.links import GroupLinks

        links = GroupLinks(db_dir)
        await run_in_threadpool(links.forget_home, conversation_id)
        await run_in_threadpool(links.unlink, conversation_id)
        return {"deleted": bool(removed)}

    @app.websocket("/")
    async def websocket_endpoint(ws: WebSocket) -> None:
        runtime = await runtime_factory.create(websocket=ws)

        await runtime.run()

    return app


app = create_app()
