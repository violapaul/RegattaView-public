"""Race-audio transcription orchestration.

Pipeline per channel:
  audio file -> ffprobe channel layout -> ffmpeg split to 16kHz mono WAV(s)
  -> Silero VAD pre-pass -> per-VAD-chunk WAV -> ASR backend (MLX or OpenAI)
  -> per-segment UTC anchoring -> merge across channels -> atomic JSON write.

UTC start is resolved via Libraries.voice_audio._resolve_audio_start
(sidecar > Voice Memos DB > raise). No mtime fallback.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import sys
import tempfile
import wave
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional, Protocol
from zoneinfo import ZoneInfo

import numpy as np

PACIFIC_TZ = ZoneInfo("America/Los_Angeles")

LOG = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from Libraries.voice_audio import _resolve_audio_start  # noqa: E402
from Scripts.swap_video_audio import ffprobe_duration_s, parse_iso_utc  # noqa: E402

FFMPEG = "/Users/viola/Bin/ffmpeg" if os.path.exists("/Users/viola/Bin/ffmpeg") else "ffmpeg"
FFPROBE = "/opt/homebrew/bin/ffprobe" if os.path.exists("/opt/homebrew/bin/ffprobe") else "ffprobe"

VAD_SAMPLING_RATE = 16000
VAD_MIN_SILENCE_MS = 500
VAD_SPEECH_PAD_MS = 200


# ----- ASR backend protocol --------------------------------------------------

@dataclass
class ChunkResult:
    text: str
    segments: list[dict]  # each: {start, end, text, no_speech_prob, avg_logprob, words}
    language: str = "en"


class AsrBackend(Protocol):
    name: str
    supports_word_timestamps: bool
    def transcribe_chunk(self, wav_path: str, language: str, initial_prompt: str) -> ChunkResult: ...


# ----- MLX-Whisper backend ---------------------------------------------------

class MlxWhisperBackend:
    def __init__(self, repo: str = "mlx-community/whisper-large-v3-mlx"):
        self.repo = repo
        self.name = f"mlx-whisper:{repo.split('/')[-1].replace('whisper-', '')}"
        self.supports_word_timestamps = True

    def transcribe_chunk(self, wav_path: str, language: str, initial_prompt: str) -> ChunkResult:
        import mlx_whisper
        out = mlx_whisper.transcribe(
            wav_path,
            path_or_hf_repo=self.repo,
            language=language,
            initial_prompt=initial_prompt,
            word_timestamps=True,
            condition_on_previous_text=False,
            verbose=None,
        )
        segs = []
        for seg in out.get("segments", []):
            words = []
            for w in seg.get("words") or []:
                words.append({
                    "start": float(w.get("start", seg["start"])),
                    "end": float(w.get("end", seg["end"])),
                    "word": w.get("word", "").strip(),
                    "prob": float(w.get("probability", w.get("prob", 0.0))),
                })
            segs.append({
                "start": float(seg["start"]),
                "end": float(seg["end"]),
                "text": (seg.get("text") or "").strip(),
                "no_speech_prob": float(seg.get("no_speech_prob", 0.0)),
                "avg_logprob": float(seg.get("avg_logprob", 0.0)),
                "words": words,
            })
        return ChunkResult(text=out.get("text", "").strip(), segments=segs, language=out.get("language", language))


# ----- OpenAI backend --------------------------------------------------------

class OpenAiBackend:
    def __init__(self, model: str = "gpt-4o-transcribe"):
        self.model = model
        self.name = f"openai:{model}"
        self.supports_word_timestamps = (model == "whisper-1")

    def transcribe_chunk(self, wav_path: str, language: str, initial_prompt: str) -> ChunkResult:
        from openai import OpenAI
        client = OpenAI()
        kwargs: dict[str, Any] = dict(
            model=self.model,
            file=open(wav_path, "rb"),
            language=language,
            prompt=initial_prompt,
        )
        if self.model == "whisper-1":
            kwargs["response_format"] = "verbose_json"
            kwargs["timestamp_granularities"] = ["segment", "word"]
        else:
            kwargs["response_format"] = "json"
        try:
            resp = client.audio.transcriptions.create(**kwargs)
        finally:
            kwargs["file"].close()

        if self.model == "whisper-1":
            return _parse_whisper1_verbose(resp, language)

        text = (getattr(resp, "text", "") or "").strip()
        return ChunkResult(text=text, segments=[], language=language)


def _parse_whisper1_verbose(resp, language) -> ChunkResult:
    segs_in = list(getattr(resp, "segments", []) or [])
    words_in = list(getattr(resp, "words", []) or [])
    out_segs = []
    for seg in segs_in:
        seg_start = float(seg.start)
        seg_end = float(seg.end)
        seg_words = []
        for w in words_in:
            ws, we = float(w.start), float(w.end)
            if ws >= seg_start - 1e-3 and we <= seg_end + 1e-3:
                seg_words.append({"start": ws, "end": we, "word": (w.word or "").strip(), "prob": 1.0})
        out_segs.append({
            "start": seg_start,
            "end": seg_end,
            "text": (seg.text or "").strip(),
            "no_speech_prob": float(getattr(seg, "no_speech_prob", 0.0)),
            "avg_logprob": float(getattr(seg, "avg_logprob", 0.0)),
            "words": seg_words,
        })
    return ChunkResult(text=(getattr(resp, "text", "") or "").strip(), segments=out_segs, language=language)


# ----- ffmpeg / ffprobe helpers ---------------------------------------------

def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=True, capture_output=True, text=True)


def ffprobe_channel_count(path: str) -> int:
    out = _run([FFPROBE, "-v", "error", "-select_streams", "a:0",
                "-show_entries", "stream=channels", "-of", "csv=p=0", path]).stdout.strip()
    return int(out) if out else 0


def split_to_mono_wav(audio_path: str, channel_idx: int, out_wav: str) -> None:
    # `-af pan=mono|c0=cN` is the modern replacement for the removed -map_channel.
    _run([FFMPEG, "-y", "-i", audio_path,
          "-af", f"pan=mono|c0=c{channel_idx}",
          "-ac", "1", "-ar", str(VAD_SAMPLING_RATE),
          "-acodec", "pcm_s16le", out_wav])


def downmix_to_mono_wav(audio_path: str, out_wav: str) -> None:
    _run([FFMPEG, "-y", "-i", audio_path,
          "-ac", "1", "-ar", str(VAD_SAMPLING_RATE),
          "-acodec", "pcm_s16le", out_wav])


def slice_wav(in_wav: str, start_s: float, end_s: float, out_wav: str) -> None:
    _run([FFMPEG, "-y", "-ss", f"{start_s:.3f}", "-to", f"{end_s:.3f}",
          "-i", in_wav, "-c", "copy", out_wav])


# ----- Silero VAD pre-pass --------------------------------------------------

def _read_wav_float64(wav_path: str) -> np.ndarray:
    # silero-vad's read_audio depends on torchcodec (since torchaudio>=2.9).
    # We control the WAV format upstream (pcm_s16le, mono, 16 kHz), so a
    # stdlib decode is bulletproof and avoids one more dep.
    with wave.open(wav_path, "rb") as w:
        assert w.getnchannels() == 1
        assert w.getsampwidth() == 2
        assert w.getframerate() == VAD_SAMPLING_RATE
        frames = w.readframes(w.getnframes())
    return np.frombuffer(frames, dtype=np.int16).astype(np.float64) / 32768.0


def _read_wav_as_tensor(wav_path: str):
    import torch
    return torch.from_numpy(_read_wav_float64(wav_path).astype(np.float32))


def _detect_collapsed_mono(wav_l: str, wav_r: str,
                           corr_threshold: float = 0.998,
                           rms_diff_ratio_threshold: float = 0.02) -> tuple[bool, dict]:
    """Return (is_collapsed, stats). 'Collapsed' means L and R carry the same
    signal — the audio was mixed to mono before stereo encoding (a common
    failure mode for body-mic receivers in safety-track mode). Triggers when
    EITHER correlation is above corr_threshold OR the residual RMS is below
    rms_diff_ratio_threshold * RMS(L). Conservative: false positives risk
    ignoring a quiet right-side speaker, so both bars are deliberately tight.
    """
    L = _read_wav_float64(wav_l)
    R = _read_wav_float64(wav_r)
    n = min(len(L), len(R))
    L, R = L[:n], R[:n]
    rms_l = float(np.sqrt((L * L).mean()))
    rms_r = float(np.sqrt((R * R).mean()))
    if rms_l < 1e-9 and rms_r < 1e-9:
        return True, {"reason": "both_silent", "rms_l": rms_l, "rms_r": rms_r}
    rms_diff = float(np.sqrt(((L - R) ** 2).mean()))
    ratio = rms_diff / max(rms_l, 1e-9)
    corr = float(np.corrcoef(L, R)[0, 1]) if rms_l > 0 and rms_r > 0 else 0.0
    stats = {"rms_l": rms_l, "rms_r": rms_r, "rms_diff_over_rms_l": ratio, "pearson_corr": corr}
    return (corr > corr_threshold or ratio < rms_diff_ratio_threshold), stats


def vad_chunks(wav_path: str) -> list[dict]:
    from silero_vad import load_silero_vad, get_speech_timestamps
    audio = _read_wav_as_tensor(wav_path)
    model = load_silero_vad()
    raw = get_speech_timestamps(
        audio, model,
        sampling_rate=VAD_SAMPLING_RATE,
        min_silence_duration_ms=VAD_MIN_SILENCE_MS,
        speech_pad_ms=VAD_SPEECH_PAD_MS,
        return_seconds=True,
    )
    return [{"start": float(c["start"]), "end": float(c["end"])} for c in raw]


# ----- Top-level orchestration ----------------------------------------------

def _utc_iso(dt: datetime) -> str:
    s = dt.astimezone(timezone.utc).isoformat(timespec="milliseconds")
    return s.replace("+00:00", "Z")


def _sha1_file(path: str, chunk_size: int = 1 << 20) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk_size)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def _atomic_write_json(path: str, obj: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


@dataclass
class TranscribeOptions:
    audio_path: str
    out_dir: str
    backend: AsrBackend
    language: str = "en"
    speaker_l: str = "Speaker A"
    speaker_r: str = "Speaker B"
    initial_prompt: str = ""
    force: bool = False
    out_basename: Optional[str] = None  # if set, used instead of source filename


def _engine_tag(name: str) -> str:
    safe = name.replace(":", "-").replace("/", "-").replace(" ", "")
    safe = safe.replace("mlx-whisper-", "mlx-")
    return safe


def _format_friendly_duration(seconds: float) -> str:
    s = int(round(seconds))
    h = s // 3600
    m = (s % 3600) // 60
    if h > 0:
        return f"{h}h{m:02d}m"
    return f"{m}m"


def friendly_basename(audio_start_utc: datetime, duration_s: float) -> str:
    """Pacific-local YYYY-MM-DD_HHMM_<duration> — scannable replacement for
    the raw iPhone export filename. e.g. `2026-05-03_0943_7h31m`. Deterministic
    given `(start, duration)`, so the same audio always resolves to the same
    basename.
    """
    local = audio_start_utc.astimezone(PACIFIC_TZ)
    date_part = local.strftime("%Y-%m-%d_%H%M")
    return f"{date_part}_{_format_friendly_duration(duration_s)}"


def resolve_friendly_basename(audio_path: str) -> tuple[str, datetime, float, str]:
    """Return (basename, audio_start_utc, duration_s, start_source) for a given
    source audio. Resolves UTC via Libraries.voice_audio._resolve_audio_start
    (sidecar > Voice Memos DB). Raises VoiceAudioUnresolvable if the start
    cannot be determined.
    """
    duration_s = ffprobe_duration_s(audio_path)
    audio_start_utc, source = _resolve_audio_start(audio_path, duration_s)
    return friendly_basename(audio_start_utc, duration_s), audio_start_utc, duration_s, source


def find_existing_transcript_basename(source_filename: str, transcripts_dir: str | Path) -> Optional[str]:
    """Return the basename of an existing transcript artifact whose
    `audio_file` header references `source_filename`, if any. Used to detect
    lessons that were transcribed under a manually-renamed basename (e.g.
    pre-friendly-basename runs like `2026-05-03_lesson`) so we don't propose
    re-transcribing the same audio under a new name.

    Returns None if no match. The first match wins if there are multiple
    (shouldn't happen — `audio_file` is the source-of-truth key).
    """
    import re
    transcripts_dir = Path(transcripts_dir)
    if not transcripts_dir.is_dir():
        return None
    for p in transcripts_dir.glob("*.transcript.*.json"):
        m = re.match(r"^(.+?)\.transcript\.[^.]+\.json$", p.name)
        if not m:
            continue
        try:
            with open(p) as f:
                d = json.load(f)
        except Exception:
            continue
        if d.get("audio_file") == source_filename:
            return m.group(1)
    return None


def output_path_for(audio_path: str, out_dir: str, backend_name: str,
                    out_basename: Optional[str] = None) -> str:
    base = out_basename or os.path.splitext(os.path.basename(audio_path))[0]
    tag = _engine_tag(backend_name)
    return os.path.join(out_dir, f"{base}.transcript.{tag}.json")


def transcribe(opts: TranscribeOptions) -> str:
    out_path = output_path_for(opts.audio_path, opts.out_dir, opts.backend.name,
                               out_basename=opts.out_basename)
    if os.path.exists(out_path) and not opts.force:
        LOG.info("output exists, skipping: %s (use force=True to overwrite)", out_path)
        return out_path

    duration_s = ffprobe_duration_s(opts.audio_path)
    audio_start_utc, source = _resolve_audio_start(opts.audio_path, duration_s)
    LOG.info("audio_start_utc=%s (%s)", _utc_iso(audio_start_utc), source)

    n_channels = ffprobe_channel_count(opts.audio_path)
    if n_channels == 0:
        raise RuntimeError(f"no audio stream in {opts.audio_path}")

    collapse_stats = None
    with tempfile.TemporaryDirectory(prefix="audio_transcribe_") as tmp:
        if n_channels >= 2:
            wav_l = os.path.join(tmp, "left.wav")
            wav_r = os.path.join(tmp, "right.wav")
            split_to_mono_wav(opts.audio_path, 0, wav_l)
            split_to_mono_wav(opts.audio_path, 1, wav_r)

            collapsed, collapse_stats = _detect_collapsed_mono(wav_l, wav_r)
            if collapsed:
                LOG.info("L/R channels effectively identical (corr=%.6f, rms_diff/rms_l=%.4f); "
                         "treating as mono",
                         collapse_stats.get("pearson_corr", 0.0),
                         collapse_stats.get("rms_diff_over_rms_l", 0.0))
                channel_layout = "stereo_collapsed_to_mono"
                channels_meta = {"M": {"speaker": opts.speaker_l}}
                wavs = {"M": wav_l}
                os.unlink(wav_r)
            else:
                channel_layout = "stereo"
                channels_meta = {
                    "L": {"speaker": opts.speaker_l},
                    "R": {"speaker": opts.speaker_r},
                }
                wavs = {"L": wav_l, "R": wav_r}
        else:
            channel_layout = "mono"
            channels_meta = {"M": {"speaker": opts.speaker_l}}
            wavs = {"M": os.path.join(tmp, "mono.wav")}
            downmix_to_mono_wav(opts.audio_path, wavs["M"])

        all_segments = []
        for ch_key, wav_path in wavs.items():
            chunks = vad_chunks(wav_path)
            LOG.info("channel %s: %d VAD chunks", ch_key, len(chunks))
            for i, chunk in enumerate(chunks):
                chunk_wav = os.path.join(tmp, f"{ch_key}_{i:05d}.wav")
                slice_wav(wav_path, chunk["start"], chunk["end"], chunk_wav)
                chunk_start_utc = audio_start_utc + timedelta(seconds=chunk["start"])
                result = opts.backend.transcribe_chunk(chunk_wav, opts.language, opts.initial_prompt)
                if not result.segments and result.text:
                    # gpt-4o-transcribe path: no internal timestamps.
                    all_segments.append({
                        "start_utc": _utc_iso(chunk_start_utc),
                        "end_utc": _utc_iso(audio_start_utc + timedelta(seconds=chunk["end"])),
                        "channel": ch_key,
                        "speaker": channels_meta[ch_key]["speaker"],
                        "text": result.text,
                        "no_speech_prob": 0.0,
                        "avg_logprob": 0.0,
                        "words": None,
                    })
                else:
                    for seg in result.segments:
                        seg_start = chunk_start_utc + timedelta(seconds=seg["start"])
                        seg_end = chunk_start_utc + timedelta(seconds=seg["end"])
                        words_out = []
                        for w in seg["words"]:
                            words_out.append({
                                "start_utc": _utc_iso(chunk_start_utc + timedelta(seconds=w["start"])),
                                "end_utc": _utc_iso(chunk_start_utc + timedelta(seconds=w["end"])),
                                "word": w["word"],
                                "prob": w["prob"],
                            })
                        all_segments.append({
                            "start_utc": _utc_iso(seg_start),
                            "end_utc": _utc_iso(seg_end),
                            "channel": ch_key,
                            "speaker": channels_meta[ch_key]["speaker"],
                            "text": seg["text"],
                            "no_speech_prob": seg["no_speech_prob"],
                            "avg_logprob": seg["avg_logprob"],
                            "words": words_out or None,
                        })

    all_segments.sort(key=lambda s: s["start_utc"])

    out = {
        "audio_file": os.path.basename(opts.audio_path),
        "audio_sha1": _sha1_file(opts.audio_path),
        "audio_start_utc": _utc_iso(audio_start_utc),
        "audio_duration_s": duration_s,
        "audio_start_source": source,
        "channel_layout": channel_layout,
        "channels": channels_meta,
        "model": opts.backend.name,
        "vad": f"silero (min_silence_duration_ms={VAD_MIN_SILENCE_MS})",
        "language": opts.language,
        "initial_prompt_sha1": hashlib.sha1(opts.initial_prompt.encode()).hexdigest(),
        "segments": all_segments,
    }
    if collapse_stats is not None:
        out["channel_collapse_stats"] = collapse_stats
    _atomic_write_json(out_path, out)
    LOG.info("wrote %s (%d segments)", out_path, len(all_segments))
    return out_path
