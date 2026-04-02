#!/usr/bin/env python3
"""Artemis II schedule monitor.

Polls NASA's Artemis II mission coverage page, detects schedule changes,
computes diffs, and sends alerts to Telegram/Slack (plus optional email/SMS).
"""

from __future__ import annotations

import difflib
import hashlib
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

DEFAULT_POLL_URL = (
    "https://www.nasa.gov/missions/artemis/artemis-2/"
    "nasa-sets-coverage-for-artemis-ii-moon-mission/"
)
DEFAULT_LIVE_URL = "https://www.nasa.gov/live/"
DEFAULT_CONFIG_PATH = "config.json"
DEFAULT_STATE_PATH = "state.json"
DEFAULT_USER_AGENT = "Artemis2Monitor"
ET = ZoneInfo("America/New_York")
PT = ZoneInfo("America/Los_Angeles")
UTC = ZoneInfo("UTC")


@dataclass
class Event:
    """Single parsed schedule item."""

    dt_et: datetime
    description: str

    def as_line(self) -> str:
        dt_utc = self.dt_et.astimezone(UTC)
        dt_pt = self.dt_et.astimezone(PT)
        return (
            f"{dt_utc:%Y-%m-%d %H:%M} UTC ({dt_pt:%Y-%m-%d %H:%M} PT)"
            f" – {self.description}"
        )


def setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def str_to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def load_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON in {path}: {exc}") from exc


def save_json(path: Path, payload: Dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def ensure_config(config_path: Path) -> Dict[str, Any]:
    """Load config and prompt for missing required values on first run."""
    config = load_json(config_path)

    required = ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "SLACK_WEBHOOK_URL"]
    prompted = False
    for key in required:
        if not config.get(key):
            config[key] = input(f"Enter {key}: ").strip()
            prompted = True

    config.setdefault("POLL_URL", DEFAULT_POLL_URL)
    config.setdefault("LIVE_URL", DEFAULT_LIVE_URL)
    config.setdefault("STATE_PATH", DEFAULT_STATE_PATH)
    config.setdefault("USER_AGENT", DEFAULT_USER_AGENT)
    config.setdefault("DRY_RUN", False)

    if prompted or not config_path.exists():
        save_json(config_path, config)
        try:
            os.chmod(config_path, 0o600)
        except OSError:
            pass
        logging.info("Saved configuration to %s", config_path)

    return config


def request_with_retry(
    method: str,
    url: str,
    *,
    headers: Optional[Dict[str, str]] = None,
    json_payload: Optional[Dict[str, Any]] = None,
    data_payload: Optional[Dict[str, Any]] = None,
    timeout: int = 30,
    retries: int = 3,
) -> requests.Response:
    """HTTP helper with exponential backoff for transient failures."""
    last_error: Optional[Exception] = None
    for attempt in range(retries):
        try:
            response = requests.request(
                method,
                url,
                headers=headers,
                json=json_payload,
                data=data_payload,
                timeout=timeout,
            )
            if response.status_code >= 500:
                response.raise_for_status()
            return response
        except requests.RequestException as exc:
            last_error = exc
            if attempt < retries - 1:
                delay = 2**attempt
                logging.warning("Request failed (%s). Retrying in %ss", exc, delay)
                time.sleep(delay)
            else:
                break
    raise RuntimeError(f"Request failed after {retries} attempts: {url}") from last_error


def fetch_page(url: str, user_agent: str) -> str:
    headers = {"User-Agent": user_agent}
    response = request_with_retry("GET", url, headers=headers)
    response.raise_for_status()
    return response.text


def _extract_text_lines(container: Any) -> List[str]:
    text = container.get_text("\n", strip=True)
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    return [line for line in lines if line]


