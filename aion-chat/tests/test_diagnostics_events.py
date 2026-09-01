import json

import provider_status
import sentinel_diagnostics


def test_provider_event_compacts_and_filters_sensitive_meta(monkeypatch, tmp_path):
    path = tmp_path / "provider_events.jsonl"
    path.write_text(
        "\n".join([
            json.dumps({"old": 1}),
            json.dumps({"old": 2}),
            json.dumps({"old": 3}),
        ]) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(provider_status, "EVENTS_PATH", path)
    monkeypatch.setattr(provider_status, "MAX_EVENTS_FILE_BYTES", 1)
    monkeypatch.setattr(provider_status, "MAX_EVENTS_FILE_LINES", 2)

    event = provider_status.record_provider_event({
        "scope": "test",
        "ok": True,
        "meta": {
            "latency_bucket": "fast",
            "prompt": "should-not-be-written",
            "api_key": "secret",
            "response_body": "also-secret",
        },
    })

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    assert json.loads(lines[0]) == {"old": 2}
    assert json.loads(lines[1]) == {"old": 3}
    written = json.loads(lines[2])
    assert event["meta"] == {"latency_bucket": "fast"}
    assert written["meta"] == {"latency_bucket": "fast"}


def test_provider_event_write_failure_reports_to_stderr(monkeypatch, tmp_path, capsys):
    not_a_dir = tmp_path / "not-a-dir"
    not_a_dir.write_text("x", encoding="utf-8")
    monkeypatch.setattr(provider_status, "EVENTS_PATH", not_a_dir / "provider_events.jsonl")

    clean = provider_status.record_provider_event({"scope": "test", "ok": False})

    assert clean["scope"] == "test"
    assert "[ProviderStatus] write_failed" in capsys.readouterr().err


def test_sentinel_event_compacts_and_filters_sensitive_meta(monkeypatch, tmp_path):
    path = tmp_path / "sentinel_events.jsonl"
    path.write_text(
        "\n".join([
            json.dumps({"old": 1}),
            json.dumps({"old": 2}),
            json.dumps({"old": 3}),
        ]) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sentinel_diagnostics, "EVENTS_PATH", path)
    monkeypatch.setattr(sentinel_diagnostics, "MAX_EVENTS_FILE_BYTES", 1)
    monkeypatch.setattr(sentinel_diagnostics, "MAX_EVENTS_FILE_LINES", 2)

    event = sentinel_diagnostics.record_sentinel_event({
        "scope": "sentinel:cycle_summary",
        "ok": True,
        "status": "decided",
        "meta": {
            "score": 3,
            "location_present": True,
            "checks": [
                {"safe_flag": True, "prompt": "nested-secret", "raw_output": "nested-raw"},
            ],
            "recent_chat_text": "should-not-be-written",
            "monitoringlog": "user-facing text",
            "raw_output": "provider output",
        },
    })

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    assert json.loads(lines[0]) == {"old": 2}
    written = json.loads(lines[2])
    assert event["meta"] == {
        "score": 3,
        "location_present": True,
        "checks": [{"safe_flag": True}],
    }
    assert written["meta"] == {
        "score": 3,
        "location_present": True,
        "checks": [{"safe_flag": True}],
    }
