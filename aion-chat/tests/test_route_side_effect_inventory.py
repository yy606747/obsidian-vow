from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ROUTES_DIR = ROOT / "routes"


ROUTE_INVENTORY = {
    "activity.py": {
        "classification": "legacy_shadow_ingestion_mixed_mutable",
        "shadow_evidence": True,
    },
    "avatars.py": {
        "classification": "legacy_file_mutable",
        "shadow_evidence": False,
    },
    "cam.py": {
        "classification": "legacy_disabled",
        "shadow_evidence": False,
    },
    "chat.py": {
        "classification": "service_backed_mutable",
        "shadow_evidence": False,
    },
    "control.py": {
        "classification": "service_backed_mutable",
        "shadow_evidence": False,
    },
    "devices.py": {
        "classification": "service_backed_mutable",
        "shadow_evidence": False,
    },
    "events.py": {
        "classification": "phase8_readonly_diagnostic",
        "shadow_evidence": False,
    },
    "files.py": {
        "classification": "legacy_mutable",
        "shadow_evidence": False,
    },
    "heart_whispers.py": {
        "classification": "legacy_mutable",
        "shadow_evidence": False,
    },
    "location.py": {
        "classification": "service_backed_shadow_ingestion_mixed_mutable",
        "shadow_evidence": False,
    },
    "memories.py": {
        "classification": "legacy_service_mixed_mutable",
        "shadow_evidence": False,
    },
    "image_memory.py": {
        "classification": "service_backed_external_io_mutable",
        "shadow_evidence": False,
    },
    "modes.py": {
        "classification": "service_backed_readonly",
        "shadow_evidence": False,
    },
    "music.py": {
        "classification": "legacy_external_io",
        "shadow_evidence": False,
    },
    "pc_screen.py": {
        "classification": "service_backed_external_io_mutable",
        "shadow_evidence": False,
    },
    "presence.py": {
        "classification": "service_backed_external_io_mutable",
        "shadow_evidence": False,
    },
    "push.py": {
        "classification": "service_backed_external_io_mutable",
        "shadow_evidence": False,
    },
    "mobile_screen.py": {
        "classification": "service_backed_external_io_mutable",
        "shadow_evidence": False,
    },
    "schedule.py": {
        "classification": "legacy_mutable",
        "shadow_evidence": False,
    },
    "sensing.py": {
        "classification": "legacy_shadow_ingestion",
        "shadow_evidence": True,
    },
    "sentinel.py": {
        "classification": "phase8_diagnostic_with_mutable_config_and_shadow_label",
        "shadow_evidence": False,
    },
    "settings.py": {
        "classification": "legacy_mutable",
        "shadow_evidence": False,
    },
    "voice.py": {
        "classification": "legacy_external_io_mutable",
        "shadow_evidence": False,
    },
    "vows.py": {
        # 誓约管理页 API（Phase 3）：VowService own-tx 方法上的薄壳
        "classification": "service_backed_mutable",
        "shadow_evidence": False,
    },
}


def test_all_route_files_are_classified_before_new_work_depends_on_them():
    route_files = {
        path.name for path in ROUTES_DIR.glob("*.py")
        if path.name != "__init__.py"
    }

    assert route_files == set(ROUTE_INVENTORY)


def test_phase8_diagnostic_routes_stay_get_only_and_readonly_named():
    for route_name, meta in ROUTE_INVENTORY.items():
        if meta["classification"] != "phase8_readonly_diagnostic":
            continue
        source = (ROUTES_DIR / route_name).read_text(encoding="utf-8")
        assert "@router.get" in source
        assert "@router.post" not in source
        assert "@router.put" not in source
        assert "@router.delete" not in source
        assert "Read-only" in source or "read-only" in source


def test_shadow_ingestion_routes_explicitly_record_evidence_safely():
    for route_name, meta in ROUTE_INVENTORY.items():
        source = (ROUTES_DIR / route_name).read_text(encoding="utf-8")
        if meta["shadow_evidence"]:
            assert "record_" in source
            assert "_safely" in source
        else:
            assert "_safely" not in source


def test_legacy_route_inventory_marks_old_debt_explicitly():
    legacy_routes = [
        route for route, meta in ROUTE_INVENTORY.items()
        if meta["classification"].startswith("legacy")
    ]

    assert "chat.py" not in legacy_routes
    assert "location.py" not in legacy_routes
    assert "sensing.py" in legacy_routes
