"""local_scribe outbound firewall manager.

Single-source-of-truth catalog of every hostname Char.app and the
broader local-scribe stack should *never* talk to in a privacy-first
local install, plus the machinery to enforce that catalog through
**two** mechanisms, picked via :class:`Mode`:

  ``Mode.PROCESS_PROXY`` *(default since v2)* — per-Char enforcement.
     ``./run.sh char launch`` does TWO things in tandem:

       1. Wraps Char in a ``sandbox-exec`` profile (see
          :mod:`char_sandbox`) that allows everything *except*
          network-outbound, and re-allows network-outbound only to
          ``127.0.0.1`` and ``::1``. Char's only route off-host is
          therefore through the local proxy.
       2. Sets ``HTTPS_PROXY=http://127.0.0.1:8889`` and
          ``HTTP_PROXY=...`` (plus ``NO_PROXY=127.0.0.1,localhost``)
          in Char's env. The :mod:`egress_proxy` listens on that
          port and refuses ``CONNECT`` requests for any catalog
          hostname.

     The sandbox layer means a hypothetical Char build that ignored
     ``HTTPS_PROXY`` *still* couldn't bypass us — its only network
     path is the loopback proxy. Other apps on the machine are
     completely unaffected. The trade-off: Char must be launched
     via the wrapper (Dock / Spotlight launches bypass both layers);
     doctor flags this if it detects Char running without our
     sandbox lineage.

     macOS has no native "block this hostname for this binary only"
     primitive — Network Extension is the only built-in answer, and
     it requires an Apple-granted entitlement we can't ship from an
     open-source repo. ``pf`` filters per-user, not per-app. So we
     compose the two primitives Apple *does* give us (sandbox-exec
     + ``HTTPS_PROXY``) into a per-Char filter.

  ``Mode.SYSTEM_HOSTS`` *(legacy / opt-in)* — machine-wide /etc/hosts
     block. Every process on the machine is affected, including any
     unrelated app that wants to use the listed providers. We keep
     this mode available for users who want belt-and-braces or who
     never run other AI-API-using apps on the same Mac.

     Trade-off: an attacker who's already running as root on the box
     can also edit ``/etc/hosts``. That's accepted in our threat
     model (``CHAR_REVIEW.md § Threat model``) — root is out of scope.

Both modes share the same :data:`BLOCK_CATALOG`. The :func:`is_blocked`
function is the single decision point used by both
:class:`egress_proxy.ConnectProxy` (process mode) and the hosts-file
generator (system mode).

When using ``Mode.SYSTEM_HOSTS`` we use ``0.0.0.0`` for the IPv4 sink
and ``::`` for the IPv6 sink (NOT ``127.0.0.1`` or ``::1``) so blocked
attempts fail-fast with ``ECONNREFUSED`` instead of looping back to
whatever happens to be listening on loopback.

The block list is categorised so the operator can opt in/out of
groups:

  ``telemetry``  – Sentry, PostHog, Tauri auto-updater (Char has no
                   in-app toggle for these; documented in
                   ``CHAR_REVIEW.md``). On by default.
  ``providers``  – external STT/LLM/TTS APIs Char ships provider
                   plugins for (OpenAI, Deepgram, Anthropic, Mistral,
                   ...). On by default because the whole point of
                   ``local_scribe`` is that none of these get used —
                   blocking them is fail-safe.
  ``char_cloud`` – Char's own integrations backend (``api.char.com``)
                   plus the GitHub OAuth callback Char goes through.
                   These only fire when the user actively connects an
                   integration. Off by default so users who *do* want
                   calendar sync don't have to re-edit their config.

The catalog lives in ``BLOCK_CATALOG``. Adding a new host = one entry
+ one test. The unit tests in ``tests/test_firewall.py`` exercise the
rendering / parsing / diff round-trip without touching the real
``/etc/hosts``; the proxy is tested in ``tests/test_egress_proxy.py``.
"""

from __future__ import annotations

import enum
import os
import shutil
import socket
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional


# ---------------------------------------------------------------------------
# Block-list catalogue
# ---------------------------------------------------------------------------
#
# Add a new host here:
#   1. Pick the right category (telemetry / providers / char_cloud).
#   2. Add the hostname (and any aliases) as one ``Entry``.
#   3. Add a ``reason`` short enough to render in ``./run.sh firewall list``.
#   4. Update CHAR_REVIEW.md if the host wasn't in the egress catalog.
#
# ``hostnames`` is a tuple so the catalogue is hashable + iterable in a
# stable order. The first entry is the canonical name; the rest are
# the wildcards we expand because /etc/hosts doesn't support glob
# patterns. See ``_expand_wildcards()`` if you need to add a new
# wildcard parent.


@dataclass(frozen=True)
class Entry:
    """One blockable hostname (or alias group)."""
    hostname: str
    reason: str
    category: str  # "telemetry" | "providers" | "char_cloud"


