import errno
import json
import os
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

import pygame as pg

from image_review import atomic as atomic_module
from image_review import lock as lock_module
from image_review import store as store_module
from image_review.access import policy_of_dir
from image_review.cli import unknown_batch_message
from image_review.lock import LOCK_NAME, WorkDirLocked, acquire_lock, boot_id, release_lock, this_process
from image_review.review_db import ReviewDB
from image_review.store import LocalStore
from tests.fixtures import ROWS, invoke_cli, make_work_dir, mark, temp_dir

THIS_BOOT = boot_id()


class LockTestCase(unittest.TestCase):
    def setUp(self):
        self.work_dir = temp_dir(self)
        make_work_dir(self.work_dir)
        self.lock_path = self.work_dir / LOCK_NAME

    def open(self, **kwargs) -> LocalStore:
        store = LocalStore(self.work_dir, **kwargs)
        self.addCleanup(store.close)
        return store

    def write_lock(self, host: str, pid: int, user: str = "alice", boot: str | None = THIS_BOOT) -> None:
        record = {"host": host, "user": user, "pid": pid, "started": "2026-09-30T12:00:00Z"}
        if boot is not None:
            record["boot_id"] = boot
        self.lock_path.write_text(json.dumps(record))

    def holder(self) -> dict:
        return json.loads(self.lock_path.read_text())


