# Race-audio transcript + summary pipeline

Standalone CLIs that turn an iPhone Voice Memos `.m4a` (one-speaker-per-channel
Hollyland body-mic recording) into three artifacts:

1. **Raw transcript JSON** — UTC-anchored segments + per-word timestamps where
   the chosen ASR backend supports them.
2. **Markdown summary** — race chronological/topical roll-up *or* lesson study
   guide organised by skill (mode-selectable).
3. **Keypoints JSON** — structured list of moments worth chaptering, each with
   a UTC anchor that has been snapped to a real spoken word in the transcript.

Times stay in UTC end-to-end so a future video-sync step is mechanical. Both
CLIs are deliberately separate from `Scripts/race_upload.py` so the prompts can
be iterated cheaply without touching the production pipeline.

---

## CLIs

Cloud processing requires explicit permission: add `--allow-cloud-processing`
when sending audio to OpenAI or transcripts to Anthropic. `new_lesson.py` defaults
to local MLX transcription; use `--local-only` to also skip cloud summarization.
Without either flag, the one-shot workflow stops before processing. The standalone
summarizer always requires `--allow-cloud-processing` unless using `--dry-run`.
Confirm that the people recorded have agreed to the selected processing mode.

The everyday workflow is the **one-shot** form (`new_lesson.py`); the
underlying transcribe + summarize CLIs remain available for fine-grained
control or debugging.

### `Scripts/find_new_audio.py` — list pending audio with friendly basenames

```bash
conda run --no-capture-output -n sailing python Scripts/find_new_audio.py \
    [--audio-dir /Users/viola/Data/Insta360/Audio] \
    [--transcripts-dir Analyze/Transcripts] \
    [--all]                                 # also list already-transcribed
    [--basenames-only]                      # one basename per line; scriptable
```

Read-only scan of `AUDIO_DROP_DIR`. For each `.m4a/.wav/.mp3/.aac` it resolves
the UTC start (sidecar > Voice Memos DB; same `Libraries.voice_audio` path the
rest of the pipeline uses), ffprobes the duration, and derives a Pacific-local
**friendly basename** of the form `YYYY-MM-DD_HHMM_<dur>` — e.g.
`2026-05-03_0943_7h31m`. Files whose friendly basename (or whose source
basename, for legacy artifacts) already has a `<base>.summary.md` are marked
transcribed and suppressed by default; `--all` shows them too.

Anything that fails resolution (no sidecar, Voice Memos miss, ffprobe error)
lands under "Unresolvable" with a one-line reason — fix the cause (drop a
sidecar, open Voice Memos.app, etc.) and re-run.

### `Scripts/new_lesson.py <basename>` — end-to-end one-shot

```bash
conda run --no-capture-output -n sailing python Scripts/new_lesson.py \
    2026-05-03_0943_7h31m
    # or:
    --source "/Users/viola/Data/Insta360/Audio/some_recording.m4a"

    [--mode {lesson,race}]              # default lesson
    [--engine {openai,mlx}]             # default mlx
    [--local-only | --allow-cloud-processing]
    [--openai-model gpt-4o-transcribe]
    [--summary-model claude-opus-4-7]
    [--max-tokens 24000]                # long-lesson default
    [--force]                           # redo even if outputs exist
    [--dry-run]                         # resolve + print, don't call APIs
```

Bundles `transcribe_audio.py` (forced to write under the friendly basename
via `--basename`) and `summarize_transcript.py` (lesson defaults). Idempotent
on the basename — re-runs skip already-produced artifacts unless `--force`.
Works equally on `basename` (looked up by scanning the audio dir) or
`--source <path>` (basename auto-derived). Prints the eventual
`lesson.html?audio=<basename>` URL on success.

### Friendly basename

