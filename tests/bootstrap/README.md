# tests/bootstrap — bootstrap-flow regression tests

`./run.sh bootstrap` is the highest-stakes path in the codebase: it walks
a fresh-laptop operator through ten stages that touch tools (`brew`,
`age`, `age-plugin-yubikey`, `ykman`, `hdiutil`, `lms`, `osascript`,
`codesign`), services (LM Studio, Char.app), and security primitives
(Touch ID, YubiKey, the macOS Keychain, the AES-256 sparsebundle vault).
Most of those side effects can't be faked cleanly, so production bugs
have historically been found only by re-running bootstrap manually on a
clean machine.

This package exists to shrink that gap. It tests the stages we can
exercise without real hardware, and explicitly documents the stages we
can't yet.

## What is covered

### `test_ensure_age_tools.py`

Drives `run.sh`'s `ensure_age_tools` helper against a tmp PATH built by
`_fake_bins.FakeBinDir`. Five tests pin the contract that brought down
bootstrap on 2026-05-11:

| Test | Models | Asserts |
|---|---|---|
| `test_all_tools_present_returns_zero` | Happy path | rc=0, brew never called |
| `test_missing_tool_triggers_brew_install` | tool absent on PATH | `brew install <tool>` called |
| `test_broken_tool_triggers_brew_reinstall` | tool on PATH but `--version` exits non-zero | **`brew reinstall <tool>` called, NOT plain install** |
| `test_post_install_verification_fails_loud` | brew "succeeds" but tool still broken | rc≠0, error names the still-broken tool |
| `test_no_brew_present_errors_clearly` | brew missing, tool missing | rc≠0, error references brew.sh |

The `broken_tool` test specifically reproduces the failure mode we hit
on 2026-05-11: `/opt/homebrew/Cellar/ykman/5.5.1/libexec/bin/python` was
a zero-byte file, so `ykman --version` died with `exec format error`,
but the old `ensure_age_tools` only checked `command -v` and waved it
through. Stage 3 then died with the much-less-helpful "no YubiKey
detected". The new test guarantees that broken binaries trigger
`brew reinstall` (not `brew install`, which is a no-op for an
already-installed formula).

### `test_ensure_config_json.py`

Drives `run.sh`'s `ensure_config_json` helper. Tests that:

- A fresh tmp HOME gets a valid `~/.config/local_scribe/config.json`.
- A second invocation is idempotent — no rewrite, no mtime touch.
- Operator hand-edits are preserved bit-for-bit.
- The Python heredoc uses the post-reorg import path
  (`from local_scribe.common.config import ...`), not the flat-module
  `from config import ...` that broke after the 2026-05-11 reorg.

### `test_run_sh_imports.py`

Static analysis. Greps every Python `^(from|import)` line out of
`run.sh` and `importlib.import_module`s each one that resolves to a
`local_scribe.*` subpackage. Any unresolved import (or any flat-module
import that leaked through the 2026-05-11 reorg) fails the test loudly
with the offending `run.sh` line number.

This is the highest-leverage test in the package: it runs in < 1 s, has
zero subprocess overhead, and catches an entire class of "I forgot to
update the import path during the refactor" regressions across the
whole 3,800-line `run.sh` in one shot.

### `tests/security/test_yubikey_backup_enroll.py` (lives next door)

Eleven real-subprocess integration tests for `yubikey_backup.enroll()`
against `tests/security/_fake_age_plugin_yubikey.py` (a faithful clone
of the age-plugin-yubikey 0.5.x CLI). Pins:

- `--identity-output` is **never** passed (the flag doesn't exist in
  0.5.x; passing it tanked bootstrap on 2026-05-11).
- `subprocess.run(...)` invariants: `stdout=PIPE`, `stderr=None`,
  `stdin=None`, `capture_output` False. Inheriting stdin is what lets
  the operator type the PIV PIN.
- Identity stub written 0600.
- Recipient correctly extracted from the stdout `# Recipient: ...`
  comment.
- `force=True` overwrites in place.

## What is NOT yet covered

### Stage 3 — master-key init

Requires faking the macOS Keychain (`security` CLI:
`add-generic-password` / `find-generic-password` /
`delete-generic-password` with ACL handling) and Touch ID. A
faithful Keychain fake is doable but non-trivial because
`secret_store.py` exercises subtle edges (`-T` ACL flags, label
collisions, error codes 25241 / 36 / 51 / 100013, `-w` byte handling).