BLOCK_CATALOG: tuple[Entry, ...] = (
    # ---- Telemetry (no in-app toggle in Char) ----
    Entry("o4506190168522752.ingest.us.sentry.io",
          "Char's Sentry DSN host (panic + tracing uploads)", "telemetry"),
    Entry("browser.sentry-cdn.com",
          "Sentry browser SDK CDN (the Tauri WebView Sentry init)",
          "telemetry"),
    Entry("us.i.posthog.com",
          "Char's PostHog analytics ingest", "telemetry"),
    Entry("eu.i.posthog.com",
          "PostHog EU ingest (referenced as fallback in the binary)",
          "telemetry"),
    Entry("desktop2.hyprnote.com",
          "Char's Tauri auto-update poller (proxies to Scarf)",
          "telemetry"),
    Entry("gateway.scarf.sh",
          "Scarf download analytics (where the updater poll lands)",
          "telemetry"),

    # ---- External STT / LLM / TTS providers ----
    #
    # Char ships provider plugins for these; configure-char keeps the
    # *current* provider on our loopback shim, but a future settings
    # change or an unrelated app reusing Char's keychain entries would
    # silently re-point at the upstream. Block by default = fail-safe.
    #
    Entry("api.openai.com",
          "OpenAI STT + Chat API (Char's default Whisper/GPT provider)",
          "providers"),
    Entry("api.deepgram.com",
          "Deepgram STT API (Char's default Custom-provider URL)",
          "providers"),
    Entry("api.assemblyai.com",   "AssemblyAI STT API", "providers"),
    Entry("api.gladia.io",        "Gladia STT API",     "providers"),
    Entry("api.granola.ai",       "Granola STT API",    "providers"),
    Entry("api.soniox.com",       "Soniox STT API",     "providers"),
    Entry("api.aquavoice.com",    "Aquavoice STT API",  "providers"),
    Entry("api.elevenlabs.io",    "ElevenLabs TTS/STT", "providers"),
    Entry("api.fireworks.ai",     "Fireworks AI LLM",   "providers"),
    Entry("api.mistral.ai",       "Mistral LLM API",    "providers"),
    Entry("api.pyannote.ai",      "Pyannote.ai diarization API",
          "providers"),
    Entry("api.anthropic.com",
          "Anthropic Claude API (referenced for the LLM provider plugin)",
          "providers"),
    Entry("generativelanguage.googleapis.com",
          "Google Gemini API", "providers"),

    # ---- Char's hosted backend (calendars, integrations, cloud sync) ----
    #
    # Off-by-default category: users who deliberately want calendar
    # sync need to keep this reachable. ``./run.sh firewall enable``
    # without ``--strict`` leaves these alone.
    #
    Entry("api.char.com",
          "Char's hosted integrations backend (calendar OAuth, etc.)",
          "char_cloud"),
    Entry("cloudsync.sqlite.ai",
          "Referenced as a future SQLite cloud-sync provider",
          "char_cloud"),
)


# Categories that are enabled by default when the user runs
# ``./run.sh firewall enable`` without flags. ``char_cloud`` is
# *off* by default because blocking it breaks the calendar /
# integrations features for users who want them. The strict-mode flag
# adds it back in.
DEFAULT_ENABLED_CATEGORIES: frozenset[str] = frozenset({"telemetry", "providers"})
ALL_CATEGORIES: frozenset[str] = frozenset(e.category for e in BLOCK_CATALOG)


# ---------------------------------------------------------------------------
# Enforcement modes
# ---------------------------------------------------------------------------


class Mode(str, enum.Enum):
    """Where + how the catalog is enforced.

    String-valued for easy CLI / config-file plumbing — argparse can
    accept ``"process"`` or ``"system"`` and convert via ``Mode(...)``.
    The new default is ``PROCESS_PROXY``; existing installs that have
    /etc/hosts blocks already in place report as ``SYSTEM_HOSTS`` until
    the operator migrates explicitly.
    """

    #: Per-Char enforcement via the local CONNECT proxy (egress_proxy.py).
    #: Other apps on the machine are unaffected. This is the new default
    #: because most Macs run more than just Char and a machine-wide block
    #: silently breaks unrelated apps that legitimately call AI APIs.
    PROCESS_PROXY = "process"

    #: Machine-wide /etc/hosts blackhole. Affects every process. Kept
    #: for users who want defense-in-depth or who never run other apps
    #: that talk to the listed providers.
    SYSTEM_HOSTS = "system"


DEFAULT_MODE: Mode = Mode.PROCESS_PROXY

# Loopback port for the CONNECT proxy. Hard-coded here so both
# :mod:`egress_proxy` and the launcher wrapper agree on the value.
# Changing this is a breaking change for any user who hard-coded
# ``HTTPS_PROXY`` elsewhere; we bump the major version in such a case.
PROXY_BIND = "127.0.0.1"
PROXY_PORT = 8889


# ---------------------------------------------------------------------------
# Decision: is_blocked
# ---------------------------------------------------------------------------
#
# Used by:
#   * :class:`egress_proxy.ConnectProxy`  (mode = PROCESS_PROXY)
#   * The /etc/hosts renderer below       (mode = SYSTEM_HOSTS, via the
#                                          catalog filter rather than
#                                          hostname matching)
#
# Matching is **exact OR suffix**: a catalog entry for
# ``api.openai.com`` blocks ``api.openai.com`` but also any
# ``<subdomain>.api.openai.com``. This is deliberate — providers
# often shard onto regional subdomains, and we don't want a tiny
# rename to bypass us.
#
# Case-insensitive (DNS is case-insensitive). Empty hostname is
# treated as "not blocked" rather than crashing the proxy.