`Libraries.audio_transcribe.friendly_basename(start_utc, duration_s)` is the
single source of truth: Pacific-local `YYYY-MM-DD_HHMM_<dur>` (e.g.
`2026-05-03_0943_7h31m`). Same `(start, duration)` always resolves to the same
basename, so `find_new_audio.py` and `new_lesson.py` always agree on the name.

The source `.m4a` filename is **not** renamed — `Libraries/voice_audio.py` and
its `applied.json` manifest still key on it, and the iPhone export name carries
the raw UTC + Voice-Memos export ID that's useful for forensics. Only the
lesson artifacts (`<basename>.transcript.*.json`, `.summary.md`,
`.keypoints.json`) take the friendly basename. The transcript JSON header
preserves the source filename in `audio_file`, so the link back to the raw
recording is one open-file away.

Collision handling: if two recordings started in the same Pacific-local
minute, the auto-derived basenames would collide; in practice this never
happens with a single-person workflow. If it does, pass `--basename
<custom>` explicitly to `transcribe_audio.py` to disambiguate.

### `Scripts/transcribe_audio.py <audio>` — audio → transcript JSON

```bash
conda run --no-capture-output -n sailing python Scripts/transcribe_audio.py \
    "/path/to/recording.m4a" \
    [--out-dir Analyze/Transcripts] \
    [--engine {mlx,openai}]                   # default mlx
    [--mlx-model mlx-community/whisper-large-v3-mlx] \
    [--openai-model {gpt-4o-transcribe,gpt-4o-mini-transcribe,whisper-1}] \
    [--language en] \
    [--speaker-l "Skipper"] [--speaker-r "Tactician"] \
    [--basename {auto,<custom>}]              # default: source filename (legacy)
    [--force]
```

Output: `<out-dir>/<basename>.transcript.<engine-tag>.json`. Idempotent —
re-running on the same audio + engine skips with a message unless `--force`.

`--basename` controls the output basename:
- omitted → source filename minus extension (legacy behaviour).
- `auto` → resolves the Pacific-local friendly basename
  (`YYYY-MM-DD_HHMM_<dur>`) via `Libraries.audio_transcribe.resolve_friendly_basename`.
- any other string → used verbatim.

`Scripts/new_lesson.py` always passes the friendly basename; you only need
this flag for one-off manual runs.

### `Scripts/summarize_transcript.py <transcript_json>` — transcript → md + keypoints

```bash
conda run --no-capture-output -n sailing python Scripts/summarize_transcript.py \
    Analyze/Transcripts/<basename>.transcript.openai-gpt-4o-transcribe.json \
    --mode {race,lesson}                       # default race
    [--model claude-opus-4-7] \
    [--max-tokens 8192] \
    [--out-md PATH] [--out-keypoints PATH] \
    [--no-summary] [--no-keypoints] \
    [--dry-run] \
    [--min-confidence 0.8]
```

Outputs `<basename>.summary.md` and `<basename>.keypoints.json` next to the
transcript by default. `--dry-run` prints the system prompt + tool definition
+ the user message (truncated) without calling Anthropic.

The Anthropic call uses `messages.stream` (not `messages.create`): the SDK
requires streaming for any request that may exceed 10 min wall, and a
forced-tool-use call with `--max-tokens 32000` on Opus can. We drain the
event stream and read `.get_final_message()` — same shape as a non-stream
response. Long-form lesson summaries (full ~7 h recordings, 60+ keypoints
across 20+ skills) need `--max-tokens` ≥ 24000 to keep the summary +
keypoints array from being truncated mid-tool-call.

---

## Pluggable ASR backends

