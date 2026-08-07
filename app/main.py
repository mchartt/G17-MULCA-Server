from sessions import SessionRuntimeFactory
from fastapi import FastAPI, WebSocket

app = FastAPI()

CLIENT_AUTHENTICATION_TIMEOUT = 5

@app.get("/")
async def root():
    #TODO: HTML landing page?
    return {"status": "server running"}

@app.websocket("/")
async def websocket_endpoint(ws: WebSocket) -> None:
    runtime = await SessionRuntimeFactory().create(websocket=ws)
    
    await runtime.run()