import csv
import dataclasses
import io
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

import pygame as pg
from click.testing import CliRunner
from fixtures import ROWS, make_work_dir, start_server

from image_review.cli import cli
from image_review.connection import RemoteTarget
from image_review.controller import ReviewSession, UIState
from image_review.remote import (
    ApiMismatch,
    FingerprintMismatch,
    PinnedHTTPSConnection,
    RemoteError,
    RemoteStore,
    parse_manifest,
    parse_pass,
    parse_skipped,
    parse_statuses,
    parse_version,
)
from image_review.server import Reply, ReviewHandler
from image_review.store import LocalStore, ManifestRow, SkippedCounts, StoreUnavailable

KEYS = [key for _, key, _ in ROWS]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class RemoteTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.work_dir = Path(tmp.name)
        make_work_dir(self.work_dir)
        self.server, self.target, stop = start_server(self.work_dir)
        self.stopped = False
        self.addCleanup(self.stop_server)
        self._stop = stop
        self.store = RemoteStore(self.target)
        self.addCleanup(self.store.close)

    def stop_server(self):
        if not self.stopped:
            self.stopped = True
            self._stop()

    def local_copy(self) -> LocalStore:
        return LocalStore(self.work_dir, read_only=True)  # the server holds the lock


class TestRoundTrips(RemoteTestCase):
    def test_reads_equal_local(self):
        local = self.local_copy()
        self.assertEqual(self.store.manifest(), local.manifest())
        self.assertEqual(self.store.current_pass(), local.current_pass())
        self.assertEqual(self.store.statuses(1), local.statuses(1))
        for key in KEYS:
            self.assertEqual(self.store.image_bytes(key), local.image_bytes(key))

    def test_image_bytes_many(self):
        local = self.local_copy()
        self.assertEqual(self.store.image_bytes_many(KEYS), local.image_bytes_many(KEYS))
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            found = self.store.image_bytes_many([KEYS[0], "batch_001/nope.jpg"])
        self.assertEqual(set(found), {KEYS[0]})
        self.assertIn("batch_001/nope.jpg", stderr.getvalue())

    def test_image_bytes_unknown_key(self):
        for key in ("batch_001/nope.jpg", "../manifest.tsv", "a b&c=d.jpg"):
            with self.subTest(key=key), self.assertRaises(KeyError):
                self.store.image_bytes(key)

    def test_mark_equals_local(self):
        changed = self.store.mark([KEYS[0], KEYS[1]], "batch_001", "CLEAN", 1)
        self.assertEqual(changed, {KEYS[0]: "CLEAN", KEYS[1]: "CLEAN"})
        self.store.mark([KEYS[2]], "batch_002", "DIRTY", 1)
        local = self.local_copy()
        self.assertEqual(self.store.statuses(1), local.statuses(1))
        self.assertEqual(local.statuses(1)[KEYS[2]], "DIRTY")
        self.assertEqual(self.store.statuses(2)[KEYS[2]], "FLAGGED")
        self.assertEqual(self.store.statuses(2), local.statuses(2))

    def test_bad_mark_is_remote_error(self):
        with self.assertRaises(RemoteError):
            self.store.mark(["nope"], "batch_001", "CLEAN", 1)

    def test_wrong_token_is_remote_error_without_token(self):
        bad = RemoteStore(dataclasses.replace(self.target, token="wrong_token_value"))
        self.addCleanup(bad.close)
        with self.assertRaises(RemoteError) as ctx:
            bad.manifest()
        self.assertNotIn("wrong_token_value", str(ctx.exception))
        self.assertIn("401", str(ctx.exception))

    def test_context_manager_closes(self):
        with RemoteStore(self.target) as store:
            store.current_pass()
        self.assertEqual(store._connections, [])


