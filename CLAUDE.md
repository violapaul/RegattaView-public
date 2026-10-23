# CLAUDE.md

Guidance for Claude Code in this repo. General working style, output format, and technical standards live in the global `~/.claude/CLAUDE.md`; this file covers only RegattaView-specific context.

## Verify Before You Conclude — Read This First

The most common failure here is **confidently asserting something the code or data plainly contradicts** — a guessed signature, an assumed format, a "this must be why" — when 60 seconds of reading would have settled it. Treat every claim about how the code behaves as a hypothesis until you've looked at the code that proves it. Don't answer from memory, a filename, or how a system "usually" works.

- **No claim without a citation.** Point at the `file:line` or command output. Can't cite it? Say "I think" and go verify before acting.
- **A code comment is not proof of behavior** — follow the call into the function that does the work.
- **Run the cheap check first.** If a `grep`, `Read`, one-line `conda run`, `sysctl`, or `dd` would confirm or kill a theory, run it *before* presenting the theory — never bundle a recommendation with an unverified diagnosis.
- **Aggregate before narrating.** Don't eyeball a few samples; compute means *and* max from the `.slog` (`sailview_log_v2.read_slog`), and confirm which clip/artifact the user's link actually points at.
- **Prefer ground truth over plausible stories.** Caches, parallel subprocesses, stale artifacts, and miscalibrated sensors manufacture convincing-but-wrong narratives. When data is surprising, suspect your reading before inventing a mechanism.
- **Multi-layer systems need multi-layer checks.** NMEA/AIS, the log/poses pipeline, and auth span firmware → Python → frontend; a conclusion from one layer is unverified until you check the layer that governs the behavior.
- **When the user pushes back with a physical/domain intuition, treat it as probably-right and go measure** — don't defend the story. When genuinely uncertain, say so and state exactly what you'd check.

If you're about to write "this is because…", "that file does…", or "the fix is…" without having just looked — stop and look.

## Context Packs — Temporarily Disabled

Use default exploration (Glob / Grep / Read / Agent). Do not `Read` `.claude/repomix/pack_*.md` unless asked. Restore guidance with `git checkout CLAUDE.md`.

## Repo Data Lives in S3, Not Git

Race notes (`Analyze/Notes/*` except `scripts/`), `Analyze/tacking_article/`, research write-ups, ops docs (`AWS_SECURITY_STATUS.md`, `BACKUP.md`, `AGENT_STATUS.md`, `DEPLOY-AUTH.md`, …), `Results/` fixtures/seeds, captures, and `Src/src/assets/images/dual_fisheye_big.jpg` are **gitignored** and stored in `s3://regattaview-repo-data/tree/` (private, versioned — S3 versions replace git history for them). `repo_data.manifest` is the list. A missing one of these files means "not pulled", not "deleted":

- `conda run -n sailing python Scripts/repo_data.py status|pull|push`. `pull` only fills in missing files (`--force` to overwrite); `push` uploads new/changed files and never deletes.
- **Fresh clones and new worktrees need `pull`** before running `Results/` tests or editing notes. The frontend uses a non-personal solid texture and builds without private repo data. Push after editing repo-data files.
- Don't add data/media/write-ups to git; add the path to `repo_data.manifest` + the `.gitignore` "Repo data" block instead.

## Git Workflow — Worktrees by Default

For any nontrivial edit, work in a named worktree, not the main checkout — keeps parallel Claudes from colliding and gives a clean `git diff` review surface. Applies to the main thread and spawned subagents alike. Read-only work and trivial one-liners can stay in main.

- **Create up front** with a kebab-case slug; all edits (main thread or subagent) go to absolute paths under it:
  ```
  git worktree add ../RegattaView-<topic> -b agent/<topic>
  ```
