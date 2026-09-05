from app.pc_context.privacy import (
    REDACTED_TITLE,
    matches_sensitive_keyword,
    sanitize_title,
    strip_path_prefix,
    strip_url_like,
    truncate_title,
)


def test_blacklisted_process_redacts_title():
    assert sanitize_title("Bitwarden.exe", "My vault") == REDACTED_TITLE
    assert sanitize_title(r"C:\Apps\WeChat.exe", "Alice") == REDACTED_TITLE


def test_sensitive_keywords_use_token_boundaries():
    assert matches_sensitive_keyword("Password Manager")
    assert matches_sensitive_keyword("my_password_file")
    assert matches_sensitive_keyword("API_KEY")
    assert matches_sensitive_keyword("api-key")
    assert matches_sensitive_keyword("付款验证码")
    assert not matches_sensitive_keyword("PasswordManager")
    assert not matches_sensitive_keyword("nopassword")


def test_sensitive_keyword_redacts_title():
    assert sanitize_title("chrome.exe", "Payment checkout") == REDACTED_TITLE
    assert sanitize_title("notepad.exe", "银行卡记录") == REDACTED_TITLE


def test_browser_title_strips_url_like_fragments():
    title = "Docs https://example.com/a www.example.com 192.168.1.1:8080/admin"
    assert sanitize_title("chrome.exe", title) == "Docs"
    assert strip_url_like("read file:///C:/a.html now") == "read now"


def test_path_sensitive_title_keeps_leaf_or_short_part():
    assert strip_path_prefix(r"C:\Users\me\repo\service.py") == "service.py"
    assert sanitize_title("Code.exe", r"C:\Users\me\repo\service.py - VS Code") == "VS Code"
    assert sanitize_title("pycharm64.exe", r"C:\secret\project\main.py") == "main.py"


def test_terminal_titles_with_remote_or_args_are_redacted():
    assert sanitize_title("WindowsTerminal.exe", "ssh prod") == REDACTED_TITLE
    assert sanitize_title("cmd.exe", "admin@example.com") == REDACTED_TITLE
    assert sanitize_title("pwsh.exe", "-NoProfile") == REDACTED_TITLE
    assert sanitize_title("powershell.exe", "ENV=prod") == REDACTED_TITLE


def test_title_truncates_to_40_chars():
    title = "a" * 60
    assert truncate_title(title) == "a" * 40
    assert sanitize_title("notepad.exe", title) == "a" * 40
