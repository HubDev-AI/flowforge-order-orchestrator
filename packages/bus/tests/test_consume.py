import asyncio

import bus.consume
import pytest
from bus import consume_forever


@pytest.fixture(autouse=True)
def fast_restart(monkeypatch):
    monkeypatch.setattr(bus.consume, "RESTART_DELAY_SECONDS", 0.01)


async def run_until(done: asyncio.Event, **loop_args):
    task = asyncio.create_task(consume_forever(**loop_args))
    try:
        async with asyncio.timeout(2):
            await done.wait()
    finally:
        task.cancel()


async def test_a_failing_handler_is_logged_and_the_next_item_is_handled(caplog):
    done, handled = asyncio.Event(), []

    async def stream():
        for item in (1, 2, 3):
            yield item

    async def handle(item):
        handled.append(item)
        if item == 3:
            done.set()
        if item == 2:
            raise RuntimeError("handler boom")

    await run_until(done, open_stream=stream, handle=handle)

    assert handled[:3] == [1, 2, 3]
    assert "handler boom" in caplog.text


async def test_a_failing_stream_is_logged_and_opened_again(caplog):
    done, opened = asyncio.Event(), []

    async def stream():
        opened.append(1)
        if len(opened) == 1:
            raise ConnectionError("channel closed")
        yield "after the restart"

    async def handle(item):
        done.set()

    await run_until(done, open_stream=stream, handle=handle)

    assert len(opened) == 2
    assert "channel closed" in caplog.text


async def test_cancelling_the_loop_stops_it():
    async def stream():
        await asyncio.sleep(60)
        yield 1

    async def handle(item):
        pass

    task = asyncio.create_task(consume_forever(stream, handle))
    await asyncio.sleep(0.01)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
