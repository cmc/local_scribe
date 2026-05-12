"""Tests for the SVG terminal renderer used by the README demo assets.

This file pins the renderer's *contract*, not its byte output: byte
pinning happens one level up in ``test_bootstrap_demo.py`` and
``test_ui_mockups.py``. Here we assert the invariants that any
future refactor of ``tools/svg_term.py`` must preserve:

* The palette has every color name the generators use, and every
  entry is a 7-character ``#rrggbb`` string.
* ``visible_len`` strips ``[color]...[/]`` markup correctly.
* ``render_animated`` raises when a recording overflows its window
  (we MUST split rather than scroll — see the renderer's
  module-level docstring for the rationale).
* Output is valid XML + parseable as an SVG.
* Output is deterministic: rendering the same recording twice in
  the same process produces byte-identical strings.
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path
from xml.etree import ElementTree as ET

# tools/svg_term.py isn't a package, so add tools/ to sys.path the
# same way the generators do.
TOOLS = Path(__file__).resolve().parents[2] / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from svg_term import (  # noqa: E402
    COLORS,
    Frame,
    Recording,
    render_animated,
    render_static,
    visible_len,
)


SVG_NS = "http://www.w3.org/2000/svg"


class PaletteContractTests(unittest.TestCase):
    """Every color name the generators reference must exist + look
    like a hex color. Catches typos in ``COLORS`` and accidental
    deletions on refactor."""

    REQUIRED_KEYS = (
        # default surfaces
        "fg", "bg", "dim", "border",
        # ANSI-ish accents the recordings use
        "red", "green", "yellow", "blue", "magenta", "cyan", "bold",
        # window chrome
        "title_bar", "title_dot_close", "title_dot_min", "title_dot_max",
    )

    def test_required_keys_present(self) -> None:
        for k in self.REQUIRED_KEYS:
            with self.subTest(key=k):
                self.assertIn(k, COLORS, f"missing palette key: {k!r}")

    def test_all_values_are_hex(self) -> None:
        for k, v in COLORS.items():
            with self.subTest(key=k):
                self.assertRegex(
                    v, r"^#[0-9a-fA-F]{6}$",
                    f"palette[{k!r}] = {v!r} is not a 6-digit hex color",
                )


class VisibleLenTests(unittest.TestCase):
    """Width-after-stripping-markup is the function tests rely on
    when asserting "no line in the bootstrap demo overflows the
    chosen column count". It MUST count exactly the visible chars."""

    def test_plain_text(self) -> None:
        self.assertEqual(visible_len("hello world"), len("hello world"))

    def test_strips_color_markup(self) -> None:
        self.assertEqual(visible_len("[green]✓[/] ok"), len("✓ ok"))

    def test_multiple_colors(self) -> None:
        self.assertEqual(
            visible_len("[red]a[/] [green]b[/] [blue]c[/]"),
            len("a b c"),
        )

    def test_unknown_color_still_visible(self) -> None:
        # An unknown color name falls back to "fg" rather than
        # raising — by design (see _parse_markup docstring). The
        # SEGMENT must still count toward the visible length.
        self.assertEqual(visible_len("[not_a_color]x[/]"), 1)

    def test_empty_string(self) -> None:
        self.assertEqual(visible_len(""), 0)


