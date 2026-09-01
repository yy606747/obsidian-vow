from app.chat.dom import _build_ai_dom_block


def test_ai_dom_prompt_keeps_core_voice_while_injecting_only_current_scene():
    prompt = _build_ai_dom_block(
        "用户A",
        "红灯",
        recent=["SCENE:tease", "HOLD:5:3"],
        scene_name="daily_watch",
        scene_elapsed=90,
    )

    assert "让她终于可以不撑着了" in prompt
    assert "每条回复恰好一个玩具指令" in prompt
    assert "【Aftercare（事后安抚）】" in prompt
    assert "【当前场景规则：daily_watch】" in prompt
    assert "目标：" in prompt
    assert "节奏：" in prompt
    assert "禁止行为：" in prompt
    assert "【当前场景规则：tease】" not in prompt
    assert "【当前场景规则：aftercare】" not in prompt


def test_ai_dom_prompt_maps_runtime_scene_aliases():
    prompt = _build_ai_dom_block(
        "用户A",
        "红灯",
        recent=[],
        scene_name="PUNISH",
    )

    assert "【当前场景规则：punishment】" in prompt
    assert "不要否定人格" in prompt
    assert "【当前场景规则：daily_watch】" not in prompt


def test_ai_dom_prompt_maps_warmup_to_daily_watch():
    prompt = _build_ai_dom_block("用户A", "红灯", recent=[], scene_name="warmup")

    assert "【当前场景规则：daily_watch】" in prompt


def test_ai_dom_prompt_without_scene_does_not_emit_scene_map_block():
    prompt = _build_ai_dom_block("用户A", "红灯", recent=[])

    assert "【当前场景规则：" not in prompt
    assert "【最近玩具指令（旧→新）】（暂无）" in prompt


def test_ai_dom_prompt_unknown_scene_only_stays_in_context():
    prompt = _build_ai_dom_block("用户A", "红灯", recent=[], scene_name="edge")

    assert "场景 edge" in prompt
    assert "【当前场景规则：" not in prompt
