from protocol import AudioStreamMessage, PlaybackFrameMessage


class AudioRouter:
    """
    Nota: 
    Questa classe è un esempio di come vorrei gestire la struttura del server.
    Vorrei usare queste sottoclassi solo come un modo per mandare i messaggi ai componenti che ne se devono occupare.
    In questo caso l'audio potrebbe essere mandato direttamente al main agent o fatto prima passare per un transcriber.
    Ogni classe di questo tipo andrebbe costruita in SessionRuntimeFactory, in modo tale che eventuali componenti (es. audio queues)
    siano peculiari per ogni sessione.
    Inoltre, questa classe eredita direttamente da AudioRouterProtocol (in app/events/dispatcher.py).
    """
    
    async def publish_user_audio(
        self,
        message: AudioStreamMessage,
    ) -> None:
        """Forwards user audio to main and (if present) auxiliary agent"""
        pass

    async def publish_playback(
        self,
        message: PlaybackFrameMessage,
    ) -> None:
        """Forwards the already played audio to main and (if present) auxiliary agent"""
        pass