class RenderAnimatedContractTests(unittest.TestCase):

    def test_rejects_overflowing_recording(self) -> None:
        """Recordings that exceed ``rows`` MUST raise — splitting
        across multiple SVGs is the contract, not scrolling."""
        rec = Recording(
            frames=[Frame(0.1, f"line {i}") for i in range(10)],
            rows=4,
            cols=20,
        )
        with self.assertRaises(ValueError) as cm:
            render_animated(rec)
        self.assertIn("rec.rows", str(cm.exception))

    def test_output_is_valid_xml(self) -> None:
        rec = Recording(
            frames=[Frame(0.1, "hello"), Frame(0.2, "world")],
            rows=4,
            cols=20,
        )
        svg = render_animated(rec)
        # Must parse without raising.
        root = ET.fromstring(svg)
        self.assertEqual(root.tag, f"{{{SVG_NS}}}svg")

    def test_deterministic(self) -> None:
        rec = Recording(
            frames=[Frame(0.3, "[green]✓[/] hello"), Frame(0.7, "world")],
            rows=4, cols=20,
        )
        self.assertEqual(render_animated(rec), render_animated(rec))

    def test_text_content_present(self) -> None:
        """Each frame's visible text must appear somewhere in the
        rendered SVG (covers the "did we forget to emit any tspan?"
        regression class)."""
        rec = Recording(
            frames=[Frame(0.1, "first line"), Frame(0.2, "[green]second[/] line")],
            rows=4, cols=20,
        )
        svg = render_animated(rec)
        self.assertIn("first line", svg)
        self.assertIn("second", svg)

    def test_begin_offsets_are_cumulative(self) -> None:
        """Each frame's begin time must equal the SUM of its and
        every prior frame's ``delay``. Catches the "off by one"
        regression where delay i was applied to frame i-1."""
        rec = Recording(
            frames=[Frame(1.0, "a"), Frame(2.0, "b"), Frame(0.5, "c")],
            rows=4, cols=4,
        )
        svg = render_animated(rec)
        begins = re.findall(r'begin="([\d.]+)s"', svg)
        # Filter out the cursor animation's nested begin (it uses
        # ``repeatCount`` not ``begin``), so all begins here come
        # from the per-line <set> elements.
        self.assertEqual(begins, ["1.000", "3.000", "3.500"])

    def test_cursor_blinker_present(self) -> None:
        """The decorative blinking cursor is part of the asciinema
        aesthetic — render_animated must always emit it so the SVG
        looks alive even after the last frame fades in."""
        rec = Recording(frames=[Frame(0.1, "x")], rows=4, cols=4)
        svg = render_animated(rec)
        self.assertIn('repeatCount="indefinite"', svg)


class RenderStaticContractTests(unittest.TestCase):

    def test_output_is_valid_xml(self) -> None:
        svg = render_static(["a", "b", "c"], cols=10)
        ET.fromstring(svg)   # must not raise

    def test_no_animation_elements(self) -> None:
        """Static renders must contain ZERO ``<set>`` / ``<animate>``
        / ``<animateTransform>`` elements — the contract is "this
        is a still image". The README depends on this so the
        ``pipeline_ready.svg`` is visible from t=0 on page load."""
        svg = render_static(["foo"], cols=10)
        for tag in ("<set ", "<animate ", "<animateTransform "):
            with self.subTest(tag=tag):
                self.assertNotIn(tag, svg)

    def test_text_content_present(self) -> None:
        svg = render_static(["hello", "[green]world[/]"], cols=20)
        self.assertIn("hello", svg)
        self.assertIn("world", svg)

    def test_default_rows_matches_line_count(self) -> None:
        """``rows=None`` defaults to ``len(lines)`` so the SVG has
        no trailing blank space. Caller-supplied ``rows`` overrides."""
        svg_tight = render_static(["a", "b"], cols=10)
        svg_padded = render_static(["a", "b"], cols=10, rows=20)
        self.assertLess(len(svg_tight), len(svg_padded))
        # Both contain the same text content.
        self.assertIn(">a<", svg_tight)
        self.assertIn(">b<", svg_tight)
        self.assertIn(">a<", svg_padded)
        self.assertIn(">b<", svg_padded)

    def test_deterministic(self) -> None:
        lines = ["[green]✓[/] a", "[red]✗[/] b"]
        self.assertEqual(
            render_static(lines, cols=10),
            render_static(lines, cols=10),
        )

    def test_color_markup_renders_as_fill(self) -> None:
        """``[green]X[/]`` must produce a tspan with the green palette
        color, not the default fg. Regression guard against any
        future refactor that breaks the markup parser."""
        svg = render_static(["[green]X[/]"], cols=10)
        green = COLORS["green"]
        self.assertIn(f'fill="{green}"', svg)


if __name__ == "__main__":   # pragma: no cover
    unittest.main()
