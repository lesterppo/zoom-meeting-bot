#!/usr/bin/env python3
"""
join_zoom.py — attend a Zoom meeting automatically via Playwright browser automation.

Designed to run as a scheduled job (GitHub Actions cron or local cron): it joins the
meeting a few minutes before start, stays connected until the meeting ends, captures
screenshot evidence, and leaves cleanly.

Live-verified against Zoom's web client (2026-08):
  - invite link  https://zoom.us/j/<id>?pwd=...  ->  interstitial ->  "Join from browser"
  - join form    #input-for-name + .preview-join-button (JS click needed; Playwright
                 click can race the button's enabled transition)
  - in-meeting   button[aria-label="Leave"] visible
  - camera check "Do you see yourself?" -> click "Yes"
  - audio        fake media flags auto-join computer audio

Usage:
  python3 join_zoom.py                        # auto: join any meeting that is due now
  python3 join_zoom.py --meeting "Rounds"     # force a specific meeting (manual dispatch)
  python3 join_zoom.py --meeting "Rounds" --start-override "2026-08-20 09:30"
  python3 join_zoom.py --validate             # parse config, print plan, exit
  python3 join_zoom.py --dry-run              # show what would join, don't launch browser

Config: meetings.yaml (see meetings.example.yaml) OR env ZOOM_MEETINGS_JSON
(base64 or plain JSON list — used by GitHub Actions so passcodes stay in secrets).

Exit codes:
  0  attended / nothing due / dry-run / validate ok
  1  configuration or join error
  2  no meeting due (auto mode)
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import time
import traceback
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
except ImportError:  # pragma: no cover
    sync_playwright = None

DEFAULT_TZ = "Asia/Hong_Kong"
DEFAULT_LEAD_MIN = 4      # join this many minutes before start
DEFAULT_GRACE_MIN = 2     # still join this many minutes after start
DEFAULT_DURATION_MIN = 60
MAX_STAY_MIN = 340        # GitHub-hosted runner job cap is 360 min
EVIDENCE_DIR = "evidence"

# --- config -------------------------------------------------------------

def _decode_secret(raw: str) -> dict | None:
    """ZOOM_MEETINGS_JSON may be base64 JSON or plain JSON."""
    raw = raw.strip()
    if not raw:
        return None
    try:
        if raw[0] not in "{[":
            raw = base64.b64decode(raw).decode("utf-8")
        data = json.loads(raw)
        if isinstance(data, dict) and "meetings" in data:
            return data
        if isinstance(data, list):
            return {"meetings": data}
        return data
    except Exception as e:
        print(f"ERROR: cannot decode ZOOM_MEETINGS_JSON: {e}")
        raise

def load_config(args) -> dict:
    env_cfg = _decode_secret(os.environ.get("ZOOM_MEETINGS_JSON", ""))
    if env_cfg:
        print("config: from ZOOM_MEETINGS_JSON env")
        return env_cfg
    path = args.config or os.path.join(os.path.dirname(os.path.abspath(__file__)), "meetings.yaml")
    if not os.path.exists(path):
        raise SystemExit(f"ERROR: config file not found: {path}\n"
                         f"       copy meetings.example.yaml to meetings.yaml and fill it in, "
                         f"or set ZOOM_MEETINGS_JSON.")
    import yaml
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    print(f"config: from {path}")
    return cfg

def _tz(cfg: dict) -> ZoneInfo:
    return ZoneInfo(cfg.get("timezone") or DEFAULT_TZ)

def _defaults(cfg: dict) -> dict:
    return cfg.get("defaults") or {}

def _parse_dt(s: str, tz: ZoneInfo) -> datetime:
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=tz)
        except ValueError:
            continue
    raise ValueError(f"start must be 'YYYY-MM-DD HH:MM' or 'HH:MM', got: {s!r}")

def next_start(m: dict, tz: ZoneInfo, now: datetime) -> datetime:
    """Resolve a meeting's start to the next concrete datetime (daily HH:MM repeats)."""
    s = str(m.get("start", "")).strip()
    if not s:
        return now  # manual/forced join
    # one-off absolute
    if re.match(r"\d{4}-\d{2}-\d{2}", s):
        return _parse_dt(s, tz)
    # daily HH:MM
    try:
        hh, mm = s.split(":")
        candidate = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
        if candidate < now - timedelta(minutes=30):
            candidate += timedelta(days=1)
        return candidate
    except ValueError:
        raise ValueError(f"invalid start {s!r}; use 'HH:MM' (daily) or 'YYYY-MM-DD HH:MM' (one-off)")

