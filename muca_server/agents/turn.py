from dataclasses import dataclass

UNKNOWN_TIME = -1
UNKNOWN_ID = -1
UNKNOWN_NAME = ""


@dataclass(slots=True)
class Turn:
    """One stretch of speaking in the conversation history."""

    start_time: int = UNKNOWN_TIME
    end_time: int = UNKNOWN_TIME
    speaker_id: int = UNKNOWN_ID
    speaker_name: str = UNKNOWN_NAME

    @property
    def is_open(self) -> bool:
        """Whether this speaker is still going."""

        return self.end_time == UNKNOWN_TIME
