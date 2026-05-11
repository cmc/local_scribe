"""Helpers for constructing PATH-prepended directories of fake binaries.

Used by bootstrap-stage tests that need to drive ``run.sh`` helpers
(``ensure_age_tools``, ``ensure_pip_deps``, ``ensure_config_json``, ...)
in a controlled environment where every external tool is a faithful
fake instead of the real binary.

Why this is its own module
--------------------------

Bootstrap touches a lot of external tools (brew, age, age-plugin-yubikey,
ykman, hdiutil, csrutil, security, lms, sysctl, codesign, sw_vers,
osascript, ...). Hand-rolling fakes in each test file would mean N
copies of "write a #!/usr/bin/env bash file, chmod 0755 it, prepend to
PATH". This module centralises:

  * The catalogue of supported fakes (``BIN_RECIPES``).
  * Knob env vars each fake honours (e.g.
    ``LOCAL_SCRIBE_FAKE_YKMAN_PRESENT=0`` to model "no YubiKey").
  * The teardown helper that restores PATH + env.

What is NOT covered
-------------------

The macOS Keychain CLI (``security`` add/find/delete-generic-password)
is currently NOT faked — it's hard to model correctly across all the
edge cases ``secret_store.py`` exercises (ACLs, Touch ID gating, label
collisions, error codes). For tests that need to exercise the
master-key stage end-to-end, a future work item is an in-memory
``LOCAL_SCRIBE_KEYCHAIN_BACKEND=memory`` test seam inside
``secret_store.py`` itself. See ``tests/bootstrap/README.md``.

Fakes provided
--------------

  age, age-plugin-yubikey, ykman   — key-management tooling
  brew                              — no-op (assume packages already present
                                       on the test PATH)
  csrutil                           — ``System Integrity Protection
                                       status: enabled.`` by default
  hdiutil                           — vault sparsebundle creation /
                                       attach / detach
  lms                               — LM Studio CLI shim
  osascript                         — no-op (returns 0)
  codesign                          — pretends Char.app and our scripts
                                       are validly signed
  sw_vers / sysctl                  — fixed plausible macOS values
"""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
from pathlib import Path
from typing import Dict, Optional


_REPO = Path(__file__).resolve().parents[2]
_FAKE_AGE_PLUGIN = _REPO / "tests" / "security" / "_fake_age_plugin_yubikey.py"


# ---------------------------------------------------------------------------
# Bash-script fakes.
#
# Each entry maps tool name → script body. The body is written verbatim
# into the tmp bin dir, ``chmod 0755``. Honor knob env vars via test-time
# environment so fakes can be reused across tests with different
# configurations.