def select_meeting(cfg: dict, name: str, start_override: str, duration_override: str,
                   now: datetime) -> tuple[dict | None, str | None]:
    """Return (meeting-or-None, reason). If --meeting given, force it."""
    tz = _tz(cfg)
    defs = _defaults(cfg)
    meetings = cfg.get("meetings") or []
    if not meetings:
        return None, "no meetings in config"

    forced = None
    if name:
        forced = next((m for m in meetings if str(m.get("name", "")) == name), None)
        if forced is None:
            raise SystemExit(f"ERROR: meeting {name!r} not found in config. Known: "
                             f"{[m.get('name') for m in meetings]}")

    if start_override:
        ov = _parse_dt(start_override, tz)
        forced = forced or meetings[0]
        forced = {**forced, "start": ov.strftime("%Y-%m-%d %H:%M")}

    if forced:
        start = next_start(forced, tz, now)
        if start_override:
            start = _parse_dt(start_override, tz)
        forced = {**forced, "_start": start}
        return forced, f"forced join {forced.get('name')} at {start:%Y-%m-%d %H:%M}"

    # auto: any meeting inside its join window
    for m in meetings:
        start = next_start(m, tz, now)
        lead = int(m.get("join_lead_min", defs.get("join_lead_min", DEFAULT_LEAD_MIN)))
        grace = int(m.get("join_grace_min", defs.get("join_grace_min", DEFAULT_GRACE_MIN)))
        # truncate to minute so a cron tick at HH:MM:03 doesn't miss a window
        # that ends at HH:MM:00 (GitHub Actions fires at minute boundaries)
        now_min = now.replace(second=0, microsecond=0)
        if start - timedelta(minutes=lead) <= now_min <= start + timedelta(minutes=grace):
            return {**m, "_start": start}, f"due window hit for {m.get('name')}"
    return None, "no meeting due now"

def build_url(m: dict) -> str:
    """Return a URL that lands directly on Zoom's web join form.

    The classic invite URL (https://zoom.us/j/<id>?pwd=...) goes through an
    interstitial that tries a zoommtg:// app handoff and is flaky under
    automation. Normalizing to https://app.zoom.us/wc/join/<id>?pwd=...
    skips the interstitial and renders #input-for-name immediately
    (live-verified 2026-08)."""
    url = (m.get("zoom_url") or "").strip()
    mid = str(m.get("meeting_id", "")).strip()
    pwd = str(m.get("passcode") or m.get("pwd") or "").strip()

    if url:
        # accept app.zoom.us/wc/join/... as-is
        if "wc/join" in url:
            return url
        # extract id + pwd from a zoom.us/j/<id>?pwd=... or zoom.us/j/<id> invite link
        m2 = re.search(r"/(?:j|wc/join)/(\d+)(?:\?pwd=([A-Za-z0-9._\-]+))?", url)
        if m2:
            mid = m2.group(1) or mid
            pwd = m2.group(2) or pwd
        else:
            return url  # unknown shape — let the bot try it

    if not mid:
        raise ValueError(f"meeting {m.get('name')!r}: need zoom_url or meeting_id")
    base = f"https://app.zoom.us/wc/join/{mid}"
    if pwd:
        return f"{base}?pwd={pwd}"
    return base

def plan_meeting(m: dict) -> dict:
    tz = m.get("_tz")
    start = m.get("_start")
    dur = int(m.get("duration_min") or DEFAULT_DURATION_MIN)
    end = start + timedelta(minutes=min(dur, MAX_STAY_MIN) + 3)
    return {
        "name": m.get("name", "meeting"),
        "start": start,
        "end": end,
        "duration_min": dur,
        "display_name": m.get("display_name") or m.get("name", "Attendee"),
    }

# --- browser join -------------------------------------------------------

