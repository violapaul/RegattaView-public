#!/usr/bin/env python
"""Transcribe a stereo (or mono) audio file into a UTC-anchored JSON transcript.

Per-channel ASR with Silero VAD pre-pass; pluggable MLX-Whisper / OpenAI backend.
UTC start time is resolved by Libraries.voice_audio (sidecar > Voice Memos DB).
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from Libraries import audio_transcribe
from Libraries.env import load_dotenv
from Libraries.sailing_glossary import WHISPER_INITIAL_PROMPT


def main():
    load_dotenv()

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("audio", help="path to audio file (.m4a/.wav/.mp3/.aac)")
    ap.add_argument("--out-dir", default=str(REPO_ROOT / "Analyze" / "Transcripts"))
    ap.add_argument("--engine", choices=["mlx", "openai"], default="mlx")
    ap.add_argument("--mlx-model", default="mlx-community/whisper-large-v3-mlx")
    ap.add_argument("--openai-model", choices=["gpt-4o-transcribe", "gpt-4o-mini-transcribe", "whisper-1"],
                    default="gpt-4o-transcribe")
    ap.add_argument("--language", default="en")
    ap.add_argument("--speaker-l", default="Speaker A")
    ap.add_argument("--speaker-r", default="Speaker B")
    ap.add_argument("--basename",
                    help="override output basename. `auto` derives a Pacific-local "
                    "YYYY-MM-DD_HHMM_<dur> name from the audio start + duration "
                    "(via Libraries.audio_transcribe.resolve_friendly_basename). "
                    "Default uses the source filename (legacy).")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--allow-cloud-processing", action="store_true",
                    help="Allow this recording/transcript to be sent to external AI providers")
    args = ap.parse_args()
    if args.engine == 'openai' and not args.allow_cloud_processing:
        ap.error('--engine openai sends audio to OpenAI; add --allow-cloud-processing after confirming permission')

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.engine == "mlx":
        backend = audio_transcribe.MlxWhisperBackend(repo=args.mlx_model)
    else:
        if not os.environ.get("OPENAI_API_KEY"):
            ap.error("OPENAI_API_KEY not set; cannot use --engine openai")
        backend = audio_transcribe.OpenAiBackend(model=args.openai_model)

    out_basename = None
    if args.basename:
        if args.basename == "auto":
            out_basename, _, _, _ = audio_transcribe.resolve_friendly_basename(args.audio)
        else:
            out_basename = args.basename

    opts = audio_transcribe.TranscribeOptions(
        audio_path=args.audio,
        out_dir=args.out_dir,
        backend=backend,
        language=args.language,
        speaker_l=args.speaker_l,
        speaker_r=args.speaker_r,
        initial_prompt=WHISPER_INITIAL_PROMPT,
        force=args.force,
        out_basename=out_basename,
    )
    out_path = audio_transcribe.transcribe(opts)
    print(out_path)


if __name__ == "__main__":
    main()
