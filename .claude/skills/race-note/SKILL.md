---
name: race-note
description: >-
  Author a club-facing race-analysis note for RegattaView: take a sailview/review
  moment (a start, rounding, tactical situation, trim question), analyze it from the
  boat's own instruments + AIS + polars, write a crew-facing Markdown note with
  figures and deep-links, build it to gated HTML, and (on go-ahead) deploy. Use when
  the user asks to "write up", "do a note on", "analyze this start/leg/rounding",
  "make a debrief", or turn a RegattaView moment into a shareable note. Encodes the
  data access, the Notes/ directory + build pipeline, the deep-link helpers, and the
  crew-facing writing voice.
---

# Write a RegattaView race note

Turn a moment on the water into a **club-facing analysis note** — published behind the
existing Google login, with deep-links back into sailview / tracks / review, in a
**directory + build pipeline that already exists** (`Analyze/Notes/`, `Scripts/build_notes.py`).

The first note (`Analyze/Notes/2026-06-03_j105-start-heel/`) is the worked reference — read it
before starting a new one.

## 0. Work in a worktree

Use a named worktree (`CLAUDE.md` → *Git Workflow*). Note-specific gotchas:
- **Note content is repo data, not git** (`CLAUDE.md` → *Repo Data Lives in S3*): `note.md`,
  `meta.yaml`, `figures/`, `note.html` and `notes_index.json` live in `s3://regattaview-repo-data`.
  Only each note's `scripts/` is in git. In a fresh worktree run
  `conda run -n sailing python Scripts/repo_data.py pull` first (pulls into the worktree).
- The build script and `note_links.py` are git-tracked, but **`Analyze/AISDays/`, `Analyze/AIS/`,
  and the parquet logs are NOT** — run analysis scripts that read those against the **main tree**
  (or pass values explicitly), writing figures into the worktree note dir.

## 1. Anchor the moment correctly (do this first — it's the usual bug)

- The user gives a sailview link like `sailview.html?video=<basename>&time=<sec>`. **Convert video
  time → unix** via the per-video `.log` `row_times` (column 20, `msec_per_sample=100`), read with
  `Libraries/sailview_log_v1.read_log_v1`. Do **not** trust `events_index.json`'s auto-detected gun
  — it has been ~40 s early. Anchor every window on the **user-confirmed true moment**.
- If the user later corrects the time (they often will), **re-anchor everything** and re-check every
  finding — a 40 s shift turned a wrong "under-heeled/slow start" read into "adequately powered".

## 2. Load the data — calibrated, with the right frames

