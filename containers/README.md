# containers/ — exploratory scaffolding, not the supported deployment

This directory exists as a forward-looking placeholder for the day someone
wants to run `local_scribe`'s pure-Python services on a Linux host. The
**supported deployment is macOS-on-Apple-Silicon, run via `./run.sh start`**,
and most of what makes this project a useful *security* tool simply doesn't
have a containerised equivalent today. The split below is meant to be
explicit about what's portable and what isn't.

## What CAN move into a container

- `local_scribe.asr.asr_server` (FastAPI app). Runs cleanly in Linux as long
  as the operator picks the `faster-whisper` backend; the default
  `parakeet-mlx` is Apple-Silicon-only by construction (MLX framework).
- `local_scribe.inspector.inspector_server` (FastAPI app). Read-only over
  whatever filesystem you mount in; no platform APIs.
- `local_scribe.egress.egress_proxy`. The HTTP CONNECT proxy itself is pure
  Python; the *integration* (sandbox-exec, `HTTPS_PROXY` env var on a
  specific child process) is macOS-only.

## What CANNOT move into a container, and why

| Component | Reason it's macOS-only |
|----------|-------------------------|
| `local_scribe.egress.char_sandbox` | Wraps `sandbox-exec(1)` — macOS-only. |
| `local_scribe.egress.firewall` | Operates `pf(8)` + `/etc/hosts` — macOS-only. |
| `local_scribe.security.secret_store` | Talks to the macOS Keychain via the Swift `touchid-keychain` helper. |
| `local_scribe.security.vault` | `hdiutil`-managed AES-256 sparse bundle on APFS. |
| `local_scribe.security.vault_unlock` | Touch ID prompt + YubiKey PIV tap, mediated by `LocalAuthentication.framework`. |
| `local_scribe.security.yubikey_backup` | `ykman` + `age` plugin path; works in containers but the operator UX is much worse. |
| `local_scribe.security.script_integrity` | git-based; works in a container but is far less load-bearing without the rest of the gate. |
| `local_scribe.security.sip_check` | Reads `csrutil` — macOS-only by definition. |
| `local_scribe.char.*` | Audits `/Applications/Char.app` — macOS-only by definition. |

## What this directory provides

- `asr.Dockerfile` — minimal Python image running `local_scribe.asr.asr_server`
  with the `faster-whisper` backend. Useful for a Linux GPU box that's purely
  a transcription worker (no security model, no Char integration).
- `inspector.Dockerfile` — pure Python web UI. Operationally trivial in a
  container; the security story it inspects, however, is gone.
- `egress-proxy.Dockerfile` — the CONNECT proxy as a standalone container.
  Pair with a host-level firewall rule that routes Char's traffic to it; on
  Linux this means `iptables`/`nftables` work that's outside this repo's
  scope.
- `compose.yaml` — wires the three above for local development/testing on a
  Linux box. **Not** a production deployment.

## How to bring this into production

You shouldn't, until at least the items in
[`../docs/FORK_CONSIDERATIONS.md`](../docs/FORK_CONSIDERATIONS.md) "If we had
a Developer ID" are resolved. The current security model assumes:
- A macOS kernel enforcing SIP,
- macOS-mediated Touch ID + YubiKey prompts,
- An APFS-backed encrypted sparse bundle,
- A `sandbox-exec` profile on Char itself.

None of those have direct Linux analogues. A real Linux deployment would
need: a hardware-rooted secure element (TPM 2.0 or YubiKey HSM mode), an
equivalent of `sandbox-exec` (probably `bubblewrap` or `firejail` with
LSMs), and a UI framework for the Touch ID-equivalent (FIDO2 prompt). All
of that is out of scope for this repo at the moment.
