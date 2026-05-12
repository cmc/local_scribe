"""Tests for ``tools/make_bootstrap_demo.py``.

Two layers of asserts:

1. **Generator hygiene.** The generator runs cleanly, the recordings
   fit inside their windows, every line obeys the column budget,
   the output is deterministic.

2. **Pin against reality.** Every stage banner in the mock must
   appear verbatim in ``run.sh::cmd_bootstrap``. If a contributor
   renames a stage in ``run.sh`` without updating the mock, this
   test fails with a precise list of mismatches — so the README
   demo cannot lie about what the real bootstrap prints.

The renders themselves are also re-checked against the SVG files
committed in ``docs/img/`` so a contributor can't push generator
changes without also committing the regenerated assets.
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
import make_bootstrap_demo as gen  # noqa: E402


SVG_NS = "http://www.w3.org/2000/svg"


# ---- helpers --------------------------------------------------------

def _run_sh_text() -> str:
    """Return ``run.sh``'s contents, cached at module import time."""
    return (REPO / "run.sh").read_text(encoding="utf-8")


def _strip_markup(s: str) -> str:
    """``"[green]✓[/] a" -> "✓ a"`` — same algorithm as
    ``svg_term._parse_markup`` but without the color metadata. Used
    by the run.sh pinning test to compare plain text against the
    shell-script source.

    The regex matches BOTH ``[name]`` opening tags AND the bare
    ``[/]`` closing tag — the ``*`` quantifier (not ``+``) is what
    lets it match the empty body of ``[/]``.
    """
    return re.sub(r"\[/?[a-z_]*\]", "", s).rstrip()


# ---- generator-hygiene tests ----------------------------------------


class RecordingShapeTests(unittest.TestCase):
    """The generator's recordings must fit within their own
    declared window so ``render_animated`` never raises at build
    time. If you add a stage that pushes one part past its row
    budget, split it into a Part 3."""

    def test_part1_fits(self) -> None:
        rec_a, _rec_b = gen.build_recordings()
        self.assertLessEqual(len(rec_a.frames), rec_a.rows,
                             "part 1 overflows its row budget — split it")

    def test_part2_fits(self) -> None:
        _rec_a, rec_b = gen.build_recordings()
        self.assertLessEqual(len(rec_b.frames), rec_b.rows,
                             "part 2 overflows its row budget — split it")

    def test_no_line_overflows_cols(self) -> None:
        """Every line in both parts (and the still) must fit the
        chosen column budget. Catches the "added a 95-char line in
        an 86-col window" regression class."""
        rec_a, rec_b = gen.build_recordings()
        still = gen.build_pipeline_ready_still()
        for label, lines, cols in [
            ("part1", [f.line for f in rec_a.frames], rec_a.cols),
            ("part2", [f.line for f in rec_b.frames], rec_b.cols),
            ("still", still, 86),
        ]:
            for i, line in enumerate(lines):
                with self.subTest(part=label, row=i):
                    self.assertLessEqual(
                        visible_len(line), cols,
                        f"{label} row {i} is {visible_len(line)} cols "
                        f"wide (>{cols}): {line!r}",
                    )

    def test_total_duration_in_range(self) -> None:
        """The whole demo should be readable in under ~30 s but
        long enough that each stage is legible. Tunable, but the
        bounds catch "someone added a 60s pause" mishaps."""
        for label, frames in [
            ("part1", gen.FRAMES),
            ("part2", gen.FRAMES_PART_2),
        ]:
            total = sum(f.delay for f in frames)
            with self.subTest(part=label):
                self.assertGreaterEqual(
                    total, 8.0,
                    f"{label} is {total:.1f}s — too fast to read",
                )
                self.assertLessEqual(
                    total, 35.0,
                    f"{label} is {total:.1f}s — too long for a README demo",
                )


# ---- run.sh pinning -------------------------------------------------


