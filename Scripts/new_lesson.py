#!/usr/bin/env python
"""End-to-end: transcribe + summarize a single audio recording as a lesson.

Takes either a friendly basename (resolved by scanning AUDIO_DROP_DIR) or an
explicit source path. Outputs land in `Analyze/Transcripts/<basename>.*` under
the friendly basename (Pacific-local YYYY-MM-DD_HHMM_<dur>) so artifacts are
scannable instead of inheriting the raw iPhone-export filename.

Internally shells out to `transcribe_audio.py` and `summarize_transcript.py`
with sensible lesson defaults — does no work the underlying CLIs don't already
do, just bundles them so a new lesson is one command.

Idempotent: re-runs skip the transcribe step if the transcript JSON already
exists, and skip the summarize step if the summary markdown exists. Use
`--force` to redo both.
"""
from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from Libraries.audio_transcribe import (
    MlxWhisperBackend,
    OpenAiBackend,
    PACIFIC_TZ,
    find_existing_transcript_basename,
    output_path_for,
    resolve_friendly_basename,
)
from Libraries.voice_audio import AUDIO_DROP_DIR, AUDIO_EXTS, VoiceAudioUnresolvable

LOG = logging.getLogger(__name__)
DEFAULT_TRANSCRIPTS_DIR = REPO_ROOT / "Analyze" / "Transcripts"


def _find_by_basename(audio_dir: str, target_basename: str) -> str:
    """Return source path whose friendly basename matches target_basename.
    Errors out cleanly if 0 or >1 matches.
    """
    matches = []
    for name in sorted(os.listdir(audio_dir)):
        if not name.lower().endswith(AUDIO_EXTS):
            continue
        path = os.path.join(audio_dir, name)
        try:
            basename, _, _, _ = resolve_friendly_basename(path)
        except Exception:
            continue
        if basename == target_basename:
            matches.append(path)
    if not matches:
        sys.exit(f"error: no audio in {audio_dir} resolves to basename {target_basename!r}.\n"
                 f"Run Scripts/find_new_audio.py to see what's available.")
    if len(matches) > 1:
        sys.exit(f"error: multiple sources match {target_basename!r}:\n  " +
                 "\n  ".join(matches))
    return matches[0]


