from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_pc_agent_privacy_and_app_map_match_backend_sources():
    pairs = [
        ("privacy.py", "obsidian-chat/app/pc_context/privacy.py", "pc_agent/privacy.py"),
        ("app_map.py", "obsidian-chat/app/pc_context/app_map.py", "pc_agent/app_map.py"),
    ]
    for label, backend, agent in pairs:
        assert (ROOT / backend).read_text(encoding="utf-8") == (
            ROOT / agent
        ).read_text(encoding="utf-8"), label