BIN_RECIPES: Dict[str, str] = {
    "age": (
        "#!/usr/bin/env bash\n"
        "# Fake age: implements the subset of flags our code uses.\n"
        "if [[ \"${1:-}\" == \"--version\" ]]; then\n"
        "  echo 'age 1.2.0'\n"
        "  exit 0\n"
        "fi\n"
        "# encrypt mode: -e -r RECIPIENT -o OUT_PATH < stdin\n"
        "# decrypt mode: -d -i IDENTITY -o OUT_PATH < stdin\n"
        "# We just pipe stdin to OUT_PATH so the file exists and is non-empty.\n"
        "out=\"\"\n"
        "while [[ $# -gt 0 ]]; do\n"
        "  case \"$1\" in\n"
        "    -o) out=\"$2\"; shift 2 ;;\n"
        "    -p) shift ;;\n"
        "    -r|-R|-i) shift 2 ;;\n"
        "    -e|-d|-a) shift ;;\n"
        "    *) shift ;;\n"
        "  esac\n"
        "done\n"
        "if [[ -n \"$out\" ]]; then\n"
        "  cat > \"$out\"\n"
        "else\n"
        "  cat\n"
        "fi\n"
        "exit 0\n"
    ),
    "ykman": (
        "#!/usr/bin/env bash\n"
        "# Fake ykman. ``ykman list`` is the only thing yubikey_backup\n"
        "# probes. The knob LOCAL_SCRIBE_FAKE_YKMAN_PRESENT=0 models\n"
        "# 'no YubiKey on USB'.\n"
        "if [[ \"${1:-}\" == \"--version\" ]]; then\n"
        "  echo 'YubiKey Manager (ykman) version: 5.9.1'\n"
        "  exit 0\n"
        "fi\n"
        "if [[ \"${1:-}\" == \"list\" ]]; then\n"
        "  if [[ \"${LOCAL_SCRIBE_FAKE_YKMAN_PRESENT:-1}\" == \"0\" ]]; then\n"
        "    exit 0   # nothing on stdout = no key\n"
        "  fi\n"
        "  echo 'YubiKey 5C Nano (5.4.3) [OTP+FIDO+CCID] Serial: 16366413'\n"
        "  exit 0\n"
        "fi\n"
        "exit 0\n"
    ),
    "ykman_broken": (
        "#!/usr/bin/env bash\n"
        "# Models the 2026-05-11 production failure: a brew install left\n"
        "# /opt/homebrew/Cellar/ykman/5.5.1/libexec/bin/python as a 0-byte\n"
        "# file. ``ykman --version`` then dies with 'exec format error'.\n"
        "echo 'exec format error: ykman' >&2\n"
        "exit 126\n"
    ),
    "brew": (
        "#!/usr/bin/env bash\n"
        "# Fake brew: install/reinstall succeed; tools are already on PATH.\n"
        "# Trace what was asked of us via\n"
        "# LOCAL_SCRIBE_FAKE_BREW_TRACE if set.\n"
        "if [[ -n \"${LOCAL_SCRIBE_FAKE_BREW_TRACE:-}\" ]]; then\n"
        "  echo \"$@\" >> \"$LOCAL_SCRIBE_FAKE_BREW_TRACE\"\n"
        "fi\n"
        "if [[ \"${LOCAL_SCRIBE_FAKE_BREW_FAIL:-0}\" == \"1\" ]]; then\n"
        "  exit 1\n"
        "fi\n"
        "case \"${1:-}\" in\n"
        "  --version) echo 'Homebrew 4.4.0'; exit 0 ;;\n"
        "  install|reinstall|update|upgrade|cleanup) exit 0 ;;\n"
        "  list) echo \"\"; exit 0 ;;\n"
        "  *) exit 0 ;;\n"
        "esac\n"
    ),
    "csrutil": (
        "#!/usr/bin/env bash\n"
        "# Fake csrutil. Default: SIP enabled. Override with\n"
        "# LOCAL_SCRIBE_FAKE_CSRUTIL_OUTPUT to model SIP-off state.\n"
        "if [[ -n \"${LOCAL_SCRIBE_FAKE_CSRUTIL_OUTPUT:-}\" ]]; then\n"
        "  printf '%s\\n' \"$LOCAL_SCRIBE_FAKE_CSRUTIL_OUTPUT\"\n"
        "  exit 0\n"
        "fi\n"
        "echo 'System Integrity Protection status: enabled.'\n"
        "exit 0\n"
    ),
    "hdiutil": (
        "#!/usr/bin/env bash\n"
        "# Fake hdiutil. ``create`` writes a marker dir at the expected\n"
        "# sparsebundle path so ``vault.exists()`` returns True.\n"
        "case \"${1:-}\" in\n"
        "  create)\n"
        "    out=\"\"\n"
        "    while [[ $# -gt 0 ]]; do\n"
        "      if [[ \"$1\" == \"-o\" || \"$1\" == \"-volname\" ]]; then\n"
        "        shift 2\n"
        "        continue\n"
        "      fi\n"
        "      case \"$1\" in\n"
        "        -*) shift ;;\n"
        "        *) out=\"$1\"; shift ;;\n"
        "      esac\n"
        "    done\n"
        "    if [[ -n \"$out\" ]]; then\n"
        "      mkdir -p \"${out}.sparsebundle\"\n"
        "      echo 'fake' > \"${out}.sparsebundle/token\"\n"
        "    fi\n"
        "    exit 0\n"
        "    ;;\n"
        "  attach|detach|eject) exit 0 ;;\n"
        "  info) echo '{}'; exit 0 ;;\n"
        "  *) exit 0 ;;\n"
        "esac\n"
    ),
    "lms": (
        "#!/usr/bin/env bash\n"
        "# Fake LM Studio CLI shim.\n"
        "case \"${1:-}\" in\n"
        "  --version|version) echo 'lms 0.4.12'; exit 0 ;;\n"
        "  ls|status) echo '{}'; exit 0 ;;\n"
        "  server)\n"
        "    case \"${2:-}\" in\n"
        "      start|stop|status) exit 0 ;;\n"
        "      *) exit 0 ;;\n"
        "    esac\n"
        "    ;;\n"
        "  load|get) exit 0 ;;\n"
        "  *) exit 0 ;;\n"
        "esac\n"
    ),
    "osascript": (
        "#!/usr/bin/env bash\n"
        "# Fake osascript: no-op, exit 0.\n"
        "if [[ -n \"${LOCAL_SCRIBE_FAKE_OSASCRIPT_TRACE:-}\" ]]; then\n"
        "  echo \"$@\" >> \"$LOCAL_SCRIBE_FAKE_OSASCRIPT_TRACE\"\n"
        "fi\n"
        "exit 0\n"
    ),
    "codesign": (
        "#!/usr/bin/env bash\n"
        "# Fake codesign: pretend everything is validly signed by\n"
        "# Apple's expected Team ID for our pinned Char build.\n"
        "if [[ \"${1:-}\" == \"--display\" || \"${1:-}\" == \"-d\" ]]; then\n"
        "  echo 'Authority=Developer ID Application: Yujong Lee (FASTREPL3)' >&2\n"
        "  exit 0\n"
        "fi\n"
        "if [[ \"${1:-}\" == \"--verify\" || \"${1:-}\" == \"-v\" ]]; then\n"
        "  exit 0\n"
        "fi\n"
        "exit 0\n"
    ),
    "sw_vers": (
        "#!/usr/bin/env bash\n"
        "case \"${1:-}\" in\n"
        "  -productVersion) echo '15.0' ;;\n"
        "  -buildVersion)   echo 'FAKE24A1' ;;\n"
        "  *)               echo 'ProductName: macOS'; echo 'ProductVersion: 15.0' ;;\n"
        "esac\n"
        "exit 0\n"
    ),
    "sysctl": (
        "#!/usr/bin/env bash\n"
        "# Fake sysctl. Return 64 GiB of RAM by default so the LLM\n"
        "# bootstrap step picks the 30B model. Knob:\n"
        "# LOCAL_SCRIBE_FAKE_RAM_BYTES=<bytes>.\n"
        "case \"${1:-}\" in\n"
        "  -n)\n"
        "    case \"${2:-}\" in\n"
        "      hw.memsize) echo \"${LOCAL_SCRIBE_FAKE_RAM_BYTES:-68719476736}\" ;;\n"
        "      hw.ncpu)    echo 10 ;;\n"
        "      *)          echo 0 ;;\n"
        "    esac\n"
        "    ;;\n"
        "  *) echo 0 ;;\n"
        "esac\n"
        "exit 0\n"
    ),
}