| Backend             | Class                | Default model                              | Word timestamps | Cost / hour audio | When to use                                                              |
|---------------------|----------------------|--------------------------------------------|-----------------|-------------------|--------------------------------------------------------------------------|
| **MLX-Whisper**     | `MlxWhisperBackend`  | `mlx-community/whisper-large-v3-mlx`       | Yes (DTW)       | Free (local)      | Day-to-day. Mac Studio GPU, ~30–50× realtime.                            |
| **OpenAI**          | `OpenAiBackend`      | `gpt-4o-transcribe`                        | No              | ~$0.36/hr         | Best terminology accuracy on noisy mic-on-person audio (e.g. `vang`).    |
| **OpenAI** (mini)   | `OpenAiBackend`      | `gpt-4o-mini-transcribe`                   | No              | ~$0.18/hr         | Cheap iteration. Quality between whisper-1 and gpt-4o-transcribe.        |
| **OpenAI** (legacy) | `OpenAiBackend`      | `whisper-1`                                | Yes (segment+word) | ~$0.36/hr      | When you want OpenAI quality *and* word timestamps in one shot.          |

`gpt-4o-transcribe` and `gpt-4o-mini-transcribe` only return `text` (no native
timestamps), so segment timing comes from VAD-chunk boundaries (~1–10 s
typical). Acceptable for the ~1 s sync target; for sub-second precision use
`whisper-1` or MLX.

Both backends consume the same chunked-WAV stream produced by the Silero VAD
pre-pass — the only divergence is the API call.

---

## Pre-processing pipeline

For every recording, regardless of backend:

1. **`_resolve_audio_start(path, duration_s)`** (reused from
   `Libraries/voice_audio.py`) returns the recording's UTC start time.
   Resolution order is **strict**: sidecar `<name>.start.txt` > Voice Memos DB
   > `VoiceAudioUnresolvable`. **No mtime fallback** — iMessage / iCloud /
   iTunes export rewrites mtime to save-time, so it is uncorrelated with
   recording start. See the project CLAUDE.md "Mic-on-Person Audio Swap"
   section for the Voice Memos DB / Full Disk Access requirements.

2. **Channel split** — `ffprobe` reads channel count.
   - 1 channel → downmix to 16 kHz mono WAV, label "Speaker A".
   - 2 channels → split via `ffmpeg -af pan=mono|c0=cN` (modern replacement
     for the removed `-map_channel`) into `LEFT.wav`/`RIGHT.wav`, both
     16 kHz mono PCM, into a `tempfile.mkdtemp()` directory.

3. **Mono-collapse detection** (stereo only, `_detect_collapsed_mono`).
   Hollyland body-mic receivers running in safety/track mode mix two mics into
   a single stream before sending stereo to the recorder, so both channels
   carry identical audio. We detect this with two checks:
   - Pearson correlation `corr(L, R)`
   - Residual RMS ratio `RMS(L − R) / RMS(L)`

   Triggers when **either** `corr > 0.998` **or** `ratio < 0.02`. If
   collapsed, we transcribe one channel only (relabelled `M`) and write
   `channel_layout: "stereo_collapsed_to_mono"` into the transcript along
   with the stats. A `both_silent` short-circuit handles dead-microphone
   recordings.

4. **Silero VAD pre-pass** per surviving channel
   (`min_silence_duration_ms=500`, `speech_pad_ms=200`,
   `sampling_rate=16000`). Uses a stdlib `wave` + numpy decoder so we do not
   pull in `torchcodec` (which `silero-vad`'s own `read_audio` requires
   since `torchaudio>=2.9`).

5. **Per-chunk ASR**: extract each VAD chunk to a temp WAV, send to backend,
   convert relative segment/word offsets to absolute UTC by adding
   `chunk_start_utc = audio_start_utc + chunk.start`.

6. **Merge + sort** all per-channel segments by `start_utc`, atomic write
   (`tmp` + `os.replace`).

VAD pre-pass is shared infrastructure — both backends see the same chunked
stream. It buys us:
- Far fewer Whisper hallucinations on silence ("thank you for watching" loops).
- Natural sentence-level chunking that pairs well with the OpenAI path's lack
  of native timestamps.

---

## Race vs lesson mode

