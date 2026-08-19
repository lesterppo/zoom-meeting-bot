# Zoom Meeting Bot (Auto-Attend)

[![GitHub license](https://img.shields.io/github/license/lesterppo/zoom-meeting-bot)](LICENSE)
[![GitHub release](https://img.shields.io/github/v/release/lesterppo/zoom-meeting-bot)](https://github.com/lesterppo/zoom-meeting-bot/releases)
[![GitHub stars](https://img.shields.io/github/stars/lesterppo/zoom-meeting-bot)](https://github.com/lesterppo/zoom-meeting-bot/stargazers)
[![Workflow](https://img.shields.io/github/actions/workflow/status/lesterppo/zoom-meeting-bot/zoom-attendance.yml)](https://github.com/lesterppo/zoom-meeting-bot/actions)

Automatically **attend Zoom meetings for you** using browser automation
(Playwright + Chromium), scheduled by **GitHub Actions**. It joins a few
minutes before start, stays connected for the whole meeting, captures
screenshot evidence, and leaves cleanly. Works for any meeting that can be
joined from a web browser (no desktop client needed).

## How it works

```
GitHub Actions cron (*/5)  ──►  check_due.py (stdlib, ~5s no-op)
                                   │  due? (join window hit or manual dispatch)
                                   ▼
                    preflight.py (HTTPS only, ~1s, no browser)
                                   │  meeting exists? (wpk issued)
                     invalid/ended ─┴─►  skip: no install, report, exit green
                                   ▼
                pip install playwright (~15s) — NO browser download:
                GitHub runners ship Google Chrome, launched via channel="chrome"
                                   ▼
                    xvfb-run join_zoom.py  (headed system Chrome)
                                   │
        app.zoom.us/wc/join/<id>?pwd=...  ──►  name form (SPA or legacy)
                                   │
                       mute + stop video (preview)
                       JS-click join (SPA .preview-join-button / legacy .submit)
                                   │
              in-meeting detected (Leave button / camera dialog)
                                   │
              enforce mute + camera-off (verified via aria state)
                                   │
              stay until end · heartbeat · screenshots every 15 min
                                   ▼
                              leave · upload evidence artifact
```

The web-client flow is **live-verified** against Zoom's current UI (2026-08):
URL normalization (`zoom.us/j/<id>?pwd=...` → `app.zoom.us/wc/join/<id>?pwd=...`
to skip the flaky interstitial), both join-form variants (SPA `#input-for-name`
and the legacy `#inputname` served to some CDN clients), the
enabled-transition race on the Join button (fixed with a JS click), the
"Do you see yourself?" camera check, fake-device audio, and
waiting-room/meeting-ended states.

### Browser strategy (reliable + fast)

- **Default: Playwright-bundled Chromium** — most reliable in live testing:
  consistent SPA join form and ~5 s join (system Chrome can get a legacy form
  variant and takes ~45 s to connect).
- **`actions/cache`** stores `~/.cache/ms-playwright`, so the ~2 min browser
  download happens once; subsequent runs restore it in seconds.
- **System Chrome fallback** (`--use-system-chrome`) still works on runners
  without a cached Playwright browser (e.g. macOS, image changes) by launching
  the pre-installed Google Chrome via `channel="chrome"`.

### HTTPS pre-flight (saves ~2 min on dead meetings)

Before installing the browser, the workflow runs `preflight.py` — a pure
HTTPS GET against Zoom's join endpoint (~1s). Zoom issues a `wpk` web
participant key **only for meetings that exist**, so the check is reliable
and needs no browser:

- `ok` → meeting exists → proceed to install + join
- `invalid` → bad ID / cancelled meeting → skip install, finish green
- `ended` → meeting already over → skip join, finish green
- `error`/ambiguous → unknown (SPA shell, geo, bot-block) → **proceed anyway**
  so a false-negative never costs attendance

### Mute + camera off (attendee privacy)

The bot joins **silent with video off**, enforced in two layers:

1. **Preview form** — clicks `Mute` and `Stop Video` before joining.
2. **In-meeting enforcement** — Zoom *resets* the preview controls on join,
   so after entering the meeting the bot reads the mic/video button state
   (`aria-label="mute my microphone"` = unmuted, `aria-label="stop my video"`
   = video on) and clicks until both are off. The final state is verified and
   logged (`in-meeting state: muted=True video_off=True`) before the JOINED
   screenshot is taken.

## Quick start

### 1. Fork / clone and configure meetings

Copy `meetings.example.yaml` to `meetings.yaml` (gitignored) and fill in your
meetings:

```yaml
timezone: Asia/Hong_Kong
defaults:
  display_name: "Meeting Attendee"
  duration_min: 60
meetings:
  - name: "Daily Standup"
    zoom_url: "https://zoom.us/j/1234567890?pwd=REPLACE_ME"
    start: "09:30"            # daily recurring
  - name: "Grand Rounds 2026-08-20"
    meeting_id: "1234567891"
    passcode: "000000"
    start: "2026-08-20 14:00" # one-off
    duration_min: 90
```

`start` is either `HH:MM` (daily) or `YYYY-MM-DD HH:MM` (one-off), in the
configured IANA `timezone`.

### 2. Push the secret (keep meetings out of git)

Add a repo secret named **`ZOOM_MEETINGS_JSON`** with the **same content** as
your `meetings.yaml` (base64 or plain JSON — both accepted):

```bash
python3 -c "import base64,json,yaml;print(base64.b64encode(json.dumps(yaml.safe_load(open('meetings.yaml'))).encode()).decode())"
```

Secrets are masked in Actions logs and never written to the repo.

### 3. Enable the workflow

Push the repo — `.github/workflows/zoom-attendance.yml` runs on a
`*/5` cron. Each tick runs a ~5s stdlib check; only ticks inside a meeting's
join window install the browser and join.

Test it immediately from the Actions tab: **Run workflow** → pick a meeting
name (or leave empty for auto) → optionally set a `start_override`
(`YYYY-MM-DD HH:MM`) or `duration`.

## Manual / local run

```bash
pip install -r requirements.txt
python -m playwright install chromium

python3 join_zoom.py                 # join any due meeting (auto)
python3 join_zoom.py --validate      # parse config, print plan
python3 join_zoom.py --dry-run       # plan without launching a browser
python3 join_zoom.py --meeting "Daily Standup" --start-override "2026-08-20 09:30"
python3 join_zoom.py --meeting-json '{"name":"x","zoom_url":"https://zoom.us/j/...?pwd=..."}'
```

On a headless box run under Xvfb: `xvfb-run -a python3 join_zoom.py …` (the
workflow does this automatically).

## Meeting config reference

| Key | Required | Description |
|---|---|---|
| `name` | ✅ | Unique id; used by `workflow_dispatch` input |
| `zoom_url` | ✅* | Full invite URL, e.g. `https://zoom.us/j/1234567890?pwd=abc` |
| `meeting_id` | ✅* | Numeric meeting id (alternative to `zoom_url`) |
| `passcode` | — | Meeting passcode (needed if not in `zoom_url`) |
| `start` | ✅ | `HH:MM` daily, or `YYYY-MM-DD HH:MM` one-off |
| `duration_min` | — | Minutes to stay connected (default 60, cap 340) |
| `display_name` | — | Name shown to participants (default: meeting name) |
| `join_lead_min` | — | Join N min early (default 4) |
| `join_grace_min` | — | Still join N min late (default 2) |

\* provide either `zoom_url` or `meeting_id`.

## Reliability notes

- **No-op cron ticks are cheap**: the due-check uses only the Python stdlib,
  so 288 daily ticks cost ~5s of runner time each; Playwright/Chromium are
  installed only on ticks that actually join.
- **Fake media devices** (`--use-fake-device-for-media-stream`) let WebRTC
  connect without a real mic/camera, and auto-accept the permission prompt.
- **Passcodes stay in secrets** — the repo contains no meeting data.
- **Meeting states handled**: waiting room, "wait for the host", meeting
  ended early, invalid link, sign-in-required (SSO), camera self-view dialog,
  audio join prompt.
- **SSO-gated meetings** cannot be joined anonymously — the bot detects the
  sign-in wall and reports it instead of hanging.
- Evidence screenshots upload as a workflow artifact after every join.

## Repo layout

```
join_zoom.py                 main bot (join → stay → leave)
preflight.py                 HTTPS-only meeting existence check (wpk signal)
check_due.py                 stdlib due-checker for the cron gate
meetings.example.yaml        config template (meetings.yaml is gitignored)
.github/workflows/zoom-attendance.yml   schedule + manual dispatch
evidence/                    screenshots (gitignored)
```

## License

MIT — see [LICENSE](LICENSE). Author: [Peter (lesterppo)](https://github.com/lesterppo).