def _human_dur(seconds: float) -> str:
    s = int(round(seconds))
    h, m, sec = s // 3600, (s % 3600) // 60, s % 60
    return f"{h}h {m}m {sec}s" if h else f"{m}m {sec}s"


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("basename", nargs="?",
                    help="friendly basename to process; resolved against the audio dir "
                    "(run Scripts/find_new_audio.py to list)")
    ap.add_argument("--source",
                    help="explicit source audio path (mutually exclusive with basename); "
                    "the friendly basename is derived automatically")
    ap.add_argument("--audio-dir", default=AUDIO_DROP_DIR)
    ap.add_argument("--out-dir", default=str(DEFAULT_TRANSCRIPTS_DIR))
    ap.add_argument("--mode", choices=["lesson", "race"], default="lesson")
    ap.add_argument("--engine", choices=["mlx", "openai"], default="mlx",
                    help="default mlx: audio remains on this computer")
    ap.add_argument("--openai-model",
                    choices=["gpt-4o-transcribe", "gpt-4o-mini-transcribe", "whisper-1"],
                    default="gpt-4o-transcribe")
    ap.add_argument("--mlx-model", default="mlx-community/whisper-large-v3-mlx")
    ap.add_argument("--summary-model", default="claude-opus-4-7",
                    help="Anthropic model for summarization")
    ap.add_argument("--max-tokens", type=int, default=24000,
                    help="summarize max-tokens; long lessons need headroom (default 24000)")
    ap.add_argument("--force", action="store_true",
                    help="re-run transcribe + summarize even if outputs exist")
    ap.add_argument("--dry-run", action="store_true",
                    help="print what would run, then exit (no API calls)")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--local-only", action="store_true", help="Use local transcription and skip cloud summarization")
    ap.add_argument("--allow-cloud-processing", action="store_true",
                    help="Allow this recording/transcript to be sent to external AI providers")
    args = ap.parse_args()
    if args.local_only:
        args.engine = 'mlx'
    elif not args.dry_run and not args.allow_cloud_processing:
        ap.error('This workflow sends transcripts to Anthropic (and audio to OpenAI with --engine openai). Use --allow-cloud-processing or --local-only.')

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    # Resolve source + friendly basename.
    if args.source and args.basename:
        sys.exit("error: specify either <basename> or --source, not both")
    if args.source:
        source = os.path.abspath(args.source)
        try:
            basename, start_utc, duration_s, _ = resolve_friendly_basename(source)
        except VoiceAudioUnresolvable as e:
            sys.exit(str(e))
    else:
        if not args.basename:
            ap.error("pass a basename (see Scripts/find_new_audio.py) or --source <path>")
        source = _find_by_basename(args.audio_dir, args.basename)
        basename, start_utc, duration_s, _ = resolve_friendly_basename(source)

    backend_name = (OpenAiBackend(model=args.openai_model).name
                    if args.engine == "openai"
                    else MlxWhisperBackend(repo=args.mlx_model).name)
    out_dir = Path(args.out_dir)
    transcript_path = Path(output_path_for(source, str(out_dir), backend_name,
                                            out_basename=basename))
    summary_path = out_dir / f"{basename}.summary.md"
    keypoints_path = out_dir / f"{basename}.keypoints.json"

    print(f"source:     {source}")
    print(f"basename:   {basename}")
    print(f"start UTC:  {start_utc.isoformat()}")
    print(f"start PT:   {start_utc.astimezone(PACIFIC_TZ).strftime('%a %Y-%m-%d %H:%M:%S %Z')}")
    print(f"duration:   {duration_s:.1f} s  ({_human_dur(duration_s)})")
    print(f"transcript: {transcript_path}")
    print(f"summary:    {summary_path}")
    print(f"keypoints:  {keypoints_path}")

    # Cross-reference: detect an existing transcript for the same source under
    # a DIFFERENT basename (e.g. a pre-friendly-basename manual rename like
    # `2026-05-03_lesson`). Don't silently spend $4 to re-transcribe.
    existing = find_existing_transcript_basename(os.path.basename(source), out_dir)
    if existing and existing != basename and not args.force:
        sys.exit(
            f"\nerror: source {os.path.basename(source)!r} is already transcribed under\n"
            f"  Analyze/Transcripts/{existing}.*\n"
            f"Refusing to process again under {basename!r} (would re-spend transcribe + summarize "
            f"costs).\nOptions:\n"
            f"  - view the existing lesson: lesson.html?audio={existing}\n"
            f"  - rename the existing artifacts to {basename!r} if you want the friendly name:\n"
            f"      cd Analyze/Transcripts && for f in {existing}.*; do mv \"$f\" \"{basename}${{f#{existing}}}\"; done\n"
            f"  - pass --force to re-process anyway (deliberate cost)"
        )

    if args.dry_run:
        print("(dry-run: nothing executed)")
        return

    out_dir.mkdir(parents=True, exist_ok=True)

    # 1. Transcribe.
    if transcript_path.exists() and not args.force:
        print(f"\n[skip transcribe] {transcript_path.name} exists (use --force to redo)")
    else:
        print(f"\n[transcribe] engine={args.engine}")
        cmd = [
            sys.executable, str(REPO_ROOT / "Scripts" / "transcribe_audio.py"),
            "--engine", args.engine,
            "--out-dir", str(out_dir),
            "--basename", basename,
        ]
        if args.engine == "openai":
            cmd += ["--openai-model", args.openai_model]
        else:
            cmd += ["--mlx-model", args.mlx_model]
        if args.force:
            cmd.append("--force")
        if args.verbose:
            cmd.append("-v")
        cmd.append(source)
        if args.allow_cloud_processing: cmd.append('--allow-cloud-processing')
        subprocess.run(cmd, check=True)

    # 2. Summarize.
    if args.local_only:
        print(f'Local transcript ready: {transcript_path}. No cloud summary requested.')
        return

    if summary_path.exists() and not args.force:
        print(f"\n[skip summarize] {summary_path.name} exists (use --force to redo)")
    else:
        print(f"\n[summarize] mode={args.mode} model={args.summary_model} max_tokens={args.max_tokens}")
        cmd = [
            sys.executable, str(REPO_ROOT / "Scripts" / "summarize_transcript.py"),
            "--mode", args.mode,
            "--model", args.summary_model,
            "--max-tokens", str(args.max_tokens),
            str(transcript_path),
        ]
        if args.verbose:
            cmd.append("-v")
        if args.allow_cloud_processing: cmd.append('--allow-cloud-processing')
        subprocess.run(cmd, check=True)

    print()
    print(f"done. Open: http://localhost:9000/lesson.html?audio={basename}")
    print(f"      or:  https://regattaview.net/lesson.html?audio={basename}")
    print(f"      (after `cd Src && npm run deploy`)")


if __name__ == "__main__":
    main()