def is_blocked(
    hostname: str,
    *,
    categories: Iterable[str] = DEFAULT_ENABLED_CATEGORIES,
) -> Optional[Entry]:
    """Return the matching :class:`Entry` if ``hostname`` is in the
    block list for ``categories``, else ``None``.

    Matches the catalog entry's hostname **exactly** or as a parent
    domain (``a.b.api.openai.com`` matches the ``api.openai.com``
    entry). The result is the entry that matched, so callers can log
    the reason / category alongside the deny.
    """
    if not hostname:
        return None
    h = hostname.strip().lower().rstrip(".")
    if not h:
        return None
    cats = set(categories)
    for entry in BLOCK_CATALOG:
        if entry.category not in cats:
            continue
        base = entry.hostname.lower().rstrip(".")
        if h == base or h.endswith("." + base):
            return entry
    return None


# ---------------------------------------------------------------------------
# Hosts-file IO
# ---------------------------------------------------------------------------
#
# Markers chosen to be:
#   - unmistakable (the project name)
#   - bash-comment safe (lead with ``#``)
#   - asymmetric (>>>/<<<) so a corrupted ``/etc/hosts`` with only one
#     marker is easy to detect
#
# DO NOT change these strings after a release — installs in the wild
# match against the literal strings, and changing them would orphan
# old blocks.

BEGIN_MARKER = "# >>> local_scribe firewall (managed; do not edit) >>>"
END_MARKER   = "# <<< local_scribe firewall <<<"

DEFAULT_HOSTS_PATH = Path("/etc/hosts")


def _entries_for_categories(categories: Iterable[str]) -> list[Entry]:
    """Return the catalog filtered by category, preserving the
    catalog's order so the rendered block is deterministic across
    runs (good for diff-clean re-applies)."""
    cats = set(categories)
    unknown = cats - ALL_CATEGORIES
    if unknown:
        raise ValueError(f"unknown firewall categor{'y' if len(unknown)==1 else 'ies'}: "
                         f"{sorted(unknown)}; known: {sorted(ALL_CATEGORIES)}")
    return [e for e in BLOCK_CATALOG if e.category in cats]


def render_block(categories: Iterable[str] = DEFAULT_ENABLED_CATEGORIES) -> str:
    """Render the marker-delimited block of /etc/hosts entries for the
    requested categories. Output is byte-stable for a given input so
    re-running enable on the same machine produces a zero-diff write.

    Each host becomes two lines (v4 + v6 sink). We also write a
    one-line comment above each host with the human-readable reason
    so ``cat /etc/hosts`` is self-documenting.
    """
    entries = _entries_for_categories(categories)
    if not entries:
        # Empty block still emits the markers so we can detect an
        # opt-out-everything state vs. uninstalled.
        return f"{BEGIN_MARKER}\n# (no categories selected)\n{END_MARKER}\n"

    lines: list[str] = [
        BEGIN_MARKER,
        "# Generated by local_scribe — see SECURITY.md and ./run.sh firewall.",
        "# Remove with: ./run.sh firewall disable",
        f"# Categories: {', '.join(sorted({e.category for e in entries}))}",
        "#",
    ]
    last_category: Optional[str] = None
    for e in entries:
        if e.category != last_category:
            lines.append(f"# --- {e.category} ---")
            last_category = e.category
        lines.append(f"# {e.reason}")
        lines.append(f"0.0.0.0 {e.hostname}")
        lines.append(f"::      {e.hostname}")
    lines.append(END_MARKER)
    return "\n".join(lines) + "\n"


def _split_around_block(text: str) -> tuple[str, Optional[str], str]:
    """Return ``(before, block_or_None, after)``. The block string
    (if present) includes both marker lines and the trailing newline.

    Tolerant of: leading/trailing whitespace inside the markers, CRLF
    line endings, a missing end marker (treat the rest of the file as
    the block — defensive), and a missing begin marker (treat
    everything as ``before``).
    """
    begin_idx = text.find(BEGIN_MARKER)
    if begin_idx < 0:
        return text, None, ""
    end_idx = text.find(END_MARKER, begin_idx)
    if end_idx < 0:
        # Malformed: treat from begin to EOF as the block. The caller
        # rewrites it with a fresh block.
        return text[:begin_idx], text[begin_idx:], ""
    # Include the END_MARKER and its trailing newline (if any) in the
    # block slice.
    end_of_end = end_idx + len(END_MARKER)
    if end_of_end < len(text) and text[end_of_end] == "\n":
        end_of_end += 1
    return text[:begin_idx], text[begin_idx:end_of_end], text[end_of_end:]


def block_present(hosts_text: str) -> bool:
    """True if our marker-delimited block exists in *hosts_text*."""
    _, block, _ = _split_around_block(hosts_text)
    return block is not None