def finished_pid() -> int:
    finished = subprocess.run(
        [sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True, check=True
    )
    return int(finished.stdout)


class TestWorkDirLock(LockTestCase):
    def test_second_writer_refused_until_first_closes(self):
        first = self.open()
        self.assertEqual(self.holder()["pid"], os.getpid())
        with self.assertRaises(WorkDirLocked) as ctx:
            LocalStore(self.work_dir)
        self.assertIn(str(self.lock_path), str(ctx.exception))
        self.assertIn(f"pid {os.getpid()}", str(ctx.exception))
        first.close()
        self.assertFalse(self.lock_path.exists())
        with LocalStore(self.work_dir) as second:
            self.assertTrue(self.lock_path.exists())
            mark(second, [ROWS[0][1]], "CLEAN")
        self.assertFalse(self.lock_path.exists())

    def test_acquire_leaves_only_the_complete_lock(self):
        self.open()
        self.assertEqual([p.name for p in self.work_dir.iterdir() if p.name.startswith(LOCK_NAME)], [LOCK_NAME])
        self.assertEqual(set(self.holder()), {"host", "boot_id", "user", "pid", "started"})

    def test_link_reply_lost_on_nfs_counts_as_acquired(self):
        real_link = os.link

        def link_then_fail(src, dst):
            real_link(src, dst)
            raise OSError(5, "simulated lost reply")

        with mock.patch("os.link", link_then_fail):
            self.open()
        self.assertEqual(self.holder()["pid"], os.getpid())

    @unittest.skipUnless(THIS_BOOT, "no boot_id on this platform")
    def test_stale_lock_on_this_machine_is_reclaimed(self):
        self.write_lock(socket.gethostname(), finished_pid())
        self.open()
        self.assertEqual(self.holder()["pid"], os.getpid())

    def test_same_hostname_other_boot_or_old_lock_is_refused(self):
        for boot in ("another-boot-id", "", None):
            with self.subTest(boot=boot):
                self.write_lock(socket.gethostname(), finished_pid(), boot=boot)
                with self.assertRaises(WorkDirLocked) as ctx:
                    LocalStore(self.work_dir)
                self.assertIn("by hand", str(ctx.exception))
                self.assertEqual(self.holder()["user"], "alice")

    def test_lock_from_another_host_is_refused(self):
        self.write_lock("node042", 1234)
        with self.assertRaises(WorkDirLocked) as ctx:
            LocalStore(self.work_dir)
        message = str(ctx.exception)
        for part in ("alice", "node042", "pid 1234", "2026-09-30T12:00:00Z", str(self.lock_path), "by hand"):
            self.assertIn(part, message)
        self.assertEqual(self.holder()["host"], "node042")

    def test_live_process_of_another_user_is_refused(self):
        self.write_lock(socket.gethostname(), 1234, user="bob")
        with mock.patch("os.kill", side_effect=PermissionError) as kill, self.assertRaises(WorkDirLocked) as ctx:
            LocalStore(self.work_dir)
        if THIS_BOOT:
            kill.assert_called_once_with(1234, 0)
        for part in (
            "bob",
            "pid 1234",
            "2026-09-30T12:00:00Z",
            "by hand",
        ):  # pid reuse: the user may still need to remove it
            self.assertIn(part, str(ctx.exception))
        self.assertEqual(self.holder()["pid"], 1234)

    @unittest.skipUnless(THIS_BOOT, "no boot_id on this platform")
    def test_concurrent_reclaimers_cannot_both_acquire(self):
        # tmpfs and disk filesystems reuse freed inode numbers differently; run on both where possible
        roots = [None, *([Path.home()] if os.access(Path.home(), os.W_OK) else [])]
        for root in roots:
            with self.subTest(root=root):
                work_dir = Path(tempfile.mkdtemp(dir=root, prefix=".image-review-lock-test-"))
                self.addCleanup(shutil.rmtree, work_dir, ignore_errors=True)
                make_work_dir(work_dir)
                self.assert_one_reclaimer_wins(work_dir)

    def assert_one_reclaimer_wins(self, work_dir: Path) -> None:
        lock_path = work_dir / LOCK_NAME
        stale = {
            "host": socket.gethostname(),
            "boot_id": THIS_BOOT,
            "user": "alice",
            "pid": finished_pid(),
            "started": "t",
        }
        lock_path.write_text(json.dumps(stale))
        real_is_stale = lock_module.is_stale
        a_started = False
        winners: list[LocalStore] = []

        def a_reclaims_first(holder, me):
            nonlocal a_started
            if not a_started:  # B has read the stale lock (and holds it open); A reclaims and re-creates it now
                a_started = True
                winners.append(LocalStore(work_dir))  # A's own is_stale call goes straight through
            return real_is_stale(holder, me)

        with mock.patch.object(lock_module, "is_stale", a_reclaims_first), self.assertRaises(WorkDirLocked):
            LocalStore(work_dir)  # B
        self.assertEqual(len(winners), 1)
        self.assertTrue(lock_path.exists())  # A's lock survived B's stale decision
        winners[0].close()
        self.assertFalse(lock_path.exists())

    @mock.patch.object(lock_module, "EMPTY_LOCK_WAIT", 0.1)
    def test_corrupt_lock_is_refused(self):
        for data in (
            b"",
            b"not json",
            b"[]",
            b'{"host": "h", "user": "u", "pid": "1", "started": "t"}',
            b'{"host": "h", "user": "u", "pid": 0, "started": "t"}',
            b"\xff\xfe\x00",
        ):
            with self.subTest(data=data):
                self.lock_path.write_bytes(data)
                with self.assertRaises(WorkDirLocked) as ctx:
                    LocalStore(self.work_dir)
                self.assertIn(str(self.lock_path), str(ctx.exception))
                self.assertIn("corrupt", str(ctx.exception))
                self.assertIn("by hand", str(ctx.exception))
                self.assertEqual(self.lock_path.read_bytes(), data)

    def test_hard_links_unsupported_falls_back_to_direct_create(self):
        for err in (errno.EPERM, errno.ENOTSUP, errno.ENOSYS):
            with self.subTest(errno=errno.errorcode[err]):
                with mock.patch("os.link", side_effect=OSError(err, os.strerror(err))):
                    store = LocalStore(self.work_dir)
                self.assertEqual(self.holder()["pid"], os.getpid())
                self.assertEqual(set(self.holder()), {"host", "boot_id", "user", "pid", "started"})
                self.assertEqual([p.name for p in self.work_dir.iterdir() if p.name.startswith(LOCK_NAME)], [LOCK_NAME])
                with (
                    mock.patch("os.link", side_effect=OSError(err, os.strerror(err))),
                    self.assertRaises(WorkDirLocked),
                ):
                    LocalStore(self.work_dir)
                store.close()
                self.assertFalse(self.lock_path.exists())

    def test_empty_lock_filled_within_wait_is_held(self):
        self.lock_path.write_text("")
        real_sleep = time.sleep

        def writer_finishes(seconds):  # the direct-create writer fills in its record while we wait
            self.write_lock("node042", 1234)
            real_sleep(seconds)

        with mock.patch("time.sleep", side_effect=writer_finishes), self.assertRaises(WorkDirLocked) as ctx:
            LocalStore(self.work_dir)
        self.assertEqual((ctx.exception.holder.host, ctx.exception.holder.pid), ("node042", 1234))
        self.assertNotIn("corrupt", str(ctx.exception))

    def test_sweeps_leftover_siblings_of_dead_processes(self):
        host, dead, live = socket.gethostname(), finished_pid(), os.getppid()
        names = {
            "dead": f"{LOCK_NAME}.{host}.{THIS_BOOT or '-'}.{dead}.abcd",
            "live": f"{LOCK_NAME}.{host}.{THIS_BOOT or '-'}.{live}.abcd",
            "other_boot": f"{LOCK_NAME}.{host}.another-boot.{dead}.abcd",
            "other_host": f"{LOCK_NAME}.node042.{THIS_BOOT or '-'}.{dead}.abcd",
        }
        for name in names.values():
            (self.work_dir / name).write_text("{}")
        self.open()
        left = {key for key, name in names.items() if (self.work_dir / name).exists()}
        self.assertEqual(left, {"live", "other_boot", "other_host"} if THIS_BOOT else set(names))

    def test_user_without_passwd_entry_falls_back_to_uid(self):
        with mock.patch("getpass.getuser", side_effect=KeyError("getpwuid(): uid not found")):
            self.open()
        self.assertEqual(self.holder()["user"], str(os.getuid()))

    def test_read_only_ignores_lock_and_refuses_mark(self):
        self.open()
        reader = self.open(read_only=True)
        self.assertEqual(set(reader.statuses(1).values()), {"UNREVIEWED"})
        with self.assertRaises(PermissionError):
            mark(reader, [ROWS[0][1]], "CLEAN")
        reader.close()
        self.assertTrue(self.lock_path.exists())  # the writer's lock is untouched

    def test_closed_store_refuses_mark(self):
        store = self.open()
        store.close()
        with self.assertRaises(PermissionError):
            mark(store, [ROWS[0][1]], "CLEAN")

    def test_read_only_and_closed_stores_refuse_undo(self):
        writer = self.open()
        mark(writer, [ROWS[0][1]], "CLEAN")
        reader = self.open(read_only=True)
        with self.assertRaises(PermissionError):
            reader.undo(1, reviewer="tester")
        reader.close()
        writer.close()
        with self.assertRaises(PermissionError):
            writer.undo(1, reviewer="tester")
        self.assertEqual(self.open(read_only=True).statuses(1)[ROWS[0][1]], "CLEAN")

    def test_close_is_idempotent_and_keeps_another_process_lock(self):
        store = self.open()
        store.close()
        store.close()
        self.assertFalse(self.lock_path.exists())
        store = self.open()
        self.write_lock(socket.gethostname(), os.getpid() + 1)  # e.g. reclaimed by another process
        store.close()
        self.assertEqual(self.holder()["pid"], os.getpid() + 1)

    def test_bad_manifest_takes_no_lock_and_bad_review_releases_it(self):
        (self.work_dir / "review.tsv").write_text("bad header\n")
        with self.assertRaises(ValueError):
            LocalStore(self.work_dir)
        self.assertFalse(self.lock_path.exists())
        with mock.patch.object(store_module, "acquire_lock") as acquire:
            (self.work_dir / "manifest.tsv").write_text("bad header\n")
            with self.assertRaises(ValueError):
                LocalStore(self.work_dir)
        acquire.assert_not_called()

    def test_lock_file_mode_follows_policy(self):
        for dir_mode, file_mode in ((0o700, 0o600), (0o2770, 0o660)):
            with self.subTest(dir_mode=oct(dir_mode)):
                os.chmod(self.work_dir, dir_mode)
                with LocalStore(self.work_dir):
                    self.assertEqual(stat.S_IMODE(self.lock_path.stat().st_mode), file_mode)

    def test_lock_is_group_readable_before_it_is_written(self):
        # a teammate who opens a directly created lock while it is still empty must wait, not get PermissionError
        os.chmod(self.work_dir, 0o2770)
        file_mode = policy_of_dir(self.work_dir).file_mode
        old = os.umask(0o002)
        self.addCleanup(os.umask, old)
        real_fchmod = os.fchmod
        opened: list[int] = []

        def spy_fchmod(fd, mode):
            opened.append(stat.S_IMODE(os.fstat(fd).st_mode))
            return real_fchmod(fd, mode)

        for link_error in (None, OSError(errno.EPERM, "no links")):
            with self.subTest(link_error=link_error):
                opened.clear()
                with (
                    mock.patch.object(atomic_module.os, "fchmod", spy_fchmod),
                    mock.patch.object(atomic_module.os, "link", side_effect=link_error, wraps=os.link),
                ):
                    me = acquire_lock(self.work_dir)
                release_lock(self.work_dir, me)
                self.assertEqual(opened, [file_mode] * (1 if link_error is None else 2))  # sibling (+ direct)

    def test_sibling_name_collision_still_acquires(self):
        real_token_hex = lock_module.secrets.token_hex
        me = this_process()
        collided = self.work_dir / f"{LOCK_NAME}.{me.host}.{me.boot_id or '-'}.{me.pid}.collide"
        collided.write_text("{}")
        tokens = iter(["collide"])

        def first_collides(nbytes=None):
            return next(tokens, None) or real_token_hex(nbytes)

        with mock.patch.object(lock_module.secrets, "token_hex", first_collides):
            self.open()
        self.assertEqual(self.holder()["pid"], os.getpid())
        self.assertEqual([p.name for p in self.work_dir.iterdir() if p.name.startswith(LOCK_NAME)], [LOCK_NAME])


class TestLockCli(LockTestCase):
    def invoke(self, *args, env=None):
        return invoke_cli(*args, "--work-dir", str(self.work_dir), env=env)

    def test_review_on_locked_dir_exits_1(self):
        self.write_lock("node042", 1234)
        with mock.patch("image_review.controller.ReviewSession") as session:
            result = self.invoke("review")
        self.assertEqual(result.exit_code, 1, result.output)
        session.assert_not_called()
        for part in ("alice", "node042", "pid 1234", str(self.lock_path)):
            self.assertIn(part, result.output)

    def test_review_on_undecodable_lock_names_it(self):
        self.lock_path.write_bytes(b"\xff\xfe\x00")
        with mock.patch("image_review.controller.ReviewSession") as session:
            result = self.invoke("review")
        self.assertEqual(result.exit_code, 1, result.output)
        session.assert_not_called()
        self.assertIn(str(self.lock_path), result.output)
        self.assertNotIn("Cannot read work directory", result.output)

    def test_review_holds_lock_while_running(self):
        seen = []
        with mock.patch("image_review.controller.ReviewSession") as session:
            session.return_value.run.side_effect = lambda: seen.append(self.holder()["pid"])
            result = self.invoke("review")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(seen, [os.getpid()])
        self.assertFalse(self.lock_path.exists())

    def test_review_hangup_releases_lock_and_restores_handler(self):
        before = signal.getsignal(signal.SIGHUP)
        with mock.patch("image_review.controller.ReviewSession") as session:
            session.return_value.run.side_effect = lambda: os.kill(os.getpid(), signal.SIGHUP)
            result = self.invoke("review")
        self.assertNotEqual(result.exit_code, 0)
        self.assertFalse(self.lock_path.exists())
        self.assertEqual(signal.getsignal(signal.SIGHUP), before)

    def assert_rejected(self, *args):
        with mock.patch("image_review.controller.ReviewSession") as session, mock.patch("pygame.init") as init:
            result = self.invoke("review", *args)
        self.assertEqual(result.exit_code, 2, result.output)
        session.assert_not_called()
        init.assert_not_called()
        self.assertFalse(self.lock_path.exists())
        return result

    def test_review_rejects_nonpositive_pass(self):
        for value in ("0", "-1"):
            self.assert_rejected("--pass", value)

    def test_review_rejects_unknown_batch_listing_known(self):
        result = self.assert_rejected("--batch", "nope")
        self.assertIn("batch_001", result.output)
        self.assertIn("batch_002", result.output)

    def test_review_rejects_empty_batch(self):
        self.assert_rejected("--batch", "")

    def test_review_rejects_bad_reviewer(self):
        for value in ("a\tb", "a\nb", "", "  ", "r" * 65):
            with self.subTest(reviewer=value):
                result = self.assert_rejected("--reviewer", value)
                self.assertIn("--reviewer", result.output)
                self.assertIn("printable", result.output)

    def test_review_passes_reviewer_to_session(self):
        cases = [
            ((), {}, "login"),
            (("--reviewer", "Dr. Lee"), {}, "Dr. Lee"),
            ((), {"IMAGE_REVIEW_REVIEWER": "env name"}, "env name"),
        ]
        for args, env, expected in cases:
            with (
                self.subTest(args=args, env=env),
                mock.patch("image_review.controller.ReviewSession") as session,
                mock.patch("image_review.cli.getpass.getuser", return_value="login"),
            ):
                result = self.invoke("review", *args, env=env)
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertEqual(session.call_args.kwargs["reviewer"], expected)

    def test_review_migration_conflict_is_a_clean_error(self):
        with (
            mock.patch.object(ReviewDB, "migrate", side_effect=RuntimeError("review.tsv changed since it was loaded")),
            mock.patch("image_review.controller.ReviewSession") as session,
        ):
            result = self.invoke("review")
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("changed since it was loaded", result.output)
        self.assertNotIn("Traceback", result.output)
        session.assert_not_called()
        self.assertFalse(self.lock_path.exists())

    def test_review_without_user_name_asks_for_reviewer(self):
        with mock.patch("image_review.cli.getpass.getuser", side_effect=OSError("no user")):
            result = self.assert_rejected()
        self.assertIn("--reviewer", result.output)

    def test_unknown_batch_message_lists_at_most_five(self):
        known = {f"batch_{i:03d}" for i in range(1, 9)}
        message = unknown_batch_message("x", known)
        self.assertIn("batch_005", message)
        self.assertNotIn("batch_006", message)
        self.assertIsNone(unknown_batch_message("batch_003", known))

    def test_review_valid_batch_reaches_session(self):
        with (
            mock.patch("image_review.controller.ReviewSession") as session,
            mock.patch("pygame.init"),
            mock.patch("pygame.quit"),
        ):
            result = self.invoke("review", "--batch", "batch_002")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(session.call_args.kwargs["batch"], "batch_002")

    def test_review_rotate_reaches_session(self):
        for args, expected in (((), "auto"), (("--rotate", "never"), "never"), (("--rotate", "always"), "always")):
            with self.subTest(args=args):
                with (
                    mock.patch("image_review.controller.ReviewSession") as session,
                    mock.patch("pygame.init"),
                    mock.patch("pygame.quit"),
                ):
                    result = self.invoke("review", *args)
                self.assertEqual(result.exit_code, 0, result.output)
                self.assertEqual(session.call_args.kwargs["rotation"], expected)

    def test_review_rejects_unknown_rotate(self):
        self.assert_rejected("--rotate", "sideways")

    def test_review_rejects_unknown_filter(self):
        self.assert_rejected("--filter", "bogus")
        with (
            mock.patch("image_review.controller.ReviewSession") as session,
            mock.patch("pygame.init"),
            mock.patch("pygame.quit"),
        ):
            result = self.invoke("review", "--filter", "clean")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(session.call_args.kwargs["status_filter"], "clean")

    def test_unwritable_work_dir_says_write(self):
        with mock.patch("os.open", side_effect=PermissionError(13, "Permission denied", str(self.lock_path) + ".x")):
            result = self.invoke("review")
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn(f"Cannot write to work directory {self.work_dir}", result.output)

    def test_missing_manifest_message_kept(self):
        (self.work_dir / "manifest.tsv").unlink()
        result = self.invoke("review")
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("No preprocessed data found", result.output)
        self.assertFalse(self.lock_path.exists())

    def test_status_counts_flagged_after_pass_one(self):
        with LocalStore(self.work_dir) as store:
            mark(store, ["batch_001/a.jpg"], "DIRTY")
            mark(store, ["batch_001/b.jpg"], "CLEAN")
            mark(store, ["batch_002/c.jpg", "batch_002/d.jpg"], "CLEAN")
        result = self.invoke("status")
        self.assertEqual(result.exit_code, 0, result.output)
        for line in (
            "  CLEAN:           3",
            "  DIRTY:           0",
            "  UNREVIEWED:      0",
            "  FLAGGED:         1",
            "Current pass: 2",
        ):
            self.assertIn(line + "\n", result.output)
        self.assertIn(f"{'Batch':<15} {'Total':>6} {'Clean':>6} {'Dirty':>6} {'Unrev':>6} {'Flag':>6}", result.output)
        self.assertIn(f"{'batch_001':<15} {2:>6} {1:>6} {0:>6} {0:>6} {1:>6}", result.output)

    def test_grid_review_names_held_back_images(self):
        with LocalStore(self.work_dir) as store:
            mark(store, ["batch_001/a.jpg"], "DIRTY")
            mark(store, ["batch_001/b.jpg"], "CLEAN")
            mark(store, ["batch_002/c.jpg", "batch_002/d.jpg"], "CLEAN")
        with mock.patch.object(pg.display, "toggle_fullscreen", lambda: None):
            result = self.invoke("review", "--mode", "grid")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn(
            "No grid items for pass 2; 1 FLAGGED/DIRTY image needs single-mode review (--mode single)", result.output
        )
        self.assertNotIn("No images to review", result.output)

    def test_status_works_on_locked_dir(self):
        self.write_lock("node042", 1234)
        result = self.invoke("status")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("UNREVIEWED:", result.output)
        self.assertEqual(self.holder()["host"], "node042")


if __name__ == "__main__":
    unittest.main()
