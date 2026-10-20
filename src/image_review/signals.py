"""Turn termination signals into KeyboardInterrupt so `finally` cleanup runs.

Slurm stops jobs with SIGTERM (scancel, time limit) and a dropped terminal or ssh session sends SIGHUP; without
this, the work dir lock or an ssh tunnel would be left behind. Must stay importable without pygame/numpy/skimage.
"""

import signal
import threading
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from types import FrameType

# SIGHUP does not exist on Windows
HANGUP_SIGNALS = (signal.SIGHUP,) if hasattr(signal, "SIGHUP") else ()
TERMINATION_SIGNALS = (signal.SIGTERM, *HANGUP_SIGNALS)


def _raise_interrupt(signum, frame):
    raise KeyboardInterrupt


@contextmanager
def _installed(handler: Callable[[int, FrameType | None], None], signals: tuple[signal.Signals, ...]) -> Iterator[None]:
    """Install `handler` for these signals while inside the block; restore the previous handlers after.

    A signal already ignored stays ignored. So does one that a caller already handles (a Python handler other than
    `signal.default_int_handler`, such as an enclosing `interrupt_once`): that outer handler stays in charge, and
    this block neither re-arms it nor restores over it. Handlers can only be installed from the main thread;
    elsewhere this does nothing.
    """
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = {}
    try:
        for sig in signals:
            current = signal.getsignal(sig)
            if current is signal.SIG_IGN or (callable(current) and current is not signal.default_int_handler):
                continue
            previous[sig] = signal.signal(sig, handler)
        yield
    finally:
        for sig, old in previous.items():
            signal.signal(sig, old)


def interrupt_on(*signals: signal.Signals) -> AbstractContextManager[None]:
    """Raise KeyboardInterrupt on these signals while inside the block; restore the previous handlers after.

    A signal already ignored (e.g. under nohup) stays ignored, and one an enclosing block already handles is left to
    that block. Handlers can only be installed from the main thread; elsewhere this does nothing.
    """
    return _installed(_raise_interrupt, signals)


def interrupt_once(*signals: signal.Signals) -> AbstractContextManager[None]:
    """Like `interrupt_on`, but only the first of these signals raises; the rest are ignored until the block exits.

    The handler sets all of them to SIG_IGN before it raises, so a second Ctrl-C or SIGTERM cannot interrupt the
    cleanup that the first one started (a lock release, a wait for a mark's fsync). Leaving the block restores the
    previous handlers, `signal.default_int_handler` for SIGINT included.
    """

    def raise_once(signum, frame):
        for sig in signals:
            if signal.getsignal(sig) is raise_once:  # only those it installed: the exit restores no others
                signal.signal(sig, signal.SIG_IGN)
        raise KeyboardInterrupt

    return _installed(raise_once, signals)
