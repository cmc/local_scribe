# Inspector

> Moved from the top-level README on 2026-05-12 as part of
> the condense-and-link pass. The content below is the
> canonical reference. The README keeps a short pointer
> paragraph linking back here.
>
> **Related docs:** [`docs/CONFIGURE_CHAR.md`](CONFIGURE_CHAR.md), [`docs/API.md`](API.md)

A tiny loopback web app at `http://127.0.0.1:8001/` that surfaces the
data Char already collects, plus our config and a Char audit. It
auto-starts as part of `./run.sh start`; you can also manage it
independently:

```bash
./run.sh inspector start        # background uvicorn on :8001
./run.sh inspector status
./run.sh inspector open         # launch your default browser
./run.sh inspector logs         # tail
./run.sh inspector stop
```

Three tabs:

* **Sessions** — every Char session on disk under
  `~/Library/Application Support/hyprnote/sessions/` listed newest
  first, with audio playback (`<audio>` streaming the same `audio.mp3`
  Char wrote), the diarised transcript flattened from
  `transcript.json` into speaker-prefixed paragraphs, every per-session
  note (`<Template>.md`), a one-click `transcript.txt` download, and a
  **Transcript history** panel per session that lists every previous
  `transcript.json` we auto-archived on re-transcription (with the ASR
  model + diarization algorithm + K + sha256 each archive captured) plus
  View / Download / Delete buttons. Read-only for the notes themselves —
  for editing those, use Char's UI.
* **Config** — form-bound editor for `~/.config/local_scribe/config.json`.
  Each field is annotated with what it does (e.g. "set `llm.host` to a
  LAN address to run LM Studio on another Mac"). Saving runs the
  validator, writes a timestamped backup, and persists the result;
  the response includes a "restart required" hint when ASR / LLM
  values change. Env vars still win over the file at process start, so
  setting `LLM_HOST=...` for a single launch overrides whatever the
  inspector wrote.
* **Char audit** — runs the same checks as `./run.sh doctor`'s Char
  block, but in a sortable table with `ok` / `warn` / `info` /
  `miss` badges per row. Verifies that
  `ai.stt.openai.base_url` still points at our local server,
  flags any provider-specific `base_url` that's been changed from its
  vendor default, masks any leftover real OpenAI key (so the inspector
  never echoes a full secret), and offers a one-click "Run
  configure-char" button that backs up `settings.json` and rewrites
  the four keys. Also lists every backup we've already saved (Char's
  settings + any extracted OpenAI keys at
  `~/.config/local_scribe/char-openai-key.<ts>.txt`) so a restore is a
  trivial `cp` away.

A status-pill row in the header pings `/api/asr/health`,
`/api/llm/health`, and the Char audit every 15 seconds — the easy way
to spot LM Studio or Char drift without leaving your editor.

### Roadmap: making the inspector the full operator control surface

Today the inspector is a read-only-ish observer plus the
Char-audit one-click fix. The `./run.sh` CLI is still the
authoritative place to install, configure, start / stop, manage
keys + vault + firewall, and re-bless integrity baselines.

