# Bootstrap automation

> Moved from the top-level README on 2026-05-12 as part of
> the condense-and-link pass. The content below is the
> canonical reference. The README keeps a short pointer
> paragraph linking back here.
>
> **Related docs:** [`SECURITY.md`](../SECURITY.md), [`docs/CONFIGURE_CHAR.md`](CONFIGURE_CHAR.md), [`docs/CHAR_VERSION_PIN.md`](CHAR_VERSION_PIN.md)

`./run.sh bootstrap` is a single command that takes a clean machine
(macOS + Python + Homebrew) all the way to a working pipeline. It runs
**nine idempotent steps** — already-done steps short-circuit with a
green checkmark, so re-running on a fully set-up machine prints the
state and exits without changing anything.

```text
(0/10) System Integrity Protection    ─── gate: refuses to continue if
                                          SIP isn't fully enabled. No
                                          operator override (see
                                          SECURITY.md § Defense layer 0)
(1/10) python venv + pip deps + helper ─── creates venv/, installs
                                          requirements.txt, compiles
                                          bin/touchid-keychain via swiftc
(2/10) key tools (age, age-plugin-yubikey, ykman)
                                       ─── brew-installs whichever are
                                          missing. The split-key flow
                                          CANNOT run without them, so
                                          bootstrap refuses to proceed
                                          if Homebrew is unavailable.
(3/10) master key (Touch ID ⊕ YubiKey) ── Option C split-key init.
                                          Generates a 256-bit master,
                                          splits it via XOR, writes
                                          kc_half to Keychain + yk_half
                                          age-encrypted to your YubiKey.
                                          Bootstrap REFUSES to continue
                                          if you decline (the most-secure
                                          default install requires it).
(4/10) encrypted vault (AES-256)      ─── hdiutil sparse bundle keyed
                                          off the master via HKDF.
                                          Relocates Char's data dir
                                          INTO the vault on first unlock.
(5/10) parakeet ASR weights           ─── ~1.2 GB MLX bundle from
                                          mlx-community/parakeet-tdt-0.6b-v3
(6/10) sherpa-onnx diarization models ─── ~45 MB ONNX (pyannote 3.0
                                          segmentation + TitaNet embedding)
(7/10) ~/.config/local_scribe/config.json
                                       ─── seeded with defaults; the
                                          inspector "Config" tab edits
                                          the same file.
(8/10) LM Studio.app + Qwen LLM       ─── see breakdown below
(9/10) Char.app — install + auto-config
(10/10) per-Char outbound firewall    ─── renders + validates the
                                          sandbox-exec profile at
                                          ~/.config/local_scribe/char.sb.
                                          NO SUDO. The egress proxy
                                          starts on the next ./run.sh
                                          start. Launch Char via
                                          ./run.sh char launch.
                                          --mode system (machine-wide
                                          /etc/hosts block) is opt-in.
```

### Step (8/9) — LM Studio.app + Qwen LLM, in detail

This is the step that handles your local LLM host end-to-end. It is
**fully unattended past two y/N prompts** (one for the brew cask
install, one for the multi-GB model download — you wouldn't want either
to start without confirmation).

1. **Install LM Studio.app** if `/Applications/LM Studio.app` is missing,
   via `brew install --cask lm-studio` (so it auto-updates and is signed).
   We pin `LMSTUDIO_KNOWN_GOOD_VERSION = 0.4.12` — installed versions
   that match get a "matches pinned" stamp; later versions get a soft
   "usually compatible" note (LM Studio's `lms` CLI surface is stable
   across patch releases). Build suffixes like `0.4.12+1` are normalised
   for the comparison.
2. **Bootstrap the `lms` CLI** by finding the binary inside the app
   bundle (`/Applications/LM Studio.app/Contents/Resources/.../lms`) and
   running `lms bootstrap`. This symlinks it into
   `~/.cache/lm-studio/bin/lms` so it's on your `PATH` for subsequent
   invocations and for `./run.sh start` to use. (Without this step, the
   `lms` symlink only gets created the first time you GUI-launch LM
   Studio.)
3. **Start the LM Studio HTTP server** on `:1234` (`lms server start
   --port 1234`). If it's already running, skipped.
