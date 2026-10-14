"""Incremental, fail-closed separation of display text and hidden controls."""

import re


class SceneOutput:
    def __init__(self, labels):
        self.labels = set(labels)
        self.pending = ""
        self.hidden = None
        self.hidden_text = ""
        self.visible = ""
        self.intents = {}
        self.notes = []
        self.awake = None
        self.ended = False
        self.invalid = False
        self.length = 0

    def feed(self, chunk):
        self.length += len(chunk)
        if self.length > 60000:
            raise ValueError("scene_output_too_large")
        if self.invalid:
            return ""
        self.pending += chunk
        display = []
        while self.pending and not self.invalid:
            if self.hidden is not None:
                closing = f"</{self.hidden}>"
                combined = self.hidden_text + self.pending
                nested = combined.find("<")
                if nested >= 0 and not (combined.startswith(closing, nested) or closing.startswith(combined[nested:])):
                    self.invalid = True
                    break
                pos = self.pending.find(closing)
                if pos < 0:
                    # Retain enough suffix to recognize split closing tags.
                    retain = len(closing) - 1
                    if len(self.pending) > retain:
                        self.hidden_text += self.pending[:-retain]
                        self.pending = self.pending[-retain:]
                    break
                content = (self.hidden_text + self.pending[:pos]).strip()
                self.pending = self.pending[pos + len(closing):]
                if "<" in content or ">" in content:
                    self.invalid = True
                elif self.hidden in self.labels and content:
                    self.intents.setdefault(self.hidden, []).append(content)
                elif self.hidden == "self_awake":
                    if content.isdigit() and 1 <= int(content) <= 86400:
                        self.awake = int(content)
                    else:
                        self.invalid = True
                elif self.hidden == "remember" and content and len(content) <= 4000 and len(self.notes) < 8:
                    self.notes.append(content)
                else:
                    self.invalid = True
                self.hidden = None
                self.hidden_text = ""
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
                break
            tag = self.pending[:end + 1]
            self.pending = self.pending[end + 1:]
            if re.fullmatch(r"<scene_end\s*/>", tag):
                self.ended = True
            else:
                match = re.fullmatch(r"<([A-Za-z_][\w-]*)>", tag)
                if match:
                    self.hidden = match[1]
                    self.hidden_text = ""
                else:
                    # An unrecognized/malformed tag poisons the rest of this
                    # turn. Never guess that text after it is safe to display.
                    self.hidden = "__invalid__"
                    self.invalid = True
        if self.invalid:
            # Once structure is ambiguous, no tail (including later chunks)
            # is safe to display. finish() still rejects the entire control turn.
            self.pending = self.hidden_text = ""
        value = "".join(display)
        self.visible += value
        return value

    def finish(self):
        if self.hidden is not None or self.pending:
            self.invalid = True
        if self.invalid:
            raise ValueError("scene_output_protocol_invalid")
        return {"visible": self.visible.strip(),
                "intents": {key: "\n".join(parts) for key, parts in self.intents.items()},
                "awake": self.awake, "ended": self.ended, "notes": list(self.notes)}