The next major piece of work (tracked in
[`TODO.md`](TODO.md#privacy--security-p0) as "Web UI as the full
operator control surface") promotes the inspector to the single
user-facing entry point — install, configure, operate, observe
the whole pipeline, with the CLI kept as the scriptable / headless
fallback. The headline pieces, all phased so each lands as an
independently reviewable slice:

* **Real-time integrity status tile** — pass / fail for every
  defense layer, pushed over SSE so the tile turns red within 5 s
  of any drift. Sources:
  [`script_integrity.verify()`](local_scribe/security/script_integrity.py),
  [`char_integrity.collect_fingerprint()`](local_scribe/char/char_integrity.py),
  [`signed_config.status()`](local_scribe/security/signed_config.py),
  the egress-proxy block log, the service-auth bypass flag.
* **Service lifecycle from the UI** — start / stop / restart
  buttons that invoke the same Python entry points `./run.sh`
  does, with Touch ID re-confirmation on each destructive op
  (the cookie alone is not enough; a stolen cookie can't
  `key rotate`).
* **Key + vault lifecycle** — `init`, `rotate`, `add-yubikey`,
  `dr-backup`, `dr-restore`, `vault init`, `mount`, `unmount`,
  `rotate-password` — each gated by a typed-confirm body + fresh
  Touch ID + (where the underlying CLI op requires it) an
  "insert your YubiKey now" modal.
* **Char + firewall + sandbox controls** — install, launch,
  baseline-update, firewall enable / disable / mode, sandbox
  profile diff-before-apply. The few `sudo` ops
  (`firewall enable --mode system` writes `/etc/hosts`) stay in
  the CLI on purpose — moving privilege escalation through a
  web UI multiplies the threat surface, and the convenience win
  isn't worth it.
* **API docs** — FastAPI's auto-generated `/docs` (Swagger UI)
  and `/redoc` (Redoc) double as the operator reference. Every
  endpoint's Pydantic model includes "Touches:", "Idempotent:",
  "Recovery:" metadata blocks pulled from the docstring so a
  privacy-conscious operator can read exactly what each button
  does before clicking.
* **Dark theme stays default** — the existing CSS variable
  system already does this; the roadmap adds a user-controlled
  toggle that overrides the `prefers-color-scheme` media query,
  persisted in `localStorage`.

Two further items are tracked as P0 follow-ups, both designed
around the threat model of "operator is away from the laptop":

* **Tamper-alert dispatch (SMS / email / push)** — fires a
  signed alert to a different device the operator owns when an
  integrity gate fails or the egress proxy blocks an unexpected
  request. Trade-off matrix for channel selection
  (Twilio / SMTP / APNs / Signal / operator-hosted relay) and
  the credential-safety problem are walked through in
  [`TODO.md`](TODO.md#privacy--security-p0).
* **Auto-dismount the vault on screen lock; Touch-ID-gated
  remount on unlock and on Char restart** — ties vault mount
  state to screen-unlock state so the data plane cycles down
  whenever the operator looks away. Four modes (`soft`,
  `cooperative`, `strict`, `paranoid`) trade off Char-stability
  against unmount-aggressiveness; full mode table + UX gotchas
  in [`TODO.md`](TODO.md#privacy--security-p0).

The CLI is not going away. The headless / scripted / CI use
cases stay first-class; the web UI is a second front-end with the
same auth model and the same primitives underneath, not a
replacement.

### Privacy posture for the Inspector

* Binds to `127.0.0.1` by default. The validator refuses any non-loopback
  bind unless `inspector.auth_token` is also set, so you can't
  accidentally expose `/api/sessions` to the LAN.
* No external CDN — all CSS/JS lives inside `inspector_server.py` and
  is served from the same origin.
* No write access to Char's session data. Only `config.json` and
  Char's `settings.json` / `store.json` are mutated, and only the
  latter via an explicit POST to `/api/char/configure`.
* No analytics, no telemetry. The inspector's only outbound network
  calls are the two health pings to your own ASR server + LM Studio.

If you ever want to expose it to your LAN (e.g. read sessions from
another laptop), set `inspector.auth_token` to a long random value
**and** `inspector.bind` to your LAN address. The validator will
refuse the latter without the former.


## Demo mode (synthetic data, no Touch ID, no YubiKey)

The screenshots in the top-level [README's Screenshots
section](../README.md#screenshots) are captured against a disposable
demo dataset under `~/.cache/local_scribe-demo/`. Reproduce them
locally:

```bash
# 1. Seed an isolated Char data dir at ~/.cache/local_scribe-demo/
python3 tools/seed_demo.py --clean

# 2. Start the demo inspector on a separate port (8765 by default).
#    No Touch ID / YubiKey prompt: the demo uses LOCAL_SCRIBE_DISABLE_AUTH
#    and is deliberately walled off from your real config + Char data.
./tools/run_demo.sh start

# 3. Open in any browser
open http://127.0.0.1:8765/

# 4. (optional) regenerate all six PNGs under docs/screenshots/
./tools/capture_screenshots.sh

# 5. When done
./tools/run_demo.sh stop
```

The demo runner sets these env vars in its own shell only — never
inherited by `./run.sh start`:

- `LOCAL_SCRIBE_DISABLE_AUTH=1` — skip the HKDF bearer-token gate.
- `LOCAL_SCRIBE_TEST_CSRUTIL_OUTPUT="System Integrity Protection status: enabled."`
  — pretend SIP is enabled (the real `./run.sh start` correctly
  *refuses* to start without it; the test hook lets the demo work
  on dev machines with SIP disabled).
- `LOCAL_SCRIBE_CONFIG_DIR=~/.cache/local_scribe-demo/config` —
  fully isolated config so the demo cannot read or write your real
  `~/.config/local_scribe/`.
- `LOCAL_SCRIBE_CHAR_DATA_DIR=~/.cache/local_scribe-demo/hyprnote` —
  fully isolated Char data so the demo cannot read or write your
  real `~/Library/Application Support/hyprnote/`.

**None of these bypasses are honoured by the production `./run.sh
start` path.** They live in the codebase specifically so the demo
+ the test suite + headless-screenshot tooling can drive the
surface without forging a YubiKey tap.
