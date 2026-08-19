#!/usr/bin/env python3
"""
check_due.py — stdlib-only due-meeting checker for GitHub Actions.

Reads ZOOM_MEETINGS_JSON (base64 or plain JSON; set as a repo secret so meeting
IDs/passcodes never appear in the repo). Prints "DUE=1" and writes the due
meeting's JSON to /tmp/due_meeting.json when a meeting is inside its join
window (or when a manual dispatch names one); otherwise prints "DUE=0".

No third-party deps — the no-op cron tick costs ~5s of runner time.
"""
import base64
import json
import os
import re
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

DEFAULT_TZ = "Asia/Hong_Kong"
OUT = "/tmp/due_meeting.json"


def load():
    raw = os.environ.get("ZOOM_MEETINGS_JSON", "").strip()
    if not raw:
        return None
    if raw[0] not in "{[":
        raw = base64.b64decode(raw).decode("utf-8")
    return json.loads(raw)


def next_start(m, tz, now):
    s = str(m.get("start", "")).strip()
    if not s:
        return now
    if re.match(r"\d{4}-\d{2}-\d{2}", s):
        return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=tz)
    hh, mm = s.split(":")
    c = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
    if c < now - timedelta(minutes=30):
        c += timedelta(days=1)
    return c


def main():
    cfg = load()
    if cfg is None:
        print("DUE=0")
        return 0
    tz = ZoneInfo(cfg.get("timezone") or DEFAULT_TZ)
    now = datetime.now(tz)
    defs = cfg.get("defaults") or {}
    meetings = cfg.get("meetings") or []

    meeting_arg = os.environ.get("MEETING", "").strip()
    start_override = os.environ.get("START_OVERRIDE", "").strip()

    chosen = None
    if meeting_arg:
        chosen = next((m for m in meetings if m.get("name") == meeting_arg), None)
        if chosen is None:
            print(f"ERROR: meeting {meeting_arg!r} not found in config")
            return 1
    else:
        for m in meetings:
            start = next_start(m, tz, now)
            lead = int(m.get("join_lead_min", defs.get("join_lead_min", 4)))
            grace = int(m.get("join_grace_min", defs.get("join_grace_min", 2)))
            now_min = now.replace(second=0, microsecond=0)
            if start - timedelta(minutes=lead) <= now_min <= start + timedelta(minutes=grace):
                chosen = m
                break

    if chosen is None:
        print("DUE=0")
        return 0

    if start_override:
        ov = datetime.strptime(start_override, "%Y-%m-%d %H:%M").replace(tzinfo=tz)
        chosen = {**chosen, "start": ov.strftime("%Y-%m-%d %H:%M")}
    chosen = {**chosen, "_tz": tz.key, "_start": str(chosen.get("_start", "")) or ""}
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(chosen, f)
    print("DUE=1")
    print(f"meeting={chosen.get('name')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
