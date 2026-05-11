"""Static-analysis tests: every Python module reference in ``run.sh``
must resolve against the actual codebase.

Why this file exists
--------------------

``run.sh`` is the bootstrap entry point and contains TWO classes of
Python-module references:

  (a) ``import`` / ``from ... import`` lines inside ``$VENV_PY - <<'PY'``
      heredocs (and inline ``-c '...'`` blocks).
  (b) ``$VENV_PY -m <module>`` invocations as plain bash subprocess calls.

When we reorganised the codebase into ``local_scribe/`` packages on
2026-05-11 the reorg missed both kinds. Heredoc imports like
``from config import ...`` produced confusing ``ModuleNotFoundError``s
during bootstrap stage 7; bash-level ``python -m char_settings_writer``
invocations exploded at bootstrap stage 9 with ``No module named
char_settings_writer`` AFTER the operator had already typed Touch ID +
done a YubiKey tap to mint the bearer token. Both are exactly the same
regression class but at different parsing layers.

This module ships two complementary tests:

  RunShPythonImportsResolveTests
        Heredoc/inline-Python ``import`` statements — pre-existing.

  RunShPythonDashMResolveTests
        ``$VENV_PY -m <module>`` (and ``python -m <module>``) bash-level
        invocations — added 2026-05-11 after the char_settings_writer
        regression.

Both run entirely in-process against this checkout (no subprocess, no
fakes). Fast and high-signal.
"""

from __future__ import annotations

import importlib
import re
import unittest
from pathlib import Path
from typing import Iterable

_REPO = Path(__file__).resolve().parents[2]
_RUN_SH = _REPO / "run.sh"

# Modules we expect run.sh's Python heredocs to use. Everything OUTSIDE
# this allowlist gets actually-imported and tested.
_STDLIB_ALLOWED = {
    "argparse", "base64", "collections", "contextlib", "csv", "datetime",
    "errno", "fcntl", "functools", "getpass", "hashlib", "hmac", "html",
    "importlib", "io", "json", "logging", "math", "os", "pathlib", "platform",
    "plistlib", "queue", "random", "re", "secrets", "shlex", "shutil",
    "signal", "socket", "socketserver", "ssl", "stat", "string", "struct",
    "subprocess", "sys", "tempfile", "textwrap", "threading", "time",
    "tomllib", "traceback", "types", "typing", "unittest", "urllib",
    "uuid", "warnings", "weakref", "wsgiref", "zipfile", "zlib",
}
# Third-party modules that we trust to be installed via pip and don't
# need to test-import here (some are heavyweight to import in tests).
_THIRD_PARTY_ALLOWED = {
    "huggingface_hub", "faster_whisper", "click", "uvicorn", "fastapi",
    "starlette", "pydantic", "numpy", "torch", "soundfile", "mlx",
    "mlx_lm", "transformers", "pyaudio", "anyio", "httpx",
}

_IMPORT_LINE_RE = re.compile(
    r"""^(
        (?:from\s+(?P<from_mod>[a-zA-Z_][a-zA-Z0-9_\.]*)\s+import\s+.+)
        |
        (?:import\s+(?P<import_mod>[a-zA-Z_][a-zA-Z0-9_\.,\s]+)\s*(?:\#.*)?)
    )$""",
    re.VERBOSE,
)


def _extract_import_lines(run_sh: Path) -> Iterable[tuple[int, str, str]]:
    """Yield ``(line_number, raw_line, root_module)`` for every import
    line in run.sh's Python heredocs.

    A heredoc is bounded by lines like ``<<'PY'`` or ``<<PY`` (open)
    and a matching ``PY`` (close). Imports outside heredocs would be
    bash syntax errors, but we don't try to be clever — any line at
    column 0 starting with ``from `` or ``import `` is a Python
    import statement and gets considered.
    """
    for lineno, line in enumerate(run_sh.read_text().splitlines(), start=1):
        if not (line.startswith("from ") or line.startswith("import ")):
            continue
        m = _IMPORT_LINE_RE.match(line)
        if not m:
            continue
        if m.group("from_mod"):
            mod = m.group("from_mod")
            yield lineno, line, mod
        else:
            mods_blob = m.group("import_mod") or ""
            for piece in mods_blob.split(","):
                mod = piece.strip().split()[0] if piece.strip() else ""
                if mod:
                    yield lineno, line, mod


class RunShPythonImportsResolveTests(unittest.TestCase):
    """Every ``local_scribe.*`` import in run.sh must actually import."""

    def test_run_sh_exists(self) -> None:
        self.assertTrue(_RUN_SH.is_file(), f"run.sh not found at {_RUN_SH}")

    def test_every_local_scribe_import_resolves(self) -> None:
        unresolved: list[str] = []
        local_imports_seen = 0
        for lineno, raw, mod in _extract_import_lines(_RUN_SH):
            root = mod.split(".")[0]
            if root in _STDLIB_ALLOWED:
                continue
            if root in _THIRD_PARTY_ALLOWED:
                continue
            if root != "local_scribe":
                # Unexpected flat-module import — this is exactly the
                # regression class we ship this test to prevent.
                unresolved.append(
                    f"run.sh:{lineno}: {raw.strip()}\n"
                    f"  -> '{mod}' is not allowed: must be a local_scribe.* "
                    "subpackage (the flat-module layout was retired during "
                    "the 2026-05-11 reorg)."
                )
                continue
            local_imports_seen += 1
            try:
                importlib.import_module(mod)
            except Exception as e:
                unresolved.append(
                    f"run.sh:{lineno}: {raw.strip()}\n"
                    f"  -> import_module({mod!r}) failed: {type(e).__name__}: {e}"
                )
        self.assertEqual(
            unresolved,
            [],
            msg="\n\n".join(unresolved),
        )
        self.assertGreater(
            local_imports_seen, 5,
            msg="Surprisingly few local_scribe.* imports found in run.sh — "
                "did the heredoc parsing break?",
        )


