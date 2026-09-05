"""
设备活动日志：上报接收、JSONL 存储、自动清理、PC 活动窗口采集、10 分钟摘要
"""

from __future__ import annotations

import json, time, threading, logging
from pathlib import Path
from collections import defaultdict
from datetime import datetime, timedelta

from config import DATA_DIR
from ws import manager

log = logging.getLogger("activity")

# ── 文件操作锁（保护 JSONL 读写不被 PC 线程和 API 协程并发冲突）──
_file_lock = threading.Lock()
_last_cleanup_ts = 0.0
_CLEANUP_INTERVAL = 300  # 每 5 分钟清理一次

# ── 路径 ──────────────────────────────────────────
ACTIVITY_LOGS_DIR = DATA_DIR / "activity_logs"
ACTIVITY_LOGS_DIR.mkdir(exist_ok=True)

# Prompt 查询窗口。原始日志保留期是独立配置，不能复用这个常量。
KEEP_HOURS = 3

# ── 手机 App 包名 → 中文名映射（服务端兜底） ───────────
# Android getApplicationLabel() 在部分 ROM 上会失败，这里做 fallback
KNOWN_APPS = {
    # 社交 / 通讯
    "com.tencent.mm": "微信",
    "com.tencent.mobileqq": "QQ",
    "com.tencent.tim": "TIM",
    "com.xingin.xhs": "小红书",
    "com.sina.weibo": "微博",
    "com.immomo.momo": "陌陌",
    "com.tencent.wework": "企业微信",
    "com.alibaba.android.rimet": "钉钉",
    "com.lark.messenger": "飞书",
    # 视频 / 直播
    "com.ss.android.ugc.aweme": "抖音",
    "com.kuaishou.nebula": "快手",
    "com.smile.gifmaker": "快手",
    "tv.danmaku.bili": "哔哩哔哩",
    "com.youku.phone": "优酷",
    "com.tencent.qqlive": "腾讯视频",
    "com.qiyi.video": "爱奇艺",
    "com.hunantv.imgo.activity": "芒果TV",
    # 音乐
    "com.netease.cloudmusic": "网易云音乐",
    "com.tencent.qqmusic": "QQ音乐",
    "com.kugou.android": "酷狗音乐",
    "com.spotify.music": "Spotify",
    # 购物
    "com.taobao.taobao": "淘宝",
    "com.jingdong.app.mall": "京东",
    "com.xunmeng.pinduoduo": "拼多多",
    "com.achievo.vipshop": "唯品会",
    # 工具 / 效率
    "com.tencent.mtt": "QQ浏览器",
    "com.UCMobile": "UC浏览器",
    "com.android.chrome": "Chrome",
    "com.microsoft.emmx": "Edge",
    "com.qihoo.browser": "360浏览器",
    "com.baidu.searchbox": "百度",
    "com.larus.nova": "豆包",
    "com.ss.android.lark.alchemy": "豆包",
    "com.openai.chatgpt": "ChatGPT",
    "com.google.android.apps.maps": "Google Maps",
    "com.autonavi.minimap": "高德地图",
    "com.baidu.BaiduMap": "百度地图",
    # 支付 / 金融
    "com.eg.android.AlipayGphone": "支付宝",
    "com.tencent.android.qqdownloader": "应用宝",
    # 外卖 / 生活
    "com.sankuai.meituan": "美团",
    "me.ele": "饿了么",
    "com.dianping.v1": "大众点评",
    "com.Qunar": "去哪儿",
    # 阅读 / 知识
    "com.zhihu.android": "知乎",
    "com.douban.frodo": "豆瓣",
    "com.ss.android.article.news": "今日头条",
    "com.netease.newsreader.activity": "网易新闻",
    # 游戏平台
    "com.miHoYo.Yuanshen": "原神",
    "com.miHoYo.hkrpg": "崩坏：星穹铁道",
    "com.tencent.tmgp.sgame": "王者荣耀",
    "com.tencent.tmgp.pubgmhd": "和平精英",
    # AI 助手
    "com.anthropic.claude": "Claude",
    "com.google.android.googlequicksearchbox": "Google搜索",
    # 系统 / 应该过滤的
    "com.android.systemui": None,       # 过滤
    "com.android.launcher": None,       # 过滤
    "com.android.launcher3": None,      # 过滤
    "com.bbk.launcher2": None,          # vivo 桌面 → 过滤
    "com.vivo.launcher": None,          # vivo 桌面 → 过滤
    "com.huawei.android.launcher": None, # 华为桌面 → 过滤
    "com.miui.home": None,              # 小米桌面 → 过滤
    "com.oppo.launcher": None,          # OPPO 桌面 → 过滤
    "com.sec.android.app.launcher": None, # 三星桌面 → 过滤
    # 屏幕状态（Android BroadcastReceiver 上报）
    "screen_off": "锁屏",
    "screen_on": "亮屏",
    # iQOO/vivo 系统
    "com.iqoo.powersaving": "省电管理",
}