class FakeBinDir:
    """Tmp directory holding fake binaries; PATH-prepended for a test.

    Usage::

        with FakeBinDir(tools=["age", "ykman", "brew"]) as bins:
            subprocess.run(..., env={"PATH": bins.path_for_env(), ...})

    Tools NOT requested are simply absent (so a test can model "brew not
    installed" by omitting brew). ``"age-plugin-yubikey"`` is special:
    when listed, we symlink the realistic Python fake from
    ``tests/security/_fake_age_plugin_yubikey.py`` instead of synthesising
    a bash script.
    """

    def __init__(self, tools: Optional[list] = None, ykman_broken: bool = False) -> None:
        self._dir = Path(tempfile.mkdtemp(prefix="ls-bootstrap-bin-"))
        wanted = list(tools or BIN_RECIPES.keys())
        if "ykman" in wanted and ykman_broken:
            wanted.remove("ykman")
            self._install("ykman", BIN_RECIPES["ykman_broken"])
            wanted = [t for t in wanted if t != "ykman_broken"]
        if "age-plugin-yubikey" in wanted:
            target = self._dir / "age-plugin-yubikey"
            target.symlink_to(_FAKE_AGE_PLUGIN)
            wanted = [t for t in wanted if t != "age-plugin-yubikey"]
        for tool in wanted:
            if tool == "ykman_broken":
                continue
            if tool not in BIN_RECIPES:
                raise KeyError(f"no fake recipe for tool: {tool}")
            self._install(tool, BIN_RECIPES[tool])

    def _install(self, name: str, body: str) -> None:
        target = self._dir / name
        target.write_text(body)
        target.chmod(0o755)
        m = target.stat().st_mode
        target.chmod(m | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    @property
    def path(self) -> Path:
        return self._dir

    def path_for_env(self, base: Optional[str] = None) -> str:
        """Return a PATH string with this dir prepended."""
        base = base if base is not None else os.environ.get("PATH", "")
        return f"{self._dir}{os.pathsep}{base}"

    def cleanup(self) -> None:
        shutil.rmtree(self._dir, ignore_errors=True)

    def __enter__(self) -> "FakeBinDir":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.cleanup()
