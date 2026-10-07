"""A command whose optional extra is missing names the install command; other import failures are not masked."""

import builtins
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import click

from image_review.cli import EXTRA_MODULES, requires_extra
from tests.fixtures import invoke_cli

REAL_IMPORT = builtins.__import__


def failing_import(failing: str, exc: ImportError):
    """An __import__ that raises `exc` for the module `failing` (or a submodule of it) and imports the rest."""

    def fake(name, globals=None, locals=None, fromlist=(), level=0):
        if level == 0 and (name == failing or name.startswith(f"{failing}.")):
            raise exc
        return REAL_IMPORT(name, globals, locals, fromlist, level)

    return fake


def missing(name: str) -> ModuleNotFoundError:
    return ModuleNotFoundError(f"No module named {name!r}", name=name)


class RequiresExtraTest(unittest.TestCase):
    def test_missing_module_of_the_extra_names_the_install_command(self):
        for extra, modules in EXTRA_MODULES.items():
            for name in [*modules, *(f"{module}.sub" for module in modules)]:
                with (
                    self.subTest(extra=extra, name=name),
                    self.assertRaises(click.ClickException) as ctx,
                    requires_extra(extra),
                ):
                    raise missing(name)
                self.assertEqual(
                    ctx.exception.message,
                    f"this command needs the {extra} extra: pip install 'image-review[{extra}]'",
                )

    def test_our_own_import_errors_are_not_masked(self):
        for exc in (
            missing("image_review.nonexistent"),
            ImportError("cannot import name 'x' from 'image_review.store'", name="image_review.store"),
        ):
            with self.subTest(exc=exc), self.assertRaises(ImportError) as ctx, requires_extra("viewer"):
                raise exc
            self.assertIs(ctx.exception, exc)

    def test_other_import_failures_are_not_masked(self):
        for exc in (
            missing("numpy"),  # not part of the viewer extra
            missing("kiwisolver"),  # a missing transitive dependency is a broken install, not a missing extra
            missing("rectpack"),  # a core dependency: missing means a broken install, not a missing extra
            missing(""),
            ModuleNotFoundError("no name"),
            ImportError("libSDL2.so: cannot open shared object file", name="pygame.base"),  # broken, not missing
        ):
            with self.subTest(exc=exc), self.assertRaises(ImportError) as ctx, requires_extra("viewer"):
                raise exc
            self.assertIs(ctx.exception, exc)


class CommandHintTest(unittest.TestCase):
    def invoke(self, failing: str, exc: ImportError, *args: str):
        with mock.patch("builtins.__import__", failing_import(failing, exc)):
            return invoke_cli(*args)

    def test_review_without_pygame(self):
        result = self.invoke("pygame", missing("pygame"), "review", "--work-dir", ".")
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("Error: this command needs the viewer extra: pip install 'image-review[viewer]'", result.stderr)

    def test_preprocess_without_matplotlib(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.invoke(
                "matplotlib", missing("matplotlib"), "preprocess", tmp, "--work-dir", str(Path(tmp) / "w")
            )
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn(
            "Error: this command needs the preprocess extra: pip install 'image-review[preprocess]'", result.stderr
        )

    def test_preprocess_without_tqdm(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.invoke("tqdm", missing("tqdm"), "preprocess", tmp, "--work-dir", str(Path(tmp) / "w"))
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("pip install 'image-review[preprocess]'", result.stderr)

    def test_an_import_error_inside_the_package_is_not_masked(self):
        exc = ImportError("cannot import name 'ReviewSession'", name="image_review.controller")

        def fake(name, globals=None, locals=None, fromlist=(), level=0):
            if level == 1 and name == "controller":  # `from .controller import ReviewSession`
                raise exc
            return REAL_IMPORT(name, globals, locals, fromlist, level)

        with mock.patch("builtins.__import__", fake):
            result = invoke_cli("review", "--work-dir", ".")
        self.assertIs(result.exception, exc)
        self.assertNotIn("extra", result.output)


if __name__ == "__main__":
    unittest.main()