Selected at summarisation time via `--mode`. The mode picks which
`(domain_context, keypoint_categories)` pair is loaded from
`Libraries/sailing_glossary.MODE_REGISTRY`.

### `--mode race` (default)

Audience is the racing crew. Output:
- `## Time overview`, `## Topical overview` (with sub-sections for sail trim,
  tactical decisions, boat handling, comms/mistakes), `## Highlights`.
- Keypoints have a fixed `category` enum:
  `start_sequence`, `tactical_decision`, `sail_change`, `mark_rounding`,
  `boat_handling`, `weather_call`, `traffic_call`, `mistake`,
  `comms_breakdown`, `debrief_moment`, `other`.
- Density: ~1 keypoint per 1–3 minutes.

### `--mode lesson`

Audience is the learner reviewing a coaching session. Output is a personal
study guide organised **by skill, not by time**:
- One `##` section per discovered skill (model picks the names — must be
  concrete, e.g. `## Vang trim in heavy westerlies`, never `## Sail trim`).
- Each section opens with a bold `**Synthesis:**` line distilling the
  coach's overall point in 1–2 sentences. The reader should be able to skim
  only the synthesis lines and walk away with the lesson.
- Bullets under each skill list the specific things the coach said, prefixed
  with `HH:MM:SS` UTC copied verbatim from the playscript.
- Sections ordered by importance to the learner, not chronologically.
- Optional `## Action items` section at the end if the coach explicitly named
  things to do or try.
- Keypoints have a **free-form `category` string** instead of an enum — the
  model uses the same string as the section heading so downstream tooling
  can group on it.

What the lesson prompt explicitly **drops**: biographical/credentials content,
event-calendar talk ("five months till NAs"), logistics, banter, and any
re-teaching of fundamentals.

---

## Output formats

### Transcript JSON — `<basename>.transcript.<engine>.json`

```json
{
  "audio_file": "20260503-094308.m4a",
  "audio_sha1": "...",
  "audio_start_utc": "2026-05-03T16:43:08.137Z",
  "audio_duration_s": 26959.5,
  "audio_start_source": "voice_memos[db]:zdate@row 472",
  "channel_layout": "stereo | stereo_collapsed_to_mono | mono",
  "channels": {
    "L": {"speaker": "Speaker A"},
    "R": {"speaker": "Speaker B"}
  },
  "model": "openai:gpt-4o-transcribe",
  "vad": "silero (min_silence_duration_ms=500)",
  "language": "en",
  "initial_prompt_sha1": "...",
  "channel_collapse_stats": {            // present only when collapse detected
    "rms_l": 0.0931, "rms_r": 0.0928,
    "rms_diff_over_rms_l": 0.0043,
    "pearson_corr": 0.999992
  },
  "segments": [
    {
      "start_utc": "2026-05-03T16:43:14.500Z",
      "end_utc":   "2026-05-03T16:43:18.000Z",
      "channel": "M",
      "speaker": "Speaker A",
      "text": "Trim the main half a turn.",
      "no_speech_prob": 0.02,
      "avg_logprob": -0.41,
      "words": [                          // null for gpt-4o-transcribe
        {"start_utc": "...", "end_utc": "...", "word": "Trim", "prob": 0.97}
      ]
    }
  ]
}
```

### Summary markdown — `<basename>.summary.md`

Mode-dependent shape; see the previous section for the templates.

### Keypoints JSON — `<basename>.keypoints.json`

```json
{
  "audio_file": "20260503-094308.m4a",
  "audio_start_utc": "2026-05-03T16:43:08.137Z",
  "model": "claude-opus-4-7",
  "mode": "lesson",
  "summary_anchored_to": "<basename>.summary.md",
  "keypoints": [
    {
      "utc": "2026-05-03T17:28:31.237Z",
      "utc_source": "excerpt_snap_local",
      "utc_original": "2026-05-03T17:27:57.000Z",
      "label": "Backstay-fine-tune partnership",
      "category": "Backstay ↔ fine-tune partnership",
      "excerpt": "[Speaker A] Every backstay move requires a fine-tune move...",
      "summary": "Coach frames backstay and fine-tune as a partnered pair...",
      "confidence": "high"
    }
  ]
}
```