TXT_JOIN_BROWSER = ["Join from browser", "從瀏覽器加入", "从浏览器加入"]
TXT_JOIN = ["Join", "加入"]
TXT_LEAVE = ["Leave", "離開", "离开"]
TXT_YES = ["Yes", "是"]
TXT_AUDIO_COMPUTER = ["Join with Computer Audio", "Join Audio by Computer", "通過電腦音頻加入", "通过电脑音频加入"]
TXT_AUDIO_BY_PHONE = ["Join by Phone", "通過電話加入", "通过电话加入"]

LAUNCH_ARGS = [
    "--use-fake-ui-for-media-stream",      # auto-accept mic/camera permission
    "--use-fake-device-for-media-stream",  # provide a fake mic/camera so WebRTC connects
    "--autoplay-policy=no-user-gesture-required",
    "--disable-dev-shm-usage",
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--no-sandbox",
]

def _visible_texts(page) -> str:
    try:
        return page.evaluate("() => document.body ? document.body.innerText : ''") or ""
    except Exception:
        return ""

def _has_any(texts: list[str]) -> str:
    """Return the first selector-like text that is safe to pass to get_by_text."""
    return texts[0]

def _click_visible(page, locator, timeout=8000):
    locator.first.wait_for(state="visible", timeout=timeout)
    locator.first.click()
    return True

def _click_text_any(page, texts, timeout=8000) -> bool:
    for t in texts:
        loc = page.get_by_text(t, exact=True)
        try:
            if loc.count() and loc.first.is_visible():
                loc.first.click(timeout=timeout)
                return True
        except Exception:
            continue
    return False

def _has_name_form(page) -> bool:
    """True if either Zoom join-form variant is visible:
      SPA:   #input-for-name
      legacy: #inputname (system-Chrome/CDN variant)"""
    for sel in ("#input-for-name", "#inputname"):
        try:
            if page.locator(sel).count() and page.locator(sel).first.is_visible():
                return True
        except Exception:
            pass
    return False


def wait_for_name_form(page, timeout_s=120) -> bool:
    """Wait for either the join form or an interstitial to click through."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if _has_name_form(page):
            return True
        body = _visible_texts(page)
        # interstitial / PWA "Join from browser" modal
        if any(t in body for t in TXT_JOIN_BROWSER):
            if _click_text_any(page, TXT_JOIN_BROWSER):
                time.sleep(3)
                continue
        # passcode prompt (meeting needs passcode not in URL)
        pcode = page.locator("input[placeholder*='passcode' i], input#input-for-passcode")
        if pcode.count() and pcode.first.is_visible():
            return True  # caller handles it
        time.sleep(2)
    return False

def enter_passcode(page, m: dict):
    pwd = str(m.get("passcode") or m.get("pwd") or "").strip()
    if not pwd:
        return
    for sel in ["input#input-for-passcode", "input[placeholder*='passcode' i]"]:
        loc = page.locator(sel)
        try:
            if loc.count() and loc.first.is_visible():
                loc.first.fill(pwd)
                _click_text_any(page, TXT_JOIN)
                time.sleep(2)
                return
        except Exception:
            continue

def click_join_button(page) -> bool:
    """Click the Join button. SPA uses .preview-join-button; the legacy
    variant uses .submit. Use JS click: Playwright click can race the enabled
    transition on .preview-join-button (button shows as enabled but click no-ops)."""
    for sel in (".preview-join-button", "#unlogin-join-form .submit, .unlogin-join-form .submit, form#unlogin-join-form button.submit"):
        try:
            page.wait_for_selector(sel, state="visible", timeout=15000)
        except PWTimeout:
            continue
        try:
            page.evaluate("""(sel) => {
                const b = document.querySelector(sel);
                if (b) { b.click(); return true; }
                return false;
            }""", sel)
            return True
        except Exception:
            try:
                page.locator(sel).first.click(timeout=5000)
                return True
            except Exception:
                continue
    return False


def mute_and_stop_video_preview(page) -> tuple[bool, bool]:
    """On the pre-join preview form, click Mute and Stop Video so the attendee
    joins silent with camera off. Returns (muted, video_off) as clicked."""
    muted = False
    video_off = False
    # preview controls: aria-label Mute / Stop Video (class preview-video__control-button)
    try:
        loc = page.locator("button[aria-label='Mute'], button[aria-label='靜音'], button[aria-label='静音']")
        if loc.count() and loc.first.is_visible():
            loc.first.click(timeout=5000)
            muted = True
            time.sleep(0.5)
    except Exception:
        pass
    try:
        loc = page.locator("button[aria-label='Stop Video'], button[aria-label='停止視訊'], button[aria-label='停止视频']")
        if loc.count() and loc.first.is_visible():
            loc.first.click(timeout=5000)
            video_off = True
            time.sleep(0.5)
    except Exception:
        pass
    return muted, video_off


def _mic_state(page) -> str:
    """'muted' | 'unmuted' | 'unknown'. Zoom in-meeting:
      aria 'mute my microphone'   = currently UNMUTED (click to mute)
      aria 'unmute my microphone' = currently MUTED"""
    try:
        if page.locator("button[aria-label='unmute my microphone']").count() and \
           page.locator("button[aria-label='unmute my microphone']").first.is_visible():
            return "muted"
        if page.locator("button[aria-label='mute my microphone']").count() and \
           page.locator("button[aria-label='mute my microphone']").first.is_visible():
            return "unmuted"
    except Exception:
        pass
    return "unknown"


def _video_state(page) -> str:
    """'on' | 'off' | 'unknown'. Zoom in-meeting:
      aria 'stop my video' = currently ON (click to stop)
      aria 'start my video' = currently OFF"""
    try:
        if page.locator("button[aria-label='start my video']").count() and \
           page.locator("button[aria-label='start my video']").first.is_visible():
            return "off"
        if page.locator("button[aria-label='stop my video']").count() and \
           page.locator("button[aria-label='stop my video']").first.is_visible():
            return "on"
    except Exception:
        pass
    return "unknown"


def _reveal_toolbar(page):
    """Zoom's footer toolbar auto-hides; wiggle the mouse to reveal it."""
    try:
        page.mouse.move(640, 870)
        time.sleep(0.8)
    except Exception:
        pass