# ---------------------------------------------------------------------------
# Bash-level ``python -m <module>`` invocations.

# Catches lines like:
#   "$VENV_PY" -m local_scribe.char.char_settings_writer
#   exec "$VENV_PY" -m local_scribe asr start
#   $("$VENV_PY" -m local_scribe.security.service_auth token asr)
# Matches the *first* token after ``-m``.
_DASH_M_RE = re.compile(
    r"""
    (?:\$\("\$VENV_PY"|"\$VENV_PY"|\$VENV_PY|python3?)   # python invocation
    \s+(?:-[A-Za-z0-9]+\s+)*                              # optional flags like -u
    -m\s+
    (?P<module>[a-zA-Z_][a-zA-Z0-9_]*(?:\.[a-zA-Z_][a-zA-Z0-9_]*)*)
    """,
    re.VERBOSE,
)

# Submodules of ``local_scribe`` that are intentionally addressed by
# the ``python -m local_scribe <subcommand>`` entry point rather than
# being importable on their own as dotted names. The ``local_scribe``
# package's ``__main__.py`` dispatches to ``asr``, ``inspector``,
# ``egress-proxy``, ``config show|verify``, ``start``, etc. -- those
# are CLI verbs, not module paths.
_LOCAL_SCRIBE_CLI_VERBS = {
    "asr", "inspector", "egress-proxy", "config", "start", "stop",
    "status", "doctor",
}

# Top-level pip-installed modules we use via ``-m`` at the bash level.
# Currently just ``pip`` (for ``$VENV_PY -m pip install``).
_DASH_M_THIRD_PARTY_OK = {"pip"}


def _extract_dash_m_modules(run_sh: Path) -> Iterable[tuple[int, str, str]]:
    """Yield ``(line_number, raw_line, module)`` for every
    ``<python_invocation> -m <module>`` reference in actual code (not
    inside whole-line bash comments).

    We deliberately do NOT try to parse inline comments — the regex
    captures only well-formed invocations with a quoted python path
    or the ``$VENV_PY`` macro, so inline-comment false matches would
    have to also look like a real invocation, which is unlikely
    enough to ignore.
    """
    for lineno, line in enumerate(run_sh.read_text().splitlines(), start=1):
        # Skip whole-line bash comments; an operator should be free
        # to document a now-renamed module path (``# was: python -m
        # egress_proxy``) without tripping the static check.
        if line.lstrip().startswith("#"):
            continue
        for m in _DASH_M_RE.finditer(line):
            yield lineno, line.strip(), m.group("module")


class RunShPythonDashMResolveTests(unittest.TestCase):
    """Every ``python -m <module>`` (or ``$VENV_PY -m <module>``)
    invocation in run.sh must resolve to a real Python module.

    This catches the 2026-05-11 ``char_settings_writer`` regression:
    the bash-level invocation referenced the pre-reorg flat module
    name, ``bash -n run.sh`` was happy, the heredoc-import test was
    happy, and the operator only saw the error at bootstrap stage 9
    after typing Touch ID for the bearer token.
    """

    def test_run_sh_has_dash_m_invocations(self) -> None:
        """Sanity: if we accidentally break the regex and find ZERO
        ``-m`` invocations, the next test would vacuously pass. Pin
        the floor."""
        found = list(_extract_dash_m_modules(_RUN_SH))
        self.assertGreater(
            len(found), 10,
            msg=f"unexpectedly few ``-m`` invocations in run.sh "
                f"({len(found)} found) — did the regex break?",
        )

    def test_every_dash_m_module_resolves(self) -> None:
        """For each ``-m <module>`` invocation: either the first
        argument is a recognised ``local_scribe <verb>`` CLI form
        (dispatched via local_scribe/__main__.py), or the module
        must import cleanly via importlib.
        """
        unresolved: list[str] = []
        for lineno, raw, mod in _extract_dash_m_modules(_RUN_SH):
            if mod in _DASH_M_THIRD_PARTY_OK:
                continue

            # ``-m local_scribe`` (alone or followed by a CLI verb) is
            # the package's CLI entry. We can import the top-level
            # package; the verb is parsed at the bash layer.
            if mod == "local_scribe":
                try:
                    importlib.import_module("local_scribe")
                except Exception as e:
                    unresolved.append(
                        f"run.sh:{lineno}: {raw}\n"
                        f"  -> import_module('local_scribe') failed: "
                        f"{type(e).__name__}: {e}"
                    )
                continue

            # Everything else must be a dotted ``local_scribe.X.Y``
            # path that imports cleanly. Anything else (e.g. a bare
            # ``char_settings_writer``) is the regression class we
            # ship this test to prevent.
            if not mod.startswith("local_scribe."):
                unresolved.append(
                    f"run.sh:{lineno}: {raw}\n"
                    f"  -> ``-m {mod}``: not allowed at the bash level — "
                    f"must be ``local_scribe.<subpackage>.<module>`` "
                    "(the flat-module layout was retired during the "
                    "2026-05-11 reorg; ``-m char_settings_writer`` was "
                    "the original regression that motivated this test)."
                )
                continue
            try:
                importlib.import_module(mod)
            except Exception as e:
                unresolved.append(
                    f"run.sh:{lineno}: {raw}\n"
                    f"  -> import_module({mod!r}) failed: "
                    f"{type(e).__name__}: {e}"
                )
        self.assertEqual(
            unresolved,
            [],
            msg="\n\n".join(unresolved),
        )


if __name__ == "__main__":
    unittest.main()
