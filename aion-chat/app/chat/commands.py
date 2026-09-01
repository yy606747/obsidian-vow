"""Chat command patterns and lightweight command parsing helpers."""

from __future__ import annotations

import re

from camera import CAM_CHECK_CMD
from routes.music import MUSIC_CMD_PATTERN
from schedule import (
    ALARM_CMD,
    MONITOR_CMD,
    REMINDER_CMD,
    SCHEDULE_DEL_CMD,
    SCHEDULE_LIST_CMD,
)

HEART_CMD_PATTERN = re.compile(r'\[HEART:([^\]]+)\]')
ACTIVITY_CHECK_PATTERN = re.compile(r'\[查看动态:(\d+)\]')
SCREEN_CHECK_PATTERN = re.compile(r'\[SCREEN_CHECK:([^\]]+)\]')
# 移动端截图：[MOBILE_SCREEN_CHECK:目标|原因]，目标可为设备名/类型/留空
MOBILE_SCREEN_CHECK_PATTERN = re.compile(r'\[MOBILE_SCREEN_CHECK:([^\]]+)\]')
REMEMBER_CMD_PATTERN = re.compile(r'\[REMEMBER:([^\]]+)\]')
PRESENCE_DRAW_PATTERN = re.compile(r'\[PRESENCE_DRAW:([^\]]+)\]')
PRESENCE_SHOW_PATTERN = re.compile(r'\[PRESENCE_SHOW:([^\]]+)\]')
SELF_WAKE_PATTERN = re.compile(r'\[SELF_WAKE:([^\]]+)\]')
SELF_WAKE_CANCEL_TOKEN = "[SELF_WAKE_CANCEL]"
SELF_WAKE_CANCEL_PATTERN = re.compile(re.escape(SELF_WAKE_CANCEL_TOKEN))
UPDATE_MODEL_CMD_PATTERN = re.compile(r'\[UPDATE_MODEL:([\s\S]*?)\]')
UNFINISHED_UPDATE_MODEL_CMD_PATTERN = re.compile(r'\[UPDATE_MODEL:[\s\S]*$')
WORKING_MODEL_REQUEST_OPEN = "[WORKING_MODEL_REQUEST]"
WORKING_MODEL_REQUEST_CLOSE = "[/WORKING_MODEL_REQUEST]"
WORKING_MODEL_REQUEST_PATTERN = re.compile(
    re.escape(WORKING_MODEL_REQUEST_OPEN)
    + r"([\s\S]*?)"
    + re.escape(WORKING_MODEL_REQUEST_CLOSE)
)
UNFINISHED_WORKING_MODEL_REQUEST_PATTERN = re.compile(
    re.escape(WORKING_MODEL_REQUEST_OPEN) + r"[\s\S]*$"
)
ORPHAN_WORKING_MODEL_REQUEST_CLOSE_PATTERN = re.compile(
    re.escape(WORKING_MODEL_REQUEST_CLOSE)
)
POI_SEARCH_PATTERN = re.compile(r'\[POI_SEARCH:([^\]]+)\]')
# 放宽匹配：兼容 [TOY:...] / [TOY：...] / 【TOY:...】 / 【TOY：...】。
TOY_CMD_PATTERN = re.compile(r'[\[【]\s*TOY\s*[:：]\s*([^\]】]+)\s*[\]】]', re.IGNORECASE)
RING_TOUCH_PATTERN = re.compile(r'\[RING:([^\]]+)\]')

_RETRY_SENTINEL = '\x00RETRY\x00'

# 允许进入上下文的 system 消息关键词（点歌、查看监控、查看动态）
_SYSTEM_MSG_CONTEXT_KEYWORDS = ('查看了监控', '搜索了', '点歌', '点了一首', '推荐了', '查看了动态')

TOY_PRESET_NAMES = {1:'微风轻拂',2:'春水初生',3:'暗流涌动',4:'如梦似幻',5:'情潮渐涨',6:'烈焰焚身',7:'极乐之巅',8:'魂飞魄散',9:'失控'}

_SCENE_LABELS = {'warmup':'渐入', 'tease':'挑逗', 'edge':'边缘', 'intense':'高强', 'soothe':'安抚'}