def resolve_app_name(app: str, title: str = "") -> str | None:
    """
    解析 App 名称。
    - 如果 app 是包名格式（含 .），查映射表
    - 映射值为 None 表示应过滤（桌面/系统 UI 等）
    - 未知包名保持原样
    - 如果 app 已经是中文名且不是乱码，直接用
    """
    # 已经是可读名称（非包名格式）
    if "." not in app:
        # 检查是否乱码（中文字符但不像正常中文）
        try:
            app.encode("ascii")
        except UnicodeEncodeError:
            # 含非 ASCII 字符，可能是中文也可能是乱码
            # 简单启发式：如果全是常见中文字符，认为有效
            pass
        return app

    # 包名格式，查表
    if app in KNOWN_APPS:
        return KNOWN_APPS[app]  # None 表示过滤

    # 未知包名，如果 title 中有更好的名称就用 title
    if title and "." not in title and title != app:
        return title

    return app


# ── JSONL 读写 ────────────────────────────────────

def _today_log_path() -> Path:
    return ACTIVITY_LOGS_DIR / f"{time.strftime('%Y-%m-%d')}.jsonl"


def append_activity_log(entry: dict):
    """追加一条活动日志"""
    path = _today_log_path()
    with _file_lock:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def read_activity_logs(date_str: str = None) -> list:
    """读取指定日期的全部活动日志"""
    if not date_str:
        date_str = time.strftime("%Y-%m-%d")
    path = ACTIVITY_LOGS_DIR / f"{date_str}.jsonl"
    if not path.exists():
        return []
    entries = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except Exception:
                    pass
    return entries


