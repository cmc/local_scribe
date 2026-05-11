"""tools/seed_demo.py — populate an isolated Char data dir with
synthetic-but-realistic demo sessions so the inspector UI has something
worth screenshotting.

The data this script writes is **synthetic**. Every participant name,
every transcript line, every note is fabricated. There is no real PII
anywhere in here. Run it against a *throwaway* directory (the default
is ``~/.cache/local_scribe-demo/hyprnote``); never against your real
``~/Library/Application Support/hyprnote``.

Usage:

    python tools/seed_demo.py [--target DIR] [--clean]

The corresponding ``./run.sh demo`` subcommand wires this together with
an isolated inspector instance on a separate port.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


DEFAULT_TARGET = Path.home() / ".cache" / "local_scribe-demo" / "hyprnote"


@dataclass
class Turn:
    """One conversational turn — one speaker, one continuous block of
    text (which will get split into per-word entries).
    """
    speaker: int
    text: str
    start: float
    duration: float


@dataclass
class DemoSession:
    sid: str
    title: str
    participants: list[str]
    created_at: str
    template: str
    turns: list[Turn]
    notes: dict[str, str] = field(default_factory=dict)
    history_revs: int = 0


def _make_words(turn: Turn, word_id_base: int) -> list[dict]:
    """Split a turn's text into per-word entries with plausible
    word-level timestamps. Returns the FastAPI-shape words list."""
    pieces = turn.text.split()
    if not pieces:
        return []
    per_word = turn.duration / max(len(pieces), 1)
    out = []
    for i, p in enumerate(pieces):
        wid = f"w{word_id_base + i:05d}"
        ws = turn.start + i * per_word
        out.append({
            "id": wid,
            "text": p,
            "start": round(ws, 3),
            "end": round(ws + per_word * 0.9, 3),
        })
    return out


def _build_transcript_json(session: DemoSession) -> dict:
    """Build the Char-shape ``transcript.json`` from a list of turns.

    Mirrors what ``char_persist.py`` produces in real use:

      * ``transcripts[0].words`` — flat per-word array.
      * ``transcripts[0].speaker_hints`` — one hint per word, carrying
        a ``provider_speaker_index`` value Char uses to surface the
        speaker label.
      * ``local_scribe.diarization.word_confidences`` — parallel array
        the inspector uses to render per-paragraph confidence.
      * ``local_scribe.diarization.speakers`` — per-speaker airtime
        aggregate.
    """
    words: list[dict] = []
    hints: list[dict] = []
    confidences: list[float] = []
    airtime: dict[int, float] = {}

    wid_base = 0
    for turn in session.turns:
        turn_words = _make_words(turn, wid_base)
        wid_base += len(turn_words)
        words.extend(turn_words)
        for w in turn_words:
            hints.append({
                "word_id": w["id"],
                "type": "provider_speaker_index",
                "value": json.dumps({
                    "provider": "openai",
                    "channel": 1 if turn.speaker == 0 else 2,
                    "speaker_index": turn.speaker,
                }),
            })
            # Plausibly varied per-word confidence (0.78 – 0.99) so
            # the UI shows non-trivial percentages.
            confidences.append(round(0.78 + 0.21 * ((hash(w["id"]) & 0xff) / 255.0), 3))
        airtime[turn.speaker] = airtime.get(turn.speaker, 0.0) + turn.duration

    speakers_agg = [
        {
            "label": f"speaker_{idx}",
            "airtime_sec": round(seconds, 1),
            "share": round(seconds / max(sum(airtime.values()), 1e-6), 3),
        }
        for idx, seconds in sorted(airtime.items())
    ]

    return {
        "id": f"transcript-{session.sid}",
        "session_id": session.sid,
        "transcripts": [{
            "words": words,
            "speaker_hints": hints,
        }],
        "local_scribe": {
            "diarization": {
                "backend": "sherpa-onnx (demo)",
                "word_confidences": confidences,
                "speakers": speakers_agg,
            },
            "asr": {
                "backend": "parakeet-mlx (demo)",
                "model": "nvidia/parakeet-tdt-0.6b-v3",
            },
            "demo": True,
        },
    }


def _ensure_silent_mp3(path: Path, duration_sec: float) -> None:
    """Render a silent MP3 of the given duration via ffmpeg. This gives
    the inspector UI a real audio file to render the duration / player
    controls against, without shipping any actual speech bytes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    cmd = [
        "ffmpeg", "-loglevel", "error",
        "-f", "lavfi",
        "-i", f"anullsrc=channel_layout=stereo:sample_rate=44100",
        "-t", f"{duration_sec:.2f}",
        "-c:a", "libmp3lame", "-b:a", "32k",
        str(path),
    ]
    subprocess.run(cmd, check=True)


