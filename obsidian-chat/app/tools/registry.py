"""Model-callable tool registry invariants.

The schema registry is the only source of tool membership. Prompt, parser and
executor modules provide bindings for those members; this module only verifies
the wiring and orders registered prompt renderers.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import Any

from .schemas import KNOWN_TOOL_DEFINITIONS, ToolDefinition


class ToolRegistryError(RuntimeError):
    pass


def registered_tools_for_surface(
    surface: str,
    *,
    capabilities: Collection[str] | None = None,
    definitions: Mapping[str, ToolDefinition] | None = None,
) -> tuple[str, ...]:
    registry = KNOWN_TOOL_DEFINITIONS if definitions is None else definitions
    allowed = None if capabilities is None else {str(item) for item in capabilities}
    ordered: list[tuple[int, str]] = []
    for tool_name, definition in registry.items():
        order = definition.prompt_order(surface)
        if order is None or (allowed is not None and tool_name not in allowed):
            continue
        ordered.append((order, tool_name))
    ordered.sort(key=lambda item: (item[0], item[1]))
    return tuple(tool_name for _order, tool_name in ordered)


def next_turn_feedback_tools(
    definitions: Mapping[str, ToolDefinition] | None = None,
) -> frozenset[str]:
    registry = KNOWN_TOOL_DEFINITIONS if definitions is None else definitions
    return frozenset(
        tool_name
        for tool_name, definition in registry.items()
        if definition.feedback_timing == "next_turn"
    )


def validate_turn_advertisement(
    actual_tools: Collection[str],
    advertised_tools: Collection[str],
) -> tuple[str, ...]:
    actual = {str(item) for item in actual_tools if str(item)}
    advertised = {str(item) for item in advertised_tools if str(item)}
    if actual != advertised:
        missing = sorted(actual - advertised)
        extra = sorted(advertised - actual)
        raise ToolRegistryError(
            "runtime tool advertisement mismatch: "
            f"missing_from_prompt={missing!r}, advertised_but_unavailable={extra!r}"
        )
    return tuple(sorted(actual))


def validate_tool_registry(
    definitions: Mapping[str, ToolDefinition] | None = None,
    *,
    prompt_renderers: Mapping[str, Any] | None = None,
    parser_bindings: Collection[str] | None = None,
    executor_bindings: Collection[str] | None = None,
) -> None:
    """Fail startup when a registered model tool lacks any required binding."""

    registry = KNOWN_TOOL_DEFINITIONS if definitions is None else definitions
    if prompt_renderers is None:
        from .prompt_renderers import TOOL_PROMPT_RENDERERS

        prompt_renderers = TOOL_PROMPT_RENDERERS
    if parser_bindings is None:
        from .parser import TOOL_COMMAND_GROUPS

        parser_bindings = TOOL_COMMAND_GROUPS
    if executor_bindings is None:
        from app.chat.action_executor import EXECUTOR_TOOL_CAPABILITIES

        executor_bindings = EXECUTOR_TOOL_CAPABILITIES

    parser_set = {str(item) for item in parser_bindings}
    executor_set = {str(item) for item in executor_bindings}
    failures: list[str] = []
    for tool_name in registry:
        missing: list[str] = []
        if not registry[tool_name].prompt_orders:
            missing.append("prompt_surface")
        if not callable(prompt_renderers.get(tool_name)):
            missing.append("renderer")
        if tool_name not in parser_set:
            missing.append("parser")
        if tool_name not in executor_set:
            missing.append("executor")
        if missing:
            failures.append(f"{tool_name}: {', '.join(missing)}")
    if failures:
        raise ToolRegistryError(
            "model tool registry has incomplete bindings: " + "; ".join(failures)
        )


__all__ = [
    "ToolRegistryError",
    "next_turn_feedback_tools",
    "registered_tools_for_surface",
    "validate_tool_registry",
    "validate_turn_advertisement",
]
