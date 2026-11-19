"""`image-review review`: the browser page served on 127.0.0.1 and opened on this machine."""

import html
import http.client
import io
import json
import os
import re
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
import unittest
import webbrowser
from pathlib import Path
from unittest import mock

from image_review import cli
from image_review.cli import browser_url, graphical_browser, has_display, is_graphical, redirect_page
from image_review.server import LOOPBACK_HOST, LoopbackReviewServer, ReviewServer
from tests.fixtures import CLEAN_ENV, invoke_cli, make_work_dir, socket_dir, temp_dir, wait_for_file

NO_DISPLAY = {"DISPLAY": None, "WAYLAND_DISPLAY": None, "SSH_CONNECTION": None}
URL = re.compile(r"http://127\.0\.0\.1:(\d+)/#([A-Za-z0-9_-]+)")


class Tty(io.StringIO):
    def isatty(self):
        return True


def join_browser() -> None:
    """Wait for the thread that opens the browser, so what it prints lands in the captured output."""
    for thread in threading.enumerate():
        if thread.name == cli.BROWSER_THREAD:
            thread.join(10)


def stopped(*args, **kwargs):
    """A serve_forever that lets the browser thread finish, then stops like Ctrl-C."""
    join_browser()
    raise KeyboardInterrupt


def fake_browser(opens: bool = True, name: str = "firefox") -> mock.Mock:
    browser = mock.Mock()
    browser.name = name
    browser.open.return_value = opens
    return browser


class TestHelpers(unittest.TestCase):
    def test_redirect_page_escapes_the_url_in_the_refresh_and_the_link(self):
        page = redirect_page('http://127.0.0.1:1/#a"<b>&c')
        escaped = "http://127.0.0.1:1/#a&quot;&lt;b&gt;&amp;c"
        self.assertIn(f'<meta http-equiv="refresh" content="0; url={escaped}">', page)
        self.assertIn(f'<a href="{escaped}">', page)
        self.assertNotIn('a"<b>', page)

    def test_has_display(self):
        cases = [
            ("darwin", {}, True),
            ("linux", {}, False),
            ("linux", {"DISPLAY": ""}, False),
            ("linux", {"DISPLAY": ":0"}, True),
            ("linux", {"WAYLAND_DISPLAY": "wayland-0"}, True),
            ("freebsd", {"DISPLAY": ":0"}, True),
            ("darwin", {"SSH_CONNECTION": "10.0.0.1 5555 10.0.0.2 22"}, False),
            ("darwin", {"SSH_CONNECTION": "10.0.0.1 5555 10.0.0.2 22", "DISPLAY": ":0"}, False),
            ("linux", {"SSH_CONNECTION": "10.0.0.1 5555 10.0.0.2 22", "DISPLAY": "localhost:10.0"}, False),  # ssh -X
            ("linux", {"SSH_CONNECTION": "", "DISPLAY": ":0"}, True),
        ]
        for platform, environ, expected in cases:
            with self.subTest(platform=platform, environ=environ):
                self.assertIs(has_display(platform, environ), expected)

    def test_console_and_foreground_browsers_are_not_graphical(self):
        cases = [
            (webbrowser.GenericBrowser("firefox"), False),  # $BROWSER without "&": runs in the foreground
            (webbrowser.BackgroundBrowser("lynx"), False),
            (webbrowser.BackgroundBrowser("/usr/bin/w3m"), False),
            (webbrowser.BackgroundBrowser("www-browser"), False),
            (webbrowser.BackgroundBrowser("xdg-open"), True),
            (webbrowser.Mozilla("firefox"), True),
        ]
        for browser, expected in cases:
            with self.subTest(browser=type(browser).__name__, name=browser.name):
                self.assertIs(is_graphical(browser), expected)

    def test_graphical_browser(self):
        with mock.patch("webbrowser.get", side_effect=webbrowser.Error("none")):
            self.assertIsNone(graphical_browser())
        with mock.patch("webbrowser.get", return_value=webbrowser.GenericBrowser("lynx")):
            self.assertIsNone(graphical_browser())
        chrome = webbrowser.Chrome("google-chrome")
        with mock.patch("webbrowser.get", return_value=chrome):
            self.assertIs(graphical_browser(), chrome)