def _write_session(target_root: Path, session: DemoSession) -> Path:
    session_dir = target_root / "sessions" / session.sid
    session_dir.mkdir(parents=True, exist_ok=True)

    duration = max((t.start + t.duration for t in session.turns), default=60.0)
    _ensure_silent_mp3(session_dir / "audio.mp3", duration)

    (session_dir / "_meta.json").write_text(json.dumps({
        "id": session.sid,
        "title": session.title,
        "created_at": session.created_at,
        "participants": session.participants,
        "template": session.template,
        "duration_sec": round(duration, 1),
        "demo": True,
    }, indent=2))

    transcript_json = _build_transcript_json(session)
    (session_dir / "transcript.json").write_text(
        json.dumps(transcript_json, indent=2)
    )

    for fname, body in session.notes.items():
        (session_dir / fname).write_text(body)

    if session.history_revs > 0:
        history_dir = session_dir / ".local_scribe_history"
        history_dir.mkdir(exist_ok=True)
        for i in range(session.history_revs):
            ts = f"2026-05-{(8 + i):02d}T10-0{i}-00Z"
            hist_path = history_dir / f"transcript.{ts}.json"
            stale = json.loads(json.dumps(transcript_json))
            stale["local_scribe"]["asr"]["model"] = (
                "nvidia/parakeet-tdt-0.6b-v2" if i == 0
                else "openai/whisper-large-v3-turbo"
            )
            stale["local_scribe"]["demo_history"] = True
            hist_path.write_text(json.dumps(stale, indent=2))

    return session_dir


