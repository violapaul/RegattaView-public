"""Per-run SD-card status accumulator for race_upload's end-of-run summary.

Card copy / wipe / eject outcomes used to be visible only as scattered
INFO/ERROR log lines that scroll past in a river of pipeline output. This
module collects them into one process-global structure so race_upload can
print a compact "cards:" roundup — plus an ATTENTION block repeating anything
that needs operator action (copy failed, refused-to-wipe, eject failed) — at
the very end of a run.

Usage:
    card_report.reset()                       # once, at run start
    card_report.record("Insta360", mounted=True, files_found=0)
    card_report.set_note("Insta360", "present, 0 new files — already "
                          "ingested; card left mounted (not ejected)")
    card_report.add_problem("Insta360", "copy failed (3 errors); card left "
                            "mounted, re-run to retry")
    for line in card_report.summary_lines():  # at run end
        print(line)

Cards appear in the roundup only if they were `record`ed, so skipped stages
(--no-video / --no-logs) simply omit their card rather than reporting a
misleading "not mounted".
"""

# name -> dict of fields. Insertion order is preserved (py3.7+), so cards
# print in the order they were first recorded.
_cards: "dict[str, dict]" = {}


def reset():
    """Clear all recorded card state. Call once at the start of each run."""
    _cards.clear()


def record(name, **fields):
    """Merge `fields` into the named card's record (creating it if new)."""
    _cards.setdefault(name, {}).update(fields)


def set_note(name, note):
    """Set the one-line human description of the card's terminal state."""
    _cards.setdefault(name, {})["note"] = note


def add_problem(name, msg):
    """Append an action item; surfaced in the ATTENTION block at run end."""
    _cards.setdefault(name, {}).setdefault("problems", []).append(msg)


def all_cards():
    """Return a shallow copy of the recorded card state (for tests)."""
    return {k: dict(v) for k, v in _cards.items()}


def _describe(fields):
    """Compose a one-line description from raw fields when no explicit note
    was set. Kept deliberately simple — recording sites that know their exact
    situation should call set_note() instead."""
    if not fields.get("mounted"):
        return "not mounted (no card)"
    parts = ["present"]
    n = fields.get("files_found")
    if n == 0:
        parts.append("0 new files")
    elif n is not None:
        parts.append(f"{n} file(s)")
    if fields.get("copied") is not None:
        parts.append(f"copied {fields['copied']}")
    if fields.get("copy_errors"):
        parts.append(f"{fields['copy_errors']} copy error(s)")
    if fields.get("wiped"):
        parts.append("wiped")
    ej = fields.get("ejected")
    if ej is True:
        parts.append("ejected OK")
    elif ej is False:
        parts.append("EJECT FAILED")
    return ", ".join(parts)


def summary_lines():
    """Render the card roundup as a list of strings (no trailing newlines).

    Returns the "cards:" block followed, if any card recorded a problem, by an
    "ATTENTION:" block. Returns an empty list if no card was recorded at all
    (e.g. both the video and logs stages were skipped), so the caller can omit
    the section entirely.
    """
    if not _cards:
        return []
    width = max(len(name) for name in _cards)
    lines = ["cards:"]
    for name, fields in _cards.items():
        note = fields.get("note") or _describe(fields)
        lines.append(f"  {name:<{width}} : {note}")
    problems = [(name, p) for name, fields in _cards.items()
                for p in fields.get("problems", [])]
    if problems:
        lines.append("")
        lines.append("ATTENTION:")
        for name, p in problems:
            lines.append(f"  - {name}: {p}")
    return lines
