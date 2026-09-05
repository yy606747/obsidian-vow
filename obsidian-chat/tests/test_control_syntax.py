import pytest

from app.chat.control_syntax import (
    ControlMarkerStreamFilter,
    LITERAL_MARKERS,
    PAIRED_MARKERS,
    VALUE_MARKERS,
    canonicalize_control_markers,
    contains_control_marker,
    strip_control_markers,
)


@pytest.mark.parametrize("name", VALUE_MARKERS.values())
@pytest.mark.parametrize(
    ("opening", "colon", "closing"),
    (("[", ":", "]"), ("【", ":", "】"), ("[", "：", "】"), ("【", "：", "]")),
)
def test_value_markers_accept_width_and_case_variants(name, opening, colon, closing):
    wire_name = name.lower() if name.isascii() else name
    raw = f"前{opening} {wire_name} {colon} 内容{closing}后"

    assert canonicalize_control_markers(raw) == f"前[{name}:内容]后"
    assert strip_control_markers(raw) == "前后"
    assert contains_control_marker(raw) is True


@pytest.mark.parametrize("name", LITERAL_MARKERS.values())
@pytest.mark.parametrize(("opening", "closing"), (("[", "]"), ("【", "】"), ("[", "】")))
def test_literal_markers_accept_width_and_case_variants(name, opening, closing):
    wire_name = name.lower()
    raw = f"前{opening} {wire_name} {closing}后"

    assert canonicalize_control_markers(raw) == f"前[{name}]后"
    assert strip_control_markers(raw) == "前后"


@pytest.mark.parametrize("name", PAIRED_MARKERS.values())
def test_paired_markers_accept_fullwidth_and_mixed_delimiters(name):
    raw = f"前【{name.lower()}】私有内容[/ {name.lower()} 】后"

    assert canonicalize_control_markers(raw) == f"前[{name}]私有内容[/{name}]后"
    assert strip_control_markers(raw) == "前后"


def test_tide_marker_and_nested_vow_are_canonicalized_without_touching_payload():
    tide = "前【tide_intent：慢一点[/ tide_intent 】后"
    vow = "前【vow：内容[TOY:9]|确认】后"

    assert canonicalize_control_markers(tide) == "前[TIDE_INTENT:慢一点[/TIDE_INTENT]后"
    assert strip_control_markers(tide) == "前后"
    assert canonicalize_control_markers(vow) == "前[VOW:内容[TOY:9]|确认]后"
    assert strip_control_markers(vow) == "前后"


def test_unknown_chinese_brackets_and_payload_text_are_untouched():
    raw = "正文【普通说明：HEART 只是英文单词】结尾"

    assert canonicalize_control_markers(raw) == raw
    assert strip_control_markers(raw) == raw
    assert contains_control_marker(raw) is False


@pytest.mark.parametrize(
    "marker",
    (
        "【HEART：秘密】",
        "[RING：轻碰]",
        "【CAM_CHECK】",
        "【RECALL_INTENT】私有【/RECALL_INTENT】",
        "【TIDE_INTENT：私有【/TIDE_INTENT】",
    ),
)
def test_stream_filter_hides_markers_across_every_chunk_boundary(marker):
    stream_filter = ControlMarkerStreamFilter()
    chunks = ["正文", *marker, "结尾"]

    visible = "".join(stream_filter.feed(chunk) for chunk in chunks)
    visible += stream_filter.flush()

    assert visible == "正文结尾"


def test_stream_filter_hides_unfinished_known_prefix_but_keeps_unknown_brackets():
    hidden = ControlMarkerStreamFilter()
    assert hidden.feed("正文【HE") == "正文"
    assert hidden.flush() == ""

    ordinary = ControlMarkerStreamFilter()
    assert ordinary.feed("正文【普通") == "正文【普通"
    assert ordinary.feed("说明】结尾") == "说明】结尾"
