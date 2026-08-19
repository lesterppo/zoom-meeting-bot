#!/usr/bin/env python3
"""
preflight.py — HTTPS-only meeting existence check (no browser needed).

Fetches Zoom's join endpoint and classifies the meeting:
  ok       meeting exists and is joinable (Zoom issued a wpk web-participant key)
  invalid  meeting link is invalid / meeting does not exist (Error - Zoom, 3,001)
  ended    meeting has already ended
  auth     meeting requires sign-in (SSO/authentication)
  error    network/HTTP failure (treated as "unknown" — let the bot try anyway)

Used by the GitHub Actions workflow BEFORE installing Playwright/Chromium so
a bogus/cancelled meeting costs ~1s instead of ~2min of install time.

Usage:
  python3 preflight.py --meeting-file /tmp/due_meeting.json
  python3 preflight.py --meeting-json '{"zoom_url": "..."}'
  python3 preflight.py --meeting-id 1234567890 --passcode abc
Prints: PREFLIGHT=<state>  (exit 0 for ok, 2 for invalid/ended, 1 for error)
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
TIMEOUT = 20

# normalized join URL builder — mirrors join_zoom.build_url
def build_join_url(m: dict) -> str:
    url = (m.get("zoom_url") or "").strip()
    mid = str(m.get("meeting_id", "")).strip()
    pwd = str(m.get("passcode") or m.get("pwd") or "").strip()
    if url:
        if "wc/join" in url:
            return url
        m2 = re.search(r"/(?:j|wc/join)/(\d+)(?:\?pwd=([A-Za-z0-9._\-]+))?", url)
        if m2:
            mid = m2.group(1) or mid
            pwd = m2.group(2) or pwd
        else:
            return url
    if not mid:
        raise ValueError("meeting needs zoom_url or meeting_id")
    base = f"https://app.zoom.us/wc/join/{mid}"
    return f"{base}?pwd={pwd}" if pwd else base


def preflight(m: dict) -> str:
    """Best-effort existence check. Zoom serves request-variant responses
    (A/B/CDN), so we retry ambiguous results up to 3x. The workflow is
    fail-open: only a CONFIRMED invalid skips the install; ambiguous/ok
    both proceed, so a real meeting is never blocked — join_zoom.py is the
    source of truth for joinability."""
    states = []
    for attempt in range(3):
        try:
            s = _preflight_once(m)
        except Exception as e:
            print(f"  preflight attempt {attempt + 1} error: {e}")
            s = "error"
        states.append(s)
        if s in ("ok", "invalid"):
            return s  # confident signal — don't wait
        time.sleep(1)
    # all ambiguous → pick the most common non-error state, else error
    from collections import Counter
    c = Counter(states)
    for s, n in c.most_common():
        if s != "error":
            return s
    return "error"


def _preflight_once(m: dict) -> str:
    url = build_join_url(m)
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html,*/*"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            final_url = resp.geturl()
            html = resp.read(200_000).decode("utf-8", errors="ignore")
            status = resp.status
    except urllib.error.HTTPError as e:
        print(f"  preflight HTTP {e.code} for {url.split('?')[0]} — treating as unknown")
        return "error"
    except Exception as e:
        print(f"  preflight network error: {e}")
        return "error"

    title_m = re.search(r"<title>([^<]*)</title>", html, re.I)
    title = title_m.group(1).strip() if title_m else ""
    low = html.lower()

    # 1. Invalid-meeting signal. The string "meeting link is invalid" appears
    #    in the shared JS bundle of EVERY page, so it alone is useless.
    #    Reliable invalid markers (server-rendered, absent from valid pages):
    #      - <title>Error - Zoom</title>
    #      - "(3,001)" error code
    #    Note: Zoom issues a wpk token even for invalid meetings when a pwd
    #    param is present, so error markers must be checked FIRST.
    if "error - zoom" in title.lower() or "(3,001)" in low:
        return "invalid"

    # 2. Zoom issues a wpk (web participant key) in the redirect for meetings
    #    that exist. This is the reliable existence signal: the SPA shell
    #    embeds every i18n string in its JS bundle, so text sniffing cannot
    #    distinguish states — only wpk issuance can.
    if "wpk=" in final_url or "wpk=" in url:
        print(f"  preflight OK ({status}) wpk issued for {url.split('?')[0]}")
        return "ok"

    # 3. no wpk but no error either — ambiguous (SPA shell, geo, bot-block, …)
    print(f"  preflight ambiguous: title={title!r} size={len(html)} — treating as unknown")
    return "error"


def main() -> int:
    ap = argparse.ArgumentParser(description="HTTPS pre-flight check that a Zoom meeting exists.")
    ap.add_argument("--meeting-file", help="JSON meeting dict from file (CI pattern)")
    ap.add_argument("--meeting-json", help="inline JSON meeting dict")
    ap.add_argument("--meeting-id", help="numeric meeting id")
    ap.add_argument("--passcode", default="", help="meeting passcode")
    args = ap.parse_args()

    m = None
    if args.meeting_json or args.meeting_file:
        raw = args.meeting_json or open(args.meeting_file).read()
        m = json.loads(raw)
    elif args.meeting_id:
        m = {"meeting_id": args.meeting_id, "passcode": args.passcode}
    else:
        print("ERROR: provide --meeting-file, --meeting-json, or --meeting-id")
        return 1

    state = preflight(m)
    print(f"PREFLIGHT={state}")
    return 0 if state == "ok" else (2 if state in ("invalid", "ended") else 1)


if __name__ == "__main__":
    sys.exit(main())