def ensure_muted_video_off(page, attempts: int = 3) -> tuple[bool, bool]:
    """Enforce mute + video-off after joining. Zoom resets the preview controls
    on join, so this is the step that actually guarantees a silent attendee
    with camera off. Returns (muted, video_off)."""
    muted = False
    video_off = False
    time.sleep(2)  # let the in-meeting toolbar settle after join
    # wait until the footer toolbar is reachable (mic or video button visible)
    for _ in range(6):
        ms0, vs0 = _mic_state(page), _video_state(page)
        if os.environ.get("ZOOM_DEBUG"):
            print(f"  [ensure] reach: mic={ms0} video={vs0}")
        if ms0 != "unknown" or vs0 != "unknown":
            break
        _reveal_toolbar(page)
        time.sleep(1)
    for _ in range(attempts):
        ms = _mic_state(page)
        if os.environ.get("ZOOM_DEBUG"):
            vs_now = _video_state(page)
            print(f"  [ensure] attempt: mic={ms} video={vs_now}")
            try:
                all_btns = page.evaluate("""() => Array.from(document.querySelectorAll('button')).filter(bn=>{const r=bn.getBoundingClientRect();return r.width>0}).map(bn=>({t:(bn.innerText||'').trim().slice(0,20),a:bn.getAttribute('aria-label')})).filter(x=>x.a && /microphone|audio|video|camera/i.test(x.a))""")
                print(f"  [ensure] audio/video btns: {json.dumps(all_btns)}")
            except Exception:
                pass
        if ms == "unmuted":
            try:
                page.locator("button[aria-label='mute my microphone']").first.click(timeout=8000)
                time.sleep(1)
            except Exception:
                pass
        elif ms == "muted":
            muted = True
        else:
            _reveal_toolbar(page)

        vs = _video_state(page)
        if vs == "on":
            try:
                page.locator("button[aria-label='stop my video']").first.click(timeout=8000)
                time.sleep(1)
            except Exception:
                pass
        elif vs == "off":
            video_off = True
        else:
            _reveal_toolbar(page)

        if muted and video_off:
            break
        time.sleep(1)

    # final verification pass
    if not muted:
        muted = _mic_state(page) == "muted"
    if not video_off:
        video_off = _video_state(page) == "off"
    return muted, video_off