def parse_managed_hosts(hosts_text: str) -> list[str]:
    """Return the list of hostnames currently inside our managed
    block. Used by ``status`` to tell the user what's blocked.

    Skips comments and blanks; tolerates either ``0.0.0.0`` or ``::``
    in the address column.
    """
    _, block, _ = _split_around_block(hosts_text)
    if block is None:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for raw in block.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        # parts[0] is the sink address, parts[1:] are hostnames; we
        # emit only the first hostname per line (our generator only
        # ever writes one).
        host = parts[1]
        if host not in seen:
            seen.add(host)
            out.append(host)
    return out


def upsert_block(hosts_text: str,
                 categories: Iterable[str] = DEFAULT_ENABLED_CATEGORIES) -> str:
    """Insert or replace our block in *hosts_text*. Pure function — the
    caller is responsible for atomically writing the result with the
    right privileges. Always appends a trailing newline so re-applies
    are idempotent.
    """
    before, _existing, after = _split_around_block(hosts_text)
    if before and not before.endswith("\n"):
        before += "\n"
    block = render_block(categories)
    if after and not after.startswith("\n") and not before.endswith("\n"):
        block = "\n" + block
    return before + block + after


def remove_block(hosts_text: str) -> str:
    """Remove our marker-delimited block. Returns *hosts_text*
    unchanged if no block is present.
    """
    before, block, after = _split_around_block(hosts_text)
    if block is None:
        return hosts_text
    # Trim a single trailing newline from ``before`` if removing the
    # block would leave a double-blank line.
    if before.endswith("\n\n") and not after.startswith("\n"):
        before = before[:-1]
    return before + after


# ---------------------------------------------------------------------------
# Status reporting
# ---------------------------------------------------------------------------


@dataclass
class Status:
    installed: bool
    blocked_hostnames: list[str]
    coverage_by_category: dict[str, dict[str, int]]  # cat -> {"blocked": n, "expected": m}
    missing_by_category: dict[str, list[str]]        # cat -> [hostnames] expected but not blocked

    def to_dict(self) -> dict:
        return {
            "installed": self.installed,
            "blocked_hostnames": self.blocked_hostnames,
            "coverage_by_category": self.coverage_by_category,
            "missing_by_category": self.missing_by_category,
        }


def status(hosts_text: Optional[str] = None,
           *,
           expected_categories: Iterable[str] = DEFAULT_ENABLED_CATEGORIES,
           hosts_path: Path = DEFAULT_HOSTS_PATH) -> Status:
    """Inspect the current /etc/hosts and compare against an expected
    category set. ``hosts_text`` lets unit tests pass synthetic input
    without touching the real /etc/hosts.
    """
    if hosts_text is None:
        hosts_text = hosts_path.read_text() if hosts_path.is_file() else ""

    blocked = parse_managed_hosts(hosts_text)
    blocked_set = set(blocked)

    expected = _entries_for_categories(expected_categories)
    coverage: dict[str, dict[str, int]] = {}
    missing: dict[str, list[str]] = {}
    for cat in sorted(set(e.category for e in expected)):
        cat_hosts = [e.hostname for e in expected if e.category == cat]
        cat_blocked = [h for h in cat_hosts if h in blocked_set]
        coverage[cat] = {"blocked": len(cat_blocked), "expected": len(cat_hosts)}
        cat_missing = [h for h in cat_hosts if h not in blocked_set]
        if cat_missing:
            missing[cat] = cat_missing

    return Status(
        installed=block_present(hosts_text),
        blocked_hostnames=blocked,
        coverage_by_category=coverage,
        missing_by_category=missing,
    )


# ---------------------------------------------------------------------------
# Resolution probe (online verification)
# ---------------------------------------------------------------------------


@dataclass
class ProbeResult:
    hostname: str
    blocked: bool
    addresses: list[str]
    error: Optional[str] = None


def _resolve(hostname: str, *, timeout: float = 2.0) -> ProbeResult:
    """Resolve *hostname* via the system resolver. ``blocked`` is True
    iff every resolved address is in our sink set (0.0.0.0 / ::) OR
    the resolution fails outright. This corresponds to the runtime
    behaviour we want: connections fail-fast.
    """
    sinks = {"0.0.0.0", "::", "0:0:0:0:0:0:0:0"}
    try:
        socket.setdefaulttimeout(timeout)
        infos = socket.getaddrinfo(hostname, None)
    except OSError as exc:
        return ProbeResult(hostname=hostname, blocked=True,
                           addresses=[], error=str(exc))
    finally:
        socket.setdefaulttimeout(None)

    addrs = sorted({info[4][0] for info in infos})
    blocked = bool(addrs) and all(a in sinks for a in addrs)
    return ProbeResult(hostname=hostname, blocked=blocked, addresses=addrs)


def verify(hostnames: Optional[Iterable[str]] = None,
           *, timeout: float = 2.0) -> list[ProbeResult]:
    """DNS-probe each hostname and return per-host results. When
    *hostnames* is None we probe everything in the catalog.

    macOS aggressively caches DNS, so the caller is expected to call
    :func:`flush_dns_cache` after install/uninstall before invoking
    verify().
    """
    if hostnames is None:
        hostnames = [e.hostname for e in BLOCK_CATALOG]
    return [_resolve(h, timeout=timeout) for h in hostnames]


