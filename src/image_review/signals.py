"""Turn termination signals into KeyboardInterrupt so `finally` cleanup runs.

Slurm stops jobs with SIGTERM (scancel, time limit) and a dropped terminal or ssh session sends SIGHUP; without
this, the work dir lock or an ssh tunnel would be left behind. Must stay importable without pygame/numpy/skimage.
"""

import signal
import threading
from collections.abc import Iterator
from contextlib import contextmanager

# SIGHUP does not exist on Windows
TERMINATION_SIGNALS = (signal.SIGTERM, *((signal.SIGHUP,) if hasattr(signal, "SIGHUP") else ()))


def _raise_interrupt(signum, frame):
    raise KeyboardInterrupt


@contextmanager
def interrupt_on(*signals: signal.Signals) -> Iterator[None]:
    """Raise KeyboardInterrupt on these signals while inside the block; restore the previous handlers after.

    A signal already ignored (e.g. under nohup) stays ignored. Handlers can only be installed from the main
    thread; elsewhere this does nothing.
    """
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = {}
    try:
        for sig in signals:
            if signal.getsignal(sig) is signal.SIG_IGN:
                continue
            previous[sig] = signal.signal(sig, _raise_interrupt)
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
