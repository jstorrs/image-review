import os
import signal
import threading
import unittest

from image_review.signals import TERMINATION_SIGNALS, interrupt_on


class TestInterruptOn(unittest.TestCase):
    def test_signal_raises_keyboard_interrupt_and_handler_is_restored(self):
        before = signal.getsignal(signal.SIGTERM)
        with self.assertRaises(KeyboardInterrupt), interrupt_on(signal.SIGTERM):
            os.kill(os.getpid(), signal.SIGTERM)
        self.assertIs(signal.getsignal(signal.SIGTERM), before)

    def test_ignored_signal_stays_ignored(self):
        previous = signal.signal(signal.SIGTERM, signal.SIG_IGN)
        self.addCleanup(signal.signal, signal.SIGTERM, previous)
        with interrupt_on(signal.SIGTERM):
            self.assertIs(signal.getsignal(signal.SIGTERM), signal.SIG_IGN)

    def test_does_nothing_off_the_main_thread(self):
        before = signal.getsignal(signal.SIGTERM)
        seen = []

        def worker():
            with interrupt_on(signal.SIGTERM):
                seen.append(signal.getsignal(signal.SIGTERM))

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()
        self.assertEqual(seen, [before])

    def test_termination_signals_include_sigterm(self):
        self.assertIn(signal.SIGTERM, TERMINATION_SIGNALS)


if __name__ == "__main__":
    unittest.main()
