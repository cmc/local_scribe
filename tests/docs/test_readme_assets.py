"""Test that the README's "What provisioning looks like" section
references real, on-disk SVG + raster assets, and that nothing in
``docs/img/`` is orphaned.

The README is a large file. Without this test it's easy for:

* A contributor to add a new SVG via ``tools/make_*.py`` but forget
  to embed it in the README → the README stays out of date.
* A contributor to remove an SVG from ``docs/img/`` but leave the
  reference in the README → broken image on GitHub.
* A contributor to typo a filename in the README → broken image on
  GitHub, found only after merge.

We close all three for both vector (SVG) and raster (JPG/PNG) assets.
The raster set is currently just the hero illustration; we keep its
own filename pin so we'll fail loudly if the hero is renamed or moved.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path
from xml.etree import ElementTree as ET


REPO = Path(__file__).resolve().parents[2]
README = REPO / "README.md"
DOCS_IMG = REPO / "docs" / "img"

# The exhaustive list of generator-produced SVGs the README is
# supposed to reference. If the generators add a new one and the
# README is supposed to show it, add it here.
EXPECTED_REFERENCED_SVGS = {
    # produced by tools/make_bootstrap_demo.py
    "bootstrap_demo_part1.svg",
    "bootstrap_demo_part2.svg",
    "pipeline_ready.svg",
    # produced by tools/make_ui_mockups.py
    "inspector_sessions.svg",
    "inspector_char_audit.svg",
    "inspector_char_audit_warning.svg",
}

# Hand-curated raster assets (illustrative, not auto-generated) the
# README is supposed to reference. Kept separate from the SVG set so
# the orphan-detection logic can still cover BOTH classes.
EXPECTED_REFERENCED_RASTERS = {
    "bootstrap_hero.jpg",
}


SVG_NS = "http://www.w3.org/2000/svg"


def _readme() -> str:
    return README.read_text(encoding="utf-8")


def _referenced_assets(suffix_pattern: str) -> set[str]:
    """All ``docs/img/*.<ext>`` filenames the README references.

    ``suffix_pattern`` is a regex alternation of allowed extensions,
    e.g. ``"svg"`` or ``"(?:jpg|png)"``. We don't reuse a single
    catch-all regex because the test wants to distinguish vector vs.
    raster references in its error messages.
    """
    pattern = re.compile(rf"docs/img/([A-Za-z0-9_./-]+\.{suffix_pattern})")
    return set(pattern.findall(_readme()))


def _referenced_svgs() -> set[str]:
    return _referenced_assets("svg")


def _referenced_rasters() -> set[str]:
    return _referenced_assets(r"(?:jpg|jpeg|png|gif|webp)")


# ---- README ↔ disk consistency --------------------------------------


class ReadmeAssetReferencesTests(unittest.TestCase):

    def test_every_expected_svg_is_referenced(self) -> None:
        """Every generator output we EXPECT the README to surface
        actually IS referenced in the README. Catches "added the
        file, forgot to embed it"."""
        referenced = _referenced_svgs()
        missing = sorted(EXPECTED_REFERENCED_SVGS - referenced)
        self.assertEqual(
            missing, [],
            "README is missing references to:\n  " + "\n  ".join(missing) +
            "\nEither embed them in the README or update "
            "EXPECTED_REFERENCED_SVGS in this test.",
        )

    def test_every_expected_raster_is_referenced(self) -> None:
        """Same idea, for hand-curated raster (JPG/PNG) assets like
        the hero illustration. Kept separate from the SVG check so a
        missing hero produces a distinct, actionable error."""
        referenced = _referenced_rasters()
        missing = sorted(EXPECTED_REFERENCED_RASTERS - referenced)
        self.assertEqual(
            missing, [],
            "README is missing references to raster assets:\n  "
            + "\n  ".join(missing) +
            "\nEither embed them in the README or update "
            "EXPECTED_REFERENCED_RASTERS in this test.",
        )

    def test_every_referenced_svg_exists_on_disk(self) -> None:
        """Every ``docs/img/*.svg`` path in the README must resolve
        to a real file. Catches typos + deletions."""
        missing_on_disk: list[str] = []
        for name in _referenced_svgs():
            if not (DOCS_IMG / name).exists():
                missing_on_disk.append(name)
        self.assertEqual(
            missing_on_disk, [],
            "README references SVG files that don't exist on disk:\n  "
            + "\n  ".join(missing_on_disk),
        )

    def test_every_referenced_raster_exists_on_disk(self) -> None:
        missing_on_disk: list[str] = []
        for name in _referenced_rasters():
            if not (DOCS_IMG / name).exists():
                missing_on_disk.append(name)
        self.assertEqual(
            missing_on_disk, [],
            "README references raster files that don't exist on disk:\n  "
            + "\n  ".join(missing_on_disk),
        )

    def test_no_orphaned_committed_assets(self) -> None:
        """Nothing in ``docs/img/`` is committed but unreferenced —
        keeps the repo from accumulating dead images. Covers BOTH
        vector + raster orphans so a stray ``hero_v2.png`` doesn't
        slip in alongside the live ``bootstrap_hero.jpg``."""
        if not DOCS_IMG.exists():
            self.skipTest("docs/img/ doesn't exist yet")
        all_on_disk = {
            p.name for p in DOCS_IMG.iterdir()
            if p.is_file() and p.suffix.lower() in {
                ".svg", ".jpg", ".jpeg", ".png", ".gif", ".webp",
            }
        }
        referenced = _referenced_svgs() | _referenced_rasters()
        orphans = sorted(all_on_disk - referenced)
        self.assertEqual(
            orphans, [],
            "docs/img/ contains image assets the README doesn't reference:\n  "
            + "\n  ".join(orphans) +
            "\nEither delete them or embed them in the README.",
        )

    def test_every_referenced_svg_is_valid(self) -> None:
        """Each referenced SVG must parse as XML rooted at <svg>.
        Detects corruption or accidental truncation pre-commit."""
        for name in _referenced_svgs():
            with self.subTest(svg=name):
                root = ET.parse(DOCS_IMG / name).getroot()
                self.assertEqual(root.tag, f"{{{SVG_NS}}}svg")

    def test_every_referenced_raster_is_non_empty(self) -> None:
        """Raster integrity is harder to verify without Pillow, but
        we can still catch the 0-byte-file regression class — and
        confirm the magic-byte header roughly matches the declared
        extension so a renamed-but-corrupt file is caught."""
        # First two bytes (or so) of each common raster format:
        magic = {
            "jpg":  b"\xff\xd8\xff",
            "jpeg": b"\xff\xd8\xff",
            "png":  b"\x89PNG\r\n\x1a\n",
            "gif":  b"GIF8",
            "webp": b"RIFF",
        }
        for name in _referenced_rasters():
            ext = name.rsplit(".", 1)[-1].lower()
            data = (DOCS_IMG / name).read_bytes()
            with self.subTest(raster=name):
                self.assertGreater(
                    len(data), 0, f"{name} is 0 bytes — corrupt or empty",
                )
                if ext in magic:
                    self.assertTrue(
                        data.startswith(magic[ext]),
                        f"{name} doesn't look like a {ext.upper()} file "
                        f"(first bytes: {data[:8]!r})",
                    )


# ---- README copy quality ---------------------------------------------


class ReadmeSectionCopyTests(unittest.TestCase):
    """Small smoke checks on the README copy itself so the "What
    provisioning looks like" section stays anchored to the actual
    feature set."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.text = _readme()

    def test_section_heading_present(self) -> None:
        self.assertIn(
            "## What provisioning looks like",
            self.text,
            "the 'What provisioning looks like end-to-end' section "
            "appears to have been removed — was that intentional?",
        )

    def test_section_references_both_generators(self) -> None:
        """The section copy points contributors at both generator
        scripts so they know how to regenerate the SVGs. If those
        paths get renamed, update both ends."""
        self.assertIn("tools/make_bootstrap_demo.py", self.text)
        self.assertIn("tools/make_ui_mockups.py", self.text)

    def test_section_references_pinning_tests(self) -> None:
        """The section claims the SVGs are "pinned by the test
        suite". The test paths it advertises must actually exist."""
        for path_str in (
            "tests/docs/test_bootstrap_demo.py",
            "tests/docs/test_ui_mockups.py",
        ):
            with self.subTest(path=path_str):
                self.assertIn(path_str, self.text)
                self.assertTrue(
                    (REPO / path_str).exists(),
                    f"README points at {path_str} but the file is missing",
                )


if __name__ == "__main__":   # pragma: no cover
    unittest.main()