**Next step**: add an in-memory backend behind a test-only env var
inside `secret_store.py` itself
(`LOCAL_SCRIBE_KEYCHAIN_BACKEND=memory`). That seam already exists for
SIP checks (`LOCAL_SCRIBE_TEST_CSRUTIL_OUTPUT`) — the pattern just
needs to be cloned for the Keychain layer. Tracked in `TODO.md`.

### Stage 4 — encrypted vault init

Requires a faithful `hdiutil` fake plus the master key from stage 3.
Once stage 3 has an in-memory Keychain seam, this falls out for free.

### Stages 5–6 — Parakeet / sherpa-onnx model fetches

The Python heredocs call `huggingface_hub.snapshot_download` and the
sherpa-onnx model helpers. Test scaffolding would mock those at the
`local_scribe.asr.backends.diarization_backend.ensure_models` boundary.
Pending — these stages have been more reliable in practice (the
download either works or fails fast with a clear network error).

### Stages 8–9 — LM Studio + Char install

The fake `lms` binary in `_fake_bins.py` is ready for use; the
corresponding stage tests just haven't been written yet.

### Stage 10 — firewall scaffolding

Non-interactive, no sudo. Could be wrapped with a quick test that
drives the helper against fake `socketserver` ports.

### End-to-end `./run.sh bootstrap --dev`

Goal: one big test that PATH-prepends every fake from `_fake_bins.py`,
sets the in-memory Keychain seam, scripts stdin with `yes` answers,
and runs the full ten-stage `cmd_bootstrap`. Asserts exit 0 + all
expected artefacts on disk. Blocked on the Keychain seam.

## Working with the test harness

### Fake binaries (`_fake_bins.py`)

```python
from tests.bootstrap._fake_bins import FakeBinDir

with FakeBinDir(tools=["age", "ykman", "brew"]) as bins:
    env = {"PATH": bins.path_for_env(), ...}
    subprocess.run(..., env=env)
```

Pass `ykman_broken=True` to simulate the 2026-05-11 broken-`ykman`
failure. Pass a subset of `tools=[...]` to model "tool X is not on
PATH at all" (use a strict `path=f"{bins.path}:/usr/bin:/bin"` rather
than `path_for_env()` so the real `/opt/homebrew/bin` doesn't
accidentally provide the missing tool).

Knob env vars honoured by individual fakes:

| Env var | Effect |
|---|---|
| `LOCAL_SCRIBE_FAKE_YKMAN_PRESENT=0` | `ykman list` reports no key |
| `LOCAL_SCRIBE_FAKE_CSRUTIL_OUTPUT=<str>` | Override `csrutil status` body |
| `LOCAL_SCRIBE_FAKE_RAM_BYTES=<bytes>` | Override `sysctl -n hw.memsize` |
| `LOCAL_SCRIBE_FAKE_BREW_TRACE=<path>` | Log every `brew <subcmd> <args>` invocation |
| `LOCAL_SCRIBE_FAKE_BREW_FAIL=1` | Every `brew install` / `reinstall` exits 1 |
| `LOCAL_SCRIBE_FAKE_AGE_PLUGIN_RECIPIENT=<str>` | Override emitted `age1yubikey1...` |
| `LOCAL_SCRIBE_FAKE_AGE_PLUGIN_SERIAL=<str>` | Override emitted `# Serial:` |
| `LOCAL_SCRIBE_FAKE_AGE_PLUGIN_REQUIRE_TTY=1` | Require `stdin.isatty()` |
| `LOCAL_SCRIBE_FAKE_AGE_PLUGIN_TRACE=<path>` | Log every plugin invocation's argv |

### Sourcing `run.sh` safely

`run.sh` now has a `[[ "${BASH_SOURCE[0]}" == "${0}" ]]` guard around
its dispatcher (the bash equivalent of `if __name__ == "__main__"`),
so tests can `source run.sh` to expose every helper function without
the dispatcher firing.

`set -euo pipefail` is still on. When invoking a helper that may
return non-zero, wrap with `if ... fi`:

```bash
source /path/to/run.sh
if ensure_age_tools 2>&1; then rc=0; else rc=$?; fi
echo "__rc=$rc"
```

…otherwise the subshell dies silently on the first non-zero return.

### Why no `pytest-bash`-style framework?

We considered `bats` / `pytest-bash` / `shellcheck-test`. They all
either require a separate test runner the rest of the project doesn't
use, or have weak fixture-isolation guarantees. Driving the bash
helpers from Python pytest via `subprocess.run(["bash", "-c", ...])`
plus the `FakeBinDir` helper gives us isolated tmp dirs, knob env
vars, and uniform reporting alongside the rest of the test suite.