def flush_dns_cache() -> tuple[bool, str]:
    """Invoke the macOS DNS-cache flush dance. Requires sudo; returns
    (ok, message). Safe to call from non-mac CI -- returns
    ``(False, "not macOS")`` if the binaries aren't there.
    """
    if not shutil.which("dscacheutil") or not shutil.which("killall"):
        return False, "dscacheutil/killall not available (non-macOS?)"
    cmds = [
        ["sudo", "-n", "dscacheutil", "-flushcache"],
        ["sudo", "-n", "killall", "-HUP", "mDNSResponder"],
    ]
    for cmd in cmds:
        rc = subprocess.run(cmd, capture_output=True, text=True)
        if rc.returncode != 0:
            return False, f"{' '.join(cmd)} failed: {rc.stderr.strip() or rc.stdout.strip()}"
    return True, "DNS cache flushed"


# ---------------------------------------------------------------------------
# Privileged installer
# ---------------------------------------------------------------------------
#
# The actual write to /etc/hosts is gated behind sudo. We support two
# elevation paths:
#
#   * ``sudo``: caller is in a terminal. We invoke ``sudo bash -c ...``
#     which falls through to ``sudo``'s own interactive ``Password:``
#     prompt. We print an explanation banner to stderr first so the
#     user understands what they're authorising.
#   * ``osascript``: caller is non-interactive (e.g. bootstrap UI,
#     scripts piped via ``run.sh``). We invoke Apple's
#     ``do shell script ... with administrator privileges`` dialog,
#     **with a custom ``with prompt`` string** so the system password
#     dialog actually explains what this app is changing (without the
#     prompt the user sees only "osascript wants to make changes",
#     which is opaque and conditions users to type their password
#     without understanding the request).
#
# Both write to a temp file alongside /etc/hosts and then atomically
# ``mv`` it into place so a concurrent reader never sees a partial
# file. The macOS resolver respects /etc/hosts immediately on inode
# replace, but we also flush the cache for good measure.


def _atomic_write_script(hosts_path: Path, new_contents: str,
                         backup_path: Optional[Path] = None) -> str:
    """Generate a bash one-liner that atomically replaces *hosts_path*
    with *new_contents*. Used by both the GUI and CLI elevation paths.

    The script:
      1. Backs up the current /etc/hosts to *backup_path* (if given).
      2. Writes new contents to /etc/hosts.tmp
      3. ``chmod 644`` + ``chown root:wheel`` to match the original.
      4. ``mv`` into place (rename(2) is atomic within /etc).
    """
    import base64
    blob_b64 = base64.b64encode(new_contents.encode("utf-8")).decode("ascii")
    tmp_path = hosts_path.with_suffix(".local_scribe.tmp")
    backup_part = ""
    if backup_path is not None:
        backup_part = f"cp -f {hosts_path} {backup_path} && "
    return (
        backup_part
        + f"echo '{blob_b64}' | base64 -d > {tmp_path} && "
        + f"chmod 644 {tmp_path} && "
        + f"chown root:wheel {tmp_path} && "
        + f"mv {tmp_path} {hosts_path}"
    )


# Hard cap on the AppleScript prompt: macOS truncates anything past
# ~255 characters in the auth dialog header, and renders newlines
# inconsistently. We keep prompts under 250 ASCII chars on a single
# line. This is enough to fit one full English sentence with file
# paths.
_AS_PROMPT_MAX = 250


def _applescript_string(s: str) -> str:
    """Escape a string for inclusion inside an AppleScript ``"..."``
    literal. AppleScript supports ``\\\\`` for backslash, ``\\"`` for
    quote, and ``\\n``/``\\t`` for newline/tab; everything else is
    literal. We also strip CRs because the auth dialog renders them
    as boxes."""
    s = s.replace("\\", "\\\\").replace('"', '\\"').replace("\r", "")
    if len(s) > _AS_PROMPT_MAX:
        s = s[: _AS_PROMPT_MAX - 1] + "…"
    return s