- **NEVER use the Agent tool's `isolation: "worktree"` flag.** Its auto-cleanup reaps worktrees with uncommitted changes — it silently wiped 4 files / 1082 lines in May 2026. Always pre-create a named worktree.
- **Default: leave changes UNTRACKED** (don't commit). When done, report: worktree path, branch, `git status` summary, and a short build/test verification. Then wait. Do not commit, merge, rebase, or touch `main` — Auto Mode is not authorization to.
- **Exception — long/risky work** (multi-hour, or a structural change atop a big untracked diff): make WIP commits on `agent/<topic>` so a crash can't lose them. No need to ask.
- **On explicit go-ahead**, commit in the worktree, then from main `git merge --ff-only agent/<topic>`. If `--ff-only` fails because `main` moved, surface it — don't reflexively rebase or force. Clean up: `git worktree remove ../RegattaView-<topic>` + `git branch -d agent/<topic>`.
- **Dev server inside a worktree runs on 9100** (`cd Src && PORT=9100 npm run start`) — without the override it binds 9000 and serves a stale, partial `Analyze/` tree.
- **Worktree `Analyze/` has only git-tracked files.** Pipeline outputs (`events_index.json`, `Events/`, `Sessions/`, `AIS/`) live only in the main tree — test anything that depends on them against main.
- **Stale opaque worktrees under `.claude/worktrees/`** (from past `isolation` usage) may hold unreviewed work — report them (`git worktree list`), don't silently delete.

## Verification & Deploy Defaults

- **Conda envs:** main pipeline + `Analyze/` → `sailing`; calibration scripts/tests → `calibration`. Prefer explicit `conda run -n <env> ...`.
- **Frontend build:** `cd Src && npm run build` on Node 22.15+ LTS or 24+. `npm test` runs the security unit tests. `tests/browser-security.cjs` checks the built site with isolated Chrome and synthetic data; set `CSP_FILE` to the policy exported from `Scripts/harden_site.py`.
- **Video rendering changes:** run `cd Src && npm run test:renderer` for real WebGL pixel checks, then inspect the actual visible canvas on a representative clip. Video `readyState` and advancing time alone do not prove that the projected image renders. Streaming textures must use non-mipmap filtering and clamp wrapping when frames replace a small placeholder.
- **Deploy is build → stage → sync, not just build.** `cd Src && npm run deploy` does all three; `npm run stage` does build + rsync `dist/` → `/Users/viola/www/sailview/` only. `aws_sync.sh` syncs from `/Users/viola/www` (hardcoded `BASE`), so the stage step is required.
- `aws_sync.sh` sets `Cache-Control: no-cache, must-revalidate` (webpack output isn't content-hashed, so long caching would strand PWAs on old builds). **Never `aws s3 cp --metadata-directive REPLACE` without `--content-type`** — `REPLACE` wipes the inferred `Content-Type` to `binary/octet-stream` (Safari then downloads instead of rendering). Fix in-place by re-uploading from local: `aws s3 cp /Users/viola/www/sailview/ s3://regattaanalysis/www/sailview/ --recursive --cache-control "no-cache, must-revalidate"`. Don't add `--acl public-read` — the bucket has Block Public Access on (since 2026-04-25).
- `Src/webpack.config.js` is the source of truth for frontend entry points. `viewer360.js` is shared 360 logic; `sailview.js`, `review.js`, `calibrate.js`, `playlists.js`, etc. are page entries.
- NMEA/AIS changes span layers — check firmware (`Arduino/include/nmea.hpp`), processing (`Libraries/bb_log.py`), extraction (`Scripts/extract_ais.py`), and `AIS_IMPLEMENTATION.md` before changing filtering.
- Absolute `/Users/viola/...` paths in this repo are developer-specific examples; don't hardcode them into shared code unless asked.

## Local Dev With Auth (sailview.html / review.html)

Both pages enforce Google Sign-In against the **production** Lambda — no localhost bypass, and it's wired to just work, so don't add shims.

- `cd Src && npm run start` → `http://localhost:9000/sailview.html` (navigate to `/review.html`, `/tracks.html` as needed). **Port must be 9000** — the OAuth client's Authorized Origins and the Lambda Function URL `AllowOrigins` are pinned to it; if webpack bumps to 9001, Google refuses the popup.
- First load bounces to `login.html` (`checkAuthorization()` at boot). After sign-in the Lambda mints an HMAC session token (HS256 JWT, 30-day TTL); `shared.js::harvestSessionToken` stores it in `localStorage['regatta_session_token']` and drops the Google ID token. Force re-login by clearing both `google_id_token` and `regatta_session_token` (keys from `auth_config.js`); use incognito to exercise the full popup path (One Tap silently re-auths).
- The Google account must be on the allowlist (`s3://regattaanalysis/auth/allowlist.json`, managed via `Scripts/manage_allowlist.py`; Lambda caches it ~5 min). Rotating the Lambda's `SESSION_SECRET` invalidates every active session — don't do it casually.
- **Local vs remote:** the dev server serves bundles + `/Analyze/*` (read) + `/Comments/*` (read) from disk, and catches `PUT /Analyze/events_edits.json` and `PUT /Comments/<alias>/comments.json` to local disk — **these never reach S3**. Video, `.poses`, `.log`, and any `signAWSUrl`/`signAWSPutPath` call hit the prod Lambda → prod S3. A populated review page needs `events_index.json`, `Analyze/Events/<id>.json`, and `Analyze/Sessions/<basename>.json` (all from `find_starts.py`) on local disk.
- Gotchas: ad blockers / strict tracking protection block the GIS iframe (button never renders — check console for `accounts.google.com` blocks). Dev server is HTTP — Google allows `http://localhost` as an exception, don't set up HTTPS. `?reset=1` clears only the `sensorFields` cookie, not auth.

## Frontend Defaults

Keep UI simple, utilitarian, accessible. No heavy dependencies without a reason. For visual changes, explain what the user will see and any tradeoffs.

## Frontend — Mobile / PWA (sailview.html)

- Mobile UX is gated on `@media (hover: none) and (pointer: coarse)` (`Src/src/css/video.css`) — touchscreen laptops report `hover/fine` and keep the desktop layout, so never use width-only breakpoints for mobile-vs-desktop.
- Default mobile state: canvas fills viewport, four translucent FABs bottom-left. `#fabControls` toggles `.mobile-visible` on `.controls-bar`; `#fabInstruments` toggles it on `.info`. `#cornerTime` (fed from `log_data.row_times`) is always visible, replacing DT on mobile.
- Pointer input: `Src/src/js/pointer_controls.js` (`attachPointerControls`), owned by `viewer360.js` (the shared 360 core both pages consume). Viewpoint is yaw + pitch only (rebuilt each frame as `rotateY(yaw)·rotateX(pitch)`) so the horizon never tilts; pinch sets FOV, double-tap resets.
- iOS Safari hijacks `<video>.play()` to native fullscreen unless `playsInline` is set — `viewer360.js` sets the property plus `playsinline`/`webkit-playsinline` attributes for version coverage.
- Autoplay: `<video>` is created `muted=true`; `sailview.js`/`review.js` override to `false`. `loadVideo({autoplay:true})` plays unmuted, and on autoplay-policy rejection retries muted — so on cold visits / Safari / iOS PWA sound may start muted (user presses **M**). No auto-unmute on gesture (would race the M-key/mute-button toggles).
- `requestFullscreen` is unsupported on iPhone — **Add to Home Screen** gives true fullscreen via the PWA meta tags; the fullscreen button falls back to a toast hint there. The home-screen icon is pinned to the start URL; JS `history.replaceState` (share-link freshness) doesn't navigate the PWA — video switching is client-side via `viewer.loadVideo({...})`.
- Instrument panel recovery: `sailview.html?reset=1` wipes the `sensorFields` cookie → `DEFAULT_FIELDS`. Cookies are origin-scoped, so this also resets the PWA for that origin.

## Project Overview

RegattaView replays sailing races by combining 360° Insta360 video with NMEA 2000 sensor data from a custom ESP32 logger (BoatBrain). Data is processed locally and served via AWS S3 / CloudFront.

```
Insta360 SD ─┐                    ┌─> Compressed video + .poses files
             ├─> race_upload.py ──┼─> Binary .log files (10Hz)
BoatBrain SD ┘                    └─> AWS S3 (regattaanalysis bucket)
```

**Prod serving:** CloudFront (dist `EPCTF7XCNPLK2`) at `https://regattaview.net/...` (the `d1waozqlewn15v.cloudfront.net` URL is a parallel alias), fetching from `regattaanalysis` via OAI `E11QZ241P7LDN9`. The bucket is private — data reads/writes go through Lambda-signed S3 URLs (`shared.js` `signAWSUrl`/`signAWSPutPath`); direct-S3-vhost URLs 403 anonymously. The Lambda emits structured CloudWatch logs (`PRESIGN`, `SIGNIN`, `AUTH_FAIL`, `WOULD_DENY`) but never raw presigned URLs. `aws_sync.sh` pushes `events_index.json`, `Analyze/Events/`, `Analyze/Sessions/` but never `events_edits.json` or `Comments/` (client-write-only).

**Lambda signing is a security boundary.** The signing policy (`lambda_function.py::_signing_policy_reason`) is always enforced, and the Lambda role's IAM (`Lambda/iam/*.json`) is narrowed to the required prefixes. Change both together. Collaborative writes use validated document operations with ETag conflict checks and account-owned comments; never re-enable raw PUT signing for the site bucket. Build assets ship publicly; CopyPlugin now excludes the entire private `src/assets/images/` folder. History: `AWS_SECURITY_STATUS.md` §9.

## Pipeline Documentation — Read Before Rediscovering

Read the relevant doc before re-deriving the flow from source:

- `TECHNICAL_DOCUMENTATION.md` — canonical: data sources + PGN whitelist, the five `race_upload.py` stages (copy → video → log → sync → pose), GPS↔BB time correction (least-squares `α·BB + β`), and the 21-column column-major uint16 `.log` format.
- `docs/BOATBRAIN_SYSTEM.md` — on-boat side: firmware, iOS UDP/BLE apps, WiFi captive portal, PGN→instrument table.
- `Libraries/README.md` — Python module reference + `bb_log.py` → `canboat.py` → parquet → `race_logs.py` data flow.
- `AIS_IMPLEMENTATION.md` — AIS is a side-channel (not in `PGN_WHITELIST`), re-parsed by `extract_ais.py` into per-MMSI JSON, then sliced into `Analyze/AIS/` (review) and `Analyze/AISDays/` (tracks).
- `POSES_FORMAT.md` — per-video `.poses` binary (v2 delta-encoded int16 quaternions, brotli) with legacy-v1 fallback.
- `docs/FISHEYE_CALIBRATION.md` — how the dual-fisheye preset (`Src/src/assets/calibration/default.json`) is produced/recalibrated; the GLSL euler-transpose trap. Read before touching `fisheye_shader.js` lens math.
- `AWS_SECURITY_STATUS.md` — S3/CloudFront/IAM hygiene after the 2026-04-22 cost anomaly, and (§8) `Scripts/cloud_activity_report.py`. **Run that tool first** for any cost / who-watched-what / sign-in / suspicious-activity / "what's running on AWS" question.
- `BACKUP.md` — disaster-recovery coverage. The web stack covers compressed video / `.poses` / `.slog` / review artifacts; raw `BBLog/`, legacy logs, and `metadata.yml` cold-back to `s3://regattaview-cold-backup` (DEEP_ARCHIVE) via `Scripts/cold_backup.sh`. Raw `.insv` originals are still uncovered. Read this for any "is X backed up / what if the drive dies / how to restore Y."
- `docs/VIRB_MIGRATION.md` — one-time 2020-2023 VIRB-era corpus migration: equirect support, stabilizer frame conventions, on-disk quat basis change.
- `docs/AUDIO_TRANSCRIBE.md` — race/lesson audio → transcript → summary + keypoints. Day-to-day: `Scripts/find_new_audio.py` then `Scripts/new_lesson.py <basename>`. Outputs to `Analyze/Transcripts/`, viewed in `lesson.html`.

**Code ground truth when docs drift:** `Libraries/bb_log.py`, `Libraries/canboat.py`, `Scripts/race_upload.py`. Trap: the 10 Hz parquet in `PandasLogs/` and the per-video uint16 `.log` are different formats — the `.log` is a per-video slice from `sailview_log_v1.save_log_to_binary()`, not `race_logs.py`. Video↔log sync cross-correlates `dHDG/dt` against the Insta360 gyro in `insta360.sync_video_to_bb()`.

### `.log` / `.slog` consumers must apply sensor calibration

Raw `.log`/`.slog` columns are NOT display-ready. After `loadLog()`, `viewer360.js::applySensorCalibration` rewrites `spd`, `awa`, `depth` and synthesizes `heel` from `zg100_roll - cal.heel.offset`. **Every page that loads a `.log`/`.slog` must run this** (e.g. `tracks.js` does, inside `loadDayLog()`) — without it SPD reads ~18% low and HEEL is 4° off, because the paddle-wheel and roll sensor are physically miscalibrated and the fix lives only in `Src/src/assets/calibration/sensor_calibration.json` (fetched at runtime). If you need `heel` before calibration runs, fall back to `zg100_roll`.

## Mic-on-Person Audio Swap

Replaces the camera mic on compressed Insta360 mp4s with an iPhone Voice Memos recording, time-aligned via the BB-corrected `.slog` anchor. `race_upload.py` runs the `voice_audio` stage automatically (idempotent via `applied.json`); standalone debug tools are `Scripts/swap_video_audio.py` and `Scripts/voice_memos_lookup.py`.

- **Drop dir:** `/Users/viola/Data/Insta360/Audio/` — move the `.m4a` there.
- **Recording-start resolution** (`voice_audio.py::_resolve_audio_start`) is strict: sidecar `<name>.start.txt` > iOS Voice Memos DB lookup (sub-second `ZDATE`) > hard error. **No mtime fallback** (iMessage/iCloud/iTunes rewrite embedded timestamps). Manual override: drop `<name>.start.txt` with one ISO 8601 line (naive = system-local).
- **Voice Memos DB needs Full Disk Access** for Terminal; opened `mode=ro` (NOT `immutable=1`, which drops un-checkpointed WAL rows). Voice Memos.app must have launched once (the run does `open -ga "Voice Memos"` to flush CloudKit deltas). iCloud-pending recordings (blob not downloaded) are silently skipped — click them in the app or AirDrop to the drop dir.
- **Auto-ingest gate** = "overlaps a BB log parquet" (`_bb_log_overlaps`), so off-boat recordings stay out and audio-only days reach the synthesis path.
- **Audio-only / camera-died:** with no `VID_*` covering the audio span, `blank_video.create_for_audio` synthesizes a `VIA_*` equirect black-frame mp4 (silent AAC track, identity-quat `.poses`, real `.log`/`.slog`) and muxes the audio in. `videos.json` marks `VIA_*` rows `AudioOnly: true` + `projection: 'equirect'`.
- **Too-short mp4s** (<30s) are quarantined to `.../TooSmall/` by `insta360.clean_too_short_videos` before `create_videos_json`.

## Subsystems With Their Own Docs

- **Race Results Crawler — `Results/`** (operating manual: `Results/README.md`). Crawls + queries PNW results (STYC, CYC archive/main, SYC) so any seed boat → all its races + leaderboards. SYC is hardest: Cloudflare-blocked live page (seeds from a committed Playwright HTML), PDF results that need Tesseract OCR fallback for Type3 fonts. Identity = sail # + fuzzy name (rapidfuzz ≥88), overrides in `identity_overrides.yaml`. 63 offline tests: `cd Results && conda run --no-capture-output -n sailing python -m pytest`.
- **Race Notes — `Analyze/Notes/`** (operating manual: `/race-note` skill at `.claude/skills/race-note/SKILL.md`; reference note `2026-06-03_j105-start-heel/`). One note = one dir with `note.md`, `meta.yaml`, `figures/`, built `note.html`, and `scripts/` (this note's thin generators). Build: `conda run --no-capture-output -n sailing python Scripts/build_notes.py --note <slug>` (or `--all`). Deep-links via `Libraries/note_links.py` (don't hand-encode). Content is 403-to-anon, fetched via Lambda-signed URL after auth like `lesson.js`; deploy with `npm run deploy`. Reusable logic has one home in the `Analyze/` package (`current_analysis`, `polar`, `race_geometry.py`) — promote shared helpers there, never copy between notes.

## Common Commands

```bash
# Python — `sailing` env
conda run -n sailing python Scripts/race_upload.py              # full pipeline (copy → process → upload)
conda run -n sailing python Scripts/extract_ais.py --date 2026-04-11
conda run --no-capture-output -n sailing python Analyze/slice_ais_day.py --days 30
conda run -n sailing python Scripts/recompute_logs.py 20241015  # reprocess one date
conda run -n sailing python Analyze/find_starts.py --days 365   # review data: events_index.json + Events/ + Sessions/

# Frontend — Src/
cd Src && npm run start    # dev server :9000 (mirrors prod S3 layout)
cd Src && npm run build    # prod build → dist/
cd Src && npm run stage    # build + rsync → /Users/viola/www/sailview/
cd Src && npm run deploy   # stage + aws_sync.sh (full S3 deploy)

# Calibration — `calibration` env
conda run -n calibration python -m pytest Calibration/tests/ -v

# Firmware — Arduino/BoatBrain
pio run -e m5stack-cores3 [-t upload]

# iOS — iOS/ (BoatData, BoatStart)
xcodebuild -scheme BoatData -destination 'platform=iOS,name=DEVICE' build
xcrun devicectl device install app --device DEVICE "/path/to/BoatData.app"
```

`find_starts.py` also runs automatically inside `race_upload.py` unless `--no-analyze` is passed.

## Architecture

### Python Libraries (`Libraries/`)
- **bb_log.py** — BoatBrain log processing: raw NMEA 2000 → JSON (canboat) → Parquet. `PGN_WHITELIST` controls which messages are kept.
- **insta360.py** — video extraction, compression, IMU pose estimation, video↔log sync.
- **canboat.py** — JSON parsing, PGN filtering, record transformation.
- **global_variables.py** — PGN defs, paths, Seattle constants. Singleton `G` used everywhere.
- **metadata.py** — race metadata YAML + Google Sheets.
- **race_logs.py** — loads parquet into DataFrames (smoothing, true wind, heel). *Not* the writer of the per-video `.log` (that's `sailview_log_v1.save_log_to_binary()`).

### Frontend (`Src/src/js/`)
- **viewer360.js** — shared 360 viewer core. **sailview.js** — main replay (video + instruments). **review.js** — race-start review/annotation. **chart.js** — Leaflet track map. **sparkline.js** — instrument sparklines.

### Firmware (`Arduino/`) & iOS (`iOS/`)
- **Arduino/BoatBrain/** — logger firmware (M5Stack CoreS3 + NMEA 2000). `include/nmea.hpp` `NMEA2000Handlers[]` controls which PGNs are logged.
- **iOS/BoatData/** — real-time instruments (UDP port 101). **BoatStart/** — race timer (RaceBox BLE GPS). **Shared/**, **UIShared/** — common code/UI. Debugging: `iOS/BoatData/DEBUGGING.md`.

### Key patterns
- **PGN filtering happens at three levels:** firmware (`nmea.hpp` handlers) → Python (`bb_log.py` `PGN_WHITELIST`) → AIS separately (`extract_ais.py`, different structure).
- **Data formats:** raw BB logs = Actisense binary (`actisense-serial | analyzer -json`); processed = 10 Hz Parquet; web binary = 16-byte header + column-major Uint16; poses = quaternion arrays.

### Data locations
Raw BB logs `/Users/viola/Data/BBLog/` · parquet `…/SailingData/PandasLogs/` · AIS `…/SailingData/AISLogs/` · video `/Users/viola/Data/Insta360/`.
