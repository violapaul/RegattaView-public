#!/usr/bin/env python
"""Summarize a UTC-anchored transcript JSON into markdown + a structured keypoints JSON.

Single Anthropic tool-use call returns both artifacts. Each keypoint UTC is
post-snapped to the nearest word in the transcript so it corresponds to a real
spoken moment.
"""
from __future__ import annotations

import argparse
import bisect
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from Libraries.env import load_dotenv
from Libraries.sailing_glossary import MODE_REGISTRY

LOG = logging.getLogger(__name__)


# ----- Playscript rendering -------------------------------------------------

COALESCE_GAP_S = 0.7
DEFAULT_MIN_CONFIDENCE = 0.8  # drop segments with no_speech_prob > this


def _parse_utc(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def _hms(dt: datetime) -> str:
    return dt.strftime("%H:%M:%S")


def _hms_ms(dt: datetime) -> str:
    return dt.strftime("%H:%M:%S.") + f"{dt.microsecond // 1000:03d}"


def _build_playscript(transcript: dict, min_no_speech_drop: float) -> tuple[str, list[dict]]:
    """Return (playscript_text, kept_segments). Coalesces same-speaker runs
    with gap < COALESCE_GAP_S and drops segments with no_speech_prob >
    min_no_speech_drop.
    """
    segs = [s for s in transcript["segments"] if s.get("no_speech_prob", 0.0) <= min_no_speech_drop]
    segs.sort(key=lambda s: s["start_utc"])

    lines = []
    coalesced = []
    cur = None
    for s in segs:
        s_start = _parse_utc(s["start_utc"])
        s_end = _parse_utc(s["end_utc"])
        spk = s["speaker"]
        if cur is not None and spk == cur["speaker"]:
            gap = (s_start - cur["end"]).total_seconds()
            if gap < COALESCE_GAP_S:
                cur["text"] = cur["text"].rstrip() + " " + s["text"].lstrip()
                cur["end"] = s_end
                continue
        if cur is not None:
            coalesced.append(cur)
        cur = {"start": s_start, "end": s_end, "speaker": spk, "text": s["text"]}
    if cur is not None:
        coalesced.append(cur)

    for c in coalesced:
        lines.append(f"{_hms(c['start'])} [{c['speaker']}] {c['text']}")
    return "\n".join(lines), segs


# ----- Excerpt-based snap ---------------------------------------------------
#
# The keypoint excerpt is verbatim playscript text. Its first quoted line is
# the authoritative anchor for the moment the keypoint represents. We snap
# the LLM's free-form `utc` to the start_utc of the transcript segment whose
# text begins (or contains, near its start) the excerpt's first non-speaker
# sentence. The LLM's `utc` is used as a tiebreaker / locality hint when
# multiple segments could match.


_NORMALIZE_RE = re.compile(r"[^a-z0-9 ]+")


def _normalize_for_match(s: str) -> str:
    s = s.lower().strip()
    s = _NORMALIZE_RE.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


def _excerpt_first_phrase(excerpt: str, min_chars: int = 25, max_chars: int = 80) -> str:
    """Return the first speaker-stripped phrase of an excerpt, normalised
    for fuzzy substring matching against the transcript. Empty if the
    excerpt couldn't be parsed.
    """
    if not excerpt:
        return ""
    # Take the first '[Speaker] text...' line (or the first non-empty line).
    for raw in excerpt.splitlines():
        line = raw.strip()
        if not line:
            continue
        # Strip leading [Speaker] tag if present.
        m = re.match(r"^\[[^\]]+\]\s*(.*)$", line)
        body = m.group(1).strip() if m else line
        if not body:
            continue
        # Take up to one sentence's worth, bounded by max_chars.
        candidate = re.split(r"(?<=[.!?])\s+", body, maxsplit=1)[0]
        if len(candidate) > max_chars:
            candidate = candidate[:max_chars]
        if len(candidate) < min_chars:
            # Too short to disambiguate — keep more of the line.
            candidate = body[:max_chars]
        return _normalize_for_match(candidate)
    return ""


def _build_segment_text_index(transcript: dict) -> list[tuple[datetime, str, str]]:
    """Sorted list of (start_utc_dt, normalised_text, raw_iso) per segment.
    Pre-normalised so the snap loop is just substring checks.
    """
    out: list[tuple[datetime, str, str]] = []
    for s in transcript["segments"]:
        text = (s.get("text") or "").strip()
        if not text:
            continue
        out.append((_parse_utc(s["start_utc"]), _normalize_for_match(text), s["start_utc"]))
    out.sort(key=lambda r: r[0])
    return out


def _snap_to_excerpt(
    llm_utc_iso: str,
    excerpt: str,
    seg_index: list[tuple[datetime, str, str]],
    locality_window_s: float = 120.0,
) -> tuple[str, str]:
    """Return (snapped_iso, source).

    Strategy:
      1. Take the first quoted phrase from the excerpt, normalise it, and
         look for a transcript segment whose normalised text contains it.
      2. Prefer segments within ±locality_window_s of the LLM's `utc`. If
         none, fall back to the closest substring hit overall.
      3. If no substring hit, fall back to time-only snap (segment whose
         start_utc is closest to the LLM's `utc`).
    """
    if not seg_index:
        return llm_utc_iso, "unsnapped"

    target = _parse_utc(llm_utc_iso) if llm_utc_iso else None
    phrase = _excerpt_first_phrase(excerpt)

    if phrase:
        local_hits = []
        all_hits = []
        for dt, norm_text, iso in seg_index:
            if phrase in norm_text or _phrase_starts_segment(phrase, norm_text):
                all_hits.append((dt, iso))
                if target is not None and abs((dt - target).total_seconds()) <= locality_window_s:
                    local_hits.append((dt, iso))
        hits = local_hits or all_hits
        if hits:
            hits.sort(key=lambda r: r[0])
            return hits[0][1], ("excerpt_snap_local" if local_hits else "excerpt_snap_global")

    # No text match — fall back to time snap to nearest segment start.
    if target is None:
        return llm_utc_iso, "unsnapped"
    closest = min(seg_index, key=lambda r: abs((r[0] - target).total_seconds()))
    return closest[2], "time_snap"


def _phrase_starts_segment(phrase: str, norm_seg_text: str) -> bool:
    """Loose 'starts with' — handles the common case where the LLM quotes
    the first 30-80 chars of a segment but the phrase boundary doesn't
    align exactly to ours. We accept a hit if the phrase matches the first
    len(phrase)+8 chars of the segment.
    """
    if not phrase or not norm_seg_text:
        return False
    head = norm_seg_text[: len(phrase) + 8]
    return phrase[: min(len(phrase), 32)] in head


def _utc_iso_ms(dt: datetime) -> str:
    s = dt.astimezone(timezone.utc).isoformat(timespec="milliseconds")
    return s.replace("+00:00", "Z")


# ----- Anthropic call -------------------------------------------------------

MODE_MARKDOWN_TEMPLATES = {
    "race": (
        "Markdown with three top-level sections in this exact order and titling: "
        "## Time overview, ## Topical overview, ## Highlights. "
        "Under ## Topical overview include these subsections (omit any that "
        "have nothing worth saying): ### Sail trim and changes, "
        "### Tactical decisions, ### Boat handling and crew work, "
        "### Communication and mistakes. "
        "Every bullet that references a moment must include a UTC HH:MM:SS "
        "timestamp copied verbatim from the playscript."
    ),
    "lesson": (
        "Markdown organized BY SKILL/TOPIC, not by time. Discover the actual "
        "skills and techniques this recording covers and emit one ## section "
        "per skill. Section titles must be concrete and specific — "
        "'## Pre-start strategy in big fleets', '## Vang trim in heavy westerlies', "
        "'## Tacking on shifts' — never generic ('## Tactics', '## Trim'). "
        "Each section opens with a bold '**Synthesis:**' line distilling the "
        "coach's overall point in 1-2 sentences (the reader should be able to "
        "skim only the synthesis lines and walk away with the lesson). Then "
        "bullets of the specific things the coach said, each prefixed with "
        "HH:MM:SS UTC copied verbatim from the playscript. Order sections by "
        "importance to the learner, not chronologically. Skip biographical, "
        "scheduling, and logistics content. End with a single '## Action items' "
        "section ONLY if the coach explicitly named things to do or try."
    ),
}

MODE_KEYPOINT_DENSITY = {
    "race": "Roughly one keypoint per 1-3 minutes of audio. More if the race is dense, fewer if it's quiet.",
    "lesson": (
        "One keypoint per substantive teaching point. Each keypoint belongs to "
        "exactly one skill (the 'category' field, free-form). Multiple keypoints "
        "share a skill string when they belong together. Skip biographical / "
        "scheduling / logistics moments — those are not keypoints."
    ),
}


def build_emit_tool(mode: str) -> dict:
    domain_context, categories = MODE_REGISTRY[mode]
    if categories is None:
        category_field = {
            "type": "string",
            "description": (
                "Skill or technique name — free-form string. Use the EXACT same "
                "string for every keypoint that belongs to this skill (downstream "
                "tooling groups on it). Match the section heading you used for "
                "this skill in the markdown (without the '## ' prefix)."
            ),
        }
    else:
        category_field = {"type": "string", "enum": categories}
    return {
        "name": "emit_race_summary",
        "description": f"Emit the {mode} summary markdown and a structured list of keypoints.",
        "input_schema": {
            "type": "object",
            "properties": {
                "summary_markdown": {
                    "type": "string",
                    "description": MODE_MARKDOWN_TEMPLATES[mode],
                },
                "keypoints": {
                    "type": "array",
                    "description": MODE_KEYPOINT_DENSITY[mode],
                    "items": {
                        "type": "object",
                        "properties": {
                            "utc": {
                                "type": "string",
                                "description": (
                                    "ISO 8601 with Z. MUST be a timestamp present in the "
                                    "playscript (HH:MM:SS), expanded to the playscript's "
                                    "date. Do not interpolate or invent."
                                ),
                            },
                            "label": {"type": "string", "description": "5-10 words, declarative, no hedging."},
                            "category": category_field,
                            "excerpt": {
                                "type": "string",
                                "description": "1-4 verbatim lines from the playscript (with [Speaker] tags).",
                            },
                            "summary": {
                                "type": "string",
                                "description": "1-2 sentences interpreting the moment for someone scanning a chapter list.",
                            },
                            "confidence": {"type": "string", "enum": ["low", "med", "high"]},
                        },
                        "required": ["utc", "label", "category", "excerpt", "summary", "confidence"],
                    },
                },
            },
            "required": ["summary_markdown", "keypoints"],
        },
    }


def build_messages(transcript: dict, playscript: str, mode: str) -> tuple[list[dict], list[dict]]:
    domain_context, _categories = MODE_REGISTRY[mode]
    audio_date = _parse_utc(transcript["audio_start_utc"]).strftime("%Y-%m-%d")
    speaker_lines = []
    for k, v in transcript["channels"].items():
        speaker_lines.append(f"  [{v['speaker']}] = channel {k}")
    speaker_block = "\n".join(speaker_lines)
    header = (
        f"Audio file: {transcript['audio_file']}\n"
        f"Date (UTC): {audio_date}\n"
        f"Duration: {transcript['audio_duration_s']:.1f} s\n"
        f"Channels:\n{speaker_block}\n"
    )
    user_text = (
        f"{header}\n"
        f"--- playscript (HH:MM:SS UTC, [Speaker] text) ---\n{playscript}\n"
    )

    system_blocks = [
        {
            "type": "text",
            "text": domain_context,
            "cache_control": {"type": "ephemeral"},
        }
    ]
    messages = [{"role": "user", "content": user_text}]
    return system_blocks, messages


def call_anthropic(model: str, system_blocks: list[dict], messages: list[dict],
                   tool: dict, max_tokens: int = 8192) -> dict:
    """Call Claude via the streaming API and return the forced tool input.

    Streaming is required by the SDK for any request that may exceed 10
    minutes wall (which a 32k-output tool-use call can). The `messages.stream`
    helper yields events while accumulating the full response under
    `.get_final_message()` — same shape as a non-stream response.
    """
    import anthropic
    client = anthropic.Anthropic()
    with client.messages.stream(
        model=model,
        max_tokens=max_tokens,
        system=system_blocks,
        tools=[tool],
        tool_choice={"type": "tool", "name": tool["name"]},
        messages=messages,
    ) as stream:
        # Drain the event stream; without this the final message is never
        # populated. We don't need per-token output, so swallow events.
        for _ in stream:
            pass
        final = stream.get_final_message()
    for block in final.content:
        if getattr(block, "type", None) == "tool_use" and block.name == tool["name"]:
            return block.input
    raise RuntimeError("Anthropic response did not contain expected tool_use block")


# ----- Word-snap on returned keypoints --------------------------------------

def _expand_hms_to_iso(hms: str, audio_date_utc: datetime) -> str:
    m = re.match(r"^(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,3}))?Z?$", hms.strip())
    if m:
        h, mi, s = int(m.group(1)), int(m.group(2)), int(m.group(3))
        ms = int((m.group(4) or "0").ljust(3, "0"))
        dt = audio_date_utc.replace(hour=h, minute=mi, second=s,
                                    microsecond=ms * 1000)
        return _utc_iso_ms(dt)
    return hms  # already ISO 8601


def snap_keypoints(keypoints: list[dict], transcript: dict) -> list[dict]:
    """Anchor each keypoint's `utc` to the start of the transcript segment
    that actually contains its excerpt.

    The LLM-emitted `utc` is treated as a hint, not as ground truth: in
    practice it is sometimes off by tens of seconds because the LLM is
    pattern-matching across a long playscript. The excerpt itself is
    verbatim transcript text and is the authoritative anchor.
    """
    seg_index = _build_segment_text_index(transcript)
    audio_date = _parse_utc(transcript["audio_start_utc"])
    out = []
    for kp in keypoints:
        raw = kp.get("utc", "")
        iso = _expand_hms_to_iso(raw, audio_date) if "T" not in raw else raw
        snapped, src = _snap_to_excerpt(iso, kp.get("excerpt", ""), seg_index)
        kp_out = dict(kp)
        kp_out["utc"] = snapped
        kp_out["utc_source"] = src
        kp_out["utc_original"] = iso
        out.append(kp_out)
    return out


# ----- IO helpers -----------------------------------------------------------

def _atomic_write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, path)


