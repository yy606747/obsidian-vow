import ast
import inspect
from types import SimpleNamespace

from app.chat import action_executor, basic_actions, streaming
from app.tools.schemas import ToolIntent


def test_basic_action_group_has_no_reverse_dependency_on_streaming():
    moved = {
        "_execute_heart_whisper", "_execute_remember_note", "_execute_music_search",
        "_music_search_intents", "_heart_whisper_intents", "_remember_intents",
    }
    module_tree = ast.parse(inspect.getsource(basic_actions))
    for node in ast.walk(module_tree):
        if isinstance(node, ast.ImportFrom):
            assert "streaming" not in (node.module or "")
            assert all(alias.name != "streaming" for alias in node.names)
        elif isinstance(node, ast.Import):
            assert all("streaming" not in alias.name for alias in node.names)
    bindings = ast.parse(inspect.getsource(action_executor._standard_bindings))
    referenced = {(node.value.id, node.attr) for node in ast.walk(bindings)
                  if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.attr in moved}
    assert referenced == {("basic_actions", name) for name in moved}
    assert all(not hasattr(streaming, name) for name in moved)


def test_fallback_ids_metadata_and_marker_order_are_preserved():
    postprocessed = SimpleNamespace(tool_intents=[], heart_whispers=["", " 心语 "], remember_notes=["记忆"])
    heart = basic_actions._heart_whisper_intents(postprocessed)
    remember = basic_actions._remember_intents(postprocessed)
    assert heart[0].id == "stream_heart_002"
    assert heart[0].raw_text == "[HEART:心语]"
    assert heart[0].metadata == {"legacy_marker": "HEART", "command_group": "heart", "source": "postprocess_result"}
    assert remember[0].id == "stream_remember_001"
    assert remember[0].raw_text == "[REMEMBER:记忆]"
    assert remember[0].arguments == {"content": "记忆"}


def test_existing_intents_are_reused_without_duplicate_fallbacks():
    intent = ToolIntent(id="original", tool_name="memory.remember", raw_text="[REMEMBER:原意图]", arguments={"content": "原意图"})
    result = basic_actions._remember_intents(SimpleNamespace(tool_intents=[intent], remember_notes=["重复候选"]))
    assert result == [intent]
    assert result[0] is intent