def _explain_intent(
    action: str,
    *,
    hosts_path: Path,
    backup_path: Optional[Path],
    n_blocks: Optional[int] = None,
    n_categories: Optional[int] = None,
) -> tuple[str, str]:
    """Return ``(stderr_banner, osascript_prompt)`` describing the
    privileged operation about to run.

    ``action`` is "install" or "remove". The stderr banner is multi-
    line and is what the user sees in the terminal alongside sudo's
    ``Password:`` prompt; the osascript prompt is the *single* line
    that goes into the GUI dialog's ``with prompt`` clause.

    Keeping these two text blobs side-by-side here (rather than
    inlining at the call sites) makes the elevation UX easy to audit
    in one place: both prompts are visible to a reviewer and both
    must be updated together when the operation changes.
    """
    a = action.lower().strip()
    if a == "install":
        verb = "Editing"
        intent = "to install local_scribe's outbound firewall block list"
        what = "blackhole hostnames that Char's binary phones home to"
        ex = "Sentry telemetry, PostHog analytics, OpenAI / Deepgram / Anthropic, etc."
    elif a == "remove":
        verb = "Editing"
        intent = "to remove local_scribe's outbound firewall block list"
        what = "restore Char's ability to reach those hostnames"
        ex = "see SECURITY.md for the full list this was blocking"
    else:  # pragma: no cover -- defensive
        verb = "Editing"
        intent = f"to {a}"
        what = ""
        ex = ""

    count_clause = ""
    if a == "install" and n_blocks is not None and n_categories is not None:
        count_clause = (
            f"affects {n_blocks} hostname(s) across "
            f"{n_categories} category(ies)"
        )

    # --- stderr banner (multi-line, for terminal users) -------------
    lines = [
        f"firewall: {verb} {hosts_path} as root {intent}.",
        f"  reason : {what}",
    ]
    if ex:
        lines.append(f"  e.g.   : {ex}")
    if count_clause:
        lines.append(f"  scope  : {count_clause}")
    if backup_path is not None:
        lines.append(f"  backup : {backup_path}")
        lines.append(
            "           (the existing /etc/hosts is copied there byte-for-byte "
            "before any change)"
        )
    lines.append(
        f"  why    : macOS requires admin rights to write {hosts_path}; "
        "we use the smallest possible bash one-liner (visible above) "
        "to do it. No persistent sudoers entry is created."
    )
    if a == "install":
        lines.append(
            "  undo   : ./run.sh firewall disable --mode system  "
            "(asks for your password again to remove the block)."
        )
    else:
        lines.append(
            "  redo   : ./run.sh firewall enable --mode system "
            "(asks for your password again to reinstall)."
        )
    banner = "\n".join(lines)

    # --- osascript single-line prompt (≤250 chars) -----------------
    if a == "install":
        prompt = (
            "local_scribe wants to edit /etc/hosts to install its "
            "outbound firewall block list (Sentry, analytics, AI "
            "providers Char would otherwise contact)."
        )
        if backup_path is not None:
            prompt += f" Backup: {backup_path.name}."
    else:
        prompt = (
            "local_scribe wants to edit /etc/hosts to remove its "
            "outbound firewall block list (restoring Char's network "
            "access to those hostnames)."
        )
        if backup_path is not None:
            prompt += f" Backup: {backup_path.name}."
    return banner, prompt


def _print_elevation_banner(banner: str) -> None:
    """Emit the explanation to stderr immediately before triggering
    elevation. We tee to stderr rather than stdout so consumers of
    ``python -m firewall enable`` JSON output (none today, but plausible)
    don't get banner bytes mixed into structured stdout."""
    # ``flush=True`` so the banner reaches the terminal *before* the
    # sudo ``Password:`` line shows up; without this they can race
    # because sudo writes to its own controlling tty rather than our
    # captured stderr.
    print(banner, file=sys.stderr, flush=True)


def _build_elevation_cmd(
    script: str,
    *,
    elevation: str,
    osascript_prompt: str,
) -> list[str]:
    """Return the argv to invoke ``script`` under the requested
    elevation. Centralised here so the install/uninstall paths can't
    drift on AppleScript escaping or sudo flags."""
    if elevation == "sudo":
        return ["sudo", "bash", "-c", script]
    if elevation == "osascript":
        as_script = _applescript_string(script)
        as_prompt = _applescript_string(osascript_prompt)
        # NOTE: order matters in AppleScript -- ``with prompt`` must
        # come BEFORE ``with administrator privileges``. Apple's
        # parser accepts only that ordering for `do shell script`.
        applescript = (
            f'do shell script "{as_script}" '
            f'with prompt "{as_prompt}" '
            "with administrator privileges"
        )
        return ["osascript", "-e", applescript]
    raise ValueError(f"unknown elevation mode: {elevation!r}")