def extract_mission_coverage(html: str) -> List[str]:
    """Extract Mission Coverage block lines with a fallback marker strategy."""
    soup = BeautifulSoup(html, "html.parser")
    heading = soup.find(
        lambda tag: tag.name in {"h1", "h2", "h3", "h4", "strong", "p"}
        and "mission coverage" in tag.get_text(" ", strip=True).lower()
    )

    if heading:
        for parent in [heading.find_parent("section"), heading.find_parent("article"), heading.parent]:
            if parent:
                lines = _extract_text_lines(parent)
                if len(lines) >= 3:
                    return lines

    all_lines = _extract_text_lines(soup)
    start_idx = next((i for i, line in enumerate(all_lines) if "mission coverage" in line.lower()), -1)
    if start_idx == -1:
        raise ValueError("Could not find 'Mission Coverage' block")

    end_markers = ("follow nasa", "###", "media contact", "nasa live")
    extracted: List[str] = []
    for line in all_lines[start_idx:]:
        if extracted and any(marker in line.lower() for marker in end_markers):
            break
        extracted.append(line)
    if len(extracted) < 2:
        raise ValueError("Mission Coverage block was empty")
    return extracted


def normalize_lines(lines: Iterable[str]) -> List[str]:
    return [re.sub(r"\s+", " ", line).strip() for line in lines if line.strip()]


def compute_hash(lines: Iterable[str]) -> str:
    joined = "\n".join(lines)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def unified_diff(old_lines: List[str], new_lines: List[str], max_lines: int = 50) -> str:
    diff = list(
        difflib.unified_diff(old_lines, new_lines, fromfile="previous", tofile="current", lineterm="")
    )
    return "\n".join(diff[:max_lines]) or "(no textual diff)"


def _is_date_header(line: str) -> Optional[Tuple[str, int]]:
    m = re.match(
        r"^(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2})(?:,\s*(\d{4}))?$",
        line,
        re.IGNORECASE,
    )
    if not m:
        return None
    month_name = m.group(1)
    day = int(m.group(2))
    month = datetime.strptime(month_name[:3], "%b").month
    return month, day


def _parse_time_and_desc(line: str) -> Optional[Tuple[str, str]]:
    patterns = [
        r"^(\d{1,2}:\d{2}\s*[ap]\.?m\.?)(?:\s*EDT|\s*ET)?\s*[–—-]\s*(.+)$",
        r"^(\d{1,2}\s*[ap]\.?m\.?)(?:\s*EDT|\s*ET)?\s*[–—-]\s*(.+)$",
    ]
    for pat in patterns:
        m = re.match(pat, line, re.IGNORECASE)
        if m:
            return m.group(1), m.group(2).strip()
    return None


def _normalize_ampm(value: str) -> str:
    return (
        value.lower()
        .replace(".", "")
        .replace(" ", "")
        .replace("am", " AM")
        .replace("pm", " PM")
        .strip()
    )


def parse_events(lines: List[str], now: Optional[datetime] = None) -> List[Event]:
    """Parse date and time lines into ET datetimes."""
    now = now or datetime.now(ET)
    current_year = now.year
    current_month_day: Optional[Tuple[int, int]] = None
    events: List[Event] = []

    for line in lines:
        header = _is_date_header(line)
        if header:
            current_month_day = header
            continue
        parsed = _parse_time_and_desc(line)
        if not parsed or not current_month_day:
            continue

        t_value, description = parsed
        normalized_t = _normalize_ampm(t_value)
        date_str = f"{current_year:04d}-{current_month_day[0]:02d}-{current_month_day[1]:02d}"
        dt_et = datetime.strptime(f"{date_str} {normalized_t}", "%Y-%m-%d %I:%M %p").replace(tzinfo=ET)

        # Handle year rollover for schedules that cross New Year's.
        if dt_et < now and (now - dt_et).days > 180:
            dt_et = dt_et.replace(year=current_year + 1)

        events.append(Event(dt_et=dt_et, description=description))

    return sorted(events, key=lambda e: e.dt_et)


def next_events(events: List[Event], count: int = 3, now: Optional[datetime] = None) -> List[Event]:
    now = now or datetime.now(ET)
    return [e for e in events if e.dt_et >= now][:count]


