from app.pc_context.app_map import get_feature_tag, normalize_app_name


def test_normalize_known_process_names():
    assert normalize_app_name("Code.exe") == "VS Code"
    assert normalize_app_name(r"C:\Program Files\Google\Chrome.exe") == "Chrome"
    assert normalize_app_name("pwsh.exe") == "PowerShell"


def test_unknown_process_falls_back_to_basename_without_exe():
    assert normalize_app_name("SomeRandomApp.exe") == "SomeRandomApp"
    assert normalize_app_name(r"C:\Tools\Thing") == "Thing"
    assert normalize_app_name("") == "Unknown"


def test_feature_tags_map_by_process_or_canonical_app():
    assert get_feature_tag("Code.exe") == "pc_foreground_dev_tool"
    assert get_feature_tag("VS Code") == "pc_foreground_dev_tool"
    assert get_feature_tag("Chrome") == "pc_foreground_browser"
    assert get_feature_tag("Spotify") == "pc_foreground_media"
    assert get_feature_tag("SomeRandomApp.exe") is None