def _strip_retry(text: str) -> str:
    if _RETRY_SENTINEL in text:
        return text.split(_RETRY_SENTINEL)[-1]
    return text

def _strip_eval_side_effect_commands(text: str) -> str:
    """Live eval calls the real model, but must not execute tool side effects."""
    for pattern in (
        MUSIC_CMD_PATTERN,
        TOY_CMD_PATTERN,
        ACTIVITY_CHECK_PATTERN,
        SCREEN_CHECK_PATTERN,
        MOBILE_SCREEN_CHECK_PATTERN,
        POI_SEARCH_PATTERN,
        HEART_CMD_PATTERN,
        RING_TOUCH_PATTERN,
        REMEMBER_CMD_PATTERN,
        PRESENCE_DRAW_PATTERN,
        PRESENCE_SHOW_PATTERN,
        SELF_WAKE_PATTERN,
        SELF_WAKE_CANCEL_PATTERN,
        UPDATE_MODEL_CMD_PATTERN,
        WORKING_MODEL_REQUEST_PATTERN,
        ORPHAN_WORKING_MODEL_REQUEST_CLOSE_PATTERN,
        ALARM_CMD,
        REMINDER_CMD,
        MONITOR_CMD,
        SCHEDULE_DEL_CMD,
        SCHEDULE_LIST_CMD,
    ):
        text = pattern.sub("", text)
    text = UNFINISHED_UPDATE_MODEL_CMD_PATTERN.sub("", text)
    text = UNFINISHED_WORKING_MODEL_REQUEST_PATTERN.sub("", text)
    return text.replace(CAM_CHECK_CMD, "").strip()

def _toy_cmd_label(cmd: str) -> str:
    """把单条 TOY 指令内容翻译成简短的中文标签（给聊天系统消息用）"""
    c = cmd.strip()
    up = c.upper()
    if up == 'STOP': return '停止'
    if up == 'EDGE': return '边缘控制'
    if up == 'PUNISH': return '惩罚'
    if up == 'HUNT': return '狩猎'
    if up == 'SHATTER': return '粉碎'
    if ':' in c:
        parts = c.split(':')
        verb = parts[0].upper()
        if verb == 'SCENE' and len(parts) >= 2:
            return '场景·' + _SCENE_LABELS.get(parts[1].lower(), parts[1])
        if verb == 'HOLD':
            if len(parts) >= 3: return f'维持·v{parts[1]}/s{parts[2]}'
            if len(parts) == 2: return f'维持·{parts[1]}档'
        if verb == 'SPIKE':
            if len(parts) >= 4: return f'脉冲·v{parts[1]}/s{parts[2]}·{parts[3]}秒'
            if len(parts) == 3: return f'脉冲·{parts[1]}档·{parts[2]}秒'
        if verb == 'DENY' and len(parts) >= 2: return f'剥夺·{parts[1]}秒'
        if verb == 'REWARD' and len(parts) >= 2: return f'奖赏·{parts[1]}档'
        if verb == 'TEASE' and len(parts) >= 2: return f'挑逗·{parts[1]}秒'
        if verb == 'OVERLOAD' and len(parts) >= 2: return f'过载·{parts[1]}秒'
        if verb == 'GRIND':
            if len(parts) >= 4: return f'碾磨·v{parts[1]}/s{parts[2]}·{parts[3]}秒'
            if len(parts) == 3: return f'碾磨·{parts[1]}档·{parts[2]}秒'
        if verb == 'SIEGE':
            if len(parts) >= 3: return f'围城·v{parts[1]}/s{parts[2]}'
            if len(parts) == 2: return f'围城·{parts[1]}档'
        if verb == 'HUNT' and len(parts) >= 2: return f'狩猎·{parts[1]}轮'
        if verb == 'SHATTER' and len(parts) >= 2: return f'粉碎·{parts[1]}秒'
        if verb == 'TRAP' and len(parts) >= 2: return f'陷阱·{parts[1]}'
        if verb == 'BREAK' and len(parts) >= 2: return f'逼供·{":".join(parts[1:])}'
        if verb == 'DILEMMA': return '抉择'
        return c
    if c.isdigit():
        n = int(c)
        return f'心动{n} · ' + TOY_PRESET_NAMES.get(n, f'档位{n}')
    return c
