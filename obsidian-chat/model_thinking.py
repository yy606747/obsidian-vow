"""Model thinking controls shared by settings and both provider transports.

Provider dialects are selected by endpoint, not by an OpenAI-compatible label.
An unset control never changes the provider's default.
"""

from urllib.parse import urlparse


THINKING_KEYS = ("enable_thinking", "thinking_level", "thinking_budget")
THINKING_LEVELS = {"MINIMAL", "LOW", "MEDIUM", "HIGH", "MAX"}


def normalize_thinking_level(value: str) -> str:
    level = str(value).strip().upper()
    if level not in THINKING_LEVELS:
        raise ValueError("不支持的思考强度")
    return level


def thinking_capabilities(endpoint: dict, model: str) -> dict:
    host = (urlparse(endpoint.get("base_url", "")).hostname or "").lower()
    native = endpoint.get("type") in {"gemini", "vertex"}
    name = model.lower()
    caps = {"protocol": "effort", "modes": ["default"], "levels": [],
            "budget": None, "hint": "此模型未识别到可用的思考开关。"}
    if host == "api.deepseek.com":
        caps.update(protocol="deepseek", modes=["default", "on", "off"],
                    levels=["LOW", "HIGH", "MAX"] if "v4" in name else [],
                    hint="跟随模型默认可能开启思考；关闭可减少思考 token 和等待时间。")
    elif host in {"api.siliconflow.cn", "api.siliconflow.com"}:
        caps["protocol"] = "siliconflow"
        if "deepseek-v4" in name or "glm-5.2" in name:
            caps.update(modes=["default", "on", "off"], levels=["HIGH", "MAX"],
                        hint="此接口支持高、最高两档；关闭时不发送强度。")
        elif ("deepseek-v3.2" in name or "qwen3" in name or "glm-4.6" in name
              or "glm-4.7" in name or "glm-5" in name) and not any(
                  word in name for word in ("instruct", "thinking", "embedding", "reranker")):
            caps.update(modes=["default", "on", "off"], budget={"min": 128, "max": 32768},
                        hint="可开关思考，也可限制思考 token；预算留空使用模型默认。")
    elif native or "gemini-" in name:
        caps["protocol"] = "gemini" if native else "gemini_compat"
        if "gemini-3" in name:
            levels = ["LOW", "MEDIUM", "HIGH"]
            if "gemini-3-pro" in name:
                levels = ["LOW", "HIGH"]
            elif any(version in name for version in ("gemini-3-flash", "gemini-3.5-flash", "gemini-3.6-flash")):
                levels.insert(0, "MINIMAL")
            caps.update(levels=levels, hint="此模型不支持完全关闭思考，可调低强度。")
        elif "gemini-2.5" in name:
            is_pro = "pro" in name
            caps.update(modes=["default", "on"] + ([] if is_pro else ["off"]),
                        hint="此模型不支持关闭思考。" if is_pro else "关闭思考对应预算 0；留空使用动态预算。")
            if native:
                caps["budget"] = {"min": 128 if is_pro else (512 if "lite" in name else 1),
                                  "max": 32768 if is_pro else 24576}
            else:
                caps["levels"] = ["LOW", "MEDIUM", "HIGH"]
    elif endpoint.get("type", "openai") == "openai":
        # Retain the existing compatible-endpoint effort control. Do not invent
        # an off switch for an unidentified gateway/model.
        caps.update(levels=["LOW", "MEDIUM", "HIGH"],
                    hint="兼容接口：强度通过 reasoning_effort 传递，是否支持取决于此型号和端点。")
    return caps


def validate_thinking_settings(endpoint: dict, model: str, values: dict) -> None:
    caps = thinking_capabilities(endpoint, model)
    enabled = values.get("enable_thinking")
    if enabled is not None:
        if not isinstance(enabled, bool):
            raise ValueError("思考开关必须为布尔值或 null")
        if ("on" if enabled else "off") not in caps["modes"]:
            raise ValueError("此模型不支持该思考开关，请使用模型默认或调整强度")
    level, budget = values.get("thinking_level"), values.get("thinking_budget")
    if level is not None and normalize_thinking_level(level) not in caps["levels"]:
        raise ValueError("此模型不支持所选思考强度")
    if budget is not None:
        bounds = caps["budget"]
        if not bounds or type(budget) is not int or not bounds["min"] <= budget <= bounds["max"]:
            raise ValueError("思考 token 预算超出此模型支持的范围")
    if level is not None and budget is not None:
        raise ValueError("思考强度和思考预算不能同时指定")


def openai_thinking_fields(endpoint: dict, model: str, *, enable_thinking=None,
                           thinking_level=None, thinking_budget=None) -> dict:
    protocol = thinking_capabilities(endpoint, model)["protocol"]
    fields = {}
    if enable_thinking is not None:
        if protocol == "deepseek":
            fields["thinking"] = {"type": "enabled" if enable_thinking else "disabled"}
        elif protocol == "gemini_compat":
            if not enable_thinking:
                fields["reasoning_effort"] = "none"
        else:
            # Preserve existing flat extras for other compatible providers.
            fields["enable_thinking"] = enable_thinking
    if enable_thinking is not False:
        if thinking_level is not None:
            fields["reasoning_effort"] = normalize_thinking_level(thinking_level).lower()
        if protocol == "siliconflow" and thinking_budget is not None and thinking_budget > 0:
            fields["thinking_budget"] = thinking_budget
    return fields


def gemini_thinking_options(*, enable_thinking=None, thinking_level=None,
                            thinking_budget=None) -> dict:
    if enable_thinking is False:
        return {"thinking_budget": 0, "thinking_level": None}
    if enable_thinking is True and thinking_level is None and thinking_budget is None:
        thinking_budget = -1
    return {"thinking_budget": thinking_budget, "thinking_level": thinking_level}
