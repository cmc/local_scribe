"""Generate ``docs/img/bootstrap_demo.svg`` — an animated SVG
"recording" of a fresh-clone bootstrap, suitable for embedding in
the README via plain ``![alt](docs/img/bootstrap_demo.svg)``.

Why a hand-rolled mock instead of a real recording?
---------------------------------------------------

* **Determinism.** A real bootstrap takes 10–30 minutes of wall
  time (model downloads + Touch ID prompts + YubiKey taps) and
  produces different timing on every run. We can't put that in CI.
* **Hardware independence.** asciinema-style recordings of the
  real flow assume a YubiKey is plugged into the recording host
  and the model downloads pass — neither holds for an open-source
  contributor running CI.
* **Reviewability.** The mock IS the spec: a reviewer can read
  this file top-to-bottom and verify every banner against
  ``run.sh::cmd_bootstrap``. The test in
  ``tests/docs/test_bootstrap_demo.py`` then asserts that every
  stage banner the generator emits also exists verbatim in
  ``run.sh``, so the README can't drift from reality.

Maintenance
-----------

If you add or rename a bootstrap stage in ``run.sh::cmd_bootstrap``:

1.  Update :data:`FRAMES` below so the demo reflects the new flow.
2.  Re-run ``./venv/bin/python -m tools.make_bootstrap_demo``.
3.  ``git add docs/img/bootstrap_demo.svg`` so reviewers see the
    new render in the diff.

The pinning test will fail the build until step 1 lands.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow ``python tools/make_bootstrap_demo.py`` (script mode) AND
# ``python -m tools.make_bootstrap_demo`` (module mode) to both find
# ``svg_term`` without needing a package install.
HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from svg_term import Frame, Recording, render_animated  # noqa: E402


# All times are in seconds. The whole recording targets ~28 s end to
# end — short enough that a README viewer doesn't lose patience, long
# enough that each stage banner is readable. We deliberately use
# round-ish numbers (no 0.137-second pauses) so a reviewer skimming
# the diff can predict the on-screen result.

FRAMES: list[Frame] = [
    # ---- shell prompt + invocation ----------------------------------
    # Delay=0 so the SVG isn't visually empty during the initial
    # 100ms of page load. Subsequent frames cascade off this anchor.
    Frame(0.0, "[blue]operator@MacBook[/] [dim]whisper_server[/] [green]%[/] [bold]./run.sh bootstrap[/]"),
    Frame(0.4, ""),
    Frame(0.3, "[bold]bootstrap[/] — first-time setup for a fresh clone"),
    Frame(0.6, ""),

    # ---- Stage 1: python venv + deps --------------------------------
    Frame(0.5, "[bold](1/10) python venv + pip deps + Touch ID helper[/]"),
    Frame(0.6, "  creating venv at [blue]./venv[/] ..."),
    Frame(0.5, "  installing pinned dependencies (44 packages) ..."),
    Frame(0.6, "  building [blue]bin/touchid_keychain[/] (Swift helper) ..."),
    Frame(0.3, "  [green]✓ venv + deps ready[/]"),

    # ---- Stage 2: age + age-plugin-yubikey + ykman ------------------
    Frame(0.5, "[bold](2/10) key-management tools (age, age-plugin-yubikey, ykman)[/]"),
    Frame(0.6, "  [green]✓ age 1.2.0 present[/]   (≥ 1.1.0 required for YubiKey recipients)"),
    Frame(0.4, "  [green]✓ age-plugin-yubikey 0.5.0 present[/]"),
    Frame(0.4, "  [green]✓ ykman 5.5.1 present[/]"),

    # ---- Stage 3: master key (Touch ID + YubiKey) -------------------
    Frame(0.6, "[bold](3/10) master key (Option C split-key: Touch ID ⊕ YubiKey)[/]"),
    Frame(0.4, "  Generating a 256-bit master key, splitting it via XOR, then:"),
    Frame(0.3, "    [cyan]kc_half[/]  →  macOS Keychain (Touch ID-gated)"),
    Frame(0.3, "    [cyan]yk_half[/]  →  age-encrypted file on disk (YubiKey-decryptable)"),
    Frame(0.6, ""),

    # ---- The "watch your hardware" banners --------------------------
    Frame(0.4, "[yellow]━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━[/]"),
    Frame(0.3, "[yellow]TOUCH ID PROMPT INCOMING[/]"),
    Frame(0.2, "  Look at your Mac — the Touch ID sheet will appear now."),
    Frame(1.5, "[yellow]━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━[/]"),
    Frame(0.3, "  [green]✓ Touch ID accepted[/]   (kc_half written)"),

    Frame(0.5, "[red]━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━[/]"),
    Frame(0.3, "[red]TAP YOUR YUBIKEY NOW[/]"),
    Frame(0.2, "  The LED on your YubiKey is now flashing — tap the gold disc."),
    Frame(1.8, "[red]━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━[/]"),
    Frame(0.3, "  [green]✓ YubiKey tap registered[/]   (yk_half written, age-encrypted)"),
]

# ---- Stage 4 onwards splits into a SECOND recording to keep each
# frame inside the fixed window. The README embeds both in sequence.

FRAMES_PART_2: list[Frame] = [
    Frame(0.6, "[bold](4/10) encrypted vault (AES-256 sparse bundle keyed off the master)[/]"),
    Frame(0.5, "  Creating [blue]~/Library/Application Support/local_scribe-vault.sparsebundle[/]"),
    Frame(0.5, "  (AES-256 sparse — starts at ~4 MB, grows on demand up to 100 GB)"),
    Frame(0.6, "  [green]✓ vault created + mounted[/]   (hdiutil passphrase HKDF-derived from master)"),
    Frame(0.4, "  [green]✓ Char data dir relocated INTO vault[/]   (symlink in place)"),

    Frame(0.6, "[bold](5/10) parakeet ASR weights[/]"),
    Frame(0.5, "  fetching [blue]mlx-community/parakeet-tdt-0.6b-v3[/] ..."),
    Frame(0.6, "  [green]✓ 1.21 GB cached[/]   ([blue]~/.cache/huggingface/...[/])"),

    Frame(0.5, "[bold](6/10) sherpa-onnx diarization models[/]"),
    Frame(0.5, "  [green]✓ 3 models present[/]   (speaker segmentation + embedding + VAD)"),

    Frame(0.5, "[bold](7/10) ~/.config/local_scribe/config.json[/]"),
    Frame(0.4, "  [green]✓ config written[/]"),

    Frame(0.5, "[bold](8/10) LM Studio.app + Qwen LLM[/]"),
    Frame(0.5, "  [green]✓ LM Studio.app already installed[/]   (skipping download)"),
    Frame(0.5, "  loading [blue]qwen3-30b-a3b-instruct-2507[/] into RAM (Metal, 32k ctx) ..."),
    Frame(0.7, "  [green]✓ model loaded[/]"),

    Frame(0.5, "[bold](9/10) Char.app — install + auto-config[/]"),
    Frame(0.5, "  Char.app already installed (matches pinned 0.0.42)."),
    Frame(0.5, "  [green]✓ Char settings patched[/]   (ASR endpoint + bearer + analytics off)"),

    Frame(0.5, "[bold](10/10) per-Char outbound firewall — sandbox + egress proxy[/]"),
    Frame(0.5, "  [green]✓ sandbox profile written[/]   ([blue]~/.config/local_scribe/char_sandbox.sb[/])"),
    Frame(0.5, "  [green]✓ sandbox profile validates cleanly[/]"),

    Frame(0.7, ""),
    Frame(0.4, "[bold]bootstrap complete[/] — next: [bold]./run.sh start[/]"),
]


def build_recordings() -> tuple[Recording, Recording]:
    """Build the two recording halves. Returned as a tuple so the
    test suite can pin both timings + frame counts directly."""
    rec_a = Recording(
        frames=list(FRAMES),
        cols=86,
        rows=len(FRAMES),
        title="local_scribe • bootstrap (1/2)",
    )
    rec_b = Recording(
        frames=list(FRAMES_PART_2),
        cols=86,
        rows=len(FRAMES_PART_2),
        title="local_scribe • bootstrap (2/2)",
    )
    return rec_a, rec_b


def build_pipeline_ready_still() -> list[str]:
    """The final "what the operator sees after ./run.sh start"
    snapshot. STATIC SVG — no animation — so the README has a clean
    "this is what success looks like" image right next to the
    animated demo."""
    return [
        "[blue]operator@MacBook[/] [dim]whisper_server[/] [green]%[/] [bold]./run.sh start[/]",
        "",
        "[bold]starting transcription pipeline[/]",
        "  [green]✓[/] SIP fully enabled",
        "  [green]✓[/] master key present (Option C: Touch ID ⊕ YubiKey)",
        "  [green]✓[/] script integrity check passes",
        "  [green]✓[/] signed-config check passes (pinned.json + char_baseline.json)",
        "  [green]✓[/] Char data lives inside the encrypted vault",
        "  [green]✓[/] vault is mounted",
        "  [green]✓[/] Char binary integrity matches recorded baseline",
        "",
        "[yellow]━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━[/]",
        "[bold]Authentication warmup[/]",
        "  One Touch ID modal + one YubiKey tap, covering ASR + inspector.",
        "  [green]✓ tokens derived[/] — starting services",
        "[yellow]━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━[/]",
        "",
        "[green]✓[/] LM Studio API up on :1234",
        "[green]✓[/] qwen3-30b-a3b-instruct-2507 loaded",
        "[green]✓[/] asr up on http://127.0.0.1:8000",
        "[green]✓[/] inspector up on http://127.0.0.1:8001",
        "[green]✓[/] egress-proxy up on http://127.0.0.1:8889",
        "",
        "[bold]──── pipeline ready ────[/]",
        "  ASR server (Parakeet TDT v3) : [green]http://127.0.0.1:8000[/]",
        "  LM Studio API (Qwen3-30B)    : [green]http://127.0.0.1:1234[/]",
        "  Inspector (web UI)           : [green]http://127.0.0.1:8001/[/]",
        "  First-time browser auth      : [blue]http://127.0.0.1:8001/auth?token=…[/]",
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

    rec_a, rec_b = build_recordings()
    (out_dir / "bootstrap_demo_part1.svg").write_text(
        render_animated(rec_a), encoding="utf-8",
    )
    (out_dir / "bootstrap_demo_part2.svg").write_text(
        render_animated(rec_b), encoding="utf-8",
    )

    # Static "success" still.
    from svg_term import render_static  # noqa: PLC0415 — keeps demo-only import out of test scope
    still_lines = build_pipeline_ready_still()
    (out_dir / "pipeline_ready.svg").write_text(
        render_static(still_lines, cols=86, title="local_scribe • pipeline ready"),
        encoding="utf-8",
    )

    print(f"wrote {out_dir}/bootstrap_demo_part1.svg")
    print(f"wrote {out_dir}/bootstrap_demo_part2.svg")
    print(f"wrote {out_dir}/pipeline_ready.svg")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
