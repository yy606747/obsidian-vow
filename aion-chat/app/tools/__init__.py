"""ToolService boundary contracts with cycle-safe lazy exports."""

from __future__ import annotations

from importlib import import_module


_EXPORT_MODULES = {
    "ALL_COMMAND_GROUPS": ".parser",
    "parse_tool_intents": ".parser",
    "tool_intents_payload": ".parser",
    "KNOWN_TOOL_DEFINITIONS": ".schemas",
    "SideEffectLevel": ".schemas",
    "ToolContext": ".schemas",
    "ToolDefinition": ".schemas",
    "ToolEvent": ".schemas",
    "ToolEventType": ".schemas",
    "ToolIntent": ".schemas",
    "ToolResult": ".schemas",
    "ToolStatus": ".schemas",
    "get_tool_definition": ".schemas",
    "tool_definitions_payload": ".schemas",
    "ToolAdapter": ".service",
    "ToolPlan": ".service",
    "ToolService": ".service",
    "tool_service": ".service",
}


def __getattr__(name: str):
    module_name = _EXPORT_MODULES.get(name)
    if module_name is None:
        raise AttributeError(name)
    value = getattr(import_module(module_name, __name__), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))

__all__ = [
    "ALL_COMMAND_GROUPS",
    "KNOWN_TOOL_DEFINITIONS",
    "SideEffectLevel",
    "ToolContext",
    "ToolDefinition",
    "ToolEvent",
    "ToolEventType",
    "ToolIntent",
    "ToolAdapter",
    "ToolPlan",
    "ToolResult",
    "ToolService",
    "ToolStatus",
    "get_tool_definition",
    "parse_tool_intents",
    "tool_service",
    "tool_definitions_payload",
    "tool_intents_payload",
]