def detect_in_meeting(page) -> str:
    """Return 'in_meeting' | 'waiting' | 'ended' | 'invalid' | 'auth' | 'other'."""
    body = _visible_texts(page)
    try:
        leave = page.locator("button[aria-label='Leave'], button[aria-label='離開'], button:has-text('Leave')")
        if leave.count() and leave.first.is_visible():
            return "in_meeting"
    except Exception:
        pass
    # the camera self-view dialog ("Do you see yourself?") only appears inside
    # a live meeting — treat it as in_meeting
    try:
        if page.locator("button:has-text('No, Try Another Camera')").count() or \
           page.locator("button:has-text('换一个摄像头')").count():
            return "in_meeting"
    except Exception:
        pass
    low = body.lower()
    if "meeting link is invalid" in low or "(3,001)" in low:
        return "invalid"
    if "meeting has ended" in low or "has ended" in low:
        return "ended"
    if "sign in to continue" in low or "sign in to join" in low or "you must sign in" in low:
        return "auth"
    if "wait for the host" in low or "waiting for the host" in low or "waiting room" in low \
       or "host will let you in" in low or "please wait" in low:
        return "waiting"
    return "other"

def handle_post_join_dialogs(page):
    """Camera self-view check + audio join prompt."""
    # camera check: "Do you see yourself?" -> Yes
    for t in ["Do you see yourself?", "你能看到自己嗎？", "你能看到自己吗？"]:
        try:
            if page.get_by_text(t).count():
                _click_text_any(page, TXT_YES)
                break
        except Exception:
            continue
    # audio join prompt
    body = _visible_texts(page)
    if any(k in body for k in ["Join with Computer Audio", "Join Audio by Computer", "通過電腦音頻加入", "通过电脑音频加入"]):
        _click_text_any(page, TXT_AUDIO_COMPUTER)
    elif any(k in body for k in ["Join by Phone", "通過電話加入", "通过电话加入"]):
        _click_text_any(page, TXT_AUDIO_BY_PHONE)

def leave_meeting(page) -> bool:
    try:
        # Zoom's toolbar auto-hides after inactivity — wiggle the mouse to reveal it
        try:
            page.mouse.move(640, 860)
            time.sleep(1)
        except Exception:
            pass
        loc = page.locator("button[aria-label='Leave'], button[aria-label='離開'], button:has-text('Leave')")
        if loc.count():
            loc.first.click(timeout=8000)
            time.sleep(2)
            # confirmation dialog
            _click_text_any(page, ["Leave Meeting", "離開會議", "离开会议"])
            return True
    except Exception:
        pass
    return False

