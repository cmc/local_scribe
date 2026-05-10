"""
Re-run ASR + diarization on an existing Char session and overwrite its
``transcript.json`` via the same sidecar path that the live request
flow uses.

Why this exists
---------------
``/v1/audio/transcriptions`` is the only path that's wired into Char's
"Generate" button, and it has to keep behaving like an OpenAI batch
endpoint for unattended UI flows. But the user often *already knows*
something about a particular session that the live flow can't infer
(e.g. "this is a 1:1 call, force exactly two speakers", or "the audio
is 3 hours long, lift the diarization cap"). Asking them to mutate
``config.json`` + restart the server + click Regenerate from inside
Char is too many moving parts.

This script gives them a single command:

    ./run.sh redo-session 77f87727-c9b8-4bac-bbfa-26934c8b4ba7 --speakers 2

It posts the session's ``audio.mp3`` against the running ASR server
with the supplied per-request overrides (``num_speakers``,
``cluster_threshold``, ``diarize`` opt-out, etc.), waits for the
sidecar write to land, and verifies the resulting ``transcript.json``
shape so the user gets a clear "ok / not ok" exit status without
having to grep server logs.

We deliberately *don't* implement an ASR/diarization second pipeline
in this file: the running ASR server is the only place that actually
holds the Parakeet model in memory, so we want the redo to use the
same code path that produced the original (broken) result.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Optional


def _err(msg: str, code: int = 1) -> int:
    print(f"redo-session: {msg}", file=sys.stderr)
    return code


def _resolve_session_dir(
    session_arg: str, char_data_dir: Path,
) -> Optional[Path]:
    """Accept either a full UUID, a UUID prefix (>= 4 chars), or a
    session title fragment. Returns None on no match. Disambiguates by
    asking the user when multiple match."""
    sessions = char_data_dir / "sessions"
    if not sessions.is_dir():
        return None

    # Direct UUID match.
    direct = sessions / session_arg
    if direct.is_dir() and (direct / "audio.mp3").is_file():
        return direct

    # Prefix / title fragment match. We check both the dir name and
    # the session's _meta.json title field so users can paste
    # "Maus Meeting" verbatim.
    needle = session_arg.lower()
    matches: list[tuple[Path, str]] = []
    for d in sessions.iterdir():
        if not d.is_dir() or not (d / "audio.mp3").is_file():
            continue
        if d.name.lower().startswith(needle):
            matches.append((d, d.name))
            continue
        meta = d / "_meta.json"
        if meta.is_file():
            try:
                title = json.loads(meta.read_text() or "{}").get("title", "")
                if needle in title.lower():
                    matches.append((d, title))
            except (OSError, json.JSONDecodeError):
                continue

    if not matches:
        return None
    if len(matches) == 1:
        return matches[0][0]

    # Multiple matches — print them and refuse to pick one
    # automatically (a misfired diarize on the wrong session would
    # silently overwrite real data).
    print("multiple sessions match; please be more specific:", file=sys.stderr)
    for d, label in matches:
        print(f"  {d.name}  {label!r}", file=sys.stderr)
    return None


def _post_audio(
    audio_path: Path,
    *,
    asr_url: str,
    num_speakers: Optional[int],
    cluster_threshold: Optional[float],
    diarize: bool,
    timeout: float,
) -> dict:
    """Stream the session's audio to the running ASR server. Returns
    the parsed terminal SSE event (or raises if the server fails)."""
    try:
        import requests  # local import: keeps cold-start of asr_server.py fast
    except ImportError as exc:
        raise SystemExit(
            f"redo-session: 'requests' not installed; "
            f"`pip install requests` in the venv ({exc})"
        )

    params = {}
    if not diarize:
        params["diarize"] = "0"
    if num_speakers is not None:
        params["num_speakers"] = str(num_speakers)
    if cluster_threshold is not None:
        params["cluster_threshold"] = f"{cluster_threshold:.4f}"

    # Bearer token for the gated ASR endpoint. Prompts Touch ID on the
    # first call unless LOCAL_SCRIBE_ASR_TOKEN / LOCAL_SCRIBE_DISABLE_AUTH
    # is set in the env. The OpenAI-batch endpoint expects the
    # ``Bearer`` scheme.
    import service_auth
    auth_h = service_auth.client_auth_header_for(
        "asr",
        prompt=f"Authenticate local_scribe to re-transcribe session "
               f"{audio_path.parent.name}",
        style="bearer",
    )
    with audio_path.open("rb") as f:
        files = {"file": (audio_path.name, f, "audio/mpeg")}
        data = {
            "model": "gpt-4o-transcribe",
            "response_format": "json",
            "stream": "true",
            "language": "en",
        }
        # We accept the streaming SSE response but only care about the
        # final `done` event (sidecar already wrote transcript.json by
        # then). Stream so we surface heartbeats during long ASR runs.
        resp = requests.post(
            f"{asr_url}/v1/audio/transcriptions",
            params=params,
            files=files,
            data=data,
            headers={**auth_h},
            stream=True,
            timeout=timeout,
        )

    if resp.status_code != 200:
        raise SystemExit(
            f"redo-session: ASR server returned HTTP {resp.status_code}: "
            f"{resp.text[:400]}"
        )

    last_event: dict = {}
    last_progress = 0.0
    for raw in resp.iter_lines(decode_unicode=True):
        if not raw or not raw.startswith("data:"):
            continue
        payload = raw[len("data:"):].strip()
        if payload == "[DONE]":
            continue
        try:
            ev = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "transcript.text.delta":
            now = time.time()
            if now - last_progress >= 5.0:
                print(".", end="", flush=True)
                last_progress = now
        elif ev.get("type") == "transcript.text.done":
            last_event = ev
    print()  # newline after the heartbeat dot stream
    return last_event


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="redo-session",
        description=(
            "Re-run ASR + diarization on an existing Char session and "
            "overwrite its transcript.json. Useful when the original "
            "Generate produced the wrong number of speakers or got "
            "tripped up by a clustering blow-up."
        ),
    )
    p.add_argument(
        "session",
        help=(
            "Char session UUID (full or prefix), or a substring of the "
            "session's title (e.g. 'Maus Meeting'). Must uniquely "
            "match a single session."
        ),
    )
    p.add_argument(
        "--speakers", type=int, default=None,
        help=(
            "Force exactly this many speakers in the diarization output. "
            "Use when you know the answer (1:1 call -> 2). When omitted, "
            "the server's clustering chooses automatically and may "
            "over-cluster on long mono recordings."
        ),
    )
    p.add_argument(
        "--cluster-threshold", type=float, default=None,
        help=(
            "Override the speaker-clustering distance threshold (0..1, "
            "default ~0.45 short / 0.7 long). Lower => more speakers, "
            "higher => fewer. Ignored if --speakers is set."
        ),
    )
    p.add_argument(
        "--no-diarize", action="store_true",
        help="Skip diarization entirely; produce a single-speaker transcript.",
    )
    p.add_argument(
        "--asr-url", default=None,
        help="ASR server base URL (default: http://127.0.0.1:8000).",
    )
    p.add_argument(
        "--timeout", type=float, default=3600.0,
        help="HTTP read timeout in seconds (default 3600 / 1h).",
    )
    args = p.parse_args(argv)

    # We import config lazily so this script still runs as
    # `python redo_session.py --help` without setting up the venv.
    from config import load_config
    cfg = load_config()

    # asr_bind may be 0.0.0.0 (listens on all interfaces); always
    # connect via loopback from the local CLI.
    bind = cfg.asr_bind
    host = "127.0.0.1" if bind in ("0.0.0.0", "::", "") else bind
    asr_url = args.asr_url or f"http://{host}:{cfg.asr_port}"
    char_data_dir = cfg.char_data_dir

    session_dir = _resolve_session_dir(args.session, char_data_dir)
    if session_dir is None:
        return _err(
            f"no session matching {args.session!r} under "
            f"{char_data_dir}/sessions/. Use the inspector "
            f"(./run.sh inspector) to browse session IDs."
        )

    audio = session_dir / "audio.mp3"
    audio_size_mb = audio.stat().st_size / (1024 * 1024)
    title = "(no title)"
    meta = session_dir / "_meta.json"
    if meta.is_file():
        try:
            title = json.loads(meta.read_text()).get("title", title)
        except (OSError, json.JSONDecodeError):
            pass

    print(f"redo-session: {session_dir.name}")
    print(f"  title       : {title}")
    print(f"  audio       : {audio} ({audio_size_mb:.1f} MB)")
    print(f"  diarize     : {'no' if args.no_diarize else 'yes'}")
    if args.speakers is not None:
        print(f"  num_speakers: {args.speakers} (forced)")
    if args.cluster_threshold is not None:
        print(f"  threshold   : {args.cluster_threshold}")
    print(f"  asr server  : {asr_url}")
    print()
    print("uploading + processing (heartbeats below; expect ~1s/min of audio)...")
    print()

    started = time.time()
    done = _post_audio(
        audio,
        asr_url=asr_url,
        num_speakers=args.speakers,
        cluster_threshold=args.cluster_threshold,
        diarize=not args.no_diarize,
        timeout=args.timeout,
    )
    elapsed = time.time() - started

    if not done:
        return _err("ASR server stream ended without a `done` event")

    text = done.get("text") or ""
    print()
    print(f"server returned {len(text):,} chars in {elapsed:.1f}s.")

    # Sidecar should have just written transcript.json. Verify.
    transcript = session_dir / "transcript.json"
    if not transcript.is_file():
        return _err(
            "ASR server completed successfully but transcript.json "
            "was NOT written. Check `./run.sh logs` for "
            "char_persist warnings (sha256 mismatch, traversal, etc.)."
        )

    try:
        loaded = json.loads(transcript.read_text())
        rows = loaded.get("transcripts") or []
        if not rows:
            return _err(f"transcript.json at {transcript} has no rows")
        words = rows[0].get("words") or []
        hints = rows[0].get("speaker_hints") or []
        speakers = sorted({
            json.loads(h.get("value", "{}")).get("speaker_index", 0)
            for h in hints
        })
    except (OSError, json.JSONDecodeError) as exc:
        return _err(f"transcript.json at {transcript} is malformed: {exc}")

    print(f"transcript  : {transcript}")
    print(f"  rows        : {len(rows)}")
    print(f"  words       : {len(words):,}")
    print(f"  speakers    : {len(speakers)} (indices: {speakers})")
    print()
    if len(speakers) <= 1 and not args.no_diarize:
        print(
            "note: diarization produced a single speaker. If you expected "
            "more, retry with --speakers N (e.g. --speakers 2 for a 1:1 "
            "call). Long-form audio also benefits from a higher "
            "--cluster-threshold (e.g. 0.85)."
        )
    else:
        print("done. Switch sessions in Char (or relaunch it) to reload.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
