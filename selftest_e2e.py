#!/usr/bin/env python3
"""E2E selftest for join_zoom.py against Zoom's live test meeting.

Fetches a fresh test-meeting URL from zoom.us/test, then runs join_zoom.py
against it with a short stay. Exits 0 on a clean join→stay→leave cycle.
Requires a display (run under xvfb-run).
"""
import re
import subprocess
import sys
import time
from playwright.sync_api import sync_playwright

def fresh_url():
    with sync_playwright() as p:
        b = p.chromium.launch(headless=False, args=["--no-sandbox"])
        pg = b.new_page()
        m = None
        for attempt in range(4):
            pg.goto("https://zoom.us/test", timeout=60000, wait_until="domcontentloaded")
            time.sleep(5)
            try:
                pg.locator("a.submit.join.user").first.click()
            except Exception:
                time.sleep(3)
            try:
                pg.wait_for_selector("text=Join from browser", timeout=15000)
                pg.get_by_text("Join from browser", exact=True).first.click()
            except Exception:
                pass
            time.sleep(6)
            m = re.search(r"zoom\.us/j/(\d+)\?pwd=([A-Za-z0-9._-]+)", pg.url)
            if m:
                break
            print(f"  fresh_url attempt {attempt + 1} failed, retrying")
        b.close()
        if not m:
            raise RuntimeError("could not obtain fresh test meeting URL")
        return f"https://zoom.us/j/{m.group(1)}?pwd={m.group(2)}"

def main():
    url = fresh_url()
    print("FRESH URL:", url.split("pwd=")[0] + "[pwd redacted]")
    meeting = {"name": "E2E Selftest", "zoom_url": url,
               "duration_min": 1, "display_name": "Hermes E2E"}
    import json
    cmd = [sys.executable, "join_zoom.py", "--meeting-json", json.dumps(meeting),
           "--evidence-dir", "evidence"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    print(r.stdout)
    if r.stderr:
        print("STDERR:", r.stderr[-2000:])
    return r.returncode

if __name__ == "__main__":
    sys.exit(main())