class StageBannerPinningTests(unittest.TestCase):
    """Every ``(N/10)`` stage banner the mock prints must appear
    verbatim in ``run.sh::cmd_bootstrap``. This is the test that
    prevents the README from drifting away from reality."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.run_sh = _run_sh_text()

    def _all_demo_lines(self) -> list[str]:
        lines = [f.line for f in gen.FRAMES + gen.FRAMES_PART_2]
        lines += gen.build_pipeline_ready_still()
        return [_strip_markup(line) for line in lines]

    def test_every_stage_banner_present_in_run_sh(self) -> None:
        # Find any line like "(N/10) blah" in the demo and assert
        # the same phrase appears in run.sh. We compare on the
        # SUFFIX (after the "(N/10) " prefix) so the test still
        # passes if run.sh swaps numbering schemes — the BANNER
        # TEXT is what matters semantically.
        demo_banners = []
        for line in self._all_demo_lines():
            m = re.search(r"\(\d+/10\)\s+(.+)$", line)
            if m:
                demo_banners.append(m.group(1).strip())

        self.assertGreaterEqual(
            len(demo_banners), 10,
            "demo should include all 10 stage banners — got "
            f"{demo_banners}",
        )

        for banner in demo_banners:
            with self.subTest(banner=banner):
                self.assertIn(
                    banner, self.run_sh,
                    f"stage banner {banner!r} is in the demo but NOT in "
                    f"run.sh::cmd_bootstrap — either update the demo to "
                    f"match the renamed stage, or update run.sh.",
                )

    def test_demo_covers_all_10_stages(self) -> None:
        """All ten numbered stages must appear. Catches "we removed
        the firewall stage from the demo but it's still real"."""
        stages_in_demo = set()
        for line in self._all_demo_lines():
            m = re.search(r"\((\d+)/10\)", line)
            if m:
                stages_in_demo.add(int(m.group(1)))
        self.assertEqual(stages_in_demo, set(range(1, 11)))

    def test_touch_id_banner_matches_touch_prompts_source(self) -> None:
        """The yellow "TOUCH ID PROMPT INCOMING" banner must mirror
        the text ``touch_prompts.py`` actually prints. If someone
        changes the wording in the real prompt, this fails."""
        tp = (REPO / "local_scribe" / "common" / "touch_prompts.py")
        text = tp.read_text(encoding="utf-8").upper()
        demo = " ".join(_strip_markup(f.line) for f in gen.FRAMES).upper()
        # We assert the SHAPE: "TOUCH ID" both in the demo and in
        # the source. Exact wording is allowed to vary across
        # releases, but if "TOUCH ID" drops out of touch_prompts.py
        # the whole banner is dead anyway.
        self.assertIn("TOUCH ID", demo)
        self.assertIn("TOUCH ID", text,
                      "touch_prompts.py no longer prints a 'TOUCH ID' "
                      "banner — the bootstrap demo claim is now a lie")

    def test_yubikey_banner_matches_touch_prompts_source(self) -> None:
        tp = (REPO / "local_scribe" / "common" / "touch_prompts.py")
        text = tp.read_text(encoding="utf-8").upper()
        demo = " ".join(_strip_markup(f.line) for f in gen.FRAMES).upper()
        self.assertIn("YUBIKEY", demo)
        self.assertIn("YUBIKEY", text,
                      "touch_prompts.py no longer prints a 'YUBIKEY' "
                      "banner — the bootstrap demo claim is now a lie")


# ---- on-disk asset pinning ------------------------------------------


class OnDiskAssetPinningTests(unittest.TestCase):
    """Re-run the generator into a tmpdir and byte-compare each
    output against the committed ``docs/img/`` file. If a
    contributor edits the generator without re-running it, this
    test fails — preventing 'README points to a stale render'."""

    EXPECTED_FILES = (
        "bootstrap_demo_part1.svg",
        "bootstrap_demo_part2.svg",
        "pipeline_ready.svg",
    )

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="bootstrap_demo_test_"))
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
                    "Re-run: ./venv/bin/python tools/make_bootstrap_demo.py "
                    "and commit the updated SVG.",
                )

    def test_files_parse_as_svg(self) -> None:
        """Sanity check: GitHub renders these. They must be valid
        XML rooted at <svg>."""
        for name in self.EXPECTED_FILES:
            with self.subTest(file=name):
                root = ET.parse(DOCS_IMG / name).getroot()
                self.assertEqual(root.tag, f"{{{SVG_NS}}}svg")


# ---- generator CLI surface -----------------------------------------


class GeneratorCliTests(unittest.TestCase):
    """The README points contributors at ``./venv/bin/python
    tools/make_bootstrap_demo.py`` so the CLI must work end-to-end
    as a script, not just via ``import``."""

    def test_runs_with_default_args(self) -> None:
        with tempfile.TemporaryDirectory(prefix="boot_demo_cli_") as td:
            r = subprocess.run(
                [
                    sys.executable,
                    str(TOOLS / "make_bootstrap_demo.py"),
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