class TestPinning(RemoteTestCase):
    def test_mismatch_aborts_before_credentials(self):
        wrong = dataclasses.replace(self.target, fingerprint="0" * 64)
        store = RemoteStore(wrong)
        self.addCleanup(store.close)
        with (
            mock.patch.object(ReviewHandler, "_authorized", autospec=True, side_effect=ReviewHandler._authorized) as auth,
            redirect_stderr(io.StringIO()) as log,
        ):
            with self.assertRaises(FingerprintMismatch):
                store.manifest()
            with self.assertRaises(FingerprintMismatch):
                store.image_bytes_many(KEYS)
            time.sleep(0.2)  # let the handler threads notice the closed sockets
        auth.assert_not_called()
        self.assertNotIn("GET", log.getvalue())

    def test_mismatch_is_a_remote_error(self):
        self.assertTrue(issubclass(FingerprintMismatch, RemoteError))
        self.assertTrue(issubclass(RemoteError, StoreUnavailable))

    def test_connection_pins_on_connect(self):
        conn = PinnedHTTPSConnection("127.0.0.1", self.target.port, "f" * 64)
        with self.assertRaises(FingerprintMismatch):
            conn.connect()
        self.assertIsNone(conn.sock)
        ok = PinnedHTTPSConnection("127.0.0.1", self.target.port, self.target.fingerprint)
        ok.connect()
        ok.close()


class TestReconnect(RemoteTestCase):
    def test_retries_once_after_server_closes_idle_connection(self):
        with mock.patch.object(ReviewHandler, "timeout", 0.3):
            self.assertEqual(self.store.current_pass(), 1)
            time.sleep(0.8)  # server drops the idle connection
            connects = []
            real_connect = PinnedHTTPSConnection.connect

            def counting_connect(conn):
                connects.append(1)
                return real_connect(conn)

            with mock.patch.object(PinnedHTTPSConnection, "connect", counting_connect):
                self.assertEqual(self.store.current_pass(), 1)
                self.assertEqual(self.store.mark([KEYS[0]], "batch_001", "CLEAN", 1)[KEYS[0]], "CLEAN")
            self.assertEqual(len(connects), 1)

    def test_second_failure_is_not_retried_again(self):
        self.store.current_pass()
        with (
            mock.patch.object(PinnedHTTPSConnection, "request", side_effect=BrokenPipeError) as request,
            self.assertRaises(RemoteError),
        ):
            self.store.current_pass()
        self.assertEqual(request.call_count, 2)


