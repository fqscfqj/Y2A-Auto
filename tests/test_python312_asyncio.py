import asyncio

import pytest

from modules.bili_sdk.utils.network import _get_or_create_event_loop
from modules.bili_sdk.utils.sync import sync


def _current_loop():
    try:
        return asyncio.get_event_loop()
    except RuntimeError:
        return None


def test_sync_creates_loop_when_thread_has_none():
    previous = _current_loop()
    asyncio.set_event_loop(None)
    try:
        assert sync(asyncio.sleep(0, result="ok")) == "ok"
        assert asyncio.get_event_loop().is_running() is False
    finally:
        current = _current_loop()
        if current is not None and current is not previous:
            current.close()
        asyncio.set_event_loop(previous)


def test_network_helper_creates_loop_when_thread_has_none():
    previous = _current_loop()
    asyncio.set_event_loop(None)
    try:
        loop = _get_or_create_event_loop()
        assert not loop.is_closed()
        assert asyncio.get_event_loop() is loop
    finally:
        current = _current_loop()
        if current is not None and current is not previous:
            current.close()
        asyncio.set_event_loop(previous)


def test_sync_runs_coroutine_from_running_loop():
    async def check():
        assert sync(asyncio.sleep(0, result=42)) == 42

    asyncio.run(check())


def test_sync_rejects_future_bound_to_running_loop():
    async def check():
        future = asyncio.get_running_loop().create_future()
        future.set_result(42)
        with pytest.raises(RuntimeError, match="cannot wait for an asyncio.Future"):
            sync(future)

    asyncio.run(check())
