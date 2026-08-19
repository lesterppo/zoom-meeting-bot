# AGENTS.md — guidance for AI coding agents

## What this repo is

An AI-agent-friendly Zoom auto-attendance bot. `join_zoom.py` drives Zoom's
web client with Playwright + Chromium to join scheduled meetings; a GitHub
Actions cron (`*/5`) gates the join via `check_due.py` so no-op ticks cost
~5s.

## Key invariants

- **No meeting data in the repo.** Meeting IDs/passcodes live in the
  `ZOOM_MEETINGS_JSON` repo secret (base64 or plain JSON) or the local,
  gitignored `meetings.yaml`. Never commit either.
- **`join_zoom.py` must stay a single file** — it is the whole bot. Keep
  new selectors/flows inside it with fallbacks for both English and
  zh-TW/zh-CN UI labels.
- **Zoom web-client UI is a moving target.** Before changing selectors,
  re-verify live against `https://zoom.us/test` (Join → "Join from browser"
  → `#input-for-name` → `.preview-join-button`). The Join button has a race
  where Playwright's click no-ops on the enabled transition — the JS-click
  fallback in `click_join_button()` exists for that reason; keep it.
- **Headed browser required for Zoom.** The workflow runs
  `xvfb-run -a python3 join_zoom.py`. Headless works for the form but the
  in-meeting client can behave differently.
- Exit codes: `0` attended/nothing due, `1` config/join error, `2` not due.

## Common tasks

- Add a meeting: edit `meetings.example.yaml` + the local `meetings.yaml`
  (gitignored) and the `ZOOM_MEETINGS_JSON` secret.
- Validate: `python3 join_zoom.py --validate` / `--dry-run`.
- Local E2E: `xvfb-run -a python3 join_zoom.py --meeting-json '{"name":"t","zoom_url":"https://zoom.us/j/<id>?pwd=<pwd>"}'`
- Manual remote run: Actions tab → Run workflow → meeting name / overrides.
- Check a run: Actions → zoom-evidence-<run> artifact holds screenshots.
