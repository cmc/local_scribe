"""Generate static SVG mockups of the inspector web UI's tabs for
the README. Each mockup is a stylised "screenshot" rendered in the
same SVG terminal aesthetic as ``bootstrap_demo.svg`` — not a true
HTML screenshot.

Why mockups instead of real Playwright screenshots?
---------------------------------------------------

* **Zero runtime dependencies.** A real screenshot pipeline needs a
  headless browser (Playwright pulls down ~300 MB of Chromium). The
  README is a documentation artefact; we don't want a browser in
  every contributor's setup just to regenerate it.
* **Reproducibility on CI without macOS state.** Real screenshots
  would need a live session DB, a configured master key, and a
  working vault. Mockups avoid that whole class of test flakiness.
* **Stylistic consistency.** The animated bootstrap demo is already
  an SVG-terminal aesthetic; rendering the inspector tabs in the
  same visual language makes the README feel cohesive instead of
  jumping between "shell capture" and "browser screenshot".

The mockups still need to MATCH the real UI's information design.
The pinning test in ``tests/docs/test_ui_mockups.py`` asserts that
every panel heading + every column header in this file also appears
in the live ``inspector_server.py`` template, so the README can't
drift away from what the actual UI shows.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from svg_term import render_static  # noqa: E402


# ---- Sessions tab ----------------------------------------------------
#
# The real inspector renders sessions in a flex grid with timestamp /
# duration / speakers / preview. We approximate the layout with
# fixed-width columns so the SVG looks "table-like" without needing
# real HTML.

SESSIONS_TAB: list[str] = [
    "[bold]local_scribe[/] [dim]inspector[/]                              [dim]http://127.0.0.1:8001/[/]",
    "",
    "  [bold]Sessions[/]   Config   Char audit   About",
    "  [dim]────────[/]",
    "",
    "  [bold]when                duration  speakers  preview[/]",
    "  [dim]────────────────────────────────────────────────────────────────────[/]",
    "  2026-05-11 14:22    23:14      3        \"…and that ties back into Q2 forecasting…\"",
    "  2026-05-11 11:05    08:47      2        \"Quick sync on the design review timeline.\"",
    "  2026-05-10 16:30    1:04:11    4        \"Engineering allhands — security retro.\"",
    "  2026-05-10 09:15    14:02      2        \"1:1 — career growth + Q3 priorities.\"",
    "  2026-05-09 13:45    32:18      5        \"Customer interview: agentic transcripts.\"",
    "",
    "  [green]●[/] 5 sessions      [dim]total: 2 h 22 m 32 s     2.4 GB encrypted on disk[/]",
]


# ---- Char audit tab --------------------------------------------------
#
# This mirrors the live UI's two-section layout in inspector_server.py:
# the upper "Char audit" panel renders the table populated by
# /api/char/audit (key + status + current + expected columns), and the
# lower "Security verification" panel renders the table populated by
# /api/security/audit (layer label + status + summary columns). The
# row LABELS used here are real strings emitted by char_audit.py and
# audit_view.py respectively — the pinning test in
# tests/docs/test_ui_mockups.py keeps the two in lockstep.

CHAR_AUDIT_TAB: list[str] = [
    "[bold]local_scribe[/] [dim]inspector[/]                              [dim]http://127.0.0.1:8001/[/]",
    "",
    "  Sessions   Config   [bold]Char audit[/]   About",
    "  [dim]──────────[/]",
    "",
    "  [bold]Char audit[/]      ([blue]GET /api/char/audit[/])",
    "  [bold]key                                  status  current[/]",
    "  [dim]────────────────────────────────────────────────────────────────────[/]",
    "  ai.stt.openai.base_url               [green]OK[/]      [dim]http://127.0.0.1:8000[/]",
    "  ai.stt.openai.api_key                [green]OK[/]      [cyan]4d842b…[/]  matches ASR",
    "  ai.current_stt_provider              [green]OK[/]      openai",
    "  ai.intelligence.openai.base_url      [green]OK[/]      [dim]http://127.0.0.1:1234[/]",
    "  store.analytics.Disabled             [green]OK[/]      [green]true[/]   (PostHog kill)",
    "  firewall.block_list                  [green]OK[/]      egress-proxy mode, [green]active[/]",
    "",
    "  [bold]Security verification[/]      ([blue]GET /api/security/audit[/])",
    "  [bold]layer                            status  summary[/]",
    "  [dim]────────────────────────────────────────────────────────────────────[/]",
    "  SIP enforcement                  [green]OK[/]      fully enabled",
    "  Option C split-key               [green]OK[/]      kc_half + yk_half present",
    "  Encrypted vault (at-rest)        [green]OK[/]      mounted, 0 plaintext copies",
    "  Char binary integrity            [green]OK[/]      CDHash matches baseline",
    "  Char settings enforcement        [green]OK[/]      6/6 keys match pinned",
    "  Signed pinned config             [green]OK[/]      pinned.json + char_baseline.json",
    "  Script integrity                 [green]OK[/]      72 files match git-pinned hashes",
    "  Egress firewall (per-Char)       [green]OK[/]      proxy up, 18-host catalog",
    "  Secret-scan pre-commit hook      [green]OK[/]      installed",
    "  Disaster-recovery backup         [green]OK[/]      age-encrypted on disk",
]


# ---- Char audit tab — variant with one warning ----------------------
#
# We also generate a "what a problem looks like" variant so the
# README can illustrate the diagnostic path, not just the steady
# state. This variant flags the most common real-world finding
# (stale plaintext backups outside the vault), driven by
# vault.find_plaintext_char_data_copies() under the hood.

CHAR_AUDIT_TAB_WARNING: list[str] = [
    "[bold]local_scribe[/] [dim]inspector[/]                              [dim]http://127.0.0.1:8001/[/]",
    "",
    "  Sessions   Config   [bold]Char audit[/]   About",
    "  [dim]──────────[/]",
    "",
    "  [bold]Security verification[/]      ([blue]GET /api/security/audit[/])",
    "  [bold]layer                            status  summary[/]",
    "  [dim]────────────────────────────────────────────────────────────────────[/]",
    "  SIP enforcement                  [green]OK[/]      fully enabled",
    "  Option C split-key               [green]OK[/]      kc_half + yk_half present",
    "  Encrypted vault (at-rest)        [yellow]WARN[/]    mounted, [yellow]3 plaintext copies[/] outside vault",
    "  Char binary integrity            [green]OK[/]      CDHash matches baseline",
    "  Signed pinned config             [green]OK[/]      pinned.json + char_baseline.json",
    "  Script integrity                 [green]OK[/]      72 files match git-pinned hashes",
    "",
    "  [bold]Plaintext copies of Char data outside the vault[/]",
    "  [dim]────────────────────────────────────────────────────────────────────[/]",
    "  [yellow]●[/] ~/Library/.../hyprnote.pre_vault_backup.2026-05-08  2.5 GiB  6 sessions  [red][Delete][/]",
    "  [yellow]●[/] ~/local_scribe_pre_arch_backup/hyprnote              2.5 GiB  5 sessions  [red][Delete][/]",
    "  [dim]●[/] ~/.cache/local_scribe-demo/hyprnote             [dim]2.1 MiB[/]  [dim]demo seed (informational)[/]",
    "",
    "  [dim]Delete requires a typed-DELETE confirmation. The deleter refuses[/]",
    "  [dim]any path not currently in the scanner's output, so a stolen[/]",
    "  [dim]inspector bearer cannot ``rm -rf`` arbitrary directories.[/]",
]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument(
        "--out-dir",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "docs" / "img",
        help="Where to write the SVGs (default: <repo>/docs/img)",
    )
    args = p.parse_args(argv)
    out_dir: Path = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    targets = [
        ("inspector_sessions.svg",
         SESSIONS_TAB,
         "local_scribe inspector • Sessions"),
        ("inspector_char_audit.svg",
         CHAR_AUDIT_TAB,
         "local_scribe inspector • Char audit (all green)"),
        ("inspector_char_audit_warning.svg",
         CHAR_AUDIT_TAB_WARNING,
         "local_scribe inspector • Char audit (with plaintext leftovers)"),
    ]
    for filename, lines, title in targets:
        svg = render_static(lines, cols=86, title=title)
        (out_dir / filename).write_text(svg, encoding="utf-8")
        print(f"wrote {out_dir / filename}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