class TestReviewCommand(unittest.TestCase):
    def setUp(self):
        self.work = temp_dir(self) / "work"
        self.work.mkdir()
        make_work_dir(self.work)
        os.chmod(self.work, 0o700)  # private, so no world_access_warning
        os.chmod(self.work / "manifest.tsv", 0o600)
        self.home = socket_dir(self)
        for patcher in (
            mock.patch.dict(os.environ, {"HOME": str(self.home)}),
            mock.patch("image_review.server.short_host", return_value="node1"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.dir = self.home / ".image-review"
        self.lock = self.work / "review.lock"

    def review(self, *args: str, serve_forever=None, browser=None, env=None, platform="linux"):
        """Run `review --work-dir ... *args` on `platform` with serve_forever replaced (by default, a Ctrl-C once
        the browser thread is done) and webbrowser.get giving `browser` (by default a graphical one that opens);
        returns (result, the browser)."""
        browser = fake_browser() if browser is None else browser
        get = {"side_effect": browser} if isinstance(browser, BaseException) else {"return_value": browser}
        with (
            mock.patch.object(ReviewServer, "serve_forever", serve_forever or stopped),
            mock.patch("webbrowser.get", **get),
            mock.patch.object(sys, "platform", platform),
        ):
            result = invoke_cli("review", "--work-dir", str(self.work), *args, env={**NO_DISPLAY, **(env or {})})
        return result, browser

    def redirect_file(self) -> Path:
        (path,) = self.dir.glob("review-*.html")
        return path

    def assert_cleaned_up(self):
        self.assertFalse(self.lock.exists())
        self.assertFalse(self.dir.exists() and list(self.dir.iterdir()))

    def test_serves_on_127_0_0_1(self):
        seen = []

        def fake_serve(server, *a, **k):
            seen.append((type(server), server.server_address[0], server.socket.getsockname()[0]))
            raise KeyboardInterrupt

        result, _ = self.review(serve_forever=fake_serve)
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(seen, [(LoopbackReviewServer, LOOPBACK_HOST, LOOPBACK_HOST)])
        self.assertNotIn("WARNING", result.stderr)
        self.assert_cleaned_up()

    def test_redirect_file_is_private_holds_the_url_and_is_removed(self):
        seen = {}

        def fake_serve(server, *a, **k):
            path = self.redirect_file()
            seen["name"] = path.name
            seen["file_mode"] = stat.S_IMODE(path.stat().st_mode)
            seen["dir_mode"] = stat.S_IMODE(self.dir.stat().st_mode)
            seen["text"] = path.read_text()
            seen["port"] = server.host_port
            raise KeyboardInterrupt

        result, _ = self.review(serve_forever=fake_serve)
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(seen["name"], f"review-node1-{os.getpid()}.html")
        self.assertEqual((seen["file_mode"], seen["dir_mode"]), (0o600, 0o700))
        match = URL.search(seen["text"])
        assert match is not None
        self.assertEqual(int(match.group(1)), seen["port"])
        self.assertEqual(seen["text"], redirect_page(browser_url(match.group(2), seen["port"])))
        self.assertNotIn("localhost", seen["text"])
        self.assert_cleaned_up()

    def test_browser_opens_the_file_uri_once_listening(self):
        seen = {}

        def open_(uri):
            path = self.redirect_file()
            seen["uri"], seen["file"] = uri, path
            port, token = URL.search(html.unescape(path.read_text())).groups()
            seen["token"] = token
            conn = http.client.HTTPConnection(LOOPBACK_HOST, int(port), timeout=5)
            conn.connect()  # the server is listening
            conn.close()
            return True

        browser = fake_browser()
        browser.open.side_effect = open_
        result, _ = self.review(browser=browser, env={"DISPLAY": ":0"})
        self.assertEqual(result.exit_code, 0, result.output)
        browser.open.assert_called_once()
        self.assertEqual(seen["uri"], seen["file"].as_uri())
        self.assertTrue(seen["uri"].startswith("file://"))
        self.assertNotIn(seen["token"], seen["uri"])
        self.assertIn("Opened it in your browser.", result.output)
        self.assertIn("access error", result.output)
        self.assertIn(f"`cat {seen['file']}`", result.output)  # off a terminal: where the URL is
        self.assertNotIn(seen["token"], result.output)
        self.assert_cleaned_up()

    def test_a_blocking_browser_does_not_hold_up_the_server(self):
        release = threading.Event()
        self.addCleanup(release.set)
        browser = fake_browser()
        browser.open.side_effect = lambda uri: release.wait(10)
        real_serve_forever = ReviewServer.serve_forever
        seen = []

        def fake_serve(server, *a, **k):
            serving = threading.Thread(target=real_serve_forever, args=(server, 0.05), daemon=True)
            serving.start()
            conn = http.client.HTTPConnection(LOOPBACK_HOST, server.host_port, timeout=5)
            conn.request("GET", "/")
            seen.append(conn.getresponse().status)
            conn.close()
            seen.append(any(t.name == cli.BROWSER_THREAD and t.is_alive() for t in threading.enumerate()))
            server.shutdown()
            serving.join(5)
            release.set()
            stopped()

        result, _ = self.review(serve_forever=fake_serve, browser=browser, env={"DISPLAY": ":0"})
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(seen, [200, True])  # answered while the browser was still opening
        browser.open.assert_called_once()
        self.assert_cleaned_up()

    def test_a_foreground_or_console_browser_is_not_used(self):
        for browser in (webbrowser.GenericBrowser("firefox"), webbrowser.BackgroundBrowser("lynx")):
            with (
                self.subTest(browser=type(browser).__name__),
                mock.patch.object(type(browser), "open") as opened,
            ):
                result, _ = self.review(browser=browser, env={"DISPLAY": ":0"})
                self.assertEqual(result.exit_code, 0, result.output)
                opened.assert_not_called()
                self.assertIn("No graphical browser found, so none was opened; open it yourself.", result.output)
                self.assert_cleaned_up()

    def test_no_default_browser_gets_the_hint(self):
        with mock.patch.object(cli, "graphical_browser", return_value=None):
            result, browser = self.review(env={"DISPLAY": ":0"})
        self.assertEqual(result.exit_code, 0, result.output)
        browser.open.assert_not_called()
        self.assertIn("No graphical browser found", result.output)

    def test_wayland_alone_counts_as_a_display(self):
        result, browser = self.review(env={"WAYLAND_DISPLAY": "wayland-0"})
        self.assertEqual(result.exit_code, 0, result.output)
        browser.open.assert_called_once()

    def test_an_ssh_session_opens_nothing(self):
        for platform, env in (("darwin", {}), ("linux", {"DISPLAY": "localhost:10.0"})):
            with self.subTest(platform=platform):
                result, browser = self.review(
                    env={"SSH_CONNECTION": "10.0.0.1 5555 10.0.0.2 22", **env}, platform=platform
                )
                self.assertEqual(result.exit_code, 0, result.output)
                browser.open.assert_not_called()
                self.assertIn("No display found", result.output)

    def test_no_browser_skips_opening(self):
        result, browser = self.review("--no-browser", env={"DISPLAY": ":0"})
        self.assertEqual(result.exit_code, 0, result.output)
        browser.open.assert_not_called()
        self.assertNotIn("open it yourself", result.output)

    def test_no_display_on_linux_skips_opening_and_says_so(self):
        result, browser = self.review()
        self.assertEqual(result.exit_code, 0, result.output)
        browser.open.assert_not_called()
        self.assertIn("No display found, so no browser was opened; open it yourself.", result.output)

    def test_a_slow_browser_choice_does_not_hold_up_the_server(self):
        release = threading.Event()
        self.addCleanup(release.set)
        seen = []

        def slow_choice():
            release.wait(10)  # then None: no graphical browser

        def fake_serve(server, *a, **k):
            seen.append("serving")
            release.set()
            stopped()

        with mock.patch.object(cli, "graphical_browser", side_effect=slow_choice):
            result, _ = self.review(serve_forever=fake_serve, env={"DISPLAY": ":0"})
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(seen, ["serving"])  # reached while the choice was still pending
        self.assertIn("No graphical browser found", result.output)

    def test_a_browser_that_fails_to_open_gets_the_hint(self):
        failing = fake_browser()
        failing.open.side_effect = OSError("gone")
        for browser in (fake_browser(opens=False), failing):
            with self.subTest(browser=browser):
                result, _ = self.review(env={"DISPLAY": ":0"}, browser=browser)
                self.assertEqual(result.exit_code, 0, result.output)
                browser.open.assert_called_once()
                self.assertIn("Could not open a browser; open it yourself.", result.output)
                self.assert_cleaned_up()

    def test_any_failure_while_choosing_or_opening_gets_a_hint(self):
        result, _ = self.review(env={"DISPLAY": ":0"}, browser=ValueError("odd $BROWSER"))
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("No graphical browser found, so none was opened; open it yourself.", result.output)
        self.assert_cleaned_up()
        failing = fake_browser()
        failing.open.side_effect = RuntimeError("boom")
        result, _ = self.review(env={"DISPLAY": ":0"}, browser=failing)
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Could not open a browser; open it yourself.", result.output)
        self.assert_cleaned_up()

    def test_nothing_is_opened_or_printed_once_stopped(self):
        release = threading.Event()
        self.addCleanup(release.set)
        browser = fake_browser()

        def slow_get(*args):
            release.wait(10)
            return browser

        with (
            mock.patch.object(ReviewServer, "serve_forever", side_effect=KeyboardInterrupt),
            mock.patch("webbrowser.get", side_effect=slow_get),
            mock.patch.object(sys, "platform", "linux"),
        ):
            result = invoke_cli("review", "--work-dir", str(self.work), env={**NO_DISPLAY, "DISPLAY": ":0"})
            self.assertEqual(result.exit_code, 0, result.output)
            self.assert_cleaned_up()
            with mock.patch.object(cli, "print", create=True) as printed:
                release.set()  # the choice returns only after the command has stopped
                join_browser()
        browser.open.assert_not_called()
        printed.assert_not_called()
        self.assertTrue(result.output.endswith("Press Ctrl-C to stop.\n"), result.output)

    def test_a_failure_while_announcing_removes_the_file(self):
        with mock.patch.object(cli, "has_display", side_effect=RuntimeError("boom")):
            result, _ = self.review(env={"DISPLAY": ":0"})
        self.assertIsInstance(result.exception, RuntimeError)
        self.assert_cleaned_up()

    def test_hangup_removes_the_file_and_the_lock(self):
        seen = []

        def fake_serve(server, *a, **k):
            seen.append(self.redirect_file().exists())
            os.kill(os.getpid(), signal.SIGHUP)

        result, _ = self.review(serve_forever=fake_serve)
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(seen, [True])
        self.assert_cleaned_up()

    def test_tty_prints_the_url(self):
        seen = []

        def fake_serve(server, *a, **k):
            seen.append(server.host_port)
            raise KeyboardInterrupt

        with (
            mock.patch.object(ReviewServer, "serve_forever", fake_serve),
            mock.patch("sys.stdout", Tty()) as out,
            mock.patch.dict(os.environ),
        ):
            for name in (*CLEAN_ENV, *NO_DISPLAY):
                os.environ.pop(name, None)
            with mock.patch.object(sys, "platform", "linux"):
                cli.cli.main(["review", "--work-dir", str(self.work)], standalone_mode=False)
        text = out.getvalue()
        match = URL.search(text)
        assert match is not None
        self.assertEqual(int(match.group(1)), seen[0])
        self.assertIn("treat it like a password", text)
        self.assertIn("Press Ctrl-C to stop.", text)
        self.assertNotIn(str(self.dir), text)
        self.assert_cleaned_up()

    def test_off_a_terminal_the_token_is_only_in_the_file(self):
        seen = []

        def fake_serve(server, *a, **k):
            seen.append(URL.search(self.redirect_file().read_text()).group(2))
            raise KeyboardInterrupt

        with (
            mock.patch.object(ReviewServer, "serve_forever", fake_serve),
            mock.patch.object(sys, "platform", "linux"),
        ):
            result = invoke_cli("-v", "review", "--work-dir", str(self.work), env=NO_DISPLAY)
        self.assertEqual(result.exit_code, 0, result.output)
        (token,) = seen
        self.assertNotIn(token, result.output)
        self.assertNotIn(token, result.stderr)
        self.assertIn(f"  {self.dir / f'review-node1-{os.getpid()}.html'}\n", result.output)
        self.assertIn("treat it like a", result.output)
        self.assertIn("Press Ctrl-C to stop.", result.output)

    def test_busy_port_exits_1(self):
        with socket.socket() as busy:
            busy.bind((LOOPBACK_HOST, 0))
            busy.listen(1)
            port = busy.getsockname()[1]
            result, browser = self.review("--port", str(port), env={"DISPLAY": ":0"})
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIsInstance(result.exception, SystemExit)
        self.assertIn(f"Cannot listen on 127.0.0.1:{port}: ", result.stderr)
        self.assertNotIn("Traceback", result.output)
        browser.open.assert_not_called()
        self.assert_cleaned_up()

    def test_privileged_or_out_of_range_port_is_a_usage_error(self):
        for port in ("80", "1023", "65536", "-1"):
            with self.subTest(port=port):
                result, _ = self.review("--port", port)
                self.assertEqual(result.exit_code, 2, result.output)
                self.assertIn("--port", result.stderr)
                self.assertIn("1024-65535", result.stderr)
                self.assert_cleaned_up()

    def test_a_bad_port_is_refused_before_the_lock(self):
        held = json.dumps({"host": "node042", "user": "alice", "pid": 1234, "started": "2026-09-30T12:00:00Z"})
        self.lock.write_text(held)
        with mock.patch.object(cli, "open_local_store") as opened:
            result, _ = self.review("--port", "80")
        self.assertEqual(result.exit_code, 2, result.output)
        self.assertIn("--port", result.stderr)
        self.assertNotIn("node042", result.output)
        opened.assert_not_called()
        self.assertEqual(self.lock.read_text(), held)

    def test_windows_is_refused(self):
        result, browser = self.review(platform="win32", env={"DISPLAY": ":0"})
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("review needs Linux or macOS", result.stderr)
        browser.open.assert_not_called()
        self.assert_cleaned_up()

    def test_chosen_port_is_used(self):
        with socket.socket() as probe:
            probe.bind((LOOPBACK_HOST, 0))
            port = probe.getsockname()[1]
        seen = []

        def fake_serve(server, *a, **k):
            seen.append(server.host_port)
            raise KeyboardInterrupt

        result, _ = self.review("--port", str(port), serve_forever=fake_serve)
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(seen, [port])

    def test_removed_options_are_unknown(self):
        for args in (
            ["--mode", "grid"],
            ["--pass", "1"],
            ["--batch", "batch_001"],
            ["--filter", "all"],
            ["--rotate", "never"],
            ["--reviewer", "Dr. Lee"],
        ):
            with self.subTest(args=args):
                result, _ = self.review(*args)
                self.assertEqual(result.exit_code, 2, result.output)
                self.assertRegex(result.stderr, rf"No such option:? '?{args[0]}'?")
                self.assert_cleaned_up()

    def test_token_from_environment_is_ignored(self):
        token = "0123456789abcdef0123456789abcdef"
        seen = []

        def fake_serve(server, *a, **k):
            seen.append(URL.search(self.redirect_file().read_text()).group(2))
            raise KeyboardInterrupt

        for value in (token, "short"):  # not even checked
            with self.subTest(value=value):
                result, _ = self.review(serve_forever=fake_serve, env={"IMAGE_REVIEW_TOKEN": value})
                self.assertEqual(result.exit_code, 0, result.output)
                self.assertNotIn("IMAGE_REVIEW_TOKEN", result.output)
        self.assertEqual(len(seen), 2)
        self.assertNotIn(token, seen)
        self.assertNotEqual(seen[0], seen[1])

    def test_reviewer_from_environment_is_not_read(self):
        # The old review refused a name with a tab; nothing reads the variable now
        result, _ = self.review(env={"IMAGE_REVIEW_REVIEWER": "a\tb"})
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertNotIn("reviewer", result.output.lower())

    def test_redirect_file_failure_releases_everything(self):
        with mock.patch("image_review.server.write_private_file", side_effect=OSError("disk full")):
            result, browser = self.review(env={"DISPLAY": ":0"})
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("Cannot write browser redirect file: disk full", result.stderr)
        browser.open.assert_not_called()
        self.assert_cleaned_up()

    def test_locked_work_dir_writes_no_file(self):
        self.lock.write_text(
            json.dumps({"host": "node042", "user": "alice", "pid": 1234, "started": "2026-09-30T12:00:00Z"})
        )
        result, browser = self.review(env={"DISPLAY": ":0"})
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("node042", result.output)
        browser.open.assert_not_called()
        self.assertFalse(self.dir.exists() and list(self.dir.iterdir()))

    def test_sigterm_removes_the_lock_and_the_redirect_file(self):
        env = {**os.environ, "HOME": str(self.home), "PYTHONDONTWRITEBYTECODE": "1"}
        for name in (*CLEAN_ENV, *NO_DISPLAY):
            env.pop(name, None)
        proc = subprocess.Popen(
            [sys.executable, "-m", "image_review.cli", "review", "--work-dir", str(self.work), "--no-browser"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(proc.kill)
        wait_for_file(self, proc, self.dir, "review-*.html")
        self.assertTrue(self.lock.exists())
        time.sleep(0.3)  # let serve_forever start
        proc.send_signal(signal.SIGTERM)
        out, err = proc.communicate(timeout=20)
        self.assertEqual(proc.returncode, 0, err)
        self.assertIn("Press Ctrl-C to stop.", out)
        self.assertEqual(list(self.dir.glob("review-*.html")), [])
        self.assertFalse(self.lock.exists())


if __name__ == "__main__":
    unittest.main()