The `utc` field has been **snapped to the start of the transcript segment
that actually contains the excerpt** (see next section). `utc_original`
preserves what the LLM returned so you can audit drift; `utc_source`
records which strategy fired.

---

## Excerpt-based snap

The summarizer LLM is asked to copy `HH:MM:SS` timestamps verbatim from the
playscript, but in practice it sometimes pattern-matches across long
playscripts and lands tens of seconds away from the moment the excerpt
actually came from. The excerpt itself, on the other hand, is verbatim
transcript text — so we treat it as the authoritative anchor and use the
LLM's `utc` only as a tiebreaker / locality hint.

For each keypoint in `Scripts/summarize_transcript.py::snap_keypoints`:

1. Strip any leading `[Speaker]` tag from the excerpt's first line, take
   the first sentence (capped at 80 chars), and normalise (lower-case,
   alpha-numeric + spaces only) → the **search phrase**.
2. Scan all transcript segments for ones whose normalised text contains
   that phrase. Prefer hits within ±120 s of the LLM's `utc` (`utc_source:
   "excerpt_snap_local"`); fall back to the earliest matching segment
   anywhere if no local hit (`"excerpt_snap_global"`).
3. If no text match at all, fall back to the closest segment by
   `start_utc` (`utc_source: "time_snap"`).
4. If neither anchor exists, `utc_source: "unsnapped"`.

Net effect: every keypoint links to a moment that was actually transcribed,
and excerpt-bearing keypoints land on the right segment even when the LLM
quoted a time-of-day from somewhere else in the playscript. The pre-`dc90ae1`
word-snap strategy was bisecting to the nearest word by time alone, which
made the symptom visible — text-anchored snap fixes the root cause.

---

## API key management — `Libraries/env.py` + `.env`

We ship a tiny `load_dotenv(path=None, *, override=False)` that reads
`KEY=VALUE` lines from a repo-local `.env` file. **Existing environment
variables win** unless `override=True`, so a shell/conda-provided secret is
never silently replaced by a stale template value.

Both CLIs call `load_dotenv()` at the very top of `main()`, before reading
`OPENAI_API_KEY` / `ANTHROPIC_API_KEY`. The `.env` file lives at the repo
root, is `.gitignore`d, and contains:

```
OPENAI_API_KEY=sk-proj-...
ANTHROPIC_API_KEY=sk-ant-...
```

This replaced an earlier 1Password-CLI / service-account approach that kept
hitting Touch ID timeouts. The `.env` file is fine for a single-user
developer machine; do not commit it.

---

## Conda environment additions

```bash
conda run --no-capture-output -n sailing pip install \
    mlx-whisper silero-vad openai anthropic
