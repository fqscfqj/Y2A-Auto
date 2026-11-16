"""
bilibili_api.utils.sync

同步执行异步函数
"""

import asyncio
from typing import Any, TypeVar, Coroutine, Union
from asyncio.futures import Future as AsyncioFuture
from concurrent.futures import Future as ConcurrentFuture

T = TypeVar("T")


def __ensure_event_loop() -> asyncio.AbstractEventLoop:
    """Return a usable loop for the current thread (Python 3.12 compatible)."""
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    if loop.is_closed():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop


def __run_coroutine(coroutine: Coroutine[Any, Any, T]) -> T:
    """Run a coroutine on a loop owned by the calling thread."""
    loop = __ensure_event_loop()
    return loop.run_until_complete(coroutine)


def __run_in_thread(coroutine: Coroutine[Any, Any, T]) -> T:
    """Run a coroutine on a dedicated thread and loop."""
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(asyncio.run, coroutine).result()


def sync(
    coroutine: Union[Coroutine[Any, Any, T], AsyncioFuture, ConcurrentFuture]
) -> T:
    """
    同步执行异步函数

    Args:
        obj (Coroutine | Future): 异步函数

    Returns:
        Any: 该异步函数的返回值
    """
    if isinstance(coroutine, ConcurrentFuture):
        return coroutine.result()

    try:
        running_loop = asyncio.get_running_loop()
    except RuntimeError:
        running_loop = None

    if isinstance(coroutine, AsyncioFuture):
        # An asyncio Future is permanently bound to its creating loop. It cannot
        # be synchronously waited from a different or currently running loop.
        future_loop = coroutine.get_loop()
        if running_loop is not None:
            if future_loop is running_loop:
                raise RuntimeError(
                    "sync() cannot wait for an asyncio.Future from its running loop"
                )
            raise RuntimeError("sync() cannot wait for a Future from another event loop")
        loop = __ensure_event_loop()
        if future_loop is not loop:
            raise RuntimeError("sync() cannot wait for a Future from another event loop")
        return loop.run_until_complete(coroutine)

    if running_loop is None:
        return __run_coroutine(coroutine)
    return __run_in_thread(coroutine)
