from fastapi import FastAPI, WebSocket

from .sessions import SessionComponentsFactory, SessionRuntimeFactory


def create_app(
    components_factory: SessionComponentsFactory | None = None,
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

    @app.websocket("/")
    async def websocket_endpoint(ws: WebSocket) -> None:
        runtime = await runtime_factory.create(websocket=ws)

        await runtime.run()

    return app


app = create_app()
