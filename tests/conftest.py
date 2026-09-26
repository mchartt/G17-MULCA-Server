import asyncio

import pytest


@pytest.fixture(autouse=True)
async def _cancel_leftover_tasks():
    """
    Conversations run background tasks (the turn queue, the clock) until
    closed; most tests never close theirs. Cancel what is left before the
    test's event loop goes away.
    """

    yield

    current = asyncio.current_task()
    leftover = [
        task
        for task in asyncio.all_tasks()
        if task is not current and not task.done()
    ]

    for task in leftover:
        task.cancel()

    await asyncio.gather(*leftover, return_exceptions=True)