def format_alert(diff_text: str, upcoming: List[Event]) -> str:
    lines = ["🚀 *Artemis II Schedule Update detected*", "", "*Diff:*", f"```\n{diff_text}\n```", "", "*Next events:*"]
    if upcoming:
        lines.extend([f"• {event.as_line()}" for event in upcoming])
    else:
        lines.append("• No upcoming events parsed from current schedule.")
    return "\n".join(lines)


def send_telegram(token: str, chat_id: str, message: str) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    resp = request_with_retry("POST", url, json_payload=payload)
    resp.raise_for_status()


def send_slack(webhook_url: str, message: str) -> None:
    resp = request_with_retry("POST", webhook_url, json_payload={"text": message})
    resp.raise_for_status()


def send_mailgun(api_url: str, api_key: str, sender: str, recipient: str, subject: str, body: str) -> None:
    headers = {"Authorization": f"Basic api:{api_key}"}
    data = {"from": sender, "to": recipient, "subject": subject, "text": body}
    resp = request_with_retry("POST", api_url, headers=headers, data_payload=data)
    resp.raise_for_status()


def send_sendgrid(api_key: str, sender: str, recipient: str, subject: str, body: str) -> None:
    headers = {"Authorization": f"Bearer {api_key}"}
    payload = {
        "personalizations": [{"to": [{"email": recipient}], "subject": subject}],
        "from": {"email": sender},
        "content": [{"type": "text/plain", "value": body}],
    }
    resp = request_with_retry(
        "POST", "https://api.sendgrid.com/v3/mail/send", headers=headers, json_payload=payload
    )
    resp.raise_for_status()


def send_twilio(api_url: str, account_sid: str, auth_token: str, to: str, from_: str, body: str) -> None:
    headers = {"Authorization": f"Basic {account_sid}:{auth_token}"}
    data = {"To": to, "From": from_, "Body": body}
    resp = request_with_retry("POST", api_url, headers=headers, data_payload=data)
    resp.raise_for_status()


def run_once(config: Dict[str, Any], state_path: Path) -> int:
    poll_url = config.get("POLL_URL", DEFAULT_POLL_URL)
    user_agent = config.get("USER_AGENT", DEFAULT_USER_AGENT)
    dry_run = str_to_bool(os.getenv("DRY_RUN", config.get("DRY_RUN", False)))

    try:
        html = fetch_page(poll_url, user_agent)
        lines = normalize_lines(extract_mission_coverage(html))
    except Exception as exc:
        logging.error("Failed to fetch/parse schedule: %s", exc)
        return 2

    new_hash = compute_hash(lines)
    state = load_json(state_path)
    old_hash = state.get("hash")
    old_lines = state.get("lines", [])

    if old_hash == new_hash:
        logging.info("No change in schedule.")
        return 0

    diff_text = unified_diff(old_lines, lines)
    events = parse_events(lines)
    upcoming = next_events(events, count=3)
    message = format_alert(diff_text, upcoming)

    if dry_run:
        logging.info("DRY_RUN is enabled; would send alert:\n%s", message)
    else:
        try:
            send_telegram(config["TELEGRAM_BOT_TOKEN"], config["TELEGRAM_CHAT_ID"], message)
            send_slack(config["SLACK_WEBHOOK_URL"], message)
            logging.info("Sent alerts to Telegram and Slack.")
        except Exception as exc:
            logging.error("Failed to send alerts: %s", exc)
            return 3

    save_json(state_path, {"hash": new_hash, "lines": lines, "updated_at": datetime.now(UTC).isoformat()})
    logging.info("State saved to %s", state_path)
    return 0


def main() -> int:
    setup_logging()
    config_path = Path(os.getenv("CONFIG_PATH", DEFAULT_CONFIG_PATH))

    try:
        config = ensure_config(config_path)
    except Exception as exc:
        logging.error("Configuration error: %s", exc)
        return 1

    state_path = Path(config.get("STATE_PATH", DEFAULT_STATE_PATH))
    return run_once(config, state_path)


if __name__ == "__main__":
    sys.exit(main())
