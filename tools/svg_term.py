"""Minimal SVG terminal renderer.

Generates animated and static SVG "screenshots" of terminal-like
output, suitable for embedding in a GitHub README via plain
``![alt](docs/img/foo.svg)`` references.

GitHub's Markdown renderer:

* Serves SVG files as ``image/svg+xml`` — SMIL animations play.
* Strips inline ``<svg>`` from rendered HTML for security — so we
  MUST reference an external file, not inline.
* Does NOT run JavaScript or external CSS in those SVGs — every
  animation has to be SMIL ``<animate>`` / ``<set>`` driven.

Design constraints baked into this module
-----------------------------------------

* **Fixed-window layout, no scrolling.** Recordings are sized to
  fit inside ``rec.rows`` rows. If your script wants to demonstrate
  more output than that, split it into multiple recordings. The
  alternative — auto-scrolling the body up — is doable in SMIL but
  fragile across renderers (Safari + Chromium handle
  ``animateTransform`` differently when the body has its own
  per-element animations); the explicit-split model is sturdier
  and easier to test.

* **Compact inline markup, not ANSI parsing.** Lines are written
  as plain Python strings with ``[<color>]text[/]`` spans inside
  them. No nested colors, no terminal state to track. Tests pin
  every supported color name.

* **Deterministic output.** Same ``Recording`` always produces the
  same SVG bytes. This is what lets
  ``tests/docs/test_bootstrap_demo.py`` round-trip the asset.

Public API
----------

* :class:`Frame` / :class:`Recording` — the recording model.
* :func:`render_animated` — animated SVG ("asciicinema-style").
* :func:`render_static`   — static SVG (the "pipeline ready" still
  and the inspector UI mockups).
* :data:`COLORS` — name → ``#rrggbb`` palette.
* :func:`visible_len` — width of a markup-tagged line, for layout
  asserts.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from typing import Iterable


# ---- palette ---------------------------------------------------------

COLORS: dict[str, str] = {
    "fg": "#e6edf3",
    "bg": "#0d1117",
    "dim": "#8b949e",
    "red": "#ff7b72",
    "green": "#7ee787",
    "yellow": "#d29922",
    "blue": "#79c0ff",
    "magenta": "#d2a8ff",
    "cyan": "#a5d6ff",
    "bold": "#ffffff",
    "border": "#30363d",
    "title_bar": "#161b22",
    "title_dot_close": "#ff5f56",
    "title_dot_min":   "#ffbd2e",
    "title_dot_max":   "#27c93f",
}


FONT_FAMILY = (
    "ui-monospace, 'SF Mono', Menlo, Monaco, 'Cascadia Mono', monospace"
)
FONT_SIZE = 13
# CHAR_W is the WIDTH ALLOCATED per column when sizing the viewBox.
# The actual font's advance varies (SF Mono ≈ 7.5, Menlo ≈ 7.8,
# WebKit's monospace fallback can hit 8.3) so we pick a number near
# the wide end of the range to ensure no row overflows the right
# edge on any common platform. The "every line shorter than ``cols``"
# generator-side check is satisfied at 86 cols × 8.5 → 731px wide,
# which is well within the inline-image width budget of GitHub READMEs.
CHAR_W = 8.5
LINE_H = 17
TITLE_BAR_H = 28
PAD_X = 16
PAD_Y = 14


# ---- inline markup ---------------------------------------------------

_MARKUP_RE = re.compile(r"\[(?P<color>[a-z_]+)\](?P<body>.*?)\[/\]")


def _parse_markup(line: str) -> list[tuple[str, str]]:
    """Return ``[(color, segment), ...]`` covering ``line`` exactly.

    Plain spans (no markup) get color ``"fg"``. Unknown color names
    fall back to ``"fg"`` — we deliberately don't raise here so a
    typo in the generator script doesn't break the render. The
    test suite pins the full palette set so typos still get caught
    in CI.
    """
    out: list[tuple[str, str]] = []
    pos = 0
    for m in _MARKUP_RE.finditer(line):
        if m.start() > pos:
            out.append(("fg", line[pos:m.start()]))
        color = m.group("color")
        if color not in COLORS:
            color = "fg"
        out.append((color, m.group("body")))
        pos = m.end()
    if pos < len(line):
        out.append(("fg", line[pos:]))
    return out


def visible_len(line: str) -> int:
    """Width of ``line`` after stripping markup."""
    return sum(len(seg) for _, seg in _parse_markup(line))


# ---- frame model -----------------------------------------------------


@dataclass(frozen=True)
class Frame:
    """One delta in an animated recording. ``delay`` is the wait
    AFTER the previous frame appears before this one fades in.
    ``line`` may use ``[color]...[/]`` markup."""
    delay: float
    line: str = ""


@dataclass
class Recording:
    """A whole recording. The frame list must fit inside ``rows`` —
    a check is enforced by :func:`render_animated`."""
    frames: list[Frame] = field(default_factory=list)
    cols: int = 84
    rows: int = 26
    title: str = "local_scribe"


# ---- common chrome --------------------------------------------------


def _svg_header(cols: int, rows: int, title: str) -> tuple[str, int, int]:
    width = int(PAD_X * 2 + CHAR_W * cols)
    height = int(TITLE_BAR_H + PAD_Y * 2 + LINE_H * rows)
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="0 0 {width} {height}" '
        f'width="{width}" height="{height}" '
        f'font-family="{FONT_FAMILY}" font-size="{FONT_SIZE}px" '
        f'role="img" aria-label="{html.escape(title)}">',
        f'<rect x="0" y="0" width="{width}" height="{height}" rx="8" ry="8" '
        f'fill="{COLORS["bg"]}" stroke="{COLORS["border"]}" stroke-width="1"/>',
        f'<rect x="0" y="0" width="{width}" height="{TITLE_BAR_H}" rx="8" ry="8" '
        f'fill="{COLORS["title_bar"]}"/>',
        f'<rect x="0" y="{TITLE_BAR_H - 6}" width="{width}" height="6" '
        f'fill="{COLORS["title_bar"]}"/>',
        f'<circle cx="14" cy="{TITLE_BAR_H // 2}" r="6" fill="{COLORS["title_dot_close"]}"/>',
        f'<circle cx="32" cy="{TITLE_BAR_H // 2}" r="6" fill="{COLORS["title_dot_min"]}"/>',
        f'<circle cx="50" cy="{TITLE_BAR_H // 2}" r="6" fill="{COLORS["title_dot_max"]}"/>',
        f'<text x="{width // 2}" y="{TITLE_BAR_H // 2 + 4}" '
        f'text-anchor="middle" fill="{COLORS["dim"]}" font-size="11px">'
        f'{html.escape(title)}</text>',
    ]
    return "\n".join(parts), width, height


def _render_line_tspans(line: str, x_origin: float) -> str:
    """Render one logical row as nested ``<tspan>`` elements.

    Only the FIRST tspan carries an ``x`` attribute (anchored at
    ``x_origin``). Subsequent tspans omit ``x`` so they concatenate
    naturally with the font's actual character advance. This is the
    only layout strategy that produces consistent spacing across
    different monospace renderers — computing absolute x positions
    off an assumed ``CHAR_W`` works on the host that wrote the
    constant but drifts on every other font.

    We also set ``xml:space="preserve"`` so embedded run-on spaces
    (used for table-column alignment in the inspector mockups)
    don't get whitespace-collapsed by the SVG renderer.
    """
    out: list[str] = []
    first = True
    for color, segment in _parse_markup(line):
        if not segment:
            continue
        fill = COLORS.get(color, COLORS["fg"])
        x_attr = f' x="{x_origin:.1f}"' if first else ""
        out.append(
            f'<tspan{x_attr} xml:space="preserve" fill="{fill}">'
            f'{html.escape(segment)}'
            f'</tspan>'
        )
        first = False
    return "".join(out)


# ---- animated -------------------------------------------------------


def render_animated(rec: Recording) -> str:
    """Render ``rec`` as an animated SVG.

    Each frame fades in at its cumulative time offset and stays
    visible thereafter. The total length is sum of all
    ``frame.delay`` values; viewers replay by refreshing the page.
    SMIL ``begin`` is absolute and we deliberately don't try to
    loop (looping inside SMIL when each line has its own ``begin``
    is doable but produces brittle, hard-to-debug SVG).

    Raises ``ValueError`` if the frame list overflows ``rec.rows``
    — recordings should be split rather than scrolled.
    """
    if len(rec.frames) > rec.rows:
        raise ValueError(
            f"recording has {len(rec.frames)} frames but rec.rows="
            f"{rec.rows} — split into multiple recordings or grow rows"
        )

    header, _w, _h = _svg_header(rec.cols, rec.rows, rec.title)
    body_y0 = TITLE_BAR_H + PAD_Y + LINE_H - 4

    cum = 0.0
    rendered: list[str] = []
    for i, fr in enumerate(rec.frames):
        cum += fr.delay
        if not fr.line:
            continue   # blank rows leave space but emit no SVG
        spans = _render_line_tspans(fr.line, x_origin=PAD_X)
        y = body_y0 + i * LINE_H
        # ``set`` with begin=t locks opacity to 1 at exactly t;
        # we don't need a fade-in animation (SMIL ``set`` is the
        # cheaper, more deterministic primitive for "show this
        # at time T"). Initial opacity is 0 via the parent <g>.
        rendered.append(
            f'<g opacity="0">'
            f'<set attributeName="opacity" to="1" begin="{cum:.3f}s" fill="freeze"/>'
            f'<text y="{y}">{spans}</text>'
            f'</g>'
        )

    # Closing cursor block — a steady blinker in the bottom-left so
    # the recording looks alive even after the last frame.
    cursor_y = body_y0 + len(rec.frames) * LINE_H - LINE_H + 3
    cursor_x = PAD_X
    cursor_w = CHAR_W
    cursor_h = LINE_H - 3
    rendered.append(
        f'<g>'
        f'<rect x="{cursor_x:.1f}" y="{cursor_y:.1f}" '
        f'width="{cursor_w:.1f}" height="{cursor_h:.1f}" '
        f'fill="{COLORS["fg"]}">'
        f'<animate attributeName="opacity" '
        f'values="1;1;0;0" keyTimes="0;0.5;0.5;1" '
        f'dur="1s" repeatCount="indefinite"/>'
        f'</rect>'
        f'</g>'
    )

    return f"{header}\n" + "".join(rendered) + "\n</svg>\n"


# ---- static ----------------------------------------------------------


def render_static(lines: Iterable[str],
                  *,
                  cols: int = 84,
                  rows: int | None = None,
                  title: str = "local_scribe") -> str:
    """Render ``lines`` as a STATIC SVG (no animations).

    Used for the "pipeline ready" still + the inspector tab
    mockups. ``rows`` defaults to ``len(lines)`` (no trailing
    blank space). Pass a larger value for a fixed-height frame.
    """
    line_list = list(lines)
    if rows is None:
        rows = max(1, len(line_list))
    header, _w, _h = _svg_header(cols, rows, title)
    body_y0 = TITLE_BAR_H + PAD_Y + LINE_H - 4

    rendered: list[str] = []
    for i, line in enumerate(line_list):
        if i >= rows:
            break
        spans = _render_line_tspans(line, x_origin=PAD_X)
        if spans:
            y = body_y0 + i * LINE_H
            rendered.append(f'<text y="{y}">{spans}</text>')

    return f"{header}\n{''.join(rendered)}\n</svg>\n"


__all__ = [
    "COLORS",
    "Frame",
    "Recording",
    "render_animated",
    "render_static",
    "visible_len",
]
