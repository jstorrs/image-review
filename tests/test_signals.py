import os
import signal
import threading
import unittest

from image_review.signals import TERMINATION_SIGNALS, interrupt_on, interrupt_once


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


class TestInterruptOnce(unittest.TestCase):
    def test_second_signal_is_ignored_after_the_first_raises(self):
        # SIGUSR1 rather than SIGINT, which `unittest --catch` handles itself
        raised = 0
        with interrupt_once(signal.SIGUSR1, signal.SIGTERM):
            try:
                os.kill(os.getpid(), signal.SIGTERM)
            except KeyboardInterrupt:
                raised += 1
            # cleanup in progress: neither signal does anything now
            os.kill(os.getpid(), signal.SIGUSR1)
            os.kill(os.getpid(), signal.SIGTERM)
            self.assertIs(signal.getsignal(signal.SIGUSR1), signal.SIG_IGN)
            self.assertIs(signal.getsignal(signal.SIGTERM), signal.SIG_IGN)
        self.assertEqual(raised, 1)

    def test_a_signal_handled_outside_is_left_alone_after_the_first_raises(self):
        def outer(signum, frame):
            pass

        previous = signal.signal(signal.SIGUSR1, outer)
        try:
            with self.assertRaises(KeyboardInterrupt), interrupt_once(signal.SIGUSR1, signal.SIGTERM):
                self.assertIs(signal.getsignal(signal.SIGUSR1), outer)
                os.kill(os.getpid(), signal.SIGTERM)
            self.assertIs(signal.getsignal(signal.SIGUSR1), outer)  # not switched to SIG_IGN and left there
        finally:
            signal.signal(signal.SIGUSR1, previous)

    def test_handlers_are_restored_on_exit(self):
        before_int = signal.getsignal(signal.SIGINT)
        before_term = signal.getsignal(signal.SIGTERM)
        with self.assertRaises(KeyboardInterrupt), interrupt_once(signal.SIGINT, signal.SIGTERM):
            os.kill(os.getpid(), signal.SIGTERM)
        self.assertIs(signal.getsignal(signal.SIGINT), before_int)
        self.assertIs(signal.getsignal(signal.SIGTERM), before_term)

    def test_nested_interrupt_on_leaves_the_outer_handler_in_charge(self):
        before = signal.getsignal(signal.SIGTERM)
        raised = 0
        with interrupt_once(signal.SIGTERM):
            outer = signal.getsignal(signal.SIGTERM)
            with interrupt_on(signal.SIGTERM):
                self.assertIs(signal.getsignal(signal.SIGTERM), outer)
                try:
                    os.kill(os.getpid(), signal.SIGTERM)
                except KeyboardInterrupt:
                    raised += 1
                os.kill(os.getpid(), signal.SIGTERM)  # the inner block is still open: ignored
            os.kill(os.getpid(), signal.SIGTERM)  # the inner block is closed: still ignored
            self.assertIs(signal.getsignal(signal.SIGTERM), signal.SIG_IGN)
        self.assertEqual(raised, 1)
        self.assertIs(signal.getsignal(signal.SIGTERM), before)

    def test_handlers_are_restored_when_no_signal_arrives(self):
        before = signal.getsignal(signal.SIGTERM)
        with interrupt_once(signal.SIGTERM):
            self.assertIsNot(signal.getsignal(signal.SIGTERM), before)
        self.assertIs(signal.getsignal(signal.SIGTERM), before)

    def test_ignored_signal_stays_ignored_and_is_restored_to_ignored(self):
        previous = signal.signal(signal.SIGTERM, signal.SIG_IGN)
        self.addCleanup(signal.signal, signal.SIGTERM, previous)
        with interrupt_once(signal.SIGTERM):
            self.assertIs(signal.getsignal(signal.SIGTERM), signal.SIG_IGN)
            os.kill(os.getpid(), signal.SIGTERM)
        self.assertIs(signal.getsignal(signal.SIGTERM), signal.SIG_IGN)

    def test_does_nothing_off_the_main_thread(self):
        before = signal.getsignal(signal.SIGTERM)
        seen = []

        def worker():
            with interrupt_once(signal.SIGTERM):
                seen.append(signal.getsignal(signal.SIGTERM))

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()
        self.assertEqual(seen, [before])


if __name__ == "__main__":
    unittest.main()