def read_recent_activity(hours: int = KEEP_HOURS) -> list:
    """读取最近 N 小时内的活动日志（跨天也支持）"""
    import datetime as _dt
    cutoff_ts = time.time() - hours * 3600
    cutoff_date = _dt.date.fromtimestamp(cutoff_ts)
    result = []
    for logfile in sorted(ACTIVITY_LOGS_DIR.glob("*.jsonl")):
        try:
            if _dt.date.fromisoformat(logfile.stem) < cutoff_date:
                continue
        except ValueError:
            continue
        with open(logfile, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    if entry.get("timestamp", 0) >= cutoff_ts:
                        result.append(entry)
                except Exception:
                    pass
    return result


def get_available_dates() -> list[str]:
    """返回所有有日志的日期列表（降序）"""
    dates = []
    for logfile in ACTIVITY_LOGS_DIR.glob("*.jsonl"):
        dates.append(logfile.stem)
    dates.sort(reverse=True)
    return dates


def cleanup_old_activity_logs():
    """按原始数据保留期清理（默认 14 天；每 5 分钟最多执行一次）。"""
    global _last_cleanup_ts
    now = time.time()
    if now - _last_cleanup_ts < _CLEANUP_INTERVAL:
        return
    _last_cleanup_ts = now

    import datetime as _dt
    from app.daily_signals.config import activity_raw_retention_days

    cutoff_ts = now - activity_raw_retention_days() * 86400
    cutoff_date = _dt.date.fromtimestamp(cutoff_ts)
    today = _dt.date.today()

    for logfile in list(ACTIVITY_LOGS_DIR.glob("*.jsonl")):
        try:
            file_date = _dt.date.fromisoformat(logfile.stem)
        except ValueError:
            continue

        # 比截止日期还早的文件整个删除
        if file_date < cutoff_date:
            logfile.unlink(missing_ok=True)
            continue

        # 截止日期当天的文件需要过滤条目
        if file_date == cutoff_date and file_date != today:
            # 加锁读取、过滤、回写
            with _file_lock:
                kept = []
                with open(logfile, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entry = json.loads(line)
                            if entry.get("timestamp", 0) >= cutoff_ts:
                                kept.append(line)
                        except Exception:
                            pass
                if kept:
                    with open(logfile, "w", encoding="utf-8") as f:
                        f.write("\n".join(kept) + "\n")
                else:
                    logfile.unlink(missing_ok=True)


# ── PC 活动窗口采集 ─────────────────────────────────

class PCActivityTracker:
    """Deprecated local-only PC title tracker kept for historical deployments."""

    def __init__(self, interval: int = 60):
        self.interval = interval  # 采集间隔（秒）
        self._thread: threading.Thread | None = None
        self._running = False
        self._last_title = ""
        self._event_loop = None

    def set_event_loop(self, loop):
        self._event_loop = loop

    def start(self):
        import sys
        print("[PCActivity] start() 被调用", flush=True)
        if self._thread and self._thread.is_alive():
            print("[PCActivity] 线程已在运行，跳过", flush=True)
            return
        try:
            import win32gui  # noqa: F401
            print("[PCActivity] win32gui 导入成功", flush=True)
        except ImportError:
            print("[PCActivity] pywin32 未安装，PC 活动采集已禁用", flush=True)
            return
        except Exception as e:
            print(f"[PCActivity] win32gui 导入失败: {e}", flush=True)
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="PCActivity")
        self._thread.start()
        print(f"[PCActivity] PC 活动采集已启动（间隔 {self.interval}s）", flush=True)

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    def _loop(self):
        try:
            import win32gui
            import win32process
        except Exception as e:
            print(f"[PCActivity] ❌ 线程内导入失败: {e}")
            self._running = False
            return
        import time as _time

        print(f"[PCActivity] 采集线程已进入循环 (pid={__import__('os').getpid()})", flush=True)

        while self._running:
            try:
                hwnd = win32gui.GetForegroundWindow()
                title = win32gui.GetWindowText(hwnd)

                # 忽略空标题和桌面（Program Manager 是 Windows 桌面，无意义）
                if title and title != "Program Manager":
                    # 尝试获取进程名
                    app_name = self._get_process_name(hwnd, win32process)
                    title_changed = title != self._last_title

                    now = _time.time()
                    entry = {
                        "timestamp": now,
                        "time": _time.strftime("%H:%M:%S"),
                        "date": _time.strftime("%Y-%m-%d"),
                        "device": "pc",
                        "app": app_name,
                        "title": title,
                    }
                    append_activity_log(entry)

                    if title_changed:
                        self._last_title = title
                        print(f"[PCActivity] {entry['time']} {app_name} - {title[:60]}", flush=True)

                    # 清理过期日志
                    try:
                        cleanup_old_activity_logs()
                    except Exception:
                        pass

                    # 广播给前端（仅窗口变化时广播，避免刷屏）
                    if title_changed and self._event_loop:
                        import asyncio
                        try:
                            from app.background_tasks import run_tracked_threadsafe
                            run_tracked_threadsafe(
                                manager.broadcast({
                                    "type": "activity_log",
                                    "data": entry
                                }),
                                self._event_loop, name="activity_broadcast",
                            )
                        except Exception as be:
                            print(f"[PCActivity] ⚠ 广播失败: {be}")

            except Exception as e:
                print(f"[PCActivity] ❌ 错误: {e}")
                import traceback
                traceback.print_exc()

            # 等待间隔（每秒检查一次是否需要停止）
            for _ in range(self.interval):
                if not self._running:
                    break
                _time.sleep(1)

        print("[PCActivity] 线程退出")

    @staticmethod
    def _get_process_name(hwnd, win32process) -> str:
        """根据窗口句柄获取进程名"""
        try:
            import psutil
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            proc = psutil.Process(pid)
            return proc.name()
        except Exception:
            pass
        # fallback: 无 psutil 时返回通用名称
        return "Unknown"


# ── 10 分钟活动摘要 ──────────────────────────────────

# App 名称美化映射（进程名 → 简称）
_APP_DISPLAY_MAP = {
    "explorer.exe": "文件管理", "msedge.exe": "Edge", "chrome.exe": "Chrome",
    "Code.exe": "VS Code", "Photoshop.exe": "PS", "WindowsTerminal.exe": "终端",
    "notepad++.exe": "Notepad++", "Notepad.exe": "记事本",
    "ApplicationFrameHost.exe": None,  # 由标题决定，_extract_hints 会提取
    "screen_off": "锁屏", "screen_on": "亮屏",
}


def _beautify_app(app: str, titles: set[str]) -> str:
    """将进程名/app名美化为简短可读名"""
    # 精确匹配优先
    if app in _APP_DISPLAY_MAP:
        name = _APP_DISPLAY_MAP[app]
        if name:
            return name
        # None 表示由标题决定，从 titles 取第一个有意义的词
        for t in titles:
            short = t.split(" - ")[0].strip()[:15]
            if short and "." not in short:
                return short
        return ""
    # 手机包名查 KNOWN_APPS
    if "." in app and app in KNOWN_APPS:
        resolved = KNOWN_APPS[app]
        if resolved:
            return resolved
    # Tortoise 系列 → SVN
    if "Tortoise" in app:
        return "SVN"
    # Claude
    if "claude" in app.lower() or any("claude" in t.lower() for t in titles):
        return "Claude"
    # 去掉 .exe 后缀
    if app.endswith(".exe"):
        return app[:-4]
    return app


def _extract_hints(app: str, titles: set[str]) -> list[str]:
    """从窗口标题提取关键上下文信息"""
    hints = []
    for t in titles:
        if t == "[redacted]":
            continue
        if "bilibili" in t.lower() or "哔哩" in t:
            name = t.split("_哔哩")[0].split("_bilibili")[0]
            if "和另外" in name:
                name = name.split(" 和另外")[0]
            hints.append(f"B站:{name[:25]}")
        elif "Visual Studio Code" in t:
            hints.append(t.split(" - ")[0].strip())
        elif "便笺" in t:
            hints.append("便笺")
        elif "文件资源管理器" in t:
            folder = t.replace(" - 文件资源管理器", "")
            if folder and folder != "Program Manager":
                hints.append(folder)
        elif "Commit" in t and ("SVN" in t or "Tortoise" in t):
            hints.append("提交代码")
        elif "TortoiseMerge" in t:
            fname = t.split(" - ")[0].strip()
            hints.append(f"对比{fname}")
        elif "Obsidian Vow" in t:
            hints.append("Obsidian Vow")
        elif t and t != app and "screen_" not in t and "Program Manager" not in t:
            short = t.split(" - ")[0].strip()[:20]
            if short and "." not in short:
                hints.append(short)
    # 去重保序
    seen = []
    for h in hints:
        if h not in seen:
            seen.append(h)
    return seen[:3]


def _format_duration(seconds: float) -> str:
    """格式化持续时间为可读字符串"""
    minutes = round(seconds / 60)
    if minutes < 1:
        return ""
    if minutes >= 60:
        h, m = divmod(minutes, 60)
        return f"{h}小时{m}分钟" if m else f"{h}小时"
    return f"{minutes}分钟"


_COARSE_DEVICE_LABELS = {"pc": "PC", "phone": "手机", "tablet": "平板"}


def _device_key(entry: dict) -> str:
    """分组键：优先稳定的 device_id，回退到 legacy coarse device。"""
    return str(entry.get("device_id") or entry.get("device") or "unknown")


def _device_label(entry: dict) -> str:
    """展示标签：优先用户可读 device_name，回退到 device_type/device 的中文标签。"""
    name = str(entry.get("device_name") or "").strip()
    if name:
        return name
    coarse = str(entry.get("device_type") or entry.get("device") or "").strip().lower()
    return _COARSE_DEVICE_LABELS.get(coarse, coarse or "设备")


def _ordered_device_keys(by_device: dict) -> list[str]:
    """PC 优先（桌面上下文），其余移动设备按稳定键排序，保证摘要输出确定。"""
    return sorted(by_device.keys(), key=lambda k: (0 if k == "pc" else 1, k))


def _summarize_window(entries: list[dict], window_start_ts: float, window_end_ts: float,
                      carry_forward: dict[str, dict] = None) -> str:
    """
    对一个 10 分钟窗口内的条目生成带时长权重的摘要。
    carry_forward: {device_key: entry} 每个设备在窗口开始前的最后一条记录，用于填补窗口开头的空白。
    """
    by_device = defaultdict(list)
    for e in entries:
        by_device[_device_key(e)].append(e)

    # 注入 carry_forward：如果某设备在窗口内有数据但第一条不在窗口起始，
    # 或者窗口内没数据但 carry_forward 有记录，补一条起始状态
    if carry_forward:
        for device, prev_entry in carry_forward.items():
            dev_list = by_device.get(device, [])
            if dev_list:
                earliest = min(e["timestamp"] for e in dev_list)
                if earliest > window_start_ts + 5:  # 有>5秒空白
                    synthetic = dict(prev_entry)
                    synthetic["timestamp"] = window_start_ts
                    by_device[device] = [synthetic] + dev_list
            else:
                # 窗口内没数据，用 carry_forward 填满整个窗口
                synthetic = dict(prev_entry)
                synthetic["timestamp"] = window_start_ts
                by_device[device] = [synthetic]

    parts = []
    for device_key in _ordered_device_keys(by_device):
        group = by_device[device_key]
        dev_entries = sorted(group, key=lambda x: x["timestamp"])
        device_label = _device_label(group[0])

        # 过滤"亮屏"（仅是过渡事件，锁屏时长由 screen_off→下一条 自动涵盖）
        dev_entries = [e for e in dev_entries if e["app"] != "screen_on"]
        if not dev_entries:
            continue

        # ① 构建连续段：(app, titles, duration_seconds)
        segments = []
        i = 0
        while i < len(dev_entries):
            app = dev_entries[i].get("app") or ""
            start_ts = dev_entries[i]["timestamp"]
            titles = set()
            t = dev_entries[i].get("title", "")
            if t:
                titles.add(t)

            j = i + 1
            while j < len(dev_entries) and (dev_entries[j].get("app") or "") == app:
                t2 = dev_entries[j].get("title", "")
                if t2:
                    titles.add(t2)
                j += 1

            # 持续时间 = 到下一段起始；最后一段取窗口结束（不超过当前时间）
            if j < len(dev_entries):
                end_ts = dev_entries[j]["timestamp"]
            else:
                end_ts = min(window_end_ts, time.time())
            duration = max(0, end_ts - start_ts)
            segments.append((app, titles, duration))
            i = j

        # ② 按 display_name 合并同名段，累加时长和标题
        merged_order = []
        merged_dur: dict[str, float] = {}
        merged_titles: dict[str, set[str]] = {}
        merged_raw: dict[str, set[str]] = {}

        for app, titles, dur in segments:
            display = _beautify_app(app, titles)
            if not display:
                display = app
            dkey = display
            if dkey not in merged_dur:
                merged_dur[dkey] = 0
                merged_titles[dkey] = set()
                merged_raw[dkey] = set()
                merged_order.append(dkey)
            merged_dur[dkey] += dur
            merged_titles[dkey] |= titles
            merged_raw[dkey].add(app)

        # ③ 按时长降序排列（主要活动排前面）
        sorted_apps = sorted(merged_order, key=lambda k: merged_dur[k], reverse=True)

        app_descs = []
        for dkey in sorted_apps:
            dur_str = _format_duration(merged_dur[dkey])
            titles = merged_titles[dkey]
            raw_apps = merged_raw[dkey]
            hints = _extract_hints(next(iter(raw_apps)), titles)
            hints = [h for h in hints if h != dkey]

            if hints and dur_str:
                desc = f"{dkey}({', '.join(hints)}) {dur_str}"
            elif dur_str:
                desc = f"{dkey} {dur_str}"
            elif hints:
                desc = f"{dkey}({', '.join(hints)})"
            else:
                desc = dkey
            app_descs.append(desc)

        if app_descs:
            parts.append(f"{device_label}: {', '.join(app_descs)}")

    return " | ".join(parts)


def generate_activity_summary(hours: int = KEEP_HOURS) -> list[dict]:
    """
    对最近 N 小时的原始活动日志生成 10 分钟窗口摘要。
    空窗口标记为"没有活动"，连续空窗口自动合并。
    时间范围：第一条记录所在窗口 ~ 上一个已结束的完整窗口。
    """
    now = time.time()
    entries = read_recent_activity(hours)
    if not entries:
        return []

    # 过滤系统应用（不做 resolve，保留原始 app 名给 _beautify_app 用）
    filtered = []
    for e in entries:
        app = e.get("app") or ""
        if "." in app and app in KNOWN_APPS and KNOWN_APPS[app] is None:
            continue
        if app == "explorer.exe" and e.get("title", "") == "Program Manager":
            continue
        filtered.append(e)

    if not filtered:
        return []

    # 按 10 分钟窗口分组
    windows = defaultdict(list)
    for e in filtered:
        dt = datetime.fromtimestamp(e["timestamp"])
        block = (dt.minute // 10) * 10
        key = f"{dt.hour:02d}:{block:02d}"
        windows[key].append(e)

    # 时间范围：第一条记录所在窗口起始 ~ 当前时间所在窗口起始（不含当前未完成窗口）
    first_ts = min(e["timestamp"] for e in filtered)
    dt_first = datetime.fromtimestamp(first_ts)
    dt_start = dt_first.replace(minute=(dt_first.minute // 10) * 10, second=0, microsecond=0)

    dt_now = datetime.fromtimestamp(now)
    # 当前所在窗口的起始时间（这个窗口还在进行中，不生成摘要）
    dt_current_window = dt_now.replace(minute=(dt_now.minute // 10) * 10, second=0, microsecond=0)

    all_keys = []
    cursor = dt_start
    while cursor < dt_current_window:
        all_keys.append((cursor, f"{cursor.hour:02d}:{cursor.minute:02d}"))
        cursor += timedelta(minutes=10)

    if not all_keys:
        return []

    # 按时间排序所有条目（用于 carry_forward 追溯）
    filtered.sort(key=lambda e: e["timestamp"])

    # 生成每个窗口的摘要（含空窗口）
    raw_items = []
    for block_dt, key in all_keys:
        window_start_ts = block_dt.timestamp()
        window_end_ts = (block_dt + timedelta(minutes=10)).timestamp()
        start_str = f"{block_dt.hour:02d}:{block_dt.minute:02d}"
        end_dt = block_dt + timedelta(minutes=10)
        end_str = f"{end_dt.hour:02d}:{end_dt.minute:02d}"

        # 计算 carry_forward：每个设备在本窗口之前的最后一条记录
        carry_forward: dict[str, dict] = {}
        for e in filtered:
            if e["timestamp"] >= window_start_ts:
                break
            carry_forward[_device_key(e)] = e

        if key in windows:
            w_entries = windows[key]
            summary = _summarize_window(w_entries, window_start_ts, window_end_ts, carry_forward)
            raw_items.append({
                "start": start_str,
                "end": end_str,
                "summary": summary or "没有活动",
                "count": len(w_entries),
                "empty": not summary,
            })
        else:
            # 窗口内无数据，但 carry_forward 可能有状态
            summary = _summarize_window([], window_start_ts, window_end_ts, carry_forward)
            raw_items.append({
                "start": start_str,
                "end": end_str,
                "summary": summary or "没有活动",
                "count": 0,
                "empty": not summary,
            })

    # 合并连续的"没有活动"窗口
    result = []
    i = 0
    while i < len(raw_items):
        item = raw_items[i]
        if item["empty"]:
            # 向后找连续空窗口
            j = i + 1
            while j < len(raw_items) and raw_items[j]["empty"]:
                j += 1
            merged_start = raw_items[i]["start"]
            merged_end = raw_items[j - 1]["end"]
            result.append({
                "start": merged_start,
                "end": merged_end,
                "summary": "没有活动",
                "count": 0,
            })
            i = j
        else:
            result.append({
                "start": item["start"],
                "end": item["end"],
                "summary": item["summary"],
                "count": item["count"],
            })
            i += 1

    return result


# ── 活动追踪总开关 ─────────────────────────────────

def is_activity_tracking_enabled() -> bool:
    """检查活动追踪总开关是否开启"""
    from config import SETTINGS
    return SETTINGS.get("activity_tracking_enabled", True)


def set_activity_tracking_enabled(enabled: bool):
    """设置活动追踪总开关"""
    from config import SETTINGS, save_settings
    SETTINGS["activity_tracking_enabled"] = enabled
    save_settings(SETTINGS)


def get_activity_summary_for_prompt(n: int = 6) -> str:
    """
    获取最新 n 条 10 分钟摘要，格式化为可注入 prompt 的文本。
    n 会被 clamp 到 1-12（对应 10-120 分钟）。
    总开关关闭时返回空字符串。
    """
    if not is_activity_tracking_enabled():
        return ""
    n = max(1, min(12, n))
    summaries = generate_activity_summary(hours=KEEP_HOURS)
    if not summaries:
        return ""
    # 取最新 n 条（列表末尾是最新的）
    recent = summaries[-n:]
    lines = []
    for s in recent:
        lines.append(f"[{s['start']}~{s['end']}] {s['summary']}")
    return "\n".join(lines)


# 全局单例
pc_tracker = PCActivityTracker()
