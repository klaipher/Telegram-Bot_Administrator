"""
https://gist.github.com/jomido/4051adfa6f08b34f070f0854f7ae2108
"""

import asyncio


def maybeAsync(callable, *args):
    """
    Turn a callable into a coroutine if it isn't
    """

    if asyncio.iscoroutine(callable):
        return callable

    return asyncio.coroutine(callable)(*args)


def fire(callable, *args, **kwargs):
    """
    Fire a callable as a coroutine, and return it's future. The cool thing
    about this function is that (via maybeAsync) it lets you treat synchronous
    and asynchronous callables the same, which simplifies code.
    """

    return asyncio.ensure_future(maybeAsync(callable, *args))


async def _call_later(delay, callable, *args, **kwargs):
    """
    The bus stop, where we wait.
    """

    await asyncio.sleep(delay)
    fire(callable, *args, **kwargs)


def call_later(delay, callable, *args, **kwargs):
    """
    After :delay seconds, call :callable with :args and :kwargs; :callable can
    be a synchronous or asynchronous callable (a coroutine). Note that _this_
    function is synchronous - mission accomplished - it can be used from within
    any synchronous or asynchronous callable.
    """

    fire(_call_later, delay, callable, *args, **kwargs)