```

- `mlx-whisper` pulls `mlx`, `numba`, `huggingface_hub`. First MLX run
  downloads `mlx-community/whisper-large-v3-mlx` (~3 GB) to
  `~/.cache/huggingface`.
- `silero-vad` pulls `torch` (CPU is fine — VAD is tiny). The model
  itself is ~5 MB and ships in the package. **Note:** installing this
  upgraded torch from 1.13.1 → 2.11.0 in the `sailing` env. If anything
  else in the env breaks, that's the change to look at.
- `openai` and `anthropic` are small.
- `ffmpeg`/`ffprobe` already present at `/Users/viola/Bin/ffmpeg` and
  `/opt/homebrew/bin/ffprobe`.

---

## Cost & timing benchmarks

Measured on the 60-min Shilshole lesson clip
(`/tmp/shilshole_clip60m.m4a`, 480 VAD chunks, mono-collapsed):

| Stage             | Backend                    | Wall time | API cost  |
|-------------------|----------------------------|-----------|-----------|
| Transcribe        | `openai:gpt-4o-transcribe` | 7m 1s     | ~$0.36    |
| Transcribe (alt)  | `mlx:large-v3` (local GPU) | ~2m       | $0        |
| Summarise         | `claude-opus-4-7`          | 2m 11s    | ~$0.65    |

Total per lesson hour: **~$1.00 and ~9 min wall** with the cloud combo, or
**~$0.65 and ~4 min** if you transcribe locally with MLX.

The Anthropic system prompt + tool schema are marked
`cache_control: ephemeral`, so iterating on the same transcript only re-charges
for the user message + output.

---

## Known limitations

- **Mic-on-person stereo is currently mono in practice.** The Hollyland
  receivers mix the two body mics before stereo encoding, so L and R are
  identical (`corr ≈ 0.999992` measured). Pipeline detects this and
  transcribes once. Routing fix on the receiver side is needed to get true
  per-mic separation.
- **`gpt-4o-transcribe` has no native timestamps.** VAD-chunk boundaries are
  the only timing source. For VAD chunks > a few seconds, in-chunk word
  timing is unavailable; keypoints snap to the chunk-start.
- **Whisper hallucinations in silence.** Silero VAD pre-pass eliminates
  the worst of it. Residual hallucinations are kept in the JSON with their
  `no_speech_prob` so a downstream filter can drop them. The summarizer
  drops `no_speech_prob > 0.8` by default (override with `--min-confidence`).
- **Sailing-specific misrecognitions.** `WHISPER_INITIAL_PROMPT` in
  `Libraries/sailing_glossary.py` biases decoding but is capped at ~244 tokens
  by Whisper. Iterating on the prompt is the v1 fix; spelling normalisation
  is out of scope. Empirically `gpt-4o-transcribe` is materially better than
  MLX-large-v3 on sailing terminology (heard `vang` correctly where MLX
  produced `bang`).
- **MLX word timestamps drift.** Cross-attention DTW is ~0.2–0.5 s typical,
  occasionally ~1 s. Within the ~1 s sync target.
- **Lesson summarisation is not auto-invoked by `race_upload.py`.** The audio
  *copy* step (`voice_audio` stage) runs as part of the pipeline so new
  recordings show up in `AUDIO_DROP_DIR` automatically. Transcription +
  summarisation is a deliberate manual step (cost, prompt iteration) — run
  `Scripts/find_new_audio.py` to see what's pending and
  `Scripts/new_lesson.py <basename>` to process one. Auto-invocation is a
  follow-up once the prompts and outputs are stable.

---

## Quick start

```bash
# 1. One-time
conda run --no-capture-output -n sailing pip install mlx-whisper silero-vad openai anthropic
# Put OPENAI_API_KEY and ANTHROPIC_API_KEY into <repo>/.env

# 2. After race_upload (which copies new audio into AUDIO_DROP_DIR), see what's pending
conda run --no-capture-output -n sailing python Scripts/find_new_audio.py
# -> Pending (3):
#      basename               dur    start (Pacific)           source
#      2026-05-03_0943_7h31m  7h31m  Sun 2026-05-03 09:43 PDT  2026-05-03T16_43_08Z 20260503-094308.m4a
#      ...

# 3. One-shot transcribe + summarize the chosen lesson
conda run --no-capture-output -n sailing python Scripts/new_lesson.py 2026-05-03_0943_7h31m

# 4. Deploy + view
cd Src && npm run deploy
# Then https://regattaview.net/lesson.html?audio=2026-05-03_0943_7h31m
```

Underlying CLIs (manual fine-grained control):

```bash
conda run --no-capture-output -n sailing python Scripts/transcribe_audio.py \
    "/Users/viola/Data/Insta360/Audio/<file>.m4a" \
    --engine openai --openai-model gpt-4o-transcribe \
    --basename auto                 # write under friendly basename

