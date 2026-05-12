"""Tests for ``tools/make_ui_mockups.py``.

Three layers of asserts:

1. **Generator hygiene.** No row in any mockup overflows its column
   budget; the CLI runs cleanly; the output is deterministic.

2. **On-disk pin.** A fresh generator run must produce byte-
   identical output to the SVGs already committed in ``docs/img/``,
   so the README cannot reference a stale render.

3. **UI-truthfulness pin.** Every panel heading and every named
   row label in the mockups must ALSO appear in the live
   inspector source (``inspector_server.py`` template + the
   ``/api/security/audit`` JSON shape). If the real UI renames a
   panel, this test fails — so the README claim "this is what the
   inspector looks like" stays honest.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from xml.etree import ElementTree as ET


REPO = Path(__file__).resolve().parents[2]
TOOLS = REPO / "tools"
DOCS_IMG = REPO / "docs" / "img"

if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from svg_term import visible_len  # noqa: E402
import make_ui_mockups as gen     # noqa: E402


SVG_NS = "http://www.w3.org/2000/svg"
INSPECTOR_PATH = REPO / "local_scribe" / "inspector" / "inspector_server.py"


def _strip_markup(s: str) -> str:
    return re.sub(r"\[/?[a-z_]*\]", "", s).rstrip()


def _inspector_source() -> str:
    return INSPECTOR_PATH.read_text(encoding="utf-8")


# ---- generator hygiene -----------------------------------------------


class RowWidthTests(unittest.TestCase):
    """No row may exceed 86 cols — that's the column budget the
    SVG is sized at, and ``svg_term.CHAR_W`` allocates pixels off
    that count. Wider rows visibly overflow the right edge."""

    MAX_COLS = 86

    def _all_tabs(self) -> list[tuple[str, list[str]]]:
        return [
            ("sessions", gen.SESSIONS_TAB),
            ("char_audit", gen.CHAR_AUDIT_TAB),
            ("char_audit_warning", gen.CHAR_AUDIT_TAB_WARNING),
        ]

    def test_every_row_fits_the_column_budget(self) -> None:
        for name, rows in self._all_tabs():
            for i, line in enumerate(rows):
                with self.subTest(tab=name, row=i):
                    w = visible_len(line)
                    self.assertLessEqual(
                        w, self.MAX_COLS,
                        f"{name} row {i} is {w} cols (>{self.MAX_COLS}): "
                        f"{line!r}",
                    )


# ---- on-disk pin -----------------------------------------------------


class OnDiskAssetPinningTests(unittest.TestCase):

    EXPECTED_FILES = (
        "inspector_sessions.svg",
        "inspector_char_audit.svg",
        "inspector_char_audit_warning.svg",
    )

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="ui_mockups_test_"))
        rc = gen.main(["--out-dir", str(cls.tmp)])
        if rc != 0:
            raise RuntimeError(f"generator returned {rc}")

    def test_files_match_committed_disk_versions(self) -> None:
        for name in self.EXPECTED_FILES:
            with self.subTest(file=name):
                on_disk = (DOCS_IMG / name).read_bytes()
                regenerated = (self.tmp / name).read_bytes()
                self.assertEqual(
                    on_disk, regenerated,
                    f"{name} on disk differs from a fresh generator run. "
                    "Re-run: ./venv/bin/python tools/make_ui_mockups.py "
                    "and commit the updated SVG.",
                )

    def test_files_parse_as_svg(self) -> None:
        for name in self.EXPECTED_FILES:
            with self.subTest(file=name):
                root = ET.parse(DOCS_IMG / name).getroot()
                self.assertEqual(root.tag, f"{{{SVG_NS}}}svg")


# ---- UI-truthfulness pin --------------------------------------------


class UiTruthPinningTests(unittest.TestCase):
    """The mockup is meant to "show what the inspector looks like".
    Every named panel + every prominent label must therefore exist
    in the live inspector source. We check the strings against
    ``inspector_server.py`` because that file is the canonical
    template-and-API source of truth for the inspector UI.

    The check is intentionally lenient: substrings (not equality).
    The inspector source may wrap a label in HTML or split it
    across attributes; we only need to know the *concept* still
    exists with the same name in the live code.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.inspector_src = _inspector_source()

    def test_inspector_has_required_tabs(self) -> None:
        """All four tabs the mockup names must be wired up in the
        real ``<nav>`` block of inspector_server.py."""
        for tab in ("Sessions", "Config", "Char audit", "About"):
            with self.subTest(tab=tab):
                self.assertIn(
                    tab, self.inspector_src,
                    f"tab {tab!r} is in the mockup but not in "
                    "inspector_server.py — UI mock has drifted",
                )

    def test_char_audit_panel_headings_exist(self) -> None:
        """Both top-level panel headings on the Char audit mock must
        exist as ``<h2>`` headings in the live inspector template.
        These are the ANCHOR strings the README's mock points at —
        if they change in the real UI the mock is no longer a
        screenshot of anything real."""
        for heading in ("Char audit", "Security verification"):
            with self.subTest(heading=heading):
                self.assertIn(
                    heading, self.inspector_src,
                    f"panel heading {heading!r} is in the mockup but "
                    "missing from inspector_server.py — UI mock has drifted",
                )

    def test_security_layer_labels_match_audit_view(self) -> None:
        """Every layer-label row in the mock's "Security verification"
        table MUST be a real label emitted by audit_view.snapshot().
        This is the strongest pinning test: it asserts the mock isn't
        showing made-up layer names."""
        audit_view_src = (
            REPO / "local_scribe" / "security" / "audit_view.py"
        ).read_text(encoding="utf-8")
        # Pull labels from the mock by matching the prefix on each
        # row of the security table — they all live between the
        # leading "  " indent and the next run of 2+ spaces.
        labels: list[str] = []
        in_security_block = False
        for raw in gen.CHAR_AUDIT_TAB:
            line = _strip_markup(raw)
            if "Security verification" in line:
                in_security_block = True
                continue
            if not in_security_block:
                continue
            if not line.strip():
                # blank line at end of security block ends it
                in_security_block = False
                continue
            if line.lstrip().startswith(("layer ", "─", "(")):
                continue
            m = re.match(r"\s+([A-Z][^\s].*?)\s{2,}", line)
            if m:
                labels.append(m.group(1).rstrip())

        self.assertGreaterEqual(
            len(labels), 5,
            "couldn't extract any layer labels from the security mock — "
            "regex probably needs updating",
        )
        for label in labels:
            with self.subTest(label=label):
                self.assertIn(
                    label, audit_view_src,
                    f"security layer label {label!r} appears in the mock "
                    "but no Layer with that label exists in "
                    "audit_view.py — the mock is showing a phantom layer",
                )

    def test_char_audit_keys_match_real_audit_emitter(self) -> None:
        """Every dotted ``key=...`` value the Char audit mock shows
        must correspond to a real ``key=`` constructor call in
        char_audit.py. ``char_audit.py`` builds some keys via
        f-string templates (``f"ai.intelligence.{provider}.base_url"``),
        so the assertion accepts EITHER a literal key match OR a
        regex over the file that handles f-string placeholders.
        Same idea as the security-layer pin but for the upper table.
        """
        char_audit_src = (
            REPO / "local_scribe" / "char" / "char_audit.py"
        ).read_text(encoding="utf-8")

        def _key_is_emitted(key: str) -> bool:
            # Literal match path -- fast common case.
            if f'key="{key}"' in char_audit_src:
                return True
            # f-string path: convert every dotted segment into a
            # regex token that matches a literal OR a ``{...}``
            # placeholder. e.g. ``ai.intelligence.openai.base_url``
            # is matched by ``ai\.intelligence\.(openai|\{[^}]+\})\.base_url``.
            tokens = key.split(".")
            pattern_parts = []
            for tok in tokens:
                pattern_parts.append(
                    rf"(?:{re.escape(tok)}|\{{[^}}]+\}})"
                )
            pat = r'key=f?"' + r"\.".join(pattern_parts) + r'"'
            return re.search(pat, char_audit_src) is not None

        for raw in gen.CHAR_AUDIT_TAB:
            line = _strip_markup(raw)
            m = re.match(r"\s+([a-z_][a-z0-9_.]*\.[a-z0-9_.]+)\s", line)
            if not m:
                continue
            key = m.group(1)
            with self.subTest(key=key):
                self.assertTrue(
                    _key_is_emitted(key),
                    f"Char-audit key {key!r} appears in the mock but no "
                    "AuditCheck (literal or f-string) with that key "
                    "exists in char_audit.py — fix the mock or wire "
                    "up the check.",
                )

    def test_security_audit_endpoint_url_matches(self) -> None:
        """The mock includes ``GET /api/security/audit``. The real
        API surface must still expose that exact route."""
        self.assertIn(
            "/api/security/audit", self.inspector_src,
            "the mock advertises GET /api/security/audit but the "
            "inspector no longer exposes that route — fix one or the other",
        )

    def test_warning_variant_mentions_real_scanner_concept(self) -> None:
        """The "plaintext leftovers" finding in the warning variant
        must correspond to a real scanner in vault.py. If we ever
        rename the scanner method, both ends should be updated."""
        vault_src = (
            REPO / "local_scribe" / "security" / "vault.py"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "find_plaintext_char_data_copies", vault_src,
            "vault.find_plaintext_char_data_copies() no longer exists — "
            "the warning-variant mock advertises a UI driven by it; "
            "either restore the scanner or remove the mock variant",
        )
        warning_text = " ".join(_strip_markup(l) for l in gen.CHAR_AUDIT_TAB_WARNING)
        self.assertIn(
            "plaintext", warning_text.lower(),
            "warning variant must surface the word 'plaintext' so the "
            "screenshot caption makes sense in the README",
        )


# ---- CLI -------------------------------------------------------------


class GeneratorCliTests(unittest.TestCase):
    """Smoke-test the script invocation contributors copy from the
    README."""

    def test_runs_with_default_args(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ui_mockups_cli_") as td:
            r = subprocess.run(
                [
                    sys.executable,
                    str(TOOLS / "make_ui_mockups.py"),
                    "--out-dir", td,
                ],
                capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(
                r.returncode, 0,
                f"CLI exited {r.returncode}; stderr={r.stderr!r}",
            )
            for name in OnDiskAssetPinningTests.EXPECTED_FILES:
                self.assertTrue(
                    (Path(td) / name).exists(),
                    f"{name} not produced under --out-dir {td}",
                )


if __name__ == "__main__":   # pragma: no cover
    unittest.main()