def install(categories: Iterable[str] = DEFAULT_ENABLED_CATEGORIES,
            *,
            mode: Mode = DEFAULT_MODE,
            hosts_path: Path = DEFAULT_HOSTS_PATH,
            backup: bool = True,
            elevation: str = "auto") -> tuple[bool, str]:
    """Install the block list. Returns (ok, message).

    ``mode`` selects the enforcement mechanism (see :class:`Mode`).
    In ``PROCESS_PROXY`` mode this function is a no-op at the config
    layer — the actual enforcement is the long-running
    :mod:`egress_proxy` process, started by ``./run.sh start``. We
    return success with a brief status message so callers can chain
    other steps.

    In ``SYSTEM_HOSTS`` mode we patch /etc/hosts atomically; that path
    needs root, hence ``elevation``:
      * ``"sudo"``    – ``sudo`` (interactive terminal prompt OK).
      * ``"osascript"`` – Apple's GUI password prompt.
      * ``"auto"``    – ``sudo`` if a TTY is attached, else osascript.
    """
    if mode == Mode.PROCESS_PROXY:
        # The per-Char proxy is the long-running enforcement layer;
        # the launcher wrapper (./run.sh char launch) is what wires
        # Char to it. There's nothing to "install" at the OS level.
        # Return a stable success message so callers don't special-
        # case process-mode in their happy paths.
        return True, (
            f"process-mode firewall: enforcement is via egress_proxy "
            f"on {PROXY_BIND}:{PROXY_PORT}. Launch Char via "
            "`./run.sh char launch` to route it through the proxy."
        )
    if not hosts_path.is_file():
        return False, f"{hosts_path} not found"

    current = hosts_path.read_text()
    new_text = upsert_block(current, categories)
    if new_text == current:
        return True, "block already up-to-date; no changes"

    backup_path: Optional[Path] = None
    if backup:
        import time
        backup_path = hosts_path.with_name(
            f"hosts.local_scribe.bak.{time.strftime('%Y%m%d-%H%M%S')}",
        )

    script = _atomic_write_script(hosts_path, new_text, backup_path)

    if elevation == "auto":
        elevation = "sudo" if os.isatty(0) else "osascript"

    # Build the user-facing explanation for THIS specific install.
    # Counting entries/categories so the banner can say "Affects 27
    # hostnames across 2 categories" rather than just "edits hosts".
    cats_list = list(categories)
    entries = _entries_for_categories(cats_list)
    banner, prompt = _explain_intent(
        "install",
        hosts_path=hosts_path,
        backup_path=backup_path,
        n_blocks=len(entries),
        n_categories=len(cats_list),
    )
    _print_elevation_banner(banner)

    try:
        cmd = _build_elevation_cmd(script, elevation=elevation,
                                   osascript_prompt=prompt)
    except ValueError as exc:
        return False, str(exc)

    rc = subprocess.run(cmd, capture_output=True, text=True)
    if rc.returncode != 0:
        return False, (f"elevation failed ({elevation}): "
                       f"{rc.stderr.strip() or rc.stdout.strip()}")

    # Best-effort cache flush. We don't bail if it fails; the install
    # already took effect at the file level.
    flush_dns_cache()

    msg = f"installed; backup at {backup_path}" if backup_path else "installed"
    return True, msg


def uninstall(*,
              mode: Optional[Mode] = None,
              hosts_path: Path = DEFAULT_HOSTS_PATH,
              backup: bool = True,
              elevation: str = "auto") -> tuple[bool, str]:
    """Remove the block list. Returns (ok, message).

    ``mode`` selects what to uninstall. If left ``None`` we
    autodetect: if /etc/hosts contains our managed block, treat it
    as a SYSTEM_HOSTS uninstall; otherwise it's a PROCESS_PROXY
    uninstall (i.e. ``./run.sh stop`` will take down the proxy —
    nothing to do here at the config layer).

    See :func:`install` for ``elevation`` semantics.
    """
    if mode is None:
        # Autodetect — older installs have /etc/hosts blocks even if
        # the operator is now on process mode, so always check.
        if hosts_path.is_file() and block_present(hosts_path.read_text()):
            mode = Mode.SYSTEM_HOSTS
        else:
            mode = Mode.PROCESS_PROXY
    if mode == Mode.PROCESS_PROXY:
        return True, (
            "process-mode firewall: nothing to uninstall at the OS "
            "layer. Stop the proxy with `./run.sh stop`."
        )
    if not hosts_path.is_file():
        return False, f"{hosts_path} not found"

    current = hosts_path.read_text()
    if not block_present(current):
        return True, "block not installed; nothing to do"

    new_text = remove_block(current)

    backup_path: Optional[Path] = None
    if backup:
        import time
        backup_path = hosts_path.with_name(
            f"hosts.local_scribe.bak.{time.strftime('%Y%m%d-%H%M%S')}",
        )

    script = _atomic_write_script(hosts_path, new_text, backup_path)

    if elevation == "auto":
        elevation = "sudo" if os.isatty(0) else "osascript"

    banner, prompt = _explain_intent(
        "remove",
        hosts_path=hosts_path,
        backup_path=backup_path,
    )
    _print_elevation_banner(banner)

    try:
        cmd = _build_elevation_cmd(script, elevation=elevation,
                                   osascript_prompt=prompt)
    except ValueError as exc:
        return False, str(exc)

    rc = subprocess.run(cmd, capture_output=True, text=True)
    if rc.returncode != 0:
        return False, (f"elevation failed ({elevation}): "
                       f"{rc.stderr.strip() or rc.stdout.strip()}")

    flush_dns_cache()
    msg = f"removed; backup at {backup_path}" if backup_path else "removed"
    return True, msg


# ---------------------------------------------------------------------------
# CLI entry point — drives ``./run.sh firewall …`` and ``python -m firewall``
# ---------------------------------------------------------------------------


def _print_status(s: Status) -> None:
    if not s.installed:
        print("local_scribe firewall: NOT INSTALLED")
        print("  run `./run.sh firewall enable` to install the default block list")
        return
    print("local_scribe firewall: INSTALLED")
    print(f"  hostnames currently blocked: {len(s.blocked_hostnames)}")
    for cat, c in s.coverage_by_category.items():
        marker = "✓" if c["blocked"] == c["expected"] else "○"
        print(f"  {marker} {cat:11s} {c['blocked']}/{c['expected']}")
    if s.missing_by_category:
        print()
        print("  missing hosts (drift from current catalog):")
        for cat, hosts in s.missing_by_category.items():
            for h in hosts:
                print(f"    - {h} [{cat}]")
        print("  re-run `./run.sh firewall enable` to refresh.")