conda run --no-capture-output -n sailing python Scripts/summarize_transcript.py \
    Analyze/Transcripts/<basename>.transcript.openai-gpt-4o-transcribe.json \
    --mode lesson --max-tokens 24000
```

---

## Lessons landing page — `lessons.html`

A static index of every lesson recording, served alongside `lesson.html`.
Filters the manifest to `mode === "lesson"`; race-mode summaries (also
written by `summarize_transcript.py`) are silently skipped.

### Manifest — `Analyze/Transcripts/index.json`

```json
{
  "version": 1,
  "generated_utc": "2026-05-10T23:09:42Z",
  "recordings": [
    {
      "audio_basename": "shilshole_clip60m",
      "audio_file": "shilshole_clip60m.m4a",
      "audio_start_utc": "2026-05-03T17:06:56.137Z",
      "audio_duration_s": 3600.0,
      "mode": "lesson",
      "model_summary": "claude-opus-4-7",
      "model_transcript": "openai:gpt-4o-transcribe",
      "transcript_engine": "openai-gpt-4o-transcribe",
      "keypoint_count": 35,
      "skill_count": 11,
      "headline": "11 skills · Inhauler management as breeze builds, Diagnosing slow: the scan sequence, Backstay ↔ fine-tune partnership"
    }
  ]
}
```

The `headline` is derived from the summary markdown — for `mode=lesson` it
joins the first three `##` section names (excluding "Action items"); for
`mode=race` it takes the first highlight bullet. Records are sorted by
`audio_start_utc` desc so newest lessons surface first.

### Builder — `Scripts/build_lesson_index.py`

```bash
conda run --no-capture-output -n sailing python Scripts/build_lesson_index.py \
    [--dir Analyze/Transcripts]
```

Scans `<dir>/*.keypoints.json`, joins each with its sibling `.summary.md`
(for the headline) and freshest `.transcript.<engine>.json` (for the
`audio_duration_s` and `model_transcript` fields), and atomically writes
`<dir>/index.json`.

The same `build_index()` function is invoked as a best-effort final step
inside `Scripts/summarize_transcript.py::main()` (try/except, logged
warning on failure — never blocks the summary write itself), so a fresh
summary always lands in the manifest. The standalone CLI is the manual
escape hatch for regenerated/renamed files.

### Page

```
http://localhost:9000/lessons.html
https://regattaview.net/lessons.html
```

One row per lesson:

```
2026-05-03 17:06 UTC   shilshole_clip60m   1h 0m   35 keypoints · 11 skills
11 skills · Inhauler management as breeze builds, Diagnosing slow: the scan sequence, …
```

Click → navigates to `lesson.html?audio=<basename>`. Empty-state shows the
two CLIs to run when no lessons exist yet.

### Deploy

`aws_sync.sh` rsyncs `Analyze/Transcripts/` (alongside the existing
`Events/`/`Sessions/`/`AIS/`/`AISDays/` whitelist) into the S3 stage tree, then the
existing `aws s3 sync Analyze "$S3/www/Analyze"` + CloudFront wildcard
invalidation pushes everything live.

---

## Per-recording viewer — `lesson.html`

A static page bundled by webpack (entry `lesson` → `Src/src/js/lesson.js`,
template `Src/src/html/lesson.html`) that renders the three artifacts as a
single-page lesson overview.

URL params:
- `?audio=<basename>` — required. Loads `Analyze/Transcripts/<basename>.summary.md`
  and `<basename>.keypoints.json`.
- `?engine=<tag>` — optional. Picks which transcript flavour to lazy-load when
  the user clicks "Show transcript". Defaults to whatever transcript file
  lives next to the keypoints; falls back to `openai-gpt-4o-transcribe`.
- `?video=<basename>` — optional. Forces every keypoint deep-link to the
  given clip with `time=0`; sailview will clamp on load. Tagged
  `anchor=override` in the link tooltip.
