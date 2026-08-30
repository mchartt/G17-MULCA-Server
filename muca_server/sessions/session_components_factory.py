from .session_components import SessionComponents, SessionContext, SessionWorker
from ..audio import AudioRouter
from ..events import EventDispatcher

class DefaultSessionComponentsFactory:
    async def create(
        self,
        context: SessionContext,
    ) -> SessionComponents:
        audio_router = await self.create_audio_router(
            context,
        )
        
        dispatcher = await self.create_dispatcher(
            context=context,
            audio_router=audio_router,
        )
        
        workers = await self.create_workers(
            context=context,
            audio_router=audio_router,
            dispatcher=dispatcher,
        )
        
        return SessionComponents(
            dispatcher=dispatcher,
            workers=workers,
        )
        
    async def create_audio_router(
        self,
        context: SessionContext,
    ) -> AudioRouter:
        return AudioRouter()
    
    async def create_dispatcher(
        self,
        *,
        context: SessionContext,
        audio_router: AudioRouter,
    ) -> EventDispatcher:
        return EventDispatcher(
            audio_router=audio_router,
        )
        
    async def create_workers(
        self,
        *,
        context: SessionContext,
        audio_router: AudioRouter,
        dispatcher: EventDispatcher,
    ) -> list[SessionWorker]:
        return []