def _print_list(categories: Iterable[str]) -> None:
    entries = _entries_for_categories(categories)
    cur_cat = None
    for e in entries:
        if e.category != cur_cat:
            print(f"\n[{e.category}]")
            cur_cat = e.category
        print(f"  {e.hostname:50s} {e.reason}")


def _print_verify(results: list[ProbeResult]) -> int:
    """Returns a process exit code: 0 if every host is blocked, 1
    otherwise (so CI can fail fast on missing coverage)."""
    rc = 0
    print(f"{'host':50s} {'status':10s} addresses")
    for r in results:
        status_str = "blocked" if r.blocked else "REACHES"
        addrs = ", ".join(r.addresses) if r.addresses else (r.error or "(no result)")
        print(f"{r.hostname:50s} {status_str:10s} {addrs}")
        if not r.blocked:
            rc = 1
    return rc


def main(argv: Optional[list[str]] = None) -> int:
    import argparse

    p = argparse.ArgumentParser(
        prog="firewall",
        description="local_scribe outbound block-list manager. See SECURITY.md.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="show current /etc/hosts coverage")
    p_list = sub.add_parser("list", help="print the would-be-blocked host catalog")
    p_list.add_argument("--strict", action="store_true",
                        help="include the char_cloud category too")

    p_enable = sub.add_parser("enable", help="install the block list")
    p_enable.add_argument("--strict", action="store_true",
                          help="also block api.char.com (calendars / OAuth)")
    p_enable.add_argument("--no-backup", action="store_true",
                          help="skip the /etc/hosts.bak.* backup")
    p_enable.add_argument("--gui", action="store_true",
                          help="force the AppleScript admin prompt instead of sudo")
    p_enable.add_argument(
        "--mode", choices=[m.value for m in Mode],
        default=DEFAULT_MODE.value,
        help=("'process' (default): per-Char filtering via local proxy "
              "(no sudo, only affects Char). 'system': machine-wide "
              "block via /etc/hosts (asks for admin password, affects "
              "all apps)."),
    )

    p_disable = sub.add_parser("disable", help="remove the block list")
    p_disable.add_argument("--no-backup", action="store_true",
                           help="skip the /etc/hosts.bak.* backup")
    p_disable.add_argument("--gui", action="store_true",
                           help="force the AppleScript admin prompt instead of sudo")
    p_disable.add_argument(
        "--mode", choices=[m.value for m in Mode], default=None,
        help=("'process' / 'system'. If omitted we autodetect: if "
              "/etc/hosts contains our block markers we treat it as "
              "system mode and remove them; otherwise we report no-op."),
    )

    sub.add_parser("mode", help="show the effective firewall mode + default")

    p_verify = sub.add_parser(
        "verify",
        help="DNS-probe the catalog; fail if any host isn't blocked")
    p_verify.add_argument("--strict", action="store_true",
                          help="include the char_cloud category in the probe")
    p_verify.add_argument("--timeout", type=float, default=2.0)

    args = p.parse_args(argv)

    if args.cmd == "status":
        _print_status(status())
        return 0

    if args.cmd == "list":
        cats = set(DEFAULT_ENABLED_CATEGORIES)
        if args.strict:
            cats |= {"char_cloud"}
        _print_list(sorted(cats))
        return 0

    if args.cmd == "enable":
        cats = set(DEFAULT_ENABLED_CATEGORIES)
        if args.strict:
            cats |= {"char_cloud"}
        elevation = "osascript" if args.gui else "auto"
        mode = Mode(args.mode)
        ok, msg = install(cats, mode=mode,
                          backup=not args.no_backup, elevation=elevation)
        print(msg)
        return 0 if ok else 1

    if args.cmd == "disable":
        elevation = "osascript" if args.gui else "auto"
        mode = Mode(args.mode) if args.mode else None
        ok, msg = uninstall(mode=mode,
                            backup=not args.no_backup, elevation=elevation)
        print(msg)
        return 0 if ok else 1

    if args.cmd == "mode":
        out = {
            "default_mode": DEFAULT_MODE.value,
            "supported_modes": [m.value for m in Mode],
            "proxy_bind": PROXY_BIND,
            "proxy_port": PROXY_PORT,
            "system_hosts_active": (
                DEFAULT_HOSTS_PATH.is_file()
                and block_present(DEFAULT_HOSTS_PATH.read_text())
            ),
        }
        import json as _json
        print(_json.dumps(out, indent=2))
        return 0

    if args.cmd == "verify":
        cats = set(DEFAULT_ENABLED_CATEGORIES)
        if args.strict:
            cats |= {"char_cloud"}
        hosts = [e.hostname for e in _entries_for_categories(cats)]
        return _print_verify(verify(hosts, timeout=args.timeout))

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
