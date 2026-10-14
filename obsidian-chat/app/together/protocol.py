"""Incremental display separation; controls commit only after a complete turn."""

import json
import math
import re


class TogetherOutput:
    TAGS = {"self_awake", "remember", "understand"}

    def __init__(self):
        self.pending = ""
        self.hidden = None
        self.body = ""
        self.visible = ""
        self.length = 0
        self.controls = {"awake": None, "look": False, "notes": [], "understandings": []}

    def feed(self, chunk):
        self.length += len(chunk)
        if self.length > 60000:
            raise ValueError("together_output_too_large")
        self.pending += chunk
        display = []
        while self.pending:
            if self.hidden:
                closing = f"</{self.hidden}>"
                end = self.pending.find(closing)
                if end < 0:
                    keep = len(closing) - 1
                    if len(self.pending) > keep:
                        self.body += self.pending[:-keep]
                        self.pending = self.pending[-keep:]
                    break
                self.body += self.pending[:end]
                self._control(self.hidden, self.body.strip())
                self.hidden, self.body = None, ""
                self.pending = self.pending[end + len(closing):]
                continue
            pos = self.pending.find("<")
            if pos < 0:
                display.append(self.pending)
                self.pending = ""
                break
            display.append(self.pending[:pos])
            self.pending = self.pending[pos:]
            end = self.pending.find(">")
            if end < 0:
                prefixes = ["<"+name+">" for name in self.TAGS] + ["<look/>", "<look />", "<silent/>", "<silent />"]
                if any(prefix.startswith(self.pending) or self.pending.startswith(prefix[:-1]) for prefix in prefixes):
                    break
                # Comparisons and emoticons such as <3 are ordinary expression.
                display.append("<")
                self.pending = self.pending[1:]
                continue
            tag = self.pending[:end + 1]
            self.pending = self.pending[end + 1:]
            if re.fullmatch(r"<look\s*/>", tag):
                self.controls["look"] = True
            elif re.fullmatch(r"<silent\s*/>", tag):
                pass
            elif tag[1:-1] in self.TAGS:
                self.hidden = tag[1:-1]
            else:
                display.append(tag)
        delta = "".join(display)
        self.visible += delta
        return delta

    def _control(self, tag, body):
        if tag == "remember":
            if not body or len(body) > 4000:
                raise ValueError("together_invalid_note")
            self.controls["notes"].append(body)
        elif tag == "understand":
            value = json.loads(body)
            if (not isinstance(value, dict) or set(value) != {"statement", "source"}
                    or any(not isinstance(v, str) or not v.strip() or len(v) > 4000 for v in value.values())):
                raise ValueError("together_invalid_understanding")
            self.controls["understandings"].append(value)
        else:
            value = json.loads(body)
            if not isinstance(value, dict):
                raise ValueError("together_invalid_wake")
            seconds = value.get("after_seconds")
            if (type(seconds) not in (int, float) or not math.isfinite(seconds) or not 0 < seconds <= 31536000
                    or type(value.get("needs_image", False)) is not bool
                    or not isinstance(value.get("intent", ""), str) or len(value.get("intent", "")) > 2000):
                raise ValueError("together_invalid_wake")
            self.controls["awake"] = {"after_seconds": seconds, "intent": value.get("intent", ""),
                                       "needs_image": value.get("needs_image", False)}

    def finish(self):
        if self.pending or self.hidden:
            raise ValueError("together_incomplete_control")
        if sum(len(self.controls[k]) for k in ("notes", "understandings")) > 16:
            raise ValueError("together_too_many_writebacks")
        return {"visible": self.visible.strip(), **self.controls}