4. **Pick the right model for your hardware.** Reads `sysctl -n
   hw.memsize`:
   - **≥48 GB unified memory** → recommends `qwen/qwen3-30b-a3b-instruct-2507`
     (32 GB MLX, ~36 GB loaded with the default 65 K context).
   - **<48 GB unified memory** → falls back to `qwen/qwen3-4b`
     (2.3 GB MLX, ~3 GB loaded). The threshold is configurable via
     `LLM_MIN_RAM_GB`; the model identifiers are `LLM_MODEL_REPO` and
     `LLM_MODEL_SMALL_REPO`.
5. **Download the chosen model** via `lms get <repo> --mlx -y` if it's
   not already in your local store. Skipped if `/api/v0/models` already
   reports the model id (or a `<owner>/<id>` variant) as known. The
   `--mlx` flag forces the Apple Silicon native variant; `-y`
   auto-accepts the default quantisation.
6. **Load the model** into RAM via `lms load <model> -y --context-length
   65536`, or skip if `/api/v0/models` reports it as `state=loaded`.
   The context length is configurable via `LLM_CONTEXT`.

After step 6, LM Studio is fully ready: server on `:1234`, model loaded,
OpenAI-compatible API at `/v1/chat/completions` waiting for Char to call.

The same orchestrator is exposed standalone as `./run.sh install-llm`,
so you can repair an LM Studio install or pull a different model later
without re-running the full bootstrap.

### Step (9/9) — Char.app, in detail

Same shape as the LM Studio step, with one extra wrinkle (the OpenAI
transcriber config patch):

- If Char isn't installed → offer to download the **pinned version**
  (`v1.0.24`, the build this repo was tested against) from the
  [`fastrepl/anarlog` GitHub Release](https://github.com/fastrepl/anarlog/releases/tag/desktop_v1.0.24),
  verify SHA256, and install it to `/Applications`. See
  [`CHAR_VERSION_PIN.md`](CHAR_VERSION_PIN.md) for what we pin and why.
- If Char *is* installed at a different version → warn that the
  pinned version is the only build the auto-config has been validated
  against, and offer to replace (default *No* — your call).
- Then, regardless of the above, prompt to wire Char's OpenAI
  transcriber at this server (equivalent to `./run.sh configure-char`).
  See [`INTEGRATION.md`](INTEGRATION.md)
  for the four `settings.json` keys this rewrites.

### What you still have to click manually

After bootstrap finishes there's exactly **one tab in Char** left to
configure that we don't auto-write — Char's *Intelligence* (LLM)
provider. Open Char → **Settings → Intelligence**, set:

- **Provider**: LM Studio
- **Base URL**: `http://127.0.0.1:1234`
- **Model**: `qwen3-30b-a3b-instruct-2507` (or `qwen/qwen3-4b` on smaller
  hardware — whichever bootstrap downloaded for you)

That's it. From there, every recording you take and every audio file
you Generate runs through Parakeet + Qwen on your laptop with no
network egress.

`./run.sh start` runs preflight first (so even if you skipped
`bootstrap` it Just Works), then brings up the services and tails the
ASR log. `Ctrl+C` detaches without stopping anything.

### What `start` will print

You'll see one of three banners:

```
──── pipeline ready ────                                # everything wired
  ASR server (Parakeet TDT v3) : http://127.0.0.1:8000  (Char's transcription endpoint)
  LM Studio API (Qwen3-30B)    : http://127.0.0.1:1234  (summary + speaker naming)
```

```
──── pipeline PARTIALLY ready ────                      # LM Studio not running
  ASR server (Parakeet TDT v3) : http://127.0.0.1:8000  (transcription works)
  LM Studio API                : NOT REACHABLE on :1234
                                 → Char's summary step will fail until you start LM Studio
```

```
──── pipeline PARTIALLY ready ────                      # Qwen not loaded
  ASR server (Parakeet TDT v3) : http://127.0.0.1:8000  (transcription works)
  LM Studio API                : http://127.0.0.1:1234  (reachable)
  qwen3-30b-a3b-instruct-2507  : NOT LOADED
                                 → Char's summary step will fail; load the model in LM Studio.app
```

In the partial cases the message tells you exactly what to fix. Re-run
`./run.sh start` once you've done it.

