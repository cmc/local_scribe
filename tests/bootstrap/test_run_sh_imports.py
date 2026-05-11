"""Static-analysis test: every Python ``import`` line in ``run.sh`` must
resolve against the actual codebase.

Why this exists
---------------

``run.sh`` embeds Python via ``$VENV_PY - <<'PY' ... PY`` heredocs (and
a few ``-c '...'`` blocks). When we reorganised the codebase into
``local_scribe/`` packages on 2026-05-11 several of these heredocs were
left with the OLD flat-module imports (e.g. ``from config import ...``).
``bash -n run.sh`` doesn't catch this — the Python only runs at the
appropriate bootstrap stage. Users hit the error mid-bootstrap when
``ensure_config_json`` (or another helper) fired hours into the flow.

This test prevents that class of regression by:

  1. Greping every ``^(from|import) <name>`` line out of run.sh.
  2. Filtering out stdlib + known third-party imports (huggingface_hub,
     faster_whisper, etc.).
  3. For each remaining import (which should all be ``local_scribe.*``
     subpackages), attempting ``importlib.import_module`` from the
     venv Python.
  4. Failing the test loudly if any import doesn't resolve, naming
     both the line in run.sh and the module that wouldn't import.

The test runs entirely in-process against this checkout — no subprocess,
no fakes, no PATH gymnastics. It is fast (< 1 s) and high-signal.
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


if __name__ == "__main__":
    unittest.main()
