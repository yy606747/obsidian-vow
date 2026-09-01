import asyncio

from app.chat.postprocess import PostProcessor
from app.chat.turn_profiles import chat_turn_profile, opportunity_turn_profile
from app.tools.registry import validate_tool_registry


def test_opportunity_runtime_gate_requires_renderer_agent_and_synced_sprite(monkeypatch):
    import opportunity
    from app.presence import presence_service, sprite_library
    from app.presence import renderer as renderer_module

    class Snapshot:
        capabilities = ()

    async def yes():
        return True

    monkeypatch.setattr(
        opportunity.mode_service, "snapshot", lambda *_args, **_kwargs: Snapshot()
    )
    monkeypatch.setattr(sprite_library, "can_draw", yes)
    monkeypatch.setattr(presence_service, "ready_for_show", yes)
    monkeypatch.setattr(renderer_module, "presence_renderer_configured", lambda: True)
    capabilities = asyncio.run(
        opportunity._runtime_capabilities(
            model_key="no-vision-model", mobile_screen_target=None
        )
    )
    assert "desktop.presence.draw" in capabilities
    assert "desktop.presence.show" in capabilities

    monkeypatch.setattr(renderer_module, "presence_renderer_configured", lambda: False)
    gated = asyncio.run(
        opportunity._runtime_capabilities(
            model_key="no-vision-model", mobile_screen_target=None
        )
    )
    assert "desktop.presence.show" not in gated


def test_presence_tools_registry_is_complete_and_opportunity_only():
    validate_tool_registry()
    opportunity = opportunity_turn_profile(
        runtime_capabilities={
            "desktop.presence.draw",
            "desktop.presence.show",
        },
        reflection_allowed=False,
    )
    assert opportunity.allows_tool("desktop.presence.draw")
    assert opportunity.allows_tool("desktop.presence.show")
    assert "presence_draw" in opportunity.enabled_commands
    assert "presence_show" in opportunity.enabled_commands
    assert not chat_turn_profile("send").allows_tool("desktop.presence.draw")
    assert not chat_turn_profile("send").allows_tool("desktop.presence.show")


def test_presence_draw_marker_parses_and_is_never_visible():
    result = asyncio.run(PostProcessor().process(
        "[PRESENCE_DRAW:人形|黑发紫衣，正面全身|我想安静地站在这里|但不必一直这样]",
        conv_id="conv_presence",
        enabled_commands={"presence_draw"},
    ))
    assert result.content == ""
    assert len(result.tool_intents) == 1
    assert result.tool_intents[0].tool_name == "desktop.presence.draw"
    assert result.tool_intents[0].arguments == {
        "form": "human",
        "prompt": "黑发紫衣，正面全身",
        "description": "我想安静地站在这里|但不必一直这样",
    }


def test_presence_draw_marker_rejects_old_or_invalid_form_without_partial_repair():
    for marker in (
        "[PRESENCE_DRAW:一团刚睡醒的紫色雾]",
        "[PRESENCE_DRAW:动物|紫色小猫|困倦时使用]",
        "[PRESENCE_DRAW:非人形||困倦时使用]",
        "[PRESENCE_DRAW:非人形|紫色小猫|]",
    ):
        result = asyncio.run(PostProcessor().process(
            marker,
            conv_id="conv_presence",
            enabled_commands={"presence_draw"},
        ))
        assert result.content == ""
        assert len(result.tool_intents) == 1
        assert result.tool_intents[0].arguments == {"parse_error": "parse_failed"}


def test_disabled_presence_marker_is_sanitized_without_execution():
    result = asyncio.run(PostProcessor().process(
        "hello [PRESENCE_DRAW:非人形|紫色小猫|困倦时使用]",
        conv_id="conv_presence",
        enabled_commands=set(),
    ))
    assert result.content == "hello"
    assert result.tool_intents == []


