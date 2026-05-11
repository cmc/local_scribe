"""Atomic JSON patcher for Char's ``settings.json``.

This module exists so ``run.sh configure-char`` can hand the ASR
bearer token to a Python helper **via stdin** rather than as an argv
parameter. Stdin is invisible to ``ps`` listings; argv is not.

Wire format (stdin, three or four lines)::

    line 1   absolute path to Char's settings.json
    line 2   ASR port (e.g. "8000")
    line 3   ASR bearer token (e.g. "ls_asr_a1b2c3...")
    line 4   (optional) launch_id (32 hex). When present, the token
             written into ``settings.json`` is suffixed with
             ``.ls<short_id>`` so the ASR server's launch-session
             gate (Layer C, see ``launch_session.py``) refuses it
             the moment ``./run.sh start`` exits.

We deliberately don't accept any of these on argv. The path / port
COULD be on argv (neither is a secret), but routing all three the
same way keeps the call sites symmetric and the threat-model argument
trivial ("nothing about Char-config is ever on argv").

Why a module + ``python -m`` rather than ``python - <<HEREDOC``
---------------------------------------------------------------

When you do ``somecmd | python - <<HEREDOC``, bash's redirect rule
makes the heredoc win for stdin only when the pipe is absent; with
a pipe present the pipe wins and ``python -`` tries to execute the
piped data as source code (loud crash). ``python -m
char_settings_writer`` has its source on disk, leaving stdin free
for the data channel.
"""

from __future__ import annotations

import json
import pathlib
import sys


def patch_settings(
    settings_path: pathlib.Path,
    port: str,
    token: str,
    launch_id: str | None = None,
) -> None:
    """Apply the local_scribe Char patch to ``settings.json``.

    Writes back the same indented JSON Char originally shipped, so the
    diff in version control + the "what changed" view in the inspector
    UI stay readable. We only touch four keys:

      * ``ai.current_stt_provider``  → ``"openai"``  (Char's enum value
        for any OpenAI-compatible transcriber; routes through the
        progressive SSE path the ASR server speaks)
      * ``ai.current_stt_model``     → ``"gpt-4o-transcribe"``  (the
        Char model id that gates the streaming path; see comment block
        below for the exact reason this matters)
      * ``ai.stt.openai.base_url``   → ``http://127.0.0.1:<PORT>/v1``
      * ``ai.stt.openai.api_key``    → HKDF-derived ASR bearer token

    Anything else (LLM provider, templates, calendars, ...) is left
    intact so the user's existing Char config survives a re-run.
    """
    data = json.loads(settings_path.read_text())
    ai  = data.setdefault("ai", {})
    stt = ai.setdefault("stt", {})
    oai = stt.setdefault("openai", {})
    ai["current_stt_provider"] = "openai"
    # ``gpt-4o-transcribe`` triggers Char's progressive (SSE-streamed)
    # batch path. Char's tauri-plugin-transcription/listener2/ext.rs
    # hardcodes a 60-second BATCH_IDLE_TIMEOUT for the non-streaming
    # ``gpt-4o-transcribe-diarize`` path, which silently aborts the
    # spawned future on any audio whose ASR takes longer than 60 s.
    # Our local Parakeet pass is ~80x realtime, so that's any meeting
    # longer than ~80 minutes. The progressive path resets the timer
    # on every SSE delta, and our /v1/audio/transcriptions endpoint
    # emits heartbeat deltas every STREAM_HEARTBEAT_SECONDS to keep
    # it alive on long files.
    ai["current_stt_model"] = "gpt-4o-transcribe"
    oai["base_url"]         = f"http://127.0.0.1:{port}/v1"
    # api_key is the per-service ASR bearer token (HKDF-derived from
    # the Keychain master key — see service_auth.py). When a
    # launch_id is provided, attach the ``.ls<short_id>`` suffix so
    # the ASR server's launch-session gate (Layer C) ties this saved
    # token to the current ``./run.sh start`` invocation.
    if launch_id:
        # Lazy import keeps char_settings_writer importable in tests
        # that don't have the rest of the runtime on PATH.
        from local_scribe.common.launch_session import attach_suffix
        token = attach_suffix(token, launch_id)
    oai["api_key"]          = token
    settings_path.write_text(json.dumps(data, indent=2) + "\n")


def main() -> int:
    lines = sys.stdin.read().splitlines()
    if len(lines) < 3:
        sys.stderr.write(
            f"error: expected 3+ stdin lines (path, port, token, "
            f"[launch_id]), got {len(lines)}\n"
        )
        return 2
    settings_path = pathlib.Path(lines[0])
    port = lines[1]
    token = lines[2]
    launch_id = lines[3] if len(lines) >= 4 and lines[3] else None
    if not settings_path.is_file():
        sys.stderr.write(f"error: settings file not found: {settings_path}\n")
        return 1
    try:
        patch_settings(settings_path, port, token, launch_id=launch_id)
    except (OSError, json.JSONDecodeError) as exc:
        sys.stderr.write(f"error: {type(exc).__name__}: {exc}\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
