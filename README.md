# Artemis II 10-Day Mission Schedule Monitor

A production-ready Python monitor that polls NASA's official Artemis II mission coverage page, detects schedule changes, and alerts Telegram + Slack with UTC/PT timestamps.

## Features

- Interactive first-run setup (`config.json`) for Telegram/Slack credentials.
- Headless operation after setup.
- Polls NASA mission coverage source with retry + backoff.
- Extracts and normalizes the **Mission Coverage** block.
- SHA-256 hash-based deduplication and unified diff generation.
- Time parsing in ET with conversion to UTC and PT (`America/Los_Angeles`).
- “Next 3 events” extraction for alert context.
- Optional channels scaffolding: Mailgun, SendGrid, Twilio.
- GitHub Actions workflow every 10 minutes.
- Unit + integration-style tests via `pytest`.

## Install

```bash
python -m pip install -r requirements.txt
```

## First run (interactive)

```bash
python artemis2_monitor.py
```

If `config.json` is missing values, the script prompts for:
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `SLACK_WEBHOOK_URL`

Then it writes `config.json` and continues.

## Headless mode

When `config.json` already exists, the script does not prompt.

Use dry-run:

```bash
DRY_RUN=true python artemis2_monitor.py
```

## Config schema

```json
{
  "TELEGRAM_BOT_TOKEN": "123:ABC...",
  "TELEGRAM_CHAT_ID": "987654321",
  "SLACK_WEBHOOK_URL": "https://hooks.slack.com/...",
  "STATE_PATH": "state.json",
  "POLL_URL": "https://www.nasa.gov/missions/artemis/artemis-2/nasa-sets-coverage-for-artemis-ii-moon-mission/",
  "USER_AGENT": "Artemis2Monitor",
  "DRY_RUN": false
}
```

## Testing

```bash
pytest -q
```

## GitHub Actions

Workflow: `.github/workflows/monitor.yml`

- Runs every 10 minutes (`*/10 * * * *`).
- Uses repository secrets:
  - `TELEGRAM_BOT_TOKEN`
  - `TELEGRAM_CHAT_ID`
  - `SLACK_WEBHOOK_URL`
- Uses `actions/cache` to keep `state.json`.

## Alert format (example)

```text
🚀 Artemis II Schedule Update detected
Diff:
-8:30 p.m. – Launch coverage begins
+8:45 p.m. – Launch coverage begins
Next events:
• 2026-04-02 03:15 UTC (2026-04-01 20:15 PT) – Mission status briefing
```

## Mermaid flowchart

```mermaid
flowchart LR
  A[Fetch NASA mission-events page] --> B[Extract "Mission Coverage" block]
  B --> C[Normalize text, compute SHA-256]
  C -->|No change| D[Exit/Wait]
  C -->|Change detected| E[Unified diff & parse next events]
  E --> F[Compose alert message (UTC & PT)]
  F --> G{DRY_RUN?}
  G -->|Yes| H[Log message only]
  G -->|No| I[POST to Telegram / Slack APIs]
  I --> J[Update state.json and exit]
```

## Notes

- Official monitored source: NASA Artemis II mission page + optional NASA Live fallback.
- ET is parsed as `America/New_York`; converted to UTC and PT.
- Exit codes:
  - `0`: success or no change
  - non-zero: parse/network/config/send errors
