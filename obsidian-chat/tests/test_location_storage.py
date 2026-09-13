import errno
import json

import pytest

import location
from app.events import EvidenceLedger
from context_delivery_runtime_readers import read_context_delivery_projection


@pytest.mark.parametrize("damaged", [b"", b'{"state":', b"null", b"[]", b"\xff"])
def test_damaged_state_does_not_break_context_and_next_fix_recovers(tmp_path, monkeypatch, caplog, damaged):
    path = tmp_path / "location_status.json"
    path.write_bytes(damaged)
    monkeypatch.setattr(location, "LOCATION_STATUS_PATH", path)

    state = location.load_location_status()
    assert state["state"] == "unknown"
    assert state["updated_at"] == 0
    assert path.read_bytes() == damaged
    assert "using unknown state" in caplog.text
    projection = read_context_delivery_projection(
        reference_time=1000,
        ledger=EvidenceLedger(now=lambda: 1000),
        activity_enabled_loader=lambda: False,
        location_config_loader=lambda: {"enabled": True},
        location_status_loader=location.load_location_status,
    )
    assert not any(item.key.startswith("location.") for item in projection.observations)
    assert any(item.source == "location.v2" and item.status == "missing" for item in projection.availability)

    location.save_location_status({"state": "outside", "updated_at": 1001, "lng": 120.1, "lat": 30.1})
    assert location.load_location_status()["updated_at"] == 1001
    assert location.load_location_status()["state"] == "outside"


@pytest.mark.parametrize("stage", ["create", "flush", "replace"])
def test_storage_failure_preserves_previous_fix(tmp_path, monkeypatch, stage):
    path = tmp_path / "location_status.json"
    old = b'{"state":"at_home","updated_at":1000}'
    path.write_bytes(old)
    monkeypatch.setattr(location, "LOCATION_STATUS_PATH", path)

    def disk_full(*_args, **_kwargs):
        raise OSError(errno.ENOSPC, "No space left on device")

    if stage == "create":
        monkeypatch.setattr(location.tempfile, "NamedTemporaryFile", disk_full)
    elif stage == "flush":
        monkeypatch.setattr(location.os, "fsync", disk_full)
    else:
        monkeypatch.setattr(location.os, "replace", disk_full)
    with pytest.raises(OSError) as error:
        location.save_location_status({"state": "outside", "updated_at": 1100})
    assert error.value.errno == errno.ENOSPC
    assert path.read_bytes() == old
    assert list(tmp_path.iterdir()) == [path]


def test_location_readers_see_old_or_complete_new_state(tmp_path, monkeypatch):
    path = tmp_path / "location_status.json"
    path.write_text('{"state":"at_home","updated_at":1000}')
    monkeypatch.setattr(location, "LOCATION_STATUS_PATH", path)
    replace = location.os.replace

    def observe_replacement(source, target):
        assert location.load_location_status()["updated_at"] == 1000
        assert json.loads(source.read_text())["updated_at"] == 1100
        replace(source, target)

    monkeypatch.setattr(location.os, "replace", observe_replacement)
    location.save_location_status({"state": "outside", "updated_at": 1100})
    assert location.load_location_status()["updated_at"] == 1100
    assert list(tmp_path.iterdir()) == [path]


def test_default_location_mutable_values_are_not_shared(tmp_path, monkeypatch):
    monkeypatch.setattr(location, "LOCATION_STATUS_PATH", tmp_path / "missing.json")
    first = location.load_location_status()
    first["weather"]["weather"] = "rain"
    assert location.load_location_status()["weather"] == {}


def test_invalid_new_state_does_not_replace_good_file(tmp_path, monkeypatch):
    path = tmp_path / "location_status.json"
    path.write_text('{"state":"at_home"}')
    monkeypatch.setattr(location, "LOCATION_STATUS_PATH", path)
    for value in ([], {"weather": object()}):
        with pytest.raises((TypeError, ValueError)):
            location.save_location_status(value)
        assert json.loads(path.read_text()) == {"state": "at_home"}