def _demo_sessions() -> list[DemoSession]:
    """A small, hand-written set of demo conversations. Picked so the
    UI renders a variety of speaker counts, lengths, templates, and
    history depths. Topics are intentionally generic so the screenshots
    don't look like they're impersonating anybody real.
    """
    sessions: list[DemoSession] = []

    sessions.append(DemoSession(
        sid="demo-001-q1-product-review",
        title="Q1 product review — demo",
        participants=["Alex (PM)", "Jordan (Eng lead)", "Sam (Design)", "Pat (QA)"],
        created_at="2026-05-08T15:30:00Z",
        template="Team meeting",
        turns=[
            Turn(0, "Thanks everyone for joining. Let's walk through where we landed this quarter and what we want to commit to for Q2.", 0.0, 6.4),
            Turn(1, "On engineering side, we shipped the new ingest pipeline and the streaming endpoint. Latency is down forty percent. There are two known issues we still need to land before the freeze.", 6.5, 12.0),
            Turn(2, "Design wrapped the redesign of the session card. We saw click-through go from twelve percent to twenty-one percent in the holdout. The empty state still needs another pass.", 18.6, 10.5),
            Turn(3, "QA has been stable. The flaky test we discussed last week turned out to be a timezone issue in the fixture. Already fixed in main.", 29.2, 7.8),
            Turn(0, "Great. Let's commit to closing those two engineering issues and a final design pass on the empty state before the freeze. Sam, can you drive that?", 37.1, 8.4),
            Turn(2, "Yes I can take it. I'll have something ready by Thursday.", 45.6, 4.0),
            Turn(0, "Perfect. Anything else before we wrap?", 49.7, 2.4),
            Turn(1, "One more thing — we should probably do a postmortem on the May incident next week.", 52.2, 5.2),
            Turn(0, "Good call. I'll put time on the calendar. Thanks all.", 57.5, 4.0),
        ],
        notes={
            "summary.md": (
                "# Q1 product review — demo\n\n"
                "**Date:** 2026-05-08 · **Duration:** 1m 02s · **Template:** Team meeting\n\n"
                "## Decisions\n\n"
                "- Close the two outstanding engineering blockers before Q1 freeze.\n"
                "- Sam to drive a final design pass on the session-card empty state by Thursday.\n"
                "- Schedule a postmortem on the May incident next week.\n\n"
                "## Action items\n\n"
                "- [ ] **Jordan**: close engineering blockers before freeze.\n"
                "- [ ] **Sam**: revised empty state mocks by 2026-05-12.\n"
                "- [ ] **Alex**: put May-incident postmortem on calendar.\n\n"
                "## Key numbers\n\n"
                "- Ingest pipeline latency: −40%.\n"
                "- Session-card click-through: 12% → 21% in holdout.\n"
            ),
        },
        history_revs=2,
    ))

    sessions.append(DemoSession(
        sid="demo-002-customer-discovery",
        title="Customer discovery — Acme Corp (synthetic)",
        participants=["You", "Riley (Acme, Head of Ops)"],
        created_at="2026-05-07T14:00:00Z",
        template="Customer interview",
        turns=[
            Turn(0, "Thanks so much for taking the time, Riley. Before we dive in, would you mind walking me through your current workflow for meeting notes?", 0.0, 8.2),
            Turn(1, "Yeah of course. Right now we use a mix of tools. Most of our team is on a paid notetaker service, but a few people just type into a doc. The biggest pain is that nothing flows back into our CRM automatically.", 8.4, 13.8),
            Turn(0, "Got it. And when you say 'biggest pain', is that costing you time, deals, or both?", 22.4, 5.6),
            Turn(1, "Honestly both. We've lost track of action items from customer calls more than once, and our account executives spend probably forty-five minutes a day cleaning up notes after the fact.", 28.2, 11.4),
            Turn(0, "That's a lot. If we could shave that to ten minutes with a privacy-first local tool, would that change anything about how your team operates?", 39.8, 9.2),
            Turn(1, "Privacy is actually a big one for us. We have some accounts in regulated verticals and they explicitly ask us not to send call recordings to third-party cloud services. So local-only would unlock those.", 49.2, 13.4),
            Turn(0, "That's really useful to hear. Are there other constraints around what 'local' has to mean — like, do the transcripts have to stay encrypted at rest?", 62.8, 9.0),
            Turn(1, "Yes, encrypted at rest is a hard requirement. We have SOC 2 commitments. Anything sensitive on a laptop has to be in an encrypted volume or it's a finding.", 72.0, 11.6),
            Turn(0, "Understood. Last question — if this existed today, would budget be a blocker?", 83.8, 6.8),
            Turn(1, "Not at the price points I've seen for the cloud tools. We'd actually pay more for something we can defend in an audit.", 90.7, 8.4),
        ],
        notes={
            "summary.md": (
                "# Customer discovery — Acme Corp (synthetic)\n\n"
                "**Date:** 2026-05-07 · **Duration:** 1m 39s · **Template:** Customer interview\n\n"
                "## Customer pain points (in order)\n\n"
                "1. Action items lost after customer calls — direct revenue impact.\n"
                "2. AEs spend ≈ 45 min/day cleaning up notes (target: 10 min).\n"
                "3. Customers in regulated verticals refuse cloud notetakers.\n"
                "4. SOC 2 commitments require encrypted-at-rest on the laptop.\n\n"
                "## Signal on willingness to pay\n\n"
                "> *\"We'd actually pay more for something we can defend in an audit.\"*\n\n"
                "## Follow-ups\n\n"
                "- [ ] Send threat-model one-pager.\n"
                "- [ ] Demo encrypted-vault flow.\n"
                "- [ ] Confirm SOC 2 evidence we can hand to their auditor.\n"
            ),
        },
        history_revs=0,
    ))

    sessions.append(DemoSession(
        sid="demo-003-eng-1on1",
        title="Engineering 1:1 — demo",
        participants=["Manager", "IC"],
        created_at="2026-05-06T16:30:00Z",
        template="1:1 meeting",
        turns=[
            Turn(0, "How are you doing? Anything blocking you this week?", 0.0, 4.8),
            Turn(1, "Mostly good. The diarization refactor is at a good stopping point, but I'm worried about the test coverage on the new clustering code path.", 5.0, 11.2),
            Turn(0, "Walk me through what's covered and what isn't.", 16.5, 4.4),
            Turn(1, "The happy path is well covered. What's missing is the edge cases — when silhouette validation rejects all candidate counts, when the embeddings are degenerate, when the audio is shorter than the minimum window.", 21.2, 14.6),
            Turn(0, "Those sound like exactly the cases that bite in production. How long would it take you to add coverage?", 36.0, 7.4),
            Turn(1, "Probably a day, day and a half. I'd rather do it now than wait for it to bite.", 43.6, 6.4),
            Turn(0, "Agreed. Let's prioritise that over the perf work this week. The perf work can slip a sprint, the correctness work can't.", 50.2, 9.6),
            Turn(1, "Sounds good. Anything you want me to be thinking about for the next quarter?", 60.0, 5.8),
            Turn(0, "Yeah — we're going to start serious work on the cloud-LLM extension. I'd love you involved in the attestation design from the start.", 66.0, 9.2),
        ],
        notes={
            "1on1.md": (
                "# Engineering 1:1 — demo\n\n"
                "**Date:** 2026-05-06 · **Duration:** 1m 16s\n\n"
                "## Topics\n\n"
                "- Diarization refactor — at a good stopping point.\n"
                "- Test coverage gaps on clustering edge cases (silhouette\n"
                "  rejection, degenerate embeddings, short audio).\n"
                "- Reprioritisation: edge-case tests this week, perf work\n"
                "  slips a sprint.\n"
                "- Q2 stretch: cloud-LLM extension / attestation design.\n"
            ),
        },
        history_revs=1,
    ))

    sessions.append(DemoSession(
        sid="demo-004-all-hands",
        title="All-hands: roadmap walkthrough",
        participants=["Founder"],
        created_at="2026-05-05T10:00:00Z",
        template="Company-wide",
        turns=[
            Turn(0, "Welcome everyone. I want to walk through where we are, where we're going, and the three big bets for the rest of the year.", 0.0, 9.2),
            Turn(0, "First — privacy. Every product decision we make is filtered through one question: would we be embarrassed if every byte of customer data leaked tomorrow? If the answer is yes, we redesign.", 9.4, 14.8),
            Turn(0, "Second — local-first. We are not building a cloud product with a desktop client wrapper. We are building a desktop product that happens to use the cloud carefully, on the customer's terms, with full attestation.", 24.6, 16.4),
            Turn(0, "Third — open source. Our scaffolding, our threat models, our key-management design — all of it is going to be published. The competitive moat is execution, not secrecy.", 41.4, 12.8),
            Turn(0, "Concretely, the three big bets for the rest of the year are: ship the encrypted vault to GA, ship the per-app firewall to GA, and stand up the private-cloud LLM extension behind Tailscale plus Nitro.", 54.6, 14.4),
            Turn(0, "Q&A — happy to take questions. There's a Slack channel open and we'll get to as many as we can.", 69.4, 7.0),
        ],
        notes={
            "summary.md": (
                "# All-hands: roadmap walkthrough\n\n"
                "**Date:** 2026-05-05 · **Duration:** 1m 16s · **Template:** Company-wide\n\n"
                "## Three principles\n\n"
                "1. **Privacy.** Filter every decision through *would we be embarrassed if every byte leaked tomorrow?*\n"
                "2. **Local-first.** Desktop product that uses cloud carefully, not the inverse.\n"
                "3. **Open source.** Scaffolding, threat models, and key management published. Moat is execution, not secrecy.\n\n"
                "## Three big bets for the rest of the year\n\n"
                "- Encrypted vault → **GA**.\n"
                "- Per-app firewall → **GA**.\n"
                "- Private-cloud LLM extension via **Tailscale + AWS Nitro Enclaves**.\n"
            ),
        },
        history_revs=0,
    ))

    sessions.append(DemoSession(
        sid="demo-005-sales-initial",
        title="Sales call: initial outreach (synthetic)",
        participants=["Account Exec", "VP Eng", "Solutions Engineer"],
        created_at="2026-05-04T13:15:00Z",
        template="Sales call",
        turns=[
            Turn(0, "Thanks for hopping on. We saw your team is hiring for security-engineer-platform roles and that you've shipped a lot around at-rest encryption. We thought it'd be worth a quick conversation.", 0.0, 11.4),
            Turn(1, "Yeah, happy to chat. We've been thinking a lot about how we handle customer-call audio internally. Right now it's a mix of tools.", 11.6, 9.6),
            Turn(2, "Could you walk us through your current architecture at a high level — where audio lives, who has access, what controls are in place?", 21.6, 9.8),
            Turn(1, "Sure. Today audio gets uploaded to a third-party transcription service, transcripts come back to a shared drive, and a separate tool does summarisation. The drive is encrypted at the disk level but anyone with read access on the team can see everything.", 31.8, 19.4),
            Turn(0, "That's actually a very common pattern. What we built is end-to-end on the customer's laptop — audio never leaves, transcripts are in an encrypted vault that only unlocks with a YubiKey plus a Touch ID prompt, and the firewall actively blocks our own client from talking to external AI providers.", 51.6, 18.2),
            Turn(2, "Is there an audit trail of who unlocked what when?", 70.2, 5.4),
            Turn(0, "Yes — every key operation lands in a structured log and the typed-DELETE gate means accidental destruction is also auditable. Happy to send you the security write-up after the call.", 75.8, 12.4),
            Turn(1, "Please do. This is exactly the shape of thing we'd be open to evaluating.", 88.4, 5.8),
        ],
        notes={
            "summary.md": (
                "# Sales call: initial outreach (synthetic)\n\n"
                "**Date:** 2026-05-04 · **Duration:** 1m 34s · **Template:** Sales call\n\n"
                "## Account snapshot\n\n"
                "- Hiring for security-engineer-platform roles.\n"
                "- Has shipped at-rest encryption work publicly.\n"
                "- Current state: third-party transcription → shared drive → separate summariser.\n\n"
                "## Their pain\n\n"
                "- Audio leaves the company perimeter today.\n"
                "- Anyone with read on the shared drive can see all transcripts.\n"
                "- No audit trail on who unlocked what when.\n\n"
                "## Their interest signal\n\n"
                "> *\"This is exactly the shape of thing we'd be open to evaluating.\"*\n\n"
                "## Follow-up\n\n"
                "- [ ] Send security write-up (SECURITY.md + threat-model diagram).\n"
                "- [ ] Schedule a 45-minute deep dive with their solutions engineer.\n"
            ),
        },
        history_revs=0,
    ))

    return sessions