def join_one(m: dict, args) -> int:
    """Join a single meeting, stay until end, leave. Returns exit code."""
    tz = m.get("_tz")
    start = m.get("_start")
    dur = int(m.get("duration_min") or DEFAULT_DURATION_MIN)
    stay_min = min(dur, MAX_STAY_MIN) + 3
    end = start + timedelta(minutes=stay_min)
    display = str(m.get("display_name") or m.get("name") or "Attendee").strip() or "Attendee"
    url = build_url(m)

    print(f"[{datetime.now(tz):%Y-%m-%d %H:%M:%S}] JOIN {m.get('name')} as {display}")
    print(f"  url: {url.split('pwd=')[0]}[pwd redacted]")
    print(f"  start: {start:%Y-%m-%d %H:%M}  end: {end:%Y-%m-%d %H:%M}  stay: {stay_min} min")

    if sync_playwright is None:
        print("ERROR: playwright not installed. Run: pip install -r requirements.txt && python -m playwright install chromium")
        return 1

    evidence = os.path.abspath(args.evidence_dir or EVIDENCE_DIR)
    os.makedirs(evidence, exist_ok=True)
    slug = re.sub(r"[^A-Za-z0-9]+", "_", str(m.get("name", "meeting"))).strip("_") or "meeting"

    with sync_playwright() as p:
        launch_kwargs = {"headless": args.headless, "args": LAUNCH_ARGS}
        if args.use_system_chrome:
            # GitHub-hosted runners ship Google Chrome pre-installed; using it
            # avoids the ~2min `playwright install chromium` download entirely.
            launch_kwargs["channel"] = "chrome"
            print("  browser: system Google Chrome (channel=chrome)")
        else:
            print("  browser: Playwright-bundled Chromium")
        browser = p.chromium.launch(**launch_kwargs)
        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        page = ctx.new_page()
        try:
            # 1. navigate
            try:
                page.goto(url, timeout=60000, wait_until="domcontentloaded")
            except Exception as e:
                print(f"WARN: goto error (continuing): {e}")
            time.sleep(4)

            # 2. reach the name form (click through interstitials)
            if not wait_for_name_form(page, timeout_s=120):
                st = detect_in_meeting(page)
                if st == "in_meeting":
                    print("already in meeting (no form shown)")
                else:
                    page.screenshot(path=os.path.join(evidence, f"{slug}_prejoin_fail.png"))
                    print(f"ERROR: could not reach join form; page state: {st}")
                    return 1

            # 3. passcode if prompted
            enter_passcode(page, m)
            if not _has_name_form(page):
                time.sleep(2)

            # 4. name + mute/video-off + join (SPA or legacy form variant)
            name_sel = "#input-for-name" if page.locator("#input-for-name").count() else "#inputname"
            name_input = page.locator(name_sel).first
            name_input.wait_for(state="visible", timeout=30000)
            name_input.fill(display)
            time.sleep(1)
            muted_pre, video_off_pre = mute_and_stop_video_preview(page)
            print(f"  preview controls: muted={muted_pre} video_off={video_off_pre}")
            if not click_join_button(page):
                print("ERROR: join button not found/clickable")
                page.screenshot(path=os.path.join(evidence, f"{slug}_joinbtn_fail.png"))
                return 1
            print("  join button clicked")

            # 5. wait for in-meeting / waiting room / error (self-healing:
            #    the legacy form can submit into the SPA form — refill & rejoin)
            state = "other"
            join_retried = False
            t0 = time.time()
            while time.time() - t0 < 150:
                time.sleep(4)
                state = detect_in_meeting(page)
                if os.environ.get("ZOOM_DEBUG"):
                    body = _visible_texts(page)[:80].replace("\n", " ")
                    print(f"  [wait] t={int(time.time()-t0)}s state={state} url={page.url[:55]} body={body!r}")
                if state in ("in_meeting", "waiting"):
                    break
                if state in ("invalid", "ended", "auth"):
                    break
                # still "other": the join click may have raced or the legacy
                # form handed off to the SPA form — refill & rejoin. Wait 70s
                # first: system Chrome can take ~45s to connect the meeting.
                if time.time() - t0 > 70 and not join_retried:
                    if _has_name_form(page):
                        name_sel = "#input-for-name" if page.locator("#input-for-name").count() else "#inputname"
                        page.locator(name_sel).first.fill(display)
                        time.sleep(0.5)
                    click_join_button(page)
                    join_retried = True
            print(f"  post-join state: {state}")

            if state in ("invalid",):
                page.screenshot(path=os.path.join(evidence, f"{slug}_invalid.png"))
                print("ERROR: meeting link is invalid — check meeting_id/passcode/URL")
                return 1
            if state == "auth":
                page.screenshot(path=os.path.join(evidence, f"{slug}_auth.png"))
                print("ERROR: meeting requires sign-in (SSO/authentication) — cannot join anonymously")
                return 1
            if state == "ended":
                print("WARN: meeting already ended — nothing to attend")
                return 0
            if state == "other":
                page.screenshot(path=os.path.join(evidence, f"{slug}_unknown.png"))
                print("ERROR: could not confirm joining the meeting (unexpected page state)")
                return 1

            handle_post_join_dialogs(page)

            # 6. enforce mute + video-off after joining (attendee is silent, camera off)
            muted, video_off = ensure_muted_video_off(page)
            print(f"  in-meeting state: muted={muted} video_off={video_off}")

            # 7. evidence of successful join
            joined_shot = os.path.join(evidence, f"{slug}_joined.png")
            page.screenshot(path=joined_shot)
            print(f"  JOINED — screenshot: {joined_shot}")

            # 8. stay until end, heartbeat + periodic evidence
            last_shot = time.time()
            heartbeat_no = 0
            while True:
                now = datetime.now(tz)
                if now >= end:
                    print(f"[{now:%H:%M:%S}] meeting end reached, leaving")
                    break
                st = detect_in_meeting(page)
                if st == "ended":
                    print(f"[{now:%H:%M:%S}] meeting ended early — leaving")
                    break
                if st == "in_meeting" and heartbeat_no % 12 == 0:
                    print(f"[{now:%H:%M:%S}] still in meeting (heartbeat {heartbeat_no})")
                if time.time() - last_shot > 900:  # every 15 min
                    page.screenshot(path=os.path.join(evidence, f"{slug}_heartbeat_{heartbeat_no}.png"))
                    last_shot = time.time()
                heartbeat_no += 1
                time.sleep(20)

            # 8. leave
            if leave_meeting(page):
                print("  left meeting (Leave clicked)")
            else:
                print("  leaving via browser close (Leave button not found)")
            time.sleep(2)
            page.screenshot(path=os.path.join(evidence, f"{slug}_left.png"))
            print("DONE")
            return 0

        except Exception as e:
            print("ERROR: unexpected failure during join:")
            traceback.print_exc()
            try:
                page.screenshot(path=os.path.join(evidence, f"{slug}_error.png"))
            except Exception:
                pass
            return 1
        finally:
            try:
                browser.close()
            except Exception:
                pass

