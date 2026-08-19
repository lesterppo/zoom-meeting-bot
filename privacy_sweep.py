#!/usr/bin/env python3
"""privacy_sweep.py — scan the repo for personal data before pushing.

Checks for: hardcoded home paths, personal emails, meeting IDs/passcodes in
plaintext, API keys, and common personal identifiers. Run before every push.

Usage: python3 privacy_sweep.py [--repo-dir .]
Exit 0 = clean, 1 = findings.
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))

PATTERNS = [
    (r"/home/[a-z0-9_]+/", "hardcoded home path"),
    (r"ppoppo2051|cyc236|cycptr|lesteryannes", "personal account id"),
    (r"@gmail\.com|@connect\.hku\.hk|@ha\.org\.hk", "personal email"),
    (r"(?i)api[_-]?key\s*[=:]\s*['\"][A-Za-z0-9_\-]{16,}['\"]", "possible API key"),
    (r"(?i)(?<![a-z])sk-[A-Za-z0-9]{20,}", "secret token (sk-...)"),
    (r"pwd=[A-Za-z0-9._\-]{8,}", "meeting passcode in URL"),
    (r"meeting_id\s*:\s*['\"]?\d{9,}", "meeting id in plaintext config"),
]

SKIP_DIRS = {".git", "__pycache__", "evidence", "node_modules", ".github"}
SKIP_FILES = {"privacy_sweep.py", "LICENSE"}


def scan(path):
    findings = []
    for dirpath, dirnames, filenames in os.walk(path):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if fn in SKIP_FILES:
                continue
            fp = os.path.join(dirpath, fn)
            # example configs / docs intentionally contain placeholder data
            if fn.endswith(".example.yaml"):
                continue
            try:
                with open(fp, "r", encoding="utf-8", errors="ignore") as f:
                    for i, line in enumerate(f, 1):
                        # README example blocks use REPLACE_ME / fake ids — allowed
                        if "REPLACE_ME" in line or "1234567890" in line or "1234567891" in line:
                            continue
                        for pat, label in PATTERNS:
                            if re.search(pat, line):
                                findings.append((fp, i, label, line.strip()[:100]))
            except Exception:
                continue
    return findings


def main():
    root = sys.argv[1] if len(sys.argv) > 1 else ROOT
    findings = scan(root)
    if not findings:
        print("CLEAN: no personal data found.")
        return 0
    print(f"FOUND {len(findings)} issue(s):")
    for fp, ln, label, snippet in findings:
        print(f"  {os.path.relpath(fp, root)}:{ln} [{label}] {snippet}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