def _write_demo_config(target_root: Path) -> None:
    """Drop a settings.json + store.json into the demo Char data dir
    so ``char_audit`` has something to surface in the Char tab. Values
    are picked to *all be in agreement* with what configure-char would
    have written, so the audit tab renders as all-green."""
    settings = {
        "ai": {
            "current_stt_provider": "openai",
            "current_stt_model": "gpt-4o-transcribe",
            "stt": {
                "openai": {
                    "base_url": "http://127.0.0.1:8000/v1",
                    "api_key": "ls_asr_demo_deadbeefdeadbeefdeadbeefdeadbeef",
                },
            },
        },
        "firewall": {
            "block_list": ["sentry.io", "us.i.posthog.com", "app.posthog.com"],
        },
    }
    store = {
        "analytics": {"Disabled": True},
    }
    (target_root / "settings.json").write_text(json.dumps(settings, indent=2))
    (target_root / "store.json").write_text(json.dumps(store, indent=2))


def seed(target_root: Path, *, clean: bool) -> list[Path]:
    """Populate ``target_root`` with demo sessions. Returns the list of
    session directories written."""
    if clean and target_root.exists():
        shutil.rmtree(target_root)
    target_root.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []
    for session in _demo_sessions():
        written.append(_write_session(target_root, session))

    _write_demo_config(target_root)
    return written