- `from Analyze.current_analysis import load_calibrated_log` →
  `load_calibrated_log("<session>.parquet", calibration="display")`. **Raw parquet/.log columns are
  NOT display-ready** — the SPD/heel/heading/AWA offsets live in
  `Src/src/assets/calibration/sensor_calibration.json` (that file is the single source of truth for
  the numbers — don't transcribe them). Always use the calibrated loader; never hand-compare a raw
  column to anything.
- Polars: `from Analyze import polar` → `polar.speed_kt(twa, tws)`, `polar.upwind_vmgmax_kt(tws)`
  (generic J/105 table — target deltas are directional, not absolute).
- Leeway / current: `Analyze/leeway_model.json` (`K≈9.77 · heel / max(spd,2)²`), current via the
  triangle estimator in `current_analysis` (scatter-dominated ~0.6 kt RMS vs SSCOFS — report
  set/drift as *indicative*).
- AIS competitors: per-start slice `Analyze/AIS/<event_id>.json` (names, tracks lat/lon/ts) and
  whole-day `Analyze/AISDays/<date>.json` (per-boat SOG + the day's `time_start`).
- Frames/units: variation is venue-global (~14.9°E), NOT the boat's stale broadcast; `.log` `twd`
  and `cog` are already TRUE, `hdg` is magnetic; J/105 LOA = 10.5 m for boatlength geometry; ε and
  tack transients live in the true-wind estimator (`TRUE_WIND_NOTES.md`).

## 3. Be honest about what the data can and can't say

These are the rules that keep a note credible — bake them into the figures and prose:

- **Our boat is 10 Hz and trustworthy; AIS is sparse.** A competitor on AIS is often ~4 real
  moments ~30 s apart (8 fixes in near-duplicate pairs). Draw AIS as **discrete dots, never a
  connecting line** (a line fakes a continuous history). Trust AIS *position* (if it agrees across
  checks), not fine speed trends. Every quantitative speed/heel/rudder number should come from our
  own instruments.
- **SOG-vs-SOG is the honest competitor comparison** (both boats share the current, so it cancels);
  our STW is NOT comparable to their SOG.
- **Don't overclaim.** A "lee-bow" needs the boat to actually be ~2–3 BL away; boats 9–15 BL off
  don't touch your wind. A *mild persistent pinch* (our AWA/AWS steady) is not a *gassing* (velocity
  collapse). Say which one the data supports.
- **Caveats inline, not buried:** TWD absolute is soft in the first ~60 s after a maneuver
  (masthead upwash); current is indicative; rudder zero may be uncalibrated; masthead TWS reads
  high vs ORC's 10 m reference.

## 4. Figures

- **Generators live WITH the note, in `Analyze/Notes/<slug>/scripts/`** — not loose in the shared
  `Analyze/` namespace (e.g. `scripts/sog_compare.py`, `creative_leebow.py`, `start_lane_analysis.py`).
  `aws_sync.sh` excludes `scripts/` from the S3 sync, so they're **never deployed**; `meta.yaml` lists
  them for provenance. The path bootstrap is `REPO_ROOT = abspath(join(HERE, "..","..","..",".."))`
  then `sys.path.insert(0, REPO_ROOT)`.
- **Keep the drivers THIN — one authoritative home for logic.** Reusable code stays in the `Analyze/`
  package: `current_analysis` (calibrated load, current, leeway), `polar`, and the shared lat/lon→m +
  course-frame projection in **`Analyze/race_geometry.py`** (`offset_en`, `course_frame`,
  `relative_course_frame`, `nearest_row`, `angdiff`). A note's `scripts/` must **import** these, never
  copy them. When a new note needs another reusable helper, **add it to `Analyze/race_geometry.py`**
  (or the right `Analyze/` module), don't re-derive it per note — that's the "second source of truth
  drifts" trap.
- **Output split:** a generator writes the figures the note actually shows to `../figures/`
  (`FIGURES = join(NOTE_DIR, "figures")`), and exploratory diagnostics (extra plots, a stats.json of
  exact numbers) to a gitignored `scripts/_out/`. Only what `note.md` references belongs in `figures/`.
- Keep figures legible and self-explanatory (titles that state the conclusion, the AIS-dots-vs-our-lines
  contrast visible at a glance).
- Prefer **simple, comparative** figures (the user repeatedly pushed for "our SOG vs their SOG" over
  a busy multi-panel). Add heel on a second axis where it's the story.
- **Video stills: use the corrected snapshot tool, NOT a raw fisheye crop.** The compressed Insta360
  mp4s are **dual-fisheye** at 3840×1920, and the camera is mounted tilted on a heeling boat — a raw
  `ffmpeg crop` of one lens circle is barrel-distorted and the horizon runs downhill, which reads as
  unintelligible. `Src/scripts/snap.mjs` reproduces exactly what the viewer shows: it runs the
  production fisheye dewarp (`fisheye_shader.js`) over the frame and **levels the horizon** with the
  per-frame `.poses` IMU quaternion. Use it for the hero frame and every on-boat still:
  ```
  NODE_NO_WARNINGS=1 node Src/scripts/snap.mjs \
      --video /Users/viola/Data/Insta360/VID/<basename>.mp4 \
      --time <clip_seconds> --out figures/hero.jpg \
      [--unix <unix_sec>]  [--label "tau = 0"]  [--yaw <deg>] [--pitch <deg>] [--fov <deg>]
  ```
  Defaults: forward over the bow, level, **fov 130** (immersive cockpit-wide), 1280×720. `--unix`
  resolves the clip time from the sibling `.slog`; `--time` is clip-relative seconds. `--yaw`/`--pitch`
  aim elsewhere (e.g. at a leeward boat). **`--fov` is the key framing knob — pick it per shot:**
  ~90 = tight natural "photo" crop, ~110 = wider but still natural, ~130 (default) = whole crew +
  nearby boats, ≥150 = near-fisheye with visible edge stretch + horizon bow. Useful range ~70–150. Needs `playwright-core` in `Src/` (renders via headless
  system Chrome) and `ffmpeg`. Read the result back to check it before committing. Generators call it
  via `subprocess` (see `2026-06-22_low-to-high-mode/scripts/build_figs.py::grab`). Embed the hero as
  the first line of `note.md`.

## 5. Write the note — crew-facing voice (this is the point)

The audience is **the crew (20-somethings)**, not the analyst. The note must **reveal its value in
the first few lines and justify the reading time.** Rules, learned directly from the user:

- **Top-level hook, not an insider one.** The title and opening must land with *any* reader of that
  boat/class, not just the crew who were aboard. Frame it as a **case study** around the universal
  question ("Holding Your Lane at 14 Knots — a J/105 Case Study"), then say the worked example
  happens to be ours. Avoid titles that only the boat's crew understand ("Why *we* lost *that*
  lane"). Open with the general gut-punch every racer knows, then dive into the instrumented case.
- **Lead with the payoff, not the methodology.** Title = the takeaway or the hooking question, not
  "Analysis of …".
- **Open with a "30-second version"** — 3–5 bullets: what happened, why, and the fix. A reader who
  stops there should still get the lesson. Then a one-line **Bottom line** in a blockquote.
- **One-number takeaway.** Give them the single thing to remember ("20–22° heel, steady"), then back
  it with detail for those who want it.
- **Second person, direct, concrete.** "We were even on speed and still losing." Short sentences.
  Bold the numbers that matter. It's OK — encouraged — to include *all* the detail and rigor; just
  make the message clear and upfront, and put the depth *after* the hook.
- **Not self-referential.** The reader does not have "the note", "Part I", "v1", or "the doc" — never
  reference the document, its versions, or its own structure ("see the test below" is fine; "as the
  note argues" is not). Write the content directly.
- **Address the natural objection.** ("Flat *feels* fast — here's why it still loses.")
- **Anchor it to the boat.** Map generic guidance to our actual instruments / calibration / to-dos
  (e.g. "zero the rudder channel", "we already apply −4° heel"). Make it actionable for *next race*.
- Keep the honest caveats, but make them one tight paragraph or a blockquote, not a hedge on every
  sentence.

### Deep-links (the user explicitly wants these)

Build them with `Libraries/note_links.py` (don't hand-encode):

```python
from Libraries.note_links import sailview_url, tracks_url, review_url
sailview_url(1780541823, fields="spd,awa,tws,heel,twd,depth")   # utc auto-resolves clip+offset
tracks_url("2026-06-03", 1780541823)        # reads day time_start from AISDays/<date>.json
review_url("2026-06-04T02_56")
```

Put a row of them near the top ("Want to relive it?") so a reader jumps straight from a claim to the
moment on the water. They open top-level (the build injects `<base target='_blank'>`).

## 6. Directory + meta

```
Analyze/Notes/<YYYY-MM-DD>_<kebab-topic>/      # slug: Pacific sailing day + topic
  note.md            # SOURCE — hand-edited; the crew-facing prose above
  meta.yaml          # title, date (Pacific day), race, type, tags, summary, provenance{session,start_unix,tack_unix,scripts,sources}
  figures/*.png      # only what note.md shows (repo data, deployed); written by scripts/
  note.html          # BUILT (do not hand-edit)
  scripts/           # this note's thin generators (in git, NOT deployed)
    *.py             #   import Analyze.race_geometry etc.; write figs to ../figures/
    .gitignore       #   _out/
    _out/            #   diagnostics: extra plots + stats.json (gitignored)
```

`meta.yaml` `summary:` is the index headline — write it crew-facing too (it's what shows in the
list). `date` is the **Pacific sailing day** even when the start is the next UTC day.

## 7. Build

```bash
conda run --no-capture-output -n sailing python Scripts/build_notes.py --note <slug>
```
pandoc renders `note.md → note.html` self-contained (`--standalone --embed-resources --toc
--toc-depth=2`, figures embedded base64), injects `<base target='_blank'>`, and rebuilds
`Analyze/Notes/notes_index.json`. The gated viewer (`Src/src/js/notes.js` + `notes.html`) does a
single signed fetch of `note.html`, so embedding figures is what makes the one-fetch viewer work.
Re-run after every `note.md` edit. `--all` rebuilds everything; `--index-only` just the manifest.

Verify: `note.html` has all figures embedded (`grep -c data:image/png note.html`), the deep-links
present, size ~0.6 MB/note, and `notes_index.json` shows the new title.

## 8. Local check (auth-gated, port 9000)

The note content is gated (everything under `www/Analyze/` is 403-to-anon, reachable only via a
Lambda-signed URL — `notes.js` signs it after `checkAuthorization()`). `cd Src && npm run start`
(must be port 9000 — see `CLAUDE.md` → *Local Dev With Auth*), open `notes.html?note=<slug>`. In
dev, `isLocal()` serves the local `Analyze/` tree (no signing); signed fetch only happens in prod.
The note shell (`notes.html`) is public; the content is not.

## 9. Land + deploy (only on explicit go-ahead)

- Land the worktree with `/land` (commit → `--ff-only` → remove worktree/branch) — that carries
  only `scripts/`. Default is to **leave the worktree untracked for review** and wait — don't
  commit or touch `main` unprompted.
- Content goes through the bucket, not git: from the worktree, `Scripts/repo_data.py push`; then
  in the main tree `Scripts/repo_data.py pull --force Analyze/Notes/<slug>/ Analyze/Notes/notes_index.json`
  (the path limit keeps `--force` from clobbering unrelated unpushed edits). Do this **before**
  removing the worktree and before deploying — `aws_sync.sh` deploys from the main tree.
- `aws_sync.sh` already syncs `Notes` (the `for dir in Events Sessions AIS AISDays Transcripts
  Notes` loop, with `--exclude 'scripts/'` so note generators don't ship). Deploy with `cd Src &&
  npm run deploy` (build → stage `dist/` → S3 sync → CloudFront invalidation).
- Verify live: `https://regattaview.net/notes.html` → 200 (public shell); anon fetch of
  `Analyze/Notes/<slug>/note.html` → **403** (correctly gated); the note reachable signed-in.

## Critical files

- Reference note: `Analyze/Notes/2026-06-03_j105-start-heel/{note.md,meta.yaml,figures/,scripts/}`
- Shared analysis helpers: `Analyze/race_geometry.py` (geometry), `Analyze/current_analysis.py`, `Analyze/polar.py`
- Build: `Scripts/build_notes.py` · Links: `Libraries/note_links.py`
- Viewer: `Src/src/js/notes.js`, `Src/src/html/notes.html` (webpack `entry.notes`)
- Data: `Analyze/current_analysis.py` (`load_calibrated_log`), `Analyze/polar.py`,
  `Analyze/leeway_model.json`, `Analyze/AIS/`, `Analyze/AISDays/`,
  `Src/src/assets/calibration/sensor_calibration.json`
- Deploy: `aws_sync.sh` (Notes in the sync loop), `cd Src && npm run deploy`
