import json
import logging
import math

from .hardware import compiler_capabilities
from .prompts import compiler_messages

logger = logging.getLogger(__name__)


class SequenceValidationError(ValueError):
    """Stable public error code plus bounded, numeric-only local diagnostics."""

    def __init__(self, code, *, field, actual, expected=None, step=None):
        super().__init__(code)
        self.detail = {"code": code, "field": field, "actual_type": type(actual).__name__}
        if step is not None:
            self.detail["step"] = step  # Zero-based, like the JSON sequence.
        if expected is not None:
            self.detail["expected"] = expected
        # Never serialize an arbitrary model string, object, or unknown key:
        # those can contain hidden intent prose, not just malformed numbers.
        if type(actual) in (bool, type(None)):
            self.detail["actual"] = actual
        elif type(actual) is int:
            if actual.bit_length() <= 128:
                self.detail["actual"] = actual
            else:
                self.detail["actual_bits"] = actual.bit_length()
        elif type(actual) is float:
            self.detail["actual"] = actual if math.isfinite(actual) else str(actual)
        elif isinstance(actual, (str, list, dict)):
            self.detail["actual_length"] = len(actual)


def validate_sequence(raw, capabilities):
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        raise SequenceValidationError("scene_sequence_json_invalid", field="sequence", actual=raw,
                                      expected="JSON object") from None
    if not isinstance(value, dict) or value.get("action") not in ("replace", "hold", "stop"):
        raise SequenceValidationError("scene_sequence_action_invalid", field="action",
                                      actual=value.get("action") if isinstance(value, dict) else value,
                                      expected=["replace", "hold", "stop"])
    action = value["action"]
    if action != "replace":
        return {"action": action}
    steps = value.get("steps")
    if not isinstance(steps, list) or not 1 <= len(steps) <= 128:
        raise SequenceValidationError("scene_sequence_steps_invalid", field="steps", actual=steps,
                                      expected={"min_count": 1, "max_count": 128})
    channels = capabilities["channels"]
    clean = []
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            raise SequenceValidationError("scene_sequence_step_invalid", field="step", actual=step,
                                          step=index, expected="object")
        duration = step.get("duration_ms")
        values = step.get("values")
        if type(duration) is not int or not capabilities.get("min_step_ms", 1) <= duration <= capabilities["max_step_ms"]:
            raise SequenceValidationError("scene_sequence_duration_invalid", field="duration_ms", actual=duration,
                                          step=index, expected={"min": capabilities.get("min_step_ms", 1),
                                          "max": capabilities["max_step_ms"], "integer": True})
        if not isinstance(values, dict) or set(values) != set(channels):
            error = SequenceValidationError("scene_sequence_channels_invalid", field="values", actual=values,
                                            step=index, expected=list(channels))
            if isinstance(values, dict):
                error.detail["missing_fields"] = sorted(set(channels) - set(values))
                error.detail["extra_field_count"] = len(set(values) - set(channels))
            raise error
        for key, number in values.items():
            bounds = channels[key]
            code = None
            if type(number) not in (int, float):
                code = "scene_sequence_number_required"
            elif type(number) is float and not math.isfinite(number):
                code = "scene_sequence_nonfinite"
            elif not bounds["min"] <= number <= bounds["max"]:
                code = "scene_sequence_range_invalid"
            elif bounds.get("integer") and type(number) is not int:
                code = "scene_sequence_integer_required"
            if code:
                raise SequenceValidationError(code, field=key, actual=number, step=index,
                                              expected={k: bounds[k] for k in ("min", "max", "integer") if k in bounds})
        clean.append({"duration_ms": duration, "values": dict(values)})
    if sum(s["duration_ms"] for s in clean) > capabilities["max_cycle_ms"]:
        raise SequenceValidationError("scene_sequence_cycle_invalid", field="cycle_ms",
                                      actual=sum(s["duration_ms"] for s in clean),
                                      expected={"max": capabilities["max_cycle_ms"]})
    return {"action": "replace", "steps": clean}


async def compile_intent(models, *, intent, device, previous, position):
    capabilities = compiler_capabilities(device.get("driver"), device["capabilities"])
    messages = compiler_messages(intent=intent, capabilities=capabilities, previous=previous, position=position)
    raw = await models.compile(messages)
    try:
        return validate_sequence(raw, capabilities)
    except SequenceValidationError as exc:
        logger.warning("scene compile rejected: %s", json.dumps(
            {"device_id": device["id"], "driver": device.get("driver"), **exc.detail},
            ensure_ascii=False, allow_nan=False,
        ))
        raise