# --- CLI ------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Auto-attend Zoom meetings (Playwright).")
    ap.add_argument("--config", help="path to meetings.yaml (default: alongside this script)")
    ap.add_argument("--meeting", help="force a meeting by name (manual dispatch)")
    ap.add_argument("--start-override", help="override start 'YYYY-MM-DD HH:MM' (manual dispatch test)")
    ap.add_argument("--duration", help="override duration in minutes")
    ap.add_argument("--display-name", help="override display name")
    ap.add_argument("--meeting-json", help="inline single-meeting JSON (skips config/due logic)")
    ap.add_argument("--meeting-file", help="read single-meeting JSON from file (CI pattern)")
    ap.add_argument("--dry-run", action="store_true", help="print plan without launching browser")
    ap.add_argument("--validate", action="store_true", help="validate config and print plan")
    ap.add_argument("--headless", action="store_true", help="headless browser (default: headed, for Xvfb)")
    ap.add_argument("--use-system-chrome", action="store_true",
                    help="launch pre-installed Google Chrome (channel=chrome) instead of Playwright's bundled Chromium — avoids the browser download; use on GitHub runners")
    ap.add_argument("--evidence-dir", help="where to write screenshots (default: ./evidence)")
    args = ap.parse_args()

    # single-meeting injection (GitHub Actions passes the due meeting this way)
    if args.meeting_json or args.meeting_file:
        raw = args.meeting_json or open(args.meeting_file).read()
        try:
            m = json.loads(raw)
        except Exception as e:
            print(f"ERROR: bad --meeting-json/--meeting-file: {e}")
            return 1
        # _tz may arrive as an IANA string (check_due.py writes tz.key) — coerce
        tzname = m.get("_tz") or DEFAULT_TZ
        m["_tz"] = ZoneInfo(tzname) if isinstance(tzname, str) else tzname
        m["_start"] = datetime.now(m["_tz"])  # already due by construction
        if args.duration:
            m["duration_min"] = int(args.duration)
        if args.display_name:
            m["display_name"] = args.display_name
        if args.dry_run:
            print(json.dumps(plan_meeting(m), indent=2, default=str))
            return 0
        return join_one(m, args)

    try:
        cfg = load_config(args)
    except SystemExit as e:
        print(e)
        return 1

    tz = _tz(cfg)
    now = datetime.now(tz)
    if args.start_override and args.meeting is None and not args.validate and not args.dry_run:
        # convenience: --start-override alone forces the first meeting
        args.meeting = (cfg.get("meetings") or [{}])[0].get("name")

    m, reason = select_meeting(cfg, args.meeting, args.start_override, args.duration, now)
    print(f"[{now:%Y-%m-%d %H:%M:%S %Z}] {reason}")

    if m is None:
        return 2 if not args.meeting else 0

    m["_tz"] = tz
    if args.duration:
        m["duration_min"] = int(args.duration)
    if args.display_name:
        m["display_name"] = args.display_name

    if args.validate or args.dry_run:
        print(json.dumps(plan_meeting(m), indent=2, default=str))
        return 0
    return join_one(m, args)

if __name__ == "__main__":
    sys.exit(main())