def _default_outputs(transcript_path: str) -> tuple[str, str]:
    base = os.path.basename(transcript_path)
    base = re.sub(r"\.transcript\.[^.]+\.json$", "", base)
    if base == os.path.basename(transcript_path):  # no .transcript.<tag>.json suffix
        base = os.path.splitext(os.path.basename(transcript_path))[0]
    d = os.path.dirname(transcript_path) or "."
    return os.path.join(d, f"{base}.summary.md"), os.path.join(d, f"{base}.keypoints.json")


# ----- CLI ------------------------------------------------------------------

def main():
    load_dotenv()

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("transcript", help="path to transcript JSON")
    ap.add_argument("--mode", choices=sorted(MODE_REGISTRY.keys()), default="race",
                    help="prompt + keypoint-category set: race (default) or lesson")
    ap.add_argument("--model", default="claude-opus-4-7")
    ap.add_argument("--out-md")
    ap.add_argument("--out-keypoints")
    ap.add_argument("--no-summary", action="store_true")
    ap.add_argument("--no-keypoints", action="store_true")
    ap.add_argument("--dry-run", action="store_true",
                    help="print prompt and exit; no API call")
    ap.add_argument("--min-confidence", type=float, default=DEFAULT_MIN_CONFIDENCE,
                    help="drop segments with no_speech_prob > this (default 0.8)")
    ap.add_argument("--max-tokens", type=int, default=8192)
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--allow-cloud-processing", action="store_true",
                    help="Allow this recording/transcript to be sent to external AI providers")
    args = ap.parse_args()
    if not args.dry_run and not args.allow_cloud_processing:
        ap.error('Summarization sends transcript text and speaker labels to Anthropic; add --allow-cloud-processing after confirming permission')

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    with open(args.transcript) as f:
        transcript = json.load(f)

    playscript, _kept = _build_playscript(transcript, args.min_confidence)

    out_md, out_kp = _default_outputs(args.transcript)
    if args.out_md:
        out_md = args.out_md
    if args.out_keypoints:
        out_kp = args.out_keypoints

    system_blocks, messages = build_messages(transcript, playscript, args.mode)
    tool = build_emit_tool(args.mode)

    if args.dry_run:
        print(f"=== MODE: {args.mode} ===")
        print("=== SYSTEM PROMPT ===")
        print(system_blocks[0]["text"])
        print("\n=== TOOL DEFINITION (truncated) ===")
        print(json.dumps({"name": tool["name"],
                          "categories": tool["input_schema"]["properties"]["keypoints"]["items"]["properties"]["category"]["enum"],
                          "markdown_template": tool["input_schema"]["properties"]["summary_markdown"]["description"]},
                         indent=2))
        print("\n=== USER MESSAGE ===")
        print(messages[0]["content"][:3000])
        if len(messages[0]["content"]) > 3000:
            print(f"\n... [truncated; full length {len(messages[0]['content'])} chars]")
        return

    if not os.environ.get("ANTHROPIC_API_KEY"):
        ap.error("ANTHROPIC_API_KEY not set")

    LOG.info("calling %s (mode=%s, playscript %d chars)", args.model, args.mode, len(playscript))
    result = call_anthropic(args.model, system_blocks, messages, tool, max_tokens=args.max_tokens)
    summary_md = result["summary_markdown"]
    keypoints = result.get("keypoints", [])
    snapped = snap_keypoints(keypoints, transcript)

    if not args.no_summary:
        _atomic_write(out_md, summary_md)
        LOG.info("wrote %s", out_md)
        print(out_md)

    if not args.no_keypoints:
        kp_out = {
            "audio_file": transcript["audio_file"],
            "audio_start_utc": transcript["audio_start_utc"],
            "model": args.model,
            "mode": args.mode,
            "summary_anchored_to": os.path.basename(out_md),
            "keypoints": snapped,
        }
        _atomic_write(out_kp, json.dumps(kp_out, indent=2, ensure_ascii=False))
        LOG.info("wrote %s (%d keypoints)", out_kp, len(snapped))
        print(out_kp)

    # Best-effort: refresh the lessons-index manifest so lessons.html sees
    # this recording on next page load. Failures are logged and ignored —
    # the index can always be rebuilt via Scripts/build_lesson_index.py.
    try:
        from Scripts.build_lesson_index import build_index
        build_index(os.path.dirname(out_kp) or ".")
    except Exception as e:
        LOG.warning("build_lesson_index failed (non-fatal): %s", e)


if __name__ == "__main__":
    main()