class TestRepin(unittest.TestCase):
    def test_pin_is_rechecked_on_automatic_reconnect(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(ReviewHandler, "timeout", 0.3):
            work_dir = Path(tmp)
            make_work_dir(work_dir)
            _, target, stop_a = start_server(work_dir)
            store = RemoteStore(target)
            self.addCleanup(store.close)
            try:
                store.current_pass()  # main-thread connection
                store.image_bytes_many(KEYS)  # pool-thread connections
            finally:
                stop_a()
            _, _, stop_b = start_server(work_dir, port=target.port)  # same address, new certificate
            self.addCleanup(stop_b)
            time.sleep(0.8)  # server A's handlers drop the idle connections
            with (
                mock.patch.object(ReviewHandler, "_authorized", autospec=True, side_effect=ReviewHandler._authorized) as auth,
                redirect_stderr(io.StringIO()),
            ):
                with self.assertRaises(FingerprintMismatch):
                    store.current_pass()
                with self.assertRaises(FingerprintMismatch):
                    store.image_bytes_many(KEYS)
                time.sleep(0.2)
            auth.assert_not_called()


class TestServerDown(unittest.TestCase):
    def setUp(self):
        self.target = RemoteTarget(host="127.0.0.1", port=free_port(), token="t" * 10, fingerprint="a" * 64)
        self.store = RemoteStore(self.target)
        self.addCleanup(self.store.close)

    def test_unreachable_is_remote_error_not_key_error(self):
        with self.assertRaises(RemoteError) as ctx:
            self.store.image_bytes("batch_001/a.jpg")
        self.assertNotIn(self.target.token, str(ctx.exception))
        with self.assertRaises(StoreUnavailable):
            self.store.manifest()

    def test_many_propagates(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(RemoteError):
            self.store.image_bytes_many(KEYS)


class TestParsing(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(parse_manifest(b'[{"key": "k", "batch": "b"}]'), [ManifestRow("k", "b")])
        self.assertEqual(parse_statuses(b'{"k": "CLEAN"}'), {"k": "CLEAN"})
        self.assertEqual(parse_statuses(b'{"k": "FLAGGED", "j": "UNREVIEWED"}'), {"k": "FLAGGED", "j": "UNREVIEWED"})
        self.assertEqual(parse_pass(b'{"pass": 3}'), 3)
        self.assertEqual(parse_version(b'{"api": 1, "version": "x"}'), 1)

    def test_malformed(self):
        for parse, bad in [
            (parse_manifest, b"not json"),
            (parse_manifest, b"{}"),
            (parse_manifest, b'[{"key": "k"}]'),
            (parse_manifest, b'[{"key": 1, "batch": "b"}]'),
            (parse_manifest, b"[1]"),
            (parse_statuses, b"[]"),
            (parse_statuses, b'{"k": "MAYBE"}'),
            (parse_statuses, b'{"k": 1}'),
            (parse_pass, b"{}"),
            (parse_pass, b'{"pass": "1"}'),
            (parse_pass, b'{"pass": true}'),
            (parse_pass, b'{"pass": 0}'),
            (parse_pass, b"\xff"),
            (parse_version, b"[]"),
            (parse_version, b'{"version": "x"}'),
            (parse_version, b'{"api": "1"}'),
            (parse_version, b'{"api": true}'),
            (parse_version, b'{"api": 0}'),
        ]:
            with self.subTest(parse=parse.__name__, bad=bad), self.assertRaises(RemoteError):
                parse(bad)


SKIPPED_TSV = "image_id\tkind\treason\n/src/p/x.dcm\tfailed\tbad\n/src/p/y.dcm\tfailed\tbad\n/src/p/z.txt\tignored\tnot an image\n"


class TestSkipped(RemoteTestCase):
    def test_absent_is_none(self):
        self.assertIsNone(self.store.skipped())

    def test_counts_equal_local(self):
        (self.work_dir / "skipped.tsv").write_text(SKIPPED_TSV)
        self.assertEqual(self.store.skipped(), SkippedCounts(failed=2, ignored=1))
        self.assertEqual(self.store.skipped(), self.local_copy().skipped())

    def test_parse_skipped(self):
        self.assertIsNone(parse_skipped(b"null"))
        self.assertEqual(parse_skipped(b'{"failed": 0, "ignored": 4}'), SkippedCounts(0, 4))
        for bad in (b"[]", b"{}", b'{"failed": 1}', b'{"failed": 1, "ignored": 2, "x": 3}', b'{"failed": "1", "ignored": 0}', b'{"failed": true, "ignored": 0}', b'{"failed": -1, "ignored": 0}', b"nope"):
            with self.subTest(bad=bad), self.assertRaises(RemoteError):
                parse_skipped(bad)


class TestCli(RemoteTestCase):
    def invoke(self, *args, **kwargs):
        kwargs.setdefault("env", {"IMAGE_REVIEW_REMOTE": None, "IMAGE_REVIEW_VIA": None})  # ignore the developer's environment
        return CliRunner().invoke(cli, list(args), **kwargs)

    def test_status_identical_to_local(self):
        self.store.mark([KEYS[0]], "batch_001", "CLEAN", 1)
        self.store.mark([KEYS[2]], "batch_002", "DIRTY", 1)
        remote = self.invoke("status", "--remote", self.target.to_uri())
        local = self.invoke("status", "--work-dir", str(self.work_dir))
        self.assertEqual(remote.exit_code, 0, remote.output)
        self.assertEqual(local.exit_code, 0, local.output)
        self.assertEqual(remote.stdout, local.stdout)

    def test_status_flagged_identical_to_local(self):
        self.store.mark([KEYS[0]], "batch_001", "DIRTY", 1)
        self.store.mark([KEYS[1]], "batch_001", "CLEAN", 1)
        self.store.mark([KEYS[2], KEYS[3]], "batch_002", "CLEAN", 1)
        remote = self.invoke("status", "--remote", self.target.to_uri())
        local = self.invoke("status", "--work-dir", str(self.work_dir))
        self.assertEqual(remote.exit_code, 0, remote.output)
        self.assertIn("  FLAGGED:         1\n", remote.stdout)
        self.assertIn("Current pass: 2\n", remote.stdout)
        self.assertEqual(remote.stdout, local.stdout)

    def test_status_skipped_line_identical_to_local(self):
        (self.work_dir / "skipped.tsv").write_text(SKIPPED_TSV)
        remote = self.invoke("status", "--remote", self.target.to_uri())
        local = self.invoke("status", "--work-dir", str(self.work_dir))
        self.assertEqual(remote.exit_code, 0, remote.output)
        self.assertIn("Skipped during preprocess: 2 failed, 1 ignored (see skipped.tsv in the work dir)", remote.stdout)
        self.assertEqual(remote.stdout, local.stdout)

    def test_status_silent_without_skips(self):
        (self.work_dir / "skipped.tsv").write_text("image_id\tkind\treason\n")
        for args in (("--remote", self.target.to_uri()), ("--work-dir", str(self.work_dir))):
            with self.subTest(args=args[0]):
                self.assertNotIn("Skipped", self.invoke("status", *args).stdout)
        (self.work_dir / "skipped.tsv").unlink()
        self.assertNotIn("Skipped", self.invoke("status", "--work-dir", str(self.work_dir)).stdout)

    def test_status_malformed_skipped_is_clean_error(self):
        (self.work_dir / "skipped.tsv").write_text("wrong\n")
        result = self.invoke("status", "--work-dir", str(self.work_dir))
        self.assertEqual(result.exit_code, 1)
        self.assertIn("skipped.tsv:1", result.output)
        self.assertNotIn("Traceback", result.output)

    def test_api_mismatch_message(self):
        with mock.patch("image_review.remote.API_VERSION", 4):
            result = self.invoke("status", "--remote", self.target.to_uri())
        self.assertEqual(result.exit_code, 1)
        self.assertIn("server speaks API v3, this client v4; install the same image-review version on both machines", result.output)
        self.assertNotIn(self.target.token, result.output)

    def test_server_without_version_endpoint(self):
        original = ReviewHandler._route

        def route(handler, method):
            if handler.path == "/version":
                return Reply(404, close=True)
            return original(handler, method)

        with mock.patch.object(ReviewHandler, "_route", route):
            result = self.invoke("status", "--remote", self.target.to_uri())
        self.assertEqual(result.exit_code, 1)
        self.assertIn("server is too old to report its API version; install the same image-review version on both machines", result.output)

    def test_check_api(self):
        self.store.check_api()
        with mock.patch("image_review.remote.API_VERSION", 4), self.assertRaises(ApiMismatch):
            self.store.check_api()

    def test_status_envvar(self):
        result = self.invoke("status", env={"IMAGE_REVIEW_REMOTE": self.target.to_uri(), "IMAGE_REVIEW_VIA": None})
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Overall: 4 images", result.output)

    def test_remote_and_work_dir_exclusive(self):
        result = self.invoke("status", "--remote", self.target.to_uri(), "--work-dir", str(self.work_dir))
        self.assertEqual(result.exit_code, 2)
        self.assertIn("mutually exclusive", result.output)
        self.assertNotIn(self.target.token, result.output)

    def test_envvar_with_explicit_work_dir_says_so(self):
        result = self.invoke("status", "--work-dir", str(self.work_dir), env={"IMAGE_REVIEW_REMOTE": self.target.to_uri(), "IMAGE_REVIEW_VIA": None})
        self.assertEqual(result.exit_code, 2)
        self.assertIn("IMAGE_REVIEW_REMOTE is set; unset it to use --work-dir", result.output)
        self.assertNotIn(self.target.token, result.output)

    def test_wrong_token_message(self):
        uri = dataclasses.replace(self.target, token="wrong_token_value").to_uri()
        result = self.invoke("status", "--remote", uri)
        self.assertEqual(result.exit_code, 1)
        self.assertIn("rejected the access token", result.output)
        self.assertNotIn("Cannot reach", result.output)
        self.assertNotIn("wrong_token_value", result.output)

    def test_bad_uri_does_not_echo_token(self):
        result = self.invoke("status", "--remote", "ir://127.0.0.1:1/?token=SECRETTOKEN&fp=nope")
        self.assertEqual(result.exit_code, 1)
        self.assertIn("Invalid --remote", result.output)
        self.assertNotIn("SECRETTOKEN", result.output)

    def test_fingerprint_mismatch_message(self):
        uri = dataclasses.replace(self.target, fingerprint="0" * 64).to_uri()
        result = self.invoke("status", "--remote", uri)
        self.assertEqual(result.exit_code, 1)
        self.assertIn("does NOT match", result.output)
        self.assertIn("before any credentials were sent", result.output)
        self.assertNotIn(self.target.token, result.output)

    def test_unreachable_message(self):
        target = dataclasses.replace(self.target, port=free_port())
        result = self.invoke("status", "--remote", target.to_uri())
        self.assertEqual(result.exit_code, 1)
        self.assertIn(f"Cannot reach server at 127.0.0.1:{target.port}", result.output)
        self.assertNotIn(target.token, result.output)

    def test_local_default_dir_missing(self):
        with tempfile.TemporaryDirectory() as empty:
            previous = os.getcwd()
            os.chdir(empty)
            try:
                result = self.invoke("status")
            finally:
                os.chdir(previous)
        self.assertEqual(result.exit_code, 2)
        self.assertIn("Invalid value for '--work-dir': Path './review_work' does not exist.", result.output)


class TestSession(RemoteTestCase):
    def setUp(self):
        super().setUp()
        pg.init()
        self.addCleanup(pg.quit)
        patcher = mock.patch.object(pg.display, "toggle_fullscreen", lambda: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def reviewed_ids(self) -> set[str]:
        with open(self.work_dir / "review.tsv", newline="") as f:
            return {r["image_id"] for r in csv.DictReader(f, delimiter="\t")}

    def test_grid_mode_marks_on_server(self):
        s = ReviewSession(self.store, mode="grid")
        s._cursor = 0
        keys = s._items[0]["keys"]
        self.assertTrue(keys)
        s._mark("CLEAN")
        self.assertEqual({s._statuses[k] for k in keys}, {"CLEAN"})
        self.assertEqual(len(self.reviewed_ids()), len(keys))
        self.assertEqual(self.local_copy().statuses(1), s.store.statuses(1))

    def test_single_mode_marks_on_server(self):
        s = ReviewSession(self.store, mode="single")
        s._cursor = 0
        s._show_current()
        key = s._items[0].key
        s._mark("DIRTY")
        self.assertEqual(s._statuses[key], "DIRTY")
        self.assertEqual(self.local_copy().statuses(1)[key], "DIRTY")

    def assert_no_repaint(self, s: ReviewSession):
        """The outage message stays: the loop's next refresh does not paint over it."""
        with mock.patch.object(s._viewer, "refresh") as refresh:
            s.refresh_if_needed()
        refresh.assert_not_called()

    def lose_server(self):
        self.stop_server()
        self.store._local.conn.close()  # the handler thread would otherwise keep serving it

    def test_server_lost_during_mark(self):
        s = ReviewSession(self.store, mode="single")
        s._cursor = 0
        before = dict(s._statuses)
        todo = s._todo_count
        self.lose_server()
        s._mark("CLEAN")
        self.assertEqual(s._ui_state, UIState.END_MESSAGE)
        self.assertEqual(s._statuses, before)
        self.assertEqual(s._todo_count, todo)
        self.assertFalse((self.work_dir / "review.tsv").exists() and self.reviewed_ids())

    def test_server_lost_during_load(self):
        s = ReviewSession(self.store, mode="single")
        self.lose_server()
        with redirect_stderr(io.StringIO()) as err:
            s.next_image()
        self.assertEqual(s._ui_state, UIState.END_MESSAGE)
        self.assert_no_repaint(s)
        self.assertFalse(s.autoplay)
        self.assertIn("Lost connection to server", err.getvalue())

    def test_no_refresh_after_outage_on_first_fetch_after_splash(self):
        s = ReviewSession(self.store, mode="single")
        s._show_splash()
        self.lose_server()
        with redirect_stderr(io.StringIO()):
            s._handle_splash_key(pg.K_SPACE)
        self.assertEqual(s._ui_state, UIState.END_MESSAGE)
        self.assert_no_repaint(s)

    def test_no_refresh_after_outage_when_statuses_fail_on_mode_switch(self):
        s = ReviewSession(self.store, mode="single")
        self.lose_server()
        with redirect_stderr(io.StringIO()):
            s._switch_to_grid(True)
        self.assertEqual(s._ui_state, UIState.END_MESSAGE)
        self.assert_no_repaint(s)

    def test_no_refresh_after_outage_when_grid_fetch_fails_on_mode_switch(self):
        s = ReviewSession(self.store, mode="single")
        with (
            mock.patch.object(self.store, "image_bytes_many", side_effect=RemoteError("down")),
            redirect_stderr(io.StringIO()),
        ):
            s._switch_to_grid(True)
        self.assertEqual(s._ui_state, UIState.END_MESSAGE)
        self.assert_no_repaint(s)


class TestImports(unittest.TestCase):
    def test_remote_is_lightweight(self):
        code = (
            "import sys, image_review.remote\n"
            "assert not {'pygame', 'numpy', 'skimage'} & set(sys.modules)"
        )
        subprocess.run([sys.executable, "-c", code], check=True)


if __name__ == "__main__":
    unittest.main()