- If `?video` is not supplied, the page fetches `Insta360/videos.json` and
  auto-resolves each keypoint to whichever clip covers its UTC. Two anchor
  paths, in priority order:
  1. **GPS-aligned** (`anchor=GPS`). When a clip has `StartUnixMs` (written
     by `Libraries/insta360.py::create_videos_json` from the v2 `.slog`
     header — see `POSES_FORMAT.md` and `TECHNICAL_DOCUMENTATION.md` on the
     `α·BB + β` GPS↔BoatBrain time fit), `viewer360.pickVideoForUnix` and
     `clipOffsetForUnix` use the millisecond-accurate UTC anchor directly.
     This is the precise path; it works on any clip whose log has been
     processed by the modern pipeline.
  2. **EXIF wall-clock** (`anchor=EXIF`). For clips without `StartUnixMs`
     (legacy / not-yet-processed), fall back to parsing the `CreateDate`
     wall-clock as US/Pacific. This drifts ~tens of seconds vs GPS — the
     camera's RTC is not disciplined.

Times in both columns are rendered in **Pacific local** (PT, via
`viewer360.wallClockInTz`); the UTC ISO is preserved in link/hover
tooltips.

Every clickable timestamp (the inline `HH:MM:SS` in the rendered summary
markdown and the per-keypoint chips) uses `target="sailview-player"`. The
first click opens one sailview tab; subsequent clicks navigate within
that same tab instead of accumulating new ones. Close the tab to reset.

Layout: left column is the rendered summary markdown; right column is the
keypoint list (clickable timestamps + label + summary + speakable excerpt).
A "Show transcript" toggle reveals the playscript (coalesced same-speaker
lines) inline so you can audit a keypoint's source.

The page enforces the same Google Sign-In as `sailview.html`/`review.html`
(via `shared.checkAuthorization()`), so it works in dev on
`http://localhost:9000/lesson.html` and in prod via CloudFront / Lambda
without any auth shim.

---

## File map

| Path                                          | Role                                                  |
|-----------------------------------------------|-------------------------------------------------------|
| `Scripts/find_new_audio.py`                   | List pending audio with friendly basenames            |
| `Scripts/new_lesson.py`                       | One-shot transcribe + summarize (preferred entry)     |
| `Scripts/transcribe_audio.py`                 | Transcribe CLI (`--basename auto` for friendly names) |
| `Scripts/summarize_transcript.py`             | Summarise CLI (also rebuilds the lessons index)       |
| `Scripts/build_lesson_index.py`               | Standalone lessons-manifest builder                   |
| `Libraries/audio_transcribe.py`               | Backend protocol, MLX/OpenAI impls, VAD, channel split, top-level `transcribe()`, `friendly_basename()` / `resolve_friendly_basename()` |
| `Libraries/sailing_glossary.py`               | `WHISPER_INITIAL_PROMPT`, race/lesson context strings, `KEYPOINT_CATEGORIES_RACE`, `MODE_REGISTRY` |
| `Libraries/env.py`                            | Tiny `load_dotenv()` helper                           |
| `Libraries/voice_audio.py` (reused)           | `_resolve_audio_start()` for UTC anchor               |
| `Scripts/swap_video_audio.py` (reused)        | `ffprobe_duration_s()`, `parse_iso_utc()`             |
| `Src/src/html/lesson.html`                    | Per-lesson viewer page                                |
| `Src/src/js/lesson.js`                        | Per-lesson viewer logic                               |
| `Src/src/html/lessons.html`                   | Lessons landing page                                  |
| `Src/src/js/lessons.js`                       | Lessons landing-page logic                            |
| `Analyze/Transcripts/`                        | Output dir (created on first run)                     |
| `.env` (gitignored)                           | `OPENAI_API_KEY` + `ANTHROPIC_API_KEY`                |
