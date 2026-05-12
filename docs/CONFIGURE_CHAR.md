# Configure Char

> Moved from the top-level README on 2026-05-12 as part of
> the condense-and-link pass. The content below is the
> canonical reference. The README keeps a short pointer
> paragraph linking back here.
>
> **Related docs:** [`docs/CHAR_REVIEW.md`](CHAR_REVIEW.md), [`docs/INTEGRATION.md`](INTEGRATION.md), [`docs/CHAR_VERSION_PIN.md`](CHAR_VERSION_PIN.md)

### Automated (recommended)

```bash
./run.sh configure-char
```

This is the same hook bootstrap offers, exposed as a standalone command so
you can re-run it any time. It:

- Locates Char's `settings.json` at `~/Library/Application Support/hyprnote/`.
- Quits Char.app if it's running (so the edit doesn't get clobbered on next save).
- **If `stt.openai.api_key` already holds a real-looking key**, prompts whether
  to save it (default Yes) to `~/.config/local_scribe/char-openai-key.<ts>.txt`
  with `chmod 600` before overwriting. If you accidentally pasted a real OpenAI
  project key into Char, this preserves it; you should still rotate that key on
  platform.openai.com because it sat unencrypted in the config file.
- Always backs up the whole `settings.json` to `settings.json.bak.<ts>` for
  trivial rollback.