def test_presence_show_marker_is_natural_language_and_never_visible():
    result = asyncio.run(PostProcessor().process(
        "[PRESENCE_SHOW:从右下角探出头，晃一下又缩回去]",
        conv_id="conv_presence",
        enabled_commands={"presence_show"},
    ))
    assert result.content == ""
    assert len(result.tool_intents) == 1
    assert result.tool_intents[0].tool_name == "desktop.presence.show"
    assert result.tool_intents[0].arguments == {
        "intent_text": "从右下角探出头，晃一下又缩回去"
    }


def test_disabled_presence_show_marker_is_sanitized_without_execution():
    result = asyncio.run(PostProcessor().process(
        "hello [PRESENCE_SHOW:must not execute]",
        conv_id="conv_presence",
        enabled_commands=set(),
    ))
    assert result.content == "hello"
    assert result.tool_intents == []


def test_summon_ability_block_never_demonstrates_visible_body():
    """The summon round voids on any body, so its examples must not show one."""

    from app.chat.prompt_builder import build_opportunity_ability_block

    profile = opportunity_turn_profile(
        runtime_capabilities=frozenset({"desktop.presence.show"}),
        reflection_allowed=False,
        web_search_allowed=False,
        kind="summon",
    )
    block = str(
        build_opportunity_ability_block(
            profile=profile,
            user_name="小羊",
            ai_name="Alaric",
            model_key="m",
            kind="summon",
        )
    )
    example_lines = [
        line for line in block.splitlines()
        if line.startswith("- ") and "PRESENCE_SHOW" in line
    ]
    assert example_lines
    for line in example_lines:
        assert line.startswith("- [PRESENCE_SHOW:"), line
    assert "让Alaric默认从小羊屏幕右侧出现" in block
    assert "避免挡住正中的工作区" in block
    assert "这条回复的文字已经够了" not in block

    idle = opportunity_turn_profile(
        runtime_capabilities=frozenset({"desktop.presence.show"}),
        reflection_allowed=False,
        web_search_allowed=False,
        kind="idle",
    )
    idle_block = str(
        build_opportunity_ability_block(
            profile=idle,
            user_name="小羊",
            ai_name="Alaric",
            model_key="m",
            kind="idle",
        )
    )
    assert '"去吃饭了。[PRESENCE_SHOW:' in idle_block


def _night_block(*, bootstrap: bool, reflection: bool) -> str:
    from app.chat.prompt_builder import build_opportunity_ability_block

    profile = opportunity_turn_profile(
        runtime_capabilities=frozenset({"desktop.presence.draw"}),
        reflection_allowed=reflection,
        web_search_allowed=False,
        kind="night",
        presence_bootstrap_required=bootstrap,
    )
    return str(
        build_opportunity_ability_block(
            profile=profile,
            user_name="小羊",
            ai_name="Alaric",
            model_key="m",
            kind="night",
            presence_requires_human=bootstrap,
        )
    )


def test_night_prompt_never_names_an_exit_the_profile_disabled():
    """Naming a disabled exit is an instruction to void the round."""

    with_reflect = _night_block(bootstrap=False, reflection=True)
    assert "[OPPORTUNITY_REFLECT]" in with_reflect
    assert "三选一" in with_reflect

    without_reflect = _night_block(bootstrap=False, reflection=False)
    assert "OPPORTUNITY_REFLECT" not in without_reflect
    assert "二选一" in without_reflect
    assert "[OPPORTUNITY_NONE]" in without_reflect

    bootstrap = _night_block(bootstrap=True, reflection=False)
    assert "OPPORTUNITY_REFLECT" not in bootstrap
    assert "OPPORTUNITY_NONE" not in bootstrap
    # The shared precedence tail must not offer speech or a null exit either.
    assert "如果不想行动，就使用规定的空操作标记" not in bootstrap
    assert "如果选择说话" not in bootstrap


def test_bootstrap_draw_prose_does_not_document_the_forbidden_branch():
    bootstrap = _night_block(bootstrap=True, reflection=False)
    assert "非人形" not in bootstrap
    assert "一天最多画两张" not in bootstrap
    assert "[PRESENCE_DRAW:人形|视觉规格|形象自述]" in bootstrap

    ordinary = _night_block(bootstrap=False, reflection=True)
    assert "非人形" in ordinary
    assert "一天最多画两张" in ordinary
