"""Bounded, temporary evidence of an actual scene, independent of its plan."""
from __future__ import annotations

from copy import deepcopy

TTL_SECONDS = 24 * 60 * 60
MAX_EVENTS = 3
MAX_ITEMS = 128
MAX_EVIDENCE_CHARS = 24000


class Experience:
    def __init__(self):
        self.items = []
        self.omitted = 0

    def add(self, item):
        self.items.append(item)
        if len(self.items) > MAX_ITEMS:
            # Keep the opening and recent developments, not an unbounded log.
            del self.items[6]
            self.omitted += 1

    def message(self, message, *, speaker):
        # The visible message grows during streaming. Its hidden tags never
        # enter this object; snapshot() also captures an interrupted last line.
        self.add({"kind": "message", "speaker": speaker, "message": message})

    def device(self, *, now, name, intent, simulated, action):
        self.add({"kind": "device", "at": now, "name": name, "content": intent,
                  "simulated": simulated, "action": action})

    def state(self, *, now, state, reason=""):
        self.add({"kind": "state", "at": now, "state": state, "reason": reason})

    def snapshot(self, *, user_name, ai_name):
        items = []
        for source in self.items:
            if source["kind"] == "message":
                message = source["message"]
                if not message["content"].strip():
                    continue
                item = {"kind": "message", "speaker": source["speaker"],
                        "at": message["at"], "content": message["content"]}
            else:
                item = deepcopy(source)
            if len(item.get("content", "")) > 2000:
                item["content"] = item["content"][:800] + "［中间省略］" + item["content"][-1100:]
            items.append(item)
        omitted = self.omitted
        while len(items) > 8 and sum(len(str(item)) for item in items) > MAX_EVIDENCE_CHARS:
            del items[3]
            omitted += 1
        return {"user_name": user_name, "ai_name": ai_name, "items": items, "omitted": omitted}


def reason_text(reason, *, user_name, ai_name):
    return {
        "owner_ended": f"{user_name}主动结束",
        "model_ended": f"{ai_name}主动收束",
        "safeword": f"{user_name}使用安全词停下设备",
        "panic": "全局急停",
        "owner_paused": f"{user_name}暂停",
        "owner_stopped": f"{user_name}停止设备",
        "heartbeat_lost": "连接心跳中断",
        "output_link_lost": "设备输出连接中断",
        "device_disconnected": "设备断开连接",
        "server_restart": "服务重启中断",
        "server_shutdown": "服务关闭中断",
        "feature_disabled": "功能被关闭",
    }.get(reason, "运行中断" if reason else "本场结束")


def evidence_text(evidence, *, user_name, ai_name, started_at, brief=False):
    items = evidence.get("items", [])
    if brief:
        # Keep actual last words alongside adopted arrangements, even if many
        # device updates occurred after the last spoken exchange.
        indexes = set()
        for kind, count in (("message", 4), ("device", 2), ("state", 2)):
            indexes.update([i for i, item in enumerate(items) if item["kind"] == kind][-count:])
        items = [item for i, item in enumerate(items) if i in indexes]
    lines = []
    for item in items:
        elapsed = max(0, int(item["at"] - started_at))
        prefix = f"入场后 {elapsed // 60} 分 {elapsed % 60} 秒："
        content = item.get("content", "")
        if brief and len(content) > 350:
            content = content[:150] + "［省略］" + content[-180:]
        if item["kind"] == "message":
            lines.append(prefix + f"{item['speaker']}说：{content}")
        elif item["kind"] == "device":
            accepted = "模拟播放器采用" if item["simulated"] else "手机确认采用"
            action = "停止安排" if item["action"] == "stop" else "控制安排"
            lines.append(prefix + f"{item['name']}的{action}，{accepted}：{content}")
        elif item["kind"] == "state":
            lines.append(prefix + (f"{ai_name}收束此前安排，交流仍在继续" if item["state"] == "aftercare" else
                "暂停后继续" if item["state"] == "resumed" else
                "暂停：" + reason_text(item.get("reason"), user_name=user_name, ai_name=ai_name)))
    if evidence.get("omitted"):
        lines.append("这里只保留了部分实际记录。")
    return "\n".join(lines)
