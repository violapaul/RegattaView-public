"""Constants for the race-audio transcript / summary pipeline.

WHISPER_INITIAL_PROMPT biases ASR decoding toward sailing vocabulary.
Capped near 244 tokens — Whisper truncates beyond that.

SUMMARY_DOMAIN_CONTEXT_RACE is the summarizer's system prompt for actual
race recordings (tactical chatter). SUMMARY_DOMAIN_CONTEXT_LESSON is for
coaching / debrief / on-water lesson recordings (didactic, conceptual).
Both are designed for hand-editing as new misrecognitions or jargon appear.

The MODE_REGISTRY at the bottom maps a CLI mode name to the
(domain_context, keypoint_categories) pair the summarizer uses.
"""

WHISPER_INITIAL_PROMPT = (
    "Sailboat racing transcript. Maneuvers: tack, jibe, gybe, bear away, "
    "head up, hoist, douse, peel, set, drop, takedown. "
    "Sails: main, jib, genoa, jenny, code zero, A2, A3, A1.5, J1, J2, J3, "
    "kite, spinnaker, blast reacher, chute. "
    "Hardware: vang, cunningham, traveler, backstay, runner, halyard, "
    "sheet, outhaul, jammer, winch, primary, secondary, tackline, "
    "lazy sheet, lazy guy, foreguy, twing. "
    "Tactics: windward, leeward, port, starboard, leebow, dirty air, "
    "layline, rhumbline, lift, header, oscillation, persistent shift, "
    "lane, stack, ladder rung. "
    "Marks and lines: weather mark, leeward gate, signal boat, pin end, "
    "RC, line sight, OCS, general recall, black flag. "
    "Wind in knots, boat speed in knots, true wind angle, apparent wind angle, "
    "TWA, AWA, TWS, AWS."
)

SUMMARY_DOMAIN_CONTEXT_RACE = """\
You are summarizing radio + on-board chatter from a competitive sailboat race.
The audience is the racing crew themselves — experienced sailors who already
know the fundamentals.

Tone:
- Do not explain what windward, leebow, layline, OCS, or any standard sailing
  term means.
- Do not hedge. Do not write platitudes ("good communication is key", "the
  crew worked well together"). The crew can read the transcript; they need
  the parts worth a debrief.
- Call out specific tactical decisions, sail-trim adjustments, mistakes,
  miscommunications, and any moment that would change how they sail next time.

Every UTC timestamp you emit MUST be copied verbatim from a line in the
provided playscript. Do not interpolate or round. Do not invent times.
"""

SUMMARY_DOMAIN_CONTEXT_LESSON = """\
You are turning a sailing coaching session into a personal study guide for the
learner — an experienced racer who needs to remember WHAT was taught, not when
it was said or by whom.

Your job is to extract the substantive coaching content and organize it BY
SKILL OR TECHNIQUE. The learner should be able to read one section and have
everything the coach said on that topic in one place — a section titled
"Tacking on shifts" should contain every tacking-related point the coach made
across the entire recording, regardless of when in the audio it appeared.

What to KEEP:
- Specific techniques, sequences, sail-trim numbers, body-position cues.
- The coach's reasoning chain — WHY a technique works, the conditions it's
  tuned for, the failure modes it solves. Reasoning is more valuable than
  conclusions alone.
- Corrections of prior habits ("you were doing X, you want to do Y because Z").
- Observations from prior races / videos when used to make a teaching point.

What to DROP:
- Biographical content: credentials, who the coach has worked with, prior wins.
- Scheduling and event-calendar talk ("five months till NAs", "PSSC is
  coming up", "we should do practice Mondays").
- Logistics, intros, banter, talk about videos to send later.
- "It was a good discussion", "important to remember", or any platitude. The
  learner will read the substance and decide for themselves what's important.
- Re-teaching of fundamentals the learner already knows.

Format requirements:
- Group bullets BY SKILL, not by time.
- Section titles are concrete and specific: "## Vang trim in heavy westerlies",
  not "## Sail trim". "## Pre-start strategy in big fleets", not "## Tactics".
  If the coach only made one point on a skill, it still gets its own section.
- Each section opens with a bold "**Synthesis:**" line: 1-2 sentences distilling
  the coach's overall point on that skill. The reader should be able to read
  only the synthesis lines and walk away with the lesson.
- Then bullets of the specific things the coach said about that skill. Each
  bullet starts with the HH:MM:SS UTC, copied verbatim from the playscript.
- Order sections by importance to the learner — most actionable first. Not
  chronologically.
- If the coach explicitly named things to do or try, append a final
  "## Action items" section. Otherwise omit it.

Every UTC timestamp you emit MUST be copied verbatim from a line in the
provided playscript. Do not interpolate or round. Do not invent times.
"""

KEYPOINT_CATEGORIES_RACE = [
    "start_sequence",
    "tactical_decision",
    "sail_change",
    "mark_rounding",
    "boat_handling",
    "weather_call",
    "traffic_call",
    "mistake",
    "comms_breakdown",
    "debrief_moment",
    "other",
]

MODE_REGISTRY = {
    # (domain_context, keypoint_categories) — categories=None means free-form
    # skill-string grouping (used by lesson mode; the model picks the names).
    "race": (SUMMARY_DOMAIN_CONTEXT_RACE, KEYPOINT_CATEGORIES_RACE),
    "lesson": (SUMMARY_DOMAIN_CONTEXT_LESSON, None),
}