def _humanise_count(paths: Iterable[Path]) -> str:
    paths = list(paths)
    return f"{len(paths)} session{'' if len(paths) == 1 else 's'}"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.strip().split("\n")[0])
    p.add_argument(
        "--target",
        type=Path,
        default=DEFAULT_TARGET,
        help=(
            "Where to write the demo Char data dir. Default: "
            f"{DEFAULT_TARGET}. NEVER point this at your real Char data dir."
        ),
    )
    p.add_argument(
        "--clean",
        action="store_true",
        help="Remove ``target`` first if it already exists.",
    )
    args = p.parse_args(argv)

    real_char_dir = Path.home() / "Library" / "Application Support" / "hyprnote"
    if args.target.resolve() == real_char_dir.resolve():
        print(
            f"refusing to seed the REAL Char data dir at {real_char_dir}.\n"
            "Pick a different --target.",
            file=sys.stderr,
        )
        return 2

    t0 = time.time()
    written = seed(args.target, clean=args.clean)
    elapsed = time.time() - t0
    print(
        f"wrote {_humanise_count(written)} to {args.target}/sessions/ "
        f"in {elapsed:.1f}s",
        file=sys.stderr,
    )
    for path in written:
        print(f"  · {path.name}", file=sys.stderr)
    print(
        "\nnext: ./run.sh demo            (start an isolated demo inspector)\n"
        "      ./run.sh demo --port 8002 (pick a port)",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