- Patches exactly four keys (everything else — LLM provider, templates,
  calendars — is left untouched):

  | key | value |
  |---|---|
  | `ai.current_stt_provider` | `openai` |
  | `ai.current_stt_model` | `gpt-4o-transcribe` (progressive/SSE — bypasses Char's 60-second non-streaming idle abort, supports any audio length) |
  | `ai.stt.openai.base_url` | `http://127.0.0.1:8000/v1` |
  | `ai.stt.openai.api_key` | `local` |

- Offers to relaunch Char (default Yes).

Safe to re-run: if `api_key` is already `local`, the backup-key prompt is
skipped; only `settings.json` is re-snapshotted.

### Manual (if you'd rather poke the UI)

Char has **two separate transcription paths** — point both at this server.

#### 1. Live recording (Custom provider)

Used while Char is recording a meeting in real time.

| field | value |
|---|---|
| **Model being used** | Custom (the `nova-2` string is decorative — this server ignores it) |
| **Configure Providers → Custom → Base URL** | `http://127.0.0.1:8000` |
| **Configure Providers → Custom → API Key** | any non-empty string (auth is ignored locally) |

This routes Char's WebSocket streaming and batch live-audio path through our
Deepgram-compatible `/v1/listen` endpoint. (It's "batch over WebSocket" — final
transcript only, no interim partials, since neither Parakeet nor faster-whisper
streams natively.)

#### 2. "Generate transcript" on existing audio (OpenAI Batch Only provider)

Used when you click *Generate* on a note that already has audio. Char's
*Custom* provider is **Deepgram-only** and only used for live recording —
batch file imports go through whichever provider you pick from its "Batch
Only" list. We expose an OpenAI Whisper API-compatible endpoint so you can
point Char's bundled OpenAI provider at us.

| field | value |
|---|---|
| **Model selector** | `gpt-4o-transcribe` *(progressive/SSE — bypasses Char's 60-second non-streaming idle abort that breaks long files; this is the model `configure-char` writes by default)* |
| **Configure Providers → OpenAI → API Key** | any non-empty string |
| **Configure Providers → OpenAI → Advanced → Base URL** | `http://127.0.0.1:8000/v1` |

`gpt-4o-transcribe` triggers Char's progressive batch path, which streams
SSE deltas and resets its idle timer on each one. Our endpoint also
accepts `gpt-4o-transcribe-diarize` for short files where you want the
structured `segments[*].speaker` shape, but anything that takes more
than 60s to transcribe must use `gpt-4o-transcribe`.

For short files, our streaming endpoint still runs sherpa-onnx
diarization and inlines `Speaker N: …` prefixes into the streamed text
(default ON, ~3-4s of extra latency on a 60s clip). `verbose_json`,
`json`, `text`, `srt`, and `vtt` are all supported too on the
non-streaming path.

**Diarization auto-K (default)** — by default the server runs a
**silhouette-validated auto-K pipeline** that picks the speaker count
from the data itself (no per-call tuning required). This is the same
approach AWS Transcribe and pyannote.audio v3.1+ use:

  1. Run sherpa-onnx pyannote 3.0 segmentation with a tight threshold
     to get rich micro-clusters (often hundreds on long audio).
  2. **Drop micro-clusters with < 3 s of total speech** — these are
     virtually always artefacts (a cough, a music sting, brief
     crosstalk) and their embeddings are noisy enough to swamp
     clustering. This is the single biggest quality win for long
     recordings: a 114-min meeting goes from 615 → ~300 reliable
     centroids.
  3. Extract one TitaNet embedding per surviving cluster.
  4. Sweep K across `[k_min=2, k_max=10]`, running spectral
     clustering at each K and scoring with the **silhouette score**
     (distance-based, canonical Rousseeuw definition). Pick the K
     with the highest silhouette, with a preference for the larger K
     when the top two scores are within 0.02. The **monologue gate**
     (mean centroid affinity ≥ 0.80) overrides to K=1 when there
     really is just one speaker.
  5. **Airtime validation**: if the chosen K produced a sliver
     cluster (< 30 s of speech AND < 3 % of total airtime), step
     down to K−1 and re-cluster. Catches the case where spectral
     clustering splits one acoustically-stable speaker into two
     thin clusters that both score reasonably.
  6. Remap raw segments through the centroid → final-label mapping.

Why silhouette and not eigengap: the textbook eigengap heuristic
picks K from the largest gap in the Laplacian's eigenvalues, but
its argmax has a well-known failure mode where the K=1 → K=2 gap
dominates the secondary maxima. On a 4-speaker legal call we hit
exactly this — eigengap picked K=2 even though K=4's silhouette was
demonstrably higher and produced four clusters with 5–28 min of
real airtime each. Silhouette directly measures within-cluster vs.
between-cluster separation, so the elbow at the true K is always
the global maximum.

The full pipeline added ~10 s to the diarization wall time (~360 s →
~370 s on a 114-min recording).

**Manual overrides** — you can still force a specific configuration
when auto-K gets it wrong (very noisy 1:1s where two voices sound
similar enough that any algorithm collapses them, etc.):

  * **One-off, no restart:** redo the session with the speaker count you
    know to be true:
    ```bash
    ./run.sh redo-session "Maus Meeting" --speakers 2
    ./run.sh redo-session 77f87727 --speakers 3 --cluster-threshold 0.85
    ```
    `redo-session` re-runs ASR + diarization on the session's existing
    `audio.mp3` and overwrites its `transcript.json` in-place. Switch
    sessions in Char (or relaunch it) to reload. Match by full UUID,
    UUID prefix, or session-title substring.
  * **Server-wide:** set `NUM_SPEAKERS=2` (or 3, etc.) before
    `./run.sh start` — every Generate forces that many speakers.
    Set `CLUSTER_THRESHOLD=0.85` to favour fewer, larger clusters across
    the board.
  * **Disable entirely:** set `OPENAI_BATCH_DIARIZE=0` or
    `asr.diarization.enabled=false` in `~/.config/local_scribe/config.json`.
    Single `speaker_0` placeholder, ~1s instead of ~5+ min on long audio.
  * **Per-request opt-out:** append `?diarize=0` to the OpenAI POST URL
    (used by `./run.sh redo-session --no-diarize`).
  * **Richest output:** `./run.sh transcribe FILE` runs the same
    diarization plus an LLM pass that maps `speaker_0/1/...` to the
    actual people's names by reading conversational cues.

The diarization auto-skip cap is `MAX_DIARIZE_SECONDS=14400` (4 hours)
by default — generous enough for any plausible single-meeting recording
while still bounding a runaway run on a 10-hour podcast. Set to `0` in
`config.json` (or env) to remove the cap entirely.

#### Speaker confidence + airtime

When auto-K diarization finishes, every micro-cluster gets a per-point
silhouette coefficient against its assigned final cluster
(`diarization_backend._per_point_silhouette`). That scalar in [−1, 1]
is then linearly mapped to a 0..1 *cluster-membership confidence* via
`silhouette_to_confidence`:

| silhouette | confidence | interpretation                                  |
|-----------:|-----------:|-------------------------------------------------|
| +1.0       | 100%       | this turn sits firmly inside its cluster        |
| +0.5       |  75%       | well-separated; easy call                       |
|  0.0       |  50%       | cluster boundary — could go either way          |
| −0.5       |  25%       | likely misclassified                            |
| −1.0       |   0%       | definitely the wrong speaker                    |

The confidence is propagated end-to-end:

* **diarization segments** carry `confidence` per turn
* **words** carry `speaker_confidence` (copied from the turn they fall in)
* **char_persist** writes them into `local_scribe.diarization.word_confidences`
  as a parallel array indexed by word position (Char's word schema is
  strict so we don't add a field to it directly)
* **inspector UI** shows `Speaker N (87%)` next to each paragraph and
  tints the percentage muted-red below 50%, amber 50–80%, green ≥80%
* **`/transcript.txt`** download includes the percentage inline:
  `speaker_0 (87%): hello world.`

Per-session **speaker airtime** is computed by
`asr_server._compute_speaker_airtime` and embedded as
`local_scribe.diarization.speakers`:

```json
{
  "speakers": [
    {"label": "speaker_0", "seconds": 1820.5, "percent": 0.42,
     "mean_confidence": 0.78, "word_count": 3214},
    {"label": "speaker_1", "seconds": 1500.1, "percent": 0.34,
     "mean_confidence": 0.81, "word_count": 2660},
    {"label": "speaker_2", "seconds": 612.4,  "percent": 0.14,
     "mean_confidence": 0.65, "word_count": 1180},
    {"label": "speaker_3", "seconds": 440.9,  "percent": 0.10,
     "mean_confidence": 0.61, "word_count": 850}
  ]
}
```

`percent` is share of *speech* time (silent gaps aren't attributed),
so the values sum to 100% across the speakers who actually spoke.

The inspector renders this as a "Speaker airtime" panel under each
session's transcript with one bar per speaker. The same data ends up
in the per-request server log so you can spot speaker-imbalance bugs
without opening a UI:

```
[openai abc...] done in 71.42s (..., speakers=4), 78k chars, lang=en
  airtime: speaker_0=42% (12m 30s, 78% conf), speaker_1=34% (10m 02s, 81% conf),
           speaker_2=14% (4m 13s, 65% conf), speaker_3=10% (3m 02s, 61% conf)
```

If a cluster's mean confidence is in the red zone (below 50%) you've
got a "K is technically right but one speaker is muddy" situation —
usually two acoustically similar voices got split, or one speaker
fragmented across two clusters. The numbers tell you to either re-run
with `--speakers N` set to a known-good count, or accept the warning
that *that particular speaker's lines* should be read with a grain of
salt.

The confidence field is intentionally omitted when diarization
collapses to K=1 (single-speaker recordings + the airtime-fallback
step-down path). With only one cluster there's no membership decision
to be confident about, and emitting `1.0` there would be misleading.

#### Transcript history (auto-backup on re-transcription)

Every time `transcript.json` is overwritten — by `./run.sh redo-session`,
by a fresh Generate in Char, or by any other code path that calls
`char_persist.write_transcript_for_audio` — the previous file is copied
to

```
<char-session>/.local_scribe_history/<YYYYMMDDTHHMMSSZ>_<sha7>.json
```

before the new one is written. Each archive is the previous file
**verbatim**, with one extra top-level key:

```json
{
  "transcripts": [ ... Char schema unchanged ... ],
  "local_scribe": {
    "written_at_iso": "2026-05-10T21:08:44Z",
    "asr_backend": "parakeet",
    "asr_model": "mlx-community/parakeet-tdt-0.6b-v3",
    "audio_duration_seconds": 59.148,
    "audio_sha256": "168eec5405db7fec...",
    "word_count": 11,
    "speaker_count": 2,
    "language": "en",
    "provider": "openai",
    "session_id": "e02ea91c-b081-410c-b01d-71187cf545e3",
    "diarization": {
      "algorithm": "auto_silhouette" | "manual_ahc" | "skipped",
      "enabled": true,
      "num_speakers": 2,
      "num_speakers_override": null,
      "cluster_threshold_override": null,
      "skipped_reason": null
    }
  }
}
```

Char ignores unknown top-level keys (verified against its tinybase
persister source), so the file is fully round-trippable — you can copy
an archive back over `transcript.json` by hand to restore it.

The inspector UI shows the history per session:

```
http://127.0.0.1:8001  →  Open session  →  Transcript history
```

…with **View JSON**, **Download**, and **Delete** for each archive.
The session list also shows a `· N archived` badge so you know which
sessions have backups without opening them.

Programmatic surface (loopback only, same trust model as the rest of
the inspector):

```bash
# list backups
curl http://127.0.0.1:8001/api/sessions/<uuid>/history

# fetch one
curl http://127.0.0.1:8001/api/sessions/<uuid>/history/<filename>.json

# delete one (idempotent: 404 if already gone)
curl -X DELETE http://127.0.0.1:8001/api/sessions/<uuid>/history/<filename>.json
```

Defaults & limits:

* **Location**: alongside the session in Char's data dir, so backups
  travel with the audio if you move your `hyprnote/sessions` directory.
* **Cap**: 50 archives per session (oldest pruned by mtime). Override
  by editing `transcript_history.DEFAULT_MAX_BACKUPS`.
* **Permissions**: `.local_scribe_history/` is created with mode 0o700
  so other macOS user accounts on the same machine can't read it.
* **Filename validation**: GET / DELETE refuse anything containing
  `/`, `\`, or `..`. The route matcher also rejects URL-decoded path
  separators before the validator runs.

#### 3. Summary / Intelligence (LM Studio)

| field | value |
|---|---|
| **Configure Providers → Char Recommended → LLM** | LM Studio @ `http://127.0.0.1:1234`, model `qwen3-30b-a3b-instruct-2507` |

After this, every call you record (live) AND every audio file you import
(Generate) routes through Parakeet, with Qwen producing the note.

