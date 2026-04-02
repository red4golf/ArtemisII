from datetime import datetime
from pathlib import Path

import pytest

import artemis2_monitor as m


SAMPLE_HTML = """
<html><body>
<section>
  <h2>Mission Coverage</h2>
  <p>April 1</p>
  <p>8:30 p.m. – Launch coverage begins</p>
  <p>April 2</p>
  <p>11:15 a.m. – Mission status briefing</p>
</section>
</body></html>
"""


def test_extract_mission_coverage_lines():
    lines = m.extract_mission_coverage(SAMPLE_HTML)
    assert "Mission Coverage" in lines[0]
    assert any("Launch coverage" in x for x in lines)


def test_hash_and_diff_changes():
    old = ["Mission Coverage", "April 1", "8:30 p.m. – Old event"]
    new = ["Mission Coverage", "April 1", "8:45 p.m. – New event"]
    assert m.compute_hash(old) != m.compute_hash(new)
    diff = m.unified_diff(old, new)
    assert "-8:30 p.m. – Old event" in diff
    assert "+8:45 p.m. – New event" in diff


def test_time_conversion_et_to_utc_and_pt():
    lines = ["Mission Coverage", "April 1", "8:30 p.m. – Launch coverage begins"]
    now = datetime(2026, 3, 30, 10, 0, tzinfo=m.ET)
    events = m.parse_events(lines, now=now)
    assert len(events) == 1
    event_line = events[0].as_line()
    # 8:30 PM ET should be 17:30 PT and 00:30 UTC next day (during daylight savings)
    assert "2026-04-02 00:30 UTC" in event_line
    assert "2026-04-01 17:30 PT" in event_line


def test_next_three_events():
    lines = [
        "Mission Coverage",
        "April 1",
        "8:30 p.m. – Event A",
        "April 2",
        "1:00 p.m. – Event B",
        "2:00 p.m. – Event C",
        "3:00 p.m. – Event D",
    ]
    now = datetime(2026, 4, 1, 0, 0, tzinfo=m.ET)
    events = m.parse_events(lines, now=now)
    upcoming = m.next_events(events, count=3, now=now)
    assert [e.description for e in upcoming] == ["Event A", "Event B", "Event C"]


def test_run_once_no_change(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    state_path = tmp_path / "state.json"
    config = {
        "POLL_URL": "https://example.com",
        "USER_AGENT": "test-agent",
        "TELEGRAM_BOT_TOKEN": "token",
        "TELEGRAM_CHAT_ID": "123",
        "SLACK_WEBHOOK_URL": "https://hooks.slack.com/services/T/B/X",
    }

    monkeypatch.setattr(m, "fetch_page", lambda *_args, **_kwargs: SAMPLE_HTML)
    lines = m.normalize_lines(m.extract_mission_coverage(SAMPLE_HTML))
    m.save_json(state_path, {"hash": m.compute_hash(lines), "lines": lines})

    sent = {"telegram": 0, "slack": 0}
    monkeypatch.setattr(m, "send_telegram", lambda *_args, **_kwargs: sent.__setitem__("telegram", sent["telegram"] + 1))
    monkeypatch.setattr(m, "send_slack", lambda *_args, **_kwargs: sent.__setitem__("slack", sent["slack"] + 1))

    rc = m.run_once(config, state_path)
    assert rc == 0
    assert sent["telegram"] == 0
    assert sent["slack"] == 0


def test_run_once_change_sends_single_alert(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    state_path = tmp_path / "state.json"
    config = {
        "POLL_URL": "https://example.com",
        "USER_AGENT": "test-agent",
        "TELEGRAM_BOT_TOKEN": "token",
        "TELEGRAM_CHAT_ID": "123",
        "SLACK_WEBHOOK_URL": "https://hooks.slack.com/services/T/B/X",
    }

    old_lines = ["Mission Coverage", "April 1", "8:30 p.m. – Event A"]
    m.save_json(state_path, {"hash": m.compute_hash(old_lines), "lines": old_lines})

    html_new = """
    <section>
      <h2>Mission Coverage</h2>
      <p>April 1</p>
      <p>8:45 p.m. – Event A</p>
    </section>
    """
    monkeypatch.setattr(m, "fetch_page", lambda *_args, **_kwargs: html_new)

    sent = {"telegram": 0, "slack": 0}

    def fake_telegram(token, chat_id, message):
        assert token == "token"
        assert chat_id == "123"
        assert "Diff" in message
        sent["telegram"] += 1

    def fake_slack(webhook, message):
        assert webhook.startswith("https://hooks.slack.com/")
        assert "Artemis II Schedule Update" in message
        sent["slack"] += 1

    monkeypatch.setattr(m, "send_telegram", fake_telegram)
    monkeypatch.setattr(m, "send_slack", fake_slack)

    rc = m.run_once(config, state_path)
    assert rc == 0
    assert sent["telegram"] == 1
    assert sent["slack"] == 1
