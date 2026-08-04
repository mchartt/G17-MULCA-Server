import asyncio
import json

from sessions import SessionRuntime
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

app = FastAPI()

CLIENT_AUTHENTICATION_TIMEOUT = 5

@app.get("/")
async def root():
    #TODO: HTML landing page?
    return {"status": "server running"}

@app.websocket("/")
async def websocket_endpoint(ws: WebSocket):
    session: SessionRuntime | None = None

    try:
        await ws.accept()

        # Client should identify in 5 seconds
        raw_message = await asyncio.wait_for(
            ws.receive_text(),
            timeout=CLIENT_AUTHENTICATION_TIMEOUT,
        )

        message = json.loads(raw_message)

        if (message.get("event") != "connect_main_agent"):
            await ws.close(
                code = 1008,
                reason="Bad request"
            )
            return

        # Valid connection
        session = SessionRuntime(ws)
        await session.start()

        while True:
            raw_message = await ws.receive_text()
            message = json.loads(raw_message)
            await session.handle_message(message)

    except asyncio.TimeoutError:
        await ws.close(
            code=1008,
            reason="Cannot establish connection"
        )

    except WebSocketDisconnect:
        #TODO: gestire disconnessioni
        print("[WS] Client disconnected")

    finally:
        if session is not None:
            await session.close()