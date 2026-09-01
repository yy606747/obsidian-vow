// ══════════════════════════════════════════════════════════
// ── 亲密控制（AI Dom 模式）——两玩具通用的自主控制 ──
// ══════════════════════════════════════════════════════════

// ── 设备描述符：Android BleBridge.configure 所需 + Web Bluetooth fallback 用 ──
const AI_DOM_DEVICES = {
  sosexy: {
    label: 'SOSEXY',
    native: {
      namePrefix: 'SOSEXY',
      serviceUuid: '0000ee01-0000-1000-8000-00805f9b34fb',
      writeUuid:   '0000ee03-0000-1000-8000-00805f9b34fb',
      notifyUuid:  '0000ee02-0000-1000-8000-00805f9b34fb',
      framing: 'chunked', writeType: 'default'
    }
  },
  cx492b: {
    label: 'CX492B',
    native: {
      namePrefix: 'CX492B',
      serviceUuid: '0000ffe0-0000-1000-8000-00805f9b34fb',
      writeUuid:   '0000ffe1-0000-1000-8000-00805f9b34fb',
      notifyUuid:  '0000ffe2-0000-1000-8000-00805f9b34fb',
      framing: 'raw', writeType: 'no_response'
    }
  },
  sk30: { label: '失控3.0', adv: true, advDevice: 'sk30' },
  sk40: { label: '失控4.0', adv: true, advDevice: 'sk40' }
};
// DeviceService owns a single frontend toy bridge; toyDriver.active is only a local driver key.
const AI_DOM_BACKEND_DEVICE_ID = 'browser_toy_bridge';

// CX492B Web Bluetooth 连接（非 Android 环境走这里）
const CX_SERVICE_UUID = 0xFFE0, CX_WRITE_UUID = 0xFFE1, CX_NOTIFY_UUID = 0xFFE2;
let cxDevice = null, cxServer = null, cxWriteChar = null, cxConnected = false;

// AI Dom 模式状态
let aiDomMode = false;
let aiDomSafeword = localStorage.getItem('aion_safeword') || '';
let aiDomCncEnabled = localStorage.getItem('aion_cnc_enabled') === '1';
let aiDomCncWeakness = (() => {
  try { const raw = localStorage.getItem('aion_cnc_weakness') || ''; return raw.split('\n').map(s=>s.trim()).filter(Boolean); }
  catch(e) { return []; }
})();

// 批次 A 信号 / 时间追踪
let aiDomSessionStartAt = 0;
let aiDomLastPunishAt = 0;
let aiDomComplianceStreak = 0;
let aiDomShortStreak = 0;
let aiDomLastResistHits = 0;
let aiDomLastReplyDelayMs = 0;
let aiDomSendClickedAt = 0;
let aiDomLastAiDoneAt = 0;
// 抗拒词（排除 safeword/认输词由调用处保证）
const AI_DOM_RESIST_RE = /(不要|别|停|够了|求求|太过|不行|算了)/;
// 示弱词：用于倔强检测——连续不出现这些词 = stubborn
const AI_DOM_YIELD_RE = /(不要|别|停|够了|求求|不行|受不了|救|饶|怕|太|好强|慢|轻|疼|痛|呜|啊啊|嗯嗯|哈啊)/;

// ── v2 猎物系统：棘轮 / 债务 / 倔强 ──
let aiDomRatchetValley = 0;   // 只升不降的回落地板，safeword 归零
let aiDomDebt = 0;            // 不服从累积债务
let aiDomStubbornStreak = 0;  // 连续未示弱轮数
let aiDomBreakActive = null;  // { demand: string, startAt: number, escalateTimer } | null
let aiDomPostOCooldown = 0;   // 上次疑似高潮的时间戳

function aiDomRatchetFloor() { return Math.min(8, aiDomRatchetValley + Math.floor(aiDomDebt)); }
function aiDomClampFloor(v) { return Math.max(v, aiDomRatchetFloor()); }
function aiDomRatchetBump() { aiDomRatchetValley = Math.min(8, aiDomRatchetValley + 1); }
function aiDomDebtAdd(n) { aiDomDebt = Math.max(0, aiDomDebt + n); }
function aiDomGrindDuration(sec) { return Math.round(sec * (1 + aiDomDebt * 0.2)); }

// ── 小工具：toast / log / guard ──
function aiDomToast(text) {
  let el = document.getElementById('_domToast');
  if (!el) { el = document.createElement('div'); el.id = '_domToast'; document.body.appendChild(el); }
  el.textContent = text;
  el.classList.add('show');
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.remove('show'), 1800);
}

function aiDomLog(msg, cls) {
  const a = document.getElementById('aiDomLogArea'); if (!a) return;
  const d = document.createElement('div');
  d.className = 'wl-' + (cls === 'err' ? 'err' : (cls === 'send' ? 'send' : 'sys'));
  d.textContent = '[' + new Date().toLocaleTimeString('zh-CN',{hour12:false}) + '] ' + msg;
  a.appendChild(d); a.scrollTop = a.scrollHeight;
}

function aiDomGuard(action) {
  if (aiDomMode) { aiDomToast('主控期间无法' + action + '，先退出'); return true; }
  return false;
}

// ── SOSEXY 连接/播放（Dom 模式路径，configure 原生桥后再 connect） ──
async function soConnect() {
  const d = AI_DOM_DEVICES.sosexy.native;
  if (window.AionBle) {
    if (window.AionBle.configure) window.AionBle.configure(JSON.stringify(d));
    window.AionBle.connect();
    return;
  }
  if (!navigator.bluetooth) { aiDomLog('浏览器不支持 Web Bluetooth', 'err'); throw new Error('no_bt'); }
  toyDevice = await navigator.bluetooth.requestDevice({
    filters: [{ namePrefix: 'SOSEXY' }], optionalServices: [TOY_SERVICE_UUID]
  });
  toyDevice.addEventListener('gattserverdisconnected', () => {
    toyConnected = false; toyWriteChar = null; aiDomRefreshUI(); if (typeof toyUpdateUI === 'function') toyUpdateUI();
    toyReportBridgeState(false, { source_event: 'dom_sosexy_disconnected' });
    if (aiDomMode) aiDomScenePause();
  });
  toyServer = await toyDevice.gatt.connect();
  const svc = await toyServer.getPrimaryService(TOY_SERVICE_UUID);
  toyWriteChar = await svc.getCharacteristic(TOY_WRITE_UUID);
  try { const nc = await svc.getCharacteristic(TOY_NOTIFY_UUID); await nc.startNotifications(); } catch(e) {}
  toyConnected = true;
  aiDomRefreshUI();
  aiDomLog('SOSEXY 已连接 ♡', 'sys');
  toyReportBridgeState(true, { source_event: 'dom_sosexy_connected' });
  if (aiDomMode) aiDomSceneResume();
}

async function soDisconnect() {
  if (window.AionBle && window.AionBle.isConnected()) window.AionBle.disconnect();
  else if (toyDevice && toyDevice.gatt && toyDevice.gatt.connected) toyDevice.gatt.disconnect();
  toyConnected = false; toyWriteChar = null;
  toyReportBridgeState(false, { source_event: 'dom_sosexy_manual_disconnect' });
}

// payload = {v, s}, 每档 0-10。v→震动(马达0)，s→吮吸(马达2)，电流(马达1)保持 0
async function soPlay(payload) {
  const vv = Math.round(Math.max(0, Math.min(10, (payload && payload.v) || 0)));
  const ss = Math.round(Math.max(0, Math.min(10, (payload && payload.s) || 0)));
  if (vv === 0 && ss === 0) return toySendData2(toyBuildStopCmd());
  if (!await toySendData2(toyBuildDualCmd(TOY_MOTORS[0].modeSpec, 1, TOY_MOTORS[0].gearsSpec, vv * 10))) return false;
  await toySleep(40);
  if (!await toySendData2(toyBuildDualCmd(TOY_MOTORS[1].modeSpec, 1, TOY_MOTORS[1].gearsSpec, 0))) return false;
  await toySleep(40);
  return await toySendData2(toyBuildDualCmd(TOY_MOTORS[2].modeSpec, 1, TOY_MOTORS[2].gearsSpec, ss * 10));
}

// ── CX492B 连接/发送（原生桥 configure 或 Web Bluetooth） ──
async function cxConnect() {
  const d = AI_DOM_DEVICES.cx492b.native;
  if (window.AionBle) {
    if (window.AionBle.configure) window.AionBle.configure(JSON.stringify(d));
    window.AionBle.connect();
    return;
  }
  if (!navigator.bluetooth) { aiDomLog('浏览器不支持 Web Bluetooth', 'err'); throw new Error('no_bt'); }
  cxDevice = await navigator.bluetooth.requestDevice({
    filters: [{ namePrefix: 'CX492B' }], optionalServices: [CX_SERVICE_UUID]
  });
  cxDevice.addEventListener('gattserverdisconnected', () => {
    cxConnected = false; cxWriteChar = null; aiDomRefreshUI();
    toyReportBridgeState(false, { source_event: 'dom_cx492b_disconnected' });
    if (aiDomMode) aiDomScenePause();
  });
  cxServer = await cxDevice.gatt.connect();
  const svc = await cxServer.getPrimaryService(CX_SERVICE_UUID);
  cxWriteChar = await svc.getCharacteristic(CX_WRITE_UUID);
  try { const nc = await svc.getCharacteristic(CX_NOTIFY_UUID); await nc.startNotifications(); } catch(e) {}
  cxConnected = true;
  aiDomRefreshUI();
  aiDomLog('CX492B 已连接 ♡', 'sys');
  toyReportBridgeState(true, { source_event: 'dom_cx492b_connected' });
  if (aiDomMode) aiDomSceneResume();
}

async function cxDisconnect() {
  if (window.AionBle && window.AionBle.isConnected()) window.AionBle.disconnect();
  else if (cxDevice && cxDevice.gatt && cxDevice.gatt.connected) cxDevice.gatt.disconnect();
  cxConnected = false; cxWriteChar = null;
  toyReportBridgeState(false, { source_event: 'dom_cx492b_manual_disconnect' });
}

async function cxSend(bytes) {
  if (window.AionBle && window.AionBle.isConnected()) {
    const hex = bytes.map(b => b.toString(16).padStart(2, '0')).join('');
    try {
      window.AionBle.sendData(hex);
      return true;
    } catch (e) {
      aiDomLog('CX 写入失败: ' + (e.message || e), 'err');
      return false;
    }
  }
  if (!cxWriteChar) return false;
  const arr = new Uint8Array(bytes);
  try { await cxWriteChar.writeValueWithoutResponse(arr); return true; }
  catch(e) {
    try { await cxWriteChar.writeValue(arr); return true; }
    catch(e2) { aiDomLog('CX 写入失败: ' + (e2.message || e2), 'err'); return false; }
  }
}

// CX492B 双通道：payload={v,s}。v=震动(CMD 0x03, mode 1, intensity v), s=吮吸(CMD 0x09, mode 1, intensity s)
async function cxPlay(payload) {
  const vv = Math.round(Math.max(0, Math.min(10, (payload && payload.v) || 0)));
  const ss = Math.round(Math.max(0, Math.min(10, (payload && payload.s) || 0)));
  const vibOk = vv <= 0
    ? await cxSend([0x55, 0x03, 0x00, 0x00, 0x00, 0x00, 0x00])
    : await cxSend([0x55, 0x03, 0x00, 0x00, 1, vv, 0x00]);
  if (!vibOk) return false;
  await toySleep(60);
  return ss <= 0
    ? await cxSend([0x55, 0x09, 0x00, 0x00, 0x00, 0x00, 0x00])
    : await cxSend([0x55, 0x09, 0x00, 0x00, 1, ss, 0x00]);
}

// ── 失控系列（sk30/sk40）：通过 AionAdv 广播桥控制 ──
let skConnected = false;

async function skConnect(devKey) {
  if (!window.AionAdv) { aiDomLog('需要 Obsidian Vow Android App（广播桥不支持浏览器）', 'err'); throw new Error('no_adv'); }
  if (!window.AionAdv.isSupported()) { aiDomLog('此设备不支持 BLE 广播', 'err'); throw new Error('no_adv_hw'); }
  const advDev = AI_DOM_DEVICES[devKey].advDevice;
  window.AionAdv.configure(JSON.stringify({ device: advDev }));
  skConnected = true;
  if (typeof aiDomRefreshUI === 'function') aiDomRefreshUI();
  if (typeof aiDomLog === 'function') aiDomLog(AI_DOM_DEVICES[devKey].label + ' 就绪 ♡', 'sys');
  if (typeof toyNativeBle !== 'undefined') toyNativeBle.onConnected();
}

async function skDisconnect() {
  if (window.AionAdv) window.AionAdv.disconnect();
  skConnected = false;
}

async function skPlay(payload) {
  if (!window.AionAdv) return false;
  const vv = Math.round(Math.max(0, Math.min(10, (payload && payload.v) || 0)));
  const ss = Math.round(Math.max(0, Math.min(10, (payload && payload.s) || 0)));
  if (vv === 0 && ss === 0) return skStop();
  window.AionAdv.play(JSON.stringify({ v: vv, s: ss }));
  return true;
}

async function skStop() {
  if (!window.AionAdv) return false;
  window.AionAdv.stop();
  return true;
}

function aiDomStopToy(reason) {
  if (window.ControlEmergencyStop?.stopDevice) {
    return ControlEmergencyStop.stopDevice(reason || 'ai_dom_stop');
  }
  try {
    if (toyDriver.active) return toyDriver.stop();
  } catch (e) {}
  try {
    if (window.AionAdv?.stop) {
      window.AionAdv.stop();
      return true;
    }
  } catch (e) {}
  return false;
}

// ── 统一驱动：按 active 分派 ──
const toyDriver = {
  active: null,
  _isAdv() { return this.active === 'sk30' || this.active === 'sk40'; },
  isConnected() {
    if (!this.active) return false;
    if (this._isAdv()) return skConnected;
    if (this.active === 'sosexy') return !!(toyConnected || (window.AionBle && window.AionBle.isConnected()));
    if (this.active === 'cx492b') return !!(cxConnected  || (window.AionBle && window.AionBle.isConnected()));
    return false;
  },
  async connect(id) {
    if (this.active && this.active !== id && this.isConnected()) await this.disconnect();
    this.active = id;
    if (id === 'sosexy') return soConnect();
    if (id === 'cx492b') return cxConnect();
    if (id === 'sk30' || id === 'sk40') return skConnect(id);
  },
  async disconnect() {
    const id = this.active;
    this.active = null;
    if (id === 'sosexy') return soDisconnect();
    if (id === 'cx492b') return cxDisconnect();
    if (id === 'sk30' || id === 'sk40') return skDisconnect();
  },
  async play(payload) {
    if (!this.active) return false;
    if (typeof payload === 'number') payload = { v: payload, s: Math.round(payload * 0.7) };
    if (this.active === 'sosexy') return soPlay(payload);
    if (this.active === 'cx492b') return cxPlay(payload);
    if (this._isAdv()) return skPlay(payload);
    return false;
  },
  async stop() {
    if (!this.active) return false;
    if (this.active === 'sosexy') return toySendData2(toyBuildStopCmd());
    if (this.active === 'cx492b') {
      if (!await cxSend([0x55, 0x03, 0x00, 0x00, 0x00, 0x00, 0x00])) return false;
      await toySleep(60);
      return await cxSend([0x55, 0x09, 0x00, 0x00, 0x00, 0x00, 0x00]);
    }
    if (this._isAdv()) return skStop();
    return false;
  }
};

// ══════════════════════════════════════════════════════════
// ── Scene 引擎：SCENE / HOLD / SPIKE / EDGE / DENY / REWARD / PUNISH / STOP ──
// ══════════════════════════════════════════════════════════

// 生成器 yield { v: 0-10, s: 0-10, hold: ms }
// v=震动(直给), s=吮吸(包裹)。尽量非对称，让两条通道对位/错位。
const AI_DOM_SCENES = {
  warmup: { label: '渐入', gen: function*() {
    for (let v = 1; v <= 3; v++) yield { v, s: 0, hold: 3500 };
    yield { v: 3, s: 1, hold: 4000 };
    yield { v: 4, s: 1, hold: 4000 };
    yield { v: 4, s: 2, hold: 4000 };
    while (true) yield { v: 4, s: 3, hold: 5000 + Math.random() * 2000 };
  }},
  tease: { label: '挑逗', gen: function*() {
    while (true) {
      const r = Math.random();
      if (r < 0.18) yield { v: 0, s: 0, hold: 1800 + Math.random() * 2500 };
      else if (r < 0.42) yield { v: 3 + Math.floor(Math.random()*2), s: 0, hold: 2000 + Math.random()*2500 };
      else if (r < 0.66) yield { v: 0, s: 3 + Math.floor(Math.random()*2), hold: 2000 + Math.random()*2500 };
      else if (r < 0.86) yield { v: 4, s: 2, hold: 2500 };
      else yield { v: 2, s: 5, hold: 2500 };
    }
  }},
  edge: { label: '边缘', gen: function*() {
    while (true) {
      for (let lv = 3; lv <= 8; lv++) yield { v: lv, s: Math.max(1, lv - 3), hold: 1100 + Math.random()*400 };
      yield { v: 8, s: 6, hold: 3000 + Math.random()*1500 };
      yield { v: 1, s: 0, hold: 2500 + Math.random()*2500 };
    }
  }},
  intense: { label: '高强', gen: function*() {
    while (true) {
      const v = 6 + Math.floor(Math.random()*4);
      const s = 5 + Math.floor(Math.random()*5);
      yield { v, s, hold: 2200 + Math.random()*1800 };
      const r = Math.random();
      if (r < 0.22) yield { v: 0, s: Math.max(1, s - 2), hold: 1500 };
      else if (r < 0.40) yield { v, s: 0, hold: 1200 };
    }
  }},
  soothe: { label: '安抚', gen: function*() {
    const steps = [{v:4,s:4},{v:3,s:3},{v:2,s:2},{v:1,s:1}];
    for (const p of steps) yield { ...p, hold: 3500 };
    while (true) yield { v: 0, s: 0, hold: 8000 };
  }}
};

let aiDomScene = { name: null, gen: null, timer: null, spikeTimer: null, denyTimer: null, last: { v: 0, s: 0 }, startAt: 0 };
let aiDomSceneSuspended = false;  // BLE 断线期间保留 gen/last，重连后续跑
let aiDomFidgetTimer = null;      // 空窗期自主小骚扰定时器

// 最近指令历史（注入 AI prompt）
const AI_DOM_HISTORY_MAX = 5;
const aiDomHistory = [];
function aiDomHistoryPush(cmd) {
  const c = (cmd || '').trim();
  if (!c || c.toUpperCase() === 'STOP' && aiDomHistory.length && aiDomHistory[aiDomHistory.length-1].cmd.toUpperCase() === 'STOP') return;
  aiDomHistory.push({ cmd: c, at: Date.now() });
  while (aiDomHistory.length > AI_DOM_HISTORY_MAX) aiDomHistory.shift();
}
function aiDomHistorySnapshot() {
  const now = Date.now();
  return aiDomHistory.map(x => `${x.cmd}(${Math.max(0, Math.floor((now - x.at)/1000))}s前)`);
}
function aiDomHistoryClear() { aiDomHistory.length = 0; }

// ── 信号采集 + 本地反射：抗拒词立即 SPIKE、短答累计推顺从 ──
// 返回 { resist_hits, short_streak, compliance_streak } 快照给 sendBody
function aiDomSignalScan(text) {
  const t = String(text || '');
  const stripped = t.trim();
  const isShort = stripped.length > 0 && stripped.length < 5;
  // 排除 safeword
  const sw = (aiDomSafeword || '').trim().toLowerCase();
  const isSafe = sw && t.toLowerCase().includes(sw);
  let resistHits = 0;
  if (!isSafe) {
    const m = t.match(new RegExp(AI_DOM_RESIST_RE.source, 'g'));
    resistHits = m ? m.length : 0;
  }
  if (resistHits > 0) {
    aiDomShortStreak = 0;
    aiDomComplianceStreak = 0;
    // 本地反射：1 秒内 v+1/s+1 SPIKE
    if (aiDomMode && !aiDomSceneSuspended && toyDriver.isConnected()
        && !aiDomScene.denyTimer && !aiDomScene.spikeTimer) {
      const prev = { v: aiDomScene.last.v || 0, s: aiDomScene.last.s || 0 };
      const v = Math.min(10, prev.v + 1);
      const s = Math.min(10, prev.s + 1);
      if (aiDomScene.timer) { clearTimeout(aiDomScene.timer); aiDomScene.timer = null; }
      toyDriver.play({ v, s });
      aiDomScene.last = { v, s };
      aiDomScene.spikeTimer = setTimeout(() => {
        aiDomScene.spikeTimer = null;
        aiDomScene.last = prev;
        toyDriver.play(prev);
        if (aiDomScene.gen && !aiDomScene.timer) aiDomSceneTick();
      }, 1000);
    }
  } else if (isShort) {
    aiDomShortStreak += 1;
    aiDomComplianceStreak += 1;
  } else if (stripped.length > 0) {
    aiDomShortStreak = 0;
    aiDomComplianceStreak += 1;
  }
  aiDomLastResistHits = resistHits;
  // 倔强检测：有示弱词 → 重置；无 → 累加
  const hasYield = AI_DOM_YIELD_RE.test(t);
  if (hasYield) {
    aiDomStubbornStreak = 0;
  } else if (stripped.length > 0) {
    aiDomStubbornStreak += 1;
  }
  // BREAK 服从检测
  if (aiDomBreakActive && stripped.length > 0) {
    const demand = aiDomBreakActive.demand.toLowerCase();
    const reply = stripped.toLowerCase();
    const hasKeywords = demand.split('').filter(c => c.trim()).some(c => reply.includes(c));
    const yieldish = AI_DOM_YIELD_RE.test(reply) || reply.length > 3;
    if (hasKeywords || yieldish) {
      aiDomBreakComply();
    } else {
      aiDomDebtAdd(1);
    }
  }
  return {
    resist_hits: resistHits,
    short_streak: aiDomShortStreak,
    compliance_streak: aiDomComplianceStreak,
  };
}

function aiDomBuildSendContext(text) {
  const sig = aiDomSignalScan(text);
  const now = Date.now();
  const sessionElapsed = aiDomSessionStartAt ? Math.floor((now - aiDomSessionStartAt) / 1000) : 0;
  const sceneElapsed = aiDomScene.startAt ? Math.floor((now - aiDomScene.startAt) / 1000) : 0;
  const sinceLastPunish = aiDomLastPunishAt ? Math.floor((now - aiDomLastPunishAt) / 1000) : null;
  const replyDelayMs = aiDomLastAiDoneAt ? Math.max(0, now - aiDomLastAiDoneAt) : 0;
  aiDomLastReplyDelayMs = replyDelayMs;
  return {
    cnc_enabled: aiDomCncEnabled,
    cnc_weakness: aiDomCncWeakness || [],
    resist_hits: sig.resist_hits,
    short_streak: sig.short_streak,
    compliance_streak: sig.compliance_streak,
    reply_delay_ms: replyDelayMs,
    session_elapsed: sessionElapsed,
    scene_name: aiDomScene.name || null,
    scene_elapsed: sceneElapsed,
    since_last_punish: sinceLastPunish,
    ratchet_valley: aiDomRatchetValley,
    debt: aiDomDebt,
    stubborn_streak: aiDomStubbornStreak,
  };
}

function aiDomBuildControlSnapshot() {
  const now = Date.now();
  const toyOnline = (typeof toyBridgeIsConnected === 'function')
    ? toyBridgeIsConnected()
    : !!(toyDriver && toyDriver.isConnected && toyDriver.isConnected());
  return {
    safeword_set: !!aiDomSafeword,
    toy_connected: !!toyOnline,
    dom_history: aiDomHistorySnapshot(),
    cnc_enabled: aiDomCncEnabled,
    cnc_weakness: aiDomCncWeakness || [],
    resist_hits: aiDomLastResistHits,
    short_streak: aiDomShortStreak,
    reply_delay_ms: aiDomLastReplyDelayMs,
    compliance_streak: aiDomComplianceStreak,
    session_elapsed: aiDomSessionStartAt ? Math.floor((now - aiDomSessionStartAt) / 1000) : 0,
    scene_name: aiDomScene.name || null,
    scene_elapsed: aiDomScene.startAt ? Math.floor((now - aiDomScene.startAt) / 1000) : 0,
    since_last_punish: aiDomLastPunishAt ? Math.floor((now - aiDomLastPunishAt) / 1000) : null,
    ratchet_valley: aiDomRatchetValley,
    debt: aiDomDebt,
    stubborn_streak: aiDomStubbornStreak,
  };
}

function aiDomSceneClearTimers() {
  if (aiDomScene.timer) { clearTimeout(aiDomScene.timer); aiDomScene.timer = null; }
  if (aiDomScene.spikeTimer) { clearTimeout(aiDomScene.spikeTimer); aiDomScene.spikeTimer = null; }
  if (aiDomScene.denyTimer) { clearTimeout(aiDomScene.denyTimer); aiDomScene.denyTimer = null; }
}

function aiDomSceneStop() {
  aiDomSceneClearTimers();
  aiDomScene.name = null;
  aiDomScene.gen = null;
  aiDomScene.startAt = 0;
  aiDomSceneSuspended = false;
  // last 保留：DENY / REWARD 等原语需要读取前一状态
}

// ── BLE 断线 / 重连：暂停 & 续跑（保留 gen/last） ──
function aiDomScenePause() {
  if (!aiDomScene.gen && !aiDomScene.spikeTimer && !aiDomScene.denyTimer) return;
  aiDomSceneClearTimers();
  aiDomSceneSuspended = true;
  aiDomLog('蓝牙断开 · 场景暂停', 'err');
}

function aiDomSceneResume() {
  if (!aiDomSceneSuspended) return;
  aiDomSceneSuspended = false;
  if (!toyDriver.isConnected()) return;
  aiDomLog('蓝牙恢复 · 续跑', 'sys');
  // 先把身体拉回断前状态
  const prev = { v: aiDomScene.last.v || 0, s: aiDomScene.last.s || 0 };
  toyDriver.play(prev);
  // 再让生成器在下一拍继续
  if (aiDomScene.gen) {
    aiDomScene.timer = setTimeout(aiDomSceneTick, 1200);
  }
}

// ── 延迟补偿 1：发送时的瞬时微反馈，告诉身体"你被听见了" ──
function aiDomInstantTap() {
  if (!aiDomMode || aiDomSceneSuspended || !toyDriver.isConnected()) return;
  if (aiDomScene.denyTimer || aiDomScene.spikeTimer) return;  // 尊重剥夺/脉冲的独占
  const prev = { v: aiDomScene.last.v || 0, s: aiDomScene.last.s || 0 };
  const bump = { v: Math.min(10, prev.v + 1), s: Math.min(10, prev.s + 1) };
  toyDriver.play(bump);
  setTimeout(() => {
    if (!aiDomMode || aiDomSceneSuspended) return;
    if (aiDomScene.denyTimer || aiDomScene.spikeTimer) return;
    toyDriver.play(prev);
  }, 350);
}

// ── 延迟补偿 2：AI 沉默期的自主骚扰，让空窗像在被观察 ──
function aiDomFidgetStart() {
  aiDomFidgetStop();
  const schedule = () => {
    const wait = 8000 + Math.random() * 7000;  // 8~15s
    aiDomFidgetTimer = setTimeout(() => {
      aiDomFidgetTimer = null;
      if (!aiDomMode) return;
      if (aiDomSceneSuspended || !toyDriver.isConnected()) { schedule(); return; }
      if (aiDomScene.denyTimer || aiDomScene.spikeTimer) { schedule(); return; }
      // 只在有场景跑、30% 概率出手
      if (aiDomScene.gen && Math.random() < 0.3) {
        const prev = { v: aiDomScene.last.v || 0, s: aiDomScene.last.s || 0 };
        const r = Math.random();
        let bump;
        if (r < 0.4) bump = { v: Math.min(10, prev.v + 1), s: prev.s };
        else if (r < 0.8) bump = { v: prev.v, s: Math.min(10, prev.s + 1) };
        else bump = { v: Math.min(10, prev.v + 1), s: Math.min(10, prev.s + 1) };
        toyDriver.play(bump);
        setTimeout(() => {
          if (!aiDomMode || aiDomSceneSuspended) return;
          if (aiDomScene.denyTimer || aiDomScene.spikeTimer) return;
          toyDriver.play(prev);
        }, 250 + Math.random() * 200);
      }
      schedule();
    }, wait);
  };
  schedule();
}
function aiDomFidgetStop() {
  if (aiDomFidgetTimer) { clearTimeout(aiDomFidgetTimer); aiDomFidgetTimer = null; }
}

function aiDomSceneTick() {
  if (!aiDomScene.gen) return;
  const it = aiDomScene.gen.next();
  if (it.done) { aiDomScene.gen = null; aiDomScene.name = null; return; }
  const v = Math.max(0, Math.min(10, it.value.v || 0));
  const s = Math.max(0, Math.min(10, it.value.s || 0));
  const hold = Math.max(300, it.value.hold || 3000);
  aiDomScene.last = { v, s };
  toyDriver.play({ v, s });
  aiDomScene.timer = setTimeout(aiDomSceneTick, hold);
}

function aiDomSceneStart(name) {
  aiDomSceneStop();
  const def = AI_DOM_SCENES[name];
  if (!def) { aiDomLog('未知场景: ' + name, 'err'); return; }
  aiDomScene.name = name;
  aiDomScene.gen = def.gen();
  aiDomScene.startAt = Date.now();
  aiDomLog('场景 · ' + def.label, 'sys');
  aiDomSceneTick();
}

// ── Dom 原语：EDGE / DENY / REWARD / PUNISH ──

function aiDomRunCustomGen(name, label, genFn) {
  aiDomSceneStop();
  aiDomScene.name = name;
  aiDomScene.gen = genFn();
  aiDomScene.startAt = Date.now();
  aiDomLog(label, 'sys');
  aiDomSceneTick();
}

function aiDomEdgeRun() {
  const fl = aiDomRatchetFloor();
  aiDomRunCustomGen('EDGE', '边缘控制', function*() {
    const cycles = 3 + Math.floor(Math.random() * 3);
    for (let i = 0; i < cycles; i++) {
      const isLast = i === cycles - 1;
      for (let lv = Math.max(4, fl); lv <= 7; lv++) {
        yield { v: lv, s: Math.max(1, lv - 3), hold: 750 + Math.random() * 300 };
      }
      yield { v: 7, s: 4, hold: 900 + Math.random() * 300 };
      const peak = 8 + Math.floor(Math.random() * 3);
      for (let lv = 8; lv <= peak; lv++) {
        yield { v: lv, s: Math.max(4, lv - 3), hold: 380 };
      }
      const holdPeak = Math.random() < 0.2 ? 3600 + Math.random() * 500 : 1300 + Math.random() * 400;
      yield { v: peak, s: Math.max(4, peak - 3), hold: holdPeak };
      if (isLast) {
        yield { v: fl, s: 0, hold: 800 };
        yield { v: 10, s: 10, hold: 1800 + Math.random() * 500 };
        yield { v: Math.max(fl, 1), s: 1, hold: 1200 };
      } else {
        const valley = Math.max(fl, 2);
        yield { v: fl, s: valley, hold: 1200 };
        for (let k = 0; k < 3; k++) {
          yield { v: Math.max(fl, Math.floor(Math.random() * 4)), s: valley, hold: 550 + Math.random() * 400 };
        }
      }
    }
    aiDomRatchetBump();
    aiDomCheckPostO();
    const resid = Math.max(fl, 3);
    while (true) yield { v: resid, s: Math.max(fl, 2), hold: 5500 };
  });
}

function aiDomPunishRun() {
  aiDomLastPunishAt = Date.now();
  aiDomRunCustomGen('PUNISH', '惩罚', function*() {
    // 随机抽 2~3 个模块组合
    const modules = ['staccato', 'isolate', 'heartbeat', 'slow'];
    const picks = [];
    const pickCount = 2 + Math.floor(Math.random() * 2);
    while (picks.length < pickCount) {
      const m = modules[Math.floor(Math.random() * modules.length)];
      if (!picks.includes(m)) picks.push(m);
    }
    for (const m of picks) {
      if (m === 'staccato') {
        // 断续挑逗：on 0.3 / off 0.8 × 8，永远进入不了状态
        for (let k = 0; k < 8; k++) {
          const lv = 3 + Math.floor(Math.random() * 2);
          yield { v: lv, s: Math.max(1, lv - 1), hold: 300 };
          yield { v: 0, s: 0, hold: 800 };
        }
      } else if (m === 'isolate') {
        // 单通道孤立：缺一条腿的刺激
        if (Math.random() < 0.5) yield { v: 5, s: 0, hold: 10000 };
        else yield { v: 0, s: 5, hold: 10000 };
      } else if (m === 'heartbeat') {
        // 心跳低档：双通道 2 档节奏性抠心
        for (let k = 0; k < 6; k++) {
          yield { v: 2, s: 2, hold: 700 };
          yield { v: 0, s: 0, hold: 900 };
        }
      } else if (m === 'slow') {
        // 错频点触：慢到发疯
        for (let k = 0; k < 4; k++) {
          yield { v: 1, s: 0, hold: 300 };
          yield { v: 0, s: 0, hold: 2200 };
        }
      }
    }
    // 终局陷阱：装稳定 → 突袭 → 再落下（尊重棘轮）
    const fl = aiDomRatchetFloor();
    yield { v: Math.max(fl, 1), s: Math.max(fl, 1), hold: 3000 };
    yield { v: 10, s: 10, hold: 1500 };
    aiDomRatchetBump();
    const resid = Math.max(fl, 3);
    while (true) yield { v: resid, s: Math.max(fl, 2), hold: 5500 };
  });
}

function aiDomRewardRun(target) {
  const cur = { v: aiDomScene.last.v || 0, s: aiDomScene.last.s || 0 };
  const endV = Math.max(1, Math.min(10, target));
  const endS = Math.max(1, Math.round(endV * 0.7));
  // 击穿奖赏：CNC 开启 + 软肋非空 时 30% 概率在 REWARD 后期对软肋方向甩一记 SPIKE
  if (aiDomCncEnabled && (aiDomCncWeakness || []).length && Math.random() < 0.3) {
    const delay = 3000 + Math.floor(Math.random() * 2000);
    setTimeout(() => {
      if (!aiDomMode || aiDomSceneSuspended || !toyDriver.isConnected()) return;
      if (aiDomScene.name !== 'REWARD') return;  // 中途换场就取消
      if (aiDomScene.denyTimer || aiDomScene.spikeTimer) return;
      const prev = { v: aiDomScene.last.v || 0, s: aiDomScene.last.s || 0 };
      const v = Math.min(10, endV + 1 + Math.floor(Math.random() * 2));
      const s = Math.min(10, endS + 1 + Math.floor(Math.random() * 2));
      const sec = 3 + Math.floor(Math.random() * 3);
      if (aiDomScene.timer) { clearTimeout(aiDomScene.timer); aiDomScene.timer = null; }
      toyDriver.play({ v, s });
      aiDomScene.last = { v, s };
      aiDomLog(`击穿 · v${v}/s${s} · ${sec}秒`, 'sys');
      aiDomScene.spikeTimer = setTimeout(() => {
        aiDomScene.spikeTimer = null;
        aiDomScene.last = prev;
        toyDriver.play(prev);
        if (aiDomScene.gen && !aiDomScene.timer) aiDomSceneTick();
      }, sec * 1000);
    }, delay);
  }
  aiDomRunCustomGen('REWARD', `奖赏 · 到 ${endV}档`, function*() {
    // 爬升段
    const steps = Math.max(3, Math.abs(endV - cur.v));
    for (let i = 1; i <= steps; i++) {
      const t = i / steps;
      yield {
        v: Math.round(cur.v + (endV - cur.v) * t),
        s: Math.round(cur.s + (endS - cur.s) * t),
        hold: 900
      };
    }
    // 呼吸 + 陷阱，共约 6 组
    for (let i = 0; i < 6; i++) {
      // 抽离陷阱：15% 概率全停 1 秒，教会 "奖赏从不安全"
      if (Math.random() < 0.15) yield { v: 0, s: 0, hold: 1000 };
      // 过载陷阱：高档奖赏时 25% 概率越界 1~2 档
      if (endV >= 7 && Math.random() < 0.25) {
        const over = Math.min(10, endV + 1 + Math.floor(Math.random() * 2));
        yield { v: over, s: Math.round(over * 0.7), hold: 2400 + Math.random() * 700 };
      }
      // 呼吸：v、s 半拍错位
      yield { v: endV, s: Math.max(1, endS - 1), hold: 1400 };
      yield { v: Math.max(1, endV - 1), s: endS, hold: 1400 };
      yield { v: endV, s: endS, hold: 1200 };
      // 礼物脉冲：30% 概率 +1 闪一下
      if (Math.random() < 0.3) {
        yield { v: Math.min(10, endV + 1), s: Math.min(10, endS + 1), hold: 400 + Math.random() * 200 };
      }
    }
    // 余味档：target-1（尊重棘轮）
    const fl = aiDomRatchetFloor();
    const residV = Math.max(fl, endV - 1);
    const residS = Math.max(fl, endS - 1);
    aiDomRatchetBump();
    aiDomCheckPostO();
    while (true) yield { v: residV, s: residS, hold: 5000 + Math.random() * 2000 };
  });
}

function aiDomTeaseRun(sec) {
  aiDomRunCustomGen('TEASE', `挑逗 · ${sec}秒`, function*() {
    const end = Date.now() + sec * 1000;
    while (Date.now() < end) {
      const ch = Math.random();
      const lv = 1 + Math.floor(Math.random() * 3);  // 1~3 档
      const dur = 200 + Math.floor(Math.random() * 300);
      if (ch < 0.4) yield { v: lv, s: 0, hold: dur };
      else if (ch < 0.8) yield { v: 0, s: lv, hold: dur };
      else yield { v: lv, s: Math.max(1, lv - 1), hold: dur };
      yield { v: 0, s: 0, hold: 2000 + Math.floor(Math.random() * 4000) };
    }
    const fl = aiDomRatchetFloor();
    while (true) yield { v: fl, s: fl, hold: 6000 };
  });
}

function aiDomOverloadRun(sec) {
  aiDomRunCustomGen('OVERLOAD', `过载 · ${sec}秒`, function*() {
    yield { v: 10, s: 10, hold: sec * 1000 };
    const fl = aiDomRatchetFloor();
    yield { v: Math.max(fl, 4), s: Math.max(fl, 3), hold: 2500 };
    yield { v: Math.max(fl, 3), s: Math.max(fl, 2), hold: 2500 };
    aiDomRatchetBump();
    aiDomCheckPostO();
    while (true) yield { v: Math.max(fl, 2), s: Math.max(fl, 2), hold: 6000 };
  });
}

function aiDomDenyRun(sec) {
  aiDomLog(`剥夺 · ${sec}秒`, 'sys');
  const resumeGen = aiDomScene.gen;
  const prev = { v: aiDomScene.last.v || 0, s: aiDomScene.last.s || 0 };
  if (aiDomScene.timer) { clearTimeout(aiDomScene.timer); aiDomScene.timer = null; }
  if (aiDomScene.denyTimer) clearTimeout(aiDomScene.denyTimer);
  toyDriver.play({ v: 0, s: 0 });
  aiDomScene.last = { v: 0, s: 0 };
  aiDomScene.denyTimer = setTimeout(() => {
    aiDomScene.denyTimer = null;
    if (!aiDomMode) return;
    if (resumeGen && aiDomScene.gen === resumeGen) {
      aiDomSceneTick();
    } else {
      aiDomScene.last = prev;
      toyDriver.play(prev);
    }
  }, sec * 1000);
}

// ── Post-O 猎杀：高潮后不停 ──
function aiDomCheckPostO() {
  const now = Date.now();
  if (aiDomPostOCooldown && now - aiDomPostOCooldown < 60000) return;
  aiDomPostOCooldown = now;
  setTimeout(() => {
    if (!aiDomMode || aiDomSceneSuspended || !toyDriver.isConnected()) return;
    if (aiDomScene.name === 'SIEGE' || aiDomScene.name === 'GRIND') return;
    const fl = aiDomRatchetFloor();
    aiDomLog('Post-O 追击', 'sys');
    aiDomRunCustomGen('SIEGE', 'Post-O 追击', function*() {
      yield { v: 0, s: 0, hold: 3000 };
      const pv = Math.max(fl, 8), ps = Math.max(fl, 7);
      while (true) {
        const jv = pv + (Math.random() < 0.3 ? (Math.random() < 0.5 ? 1 : -1) : 0);
        const js = ps + (Math.random() < 0.3 ? (Math.random() < 0.5 ? 1 : -1) : 0);
        yield { v: Math.max(fl, Math.min(10, jv)), s: Math.max(fl, Math.min(10, js)), hold: 2000 + Math.random() * 3000 };
      }
    });
  }, 3000);
}

// ── BREAK 服从处理 ──
function aiDomBreakComply() {
  if (!aiDomBreakActive) return;
  if (aiDomBreakActive.escalateTimer) clearInterval(aiDomBreakActive.escalateTimer);
  aiDomBreakActive = null;
  aiDomDebtAdd(-1);
  aiDomLog('逼供·服从', 'sys');
  const fl = aiDomRatchetFloor();
  aiDomRewardRun(Math.max(fl + 2, 7));
}

// ── v2 新指令：GRIND / SIEGE / HUNT / SHATTER / TRAP ──

function aiDomGrindRun(v, s, sec) {
  const dur = aiDomGrindDuration(sec);
  aiDomRunCustomGen('GRIND', `碾磨 · v${v}/s${s} · ${dur}秒`, function*() {
    const end = Date.now() + dur * 1000;
    while (Date.now() < end) {
      const jv = v + (Math.random() < 0.25 ? (Math.random() < 0.5 ? 1 : -1) : 0);
      const js = s + (Math.random() < 0.25 ? (Math.random() < 0.5 ? 1 : -1) : 0);
      yield { v: Math.max(0, Math.min(10, jv)), s: Math.max(0, Math.min(10, js)), hold: 2000 + Math.random() * 2000 };
    }
    aiDomRatchetBump();
    const fl = aiDomRatchetFloor();
    while (true) yield { v: Math.max(fl, v - 1), s: Math.max(fl, s - 1), hold: 5000 };
  });
}

function aiDomSiegeRun(v, s) {
  aiDomRunCustomGen('SIEGE', `围城 · v${v}/s${s}`, function*() {
    while (true) {
      const jv = v + (Math.random() < 0.3 ? (Math.random() < 0.5 ? 1 : -1) : 0);
      const js = s + (Math.random() < 0.3 ? (Math.random() < 0.5 ? 1 : -1) : 0);
      yield { v: Math.max(0, Math.min(10, jv)), s: Math.max(0, Math.min(10, js)), hold: 2500 + Math.random() * 3000 };
    }
  });
}

function aiDomHuntRun(cycles) {
  const fl = aiDomRatchetFloor();
  aiDomRunCustomGen('HUNT', `狩猎 · ${cycles}轮`, function*() {
    for (let i = 0; i < cycles; i++) {
      const isLast = i === cycles - 1;
      // 攀升：从 floor 推到 8-9
      for (let lv = Math.max(fl, 4); lv <= 8; lv++) {
        yield { v: lv, s: Math.max(1, lv - 2), hold: 800 + Math.random() * 400 };
      }
      // 推到峰值并持续
      const peak = 9 + Math.floor(Math.random() * 2);
      yield { v: peak, s: Math.max(5, peak - 3), hold: 2500 + Math.random() * 1500 };
      // 切断
      if (isLast && Math.random() < 0.6) {
        // 最后一轮 60% 放开
        yield { v: 10, s: 10, hold: 3000 + Math.random() * 2000 };
        aiDomCheckPostO();
      } else {
        // 切断回落到比上次谷底更高
        const valley = Math.max(fl, 2 + i);
        yield { v: valley, s: Math.max(1, valley - 1), hold: 3000 + Math.random() * 2000 };
      }
    }
    aiDomRatchetBump();
    const rfl = aiDomRatchetFloor();
    while (true) yield { v: Math.max(rfl, 3), s: Math.max(rfl, 2), hold: 5000 };
  });
}

function aiDomShatterRun(sec) {
  aiDomRunCustomGen('SHATTER', `粉碎 · ${sec}秒`, function*() {
    const end = Date.now() + sec * 1000;
    let gap = 500;
    while (Date.now() < end) {
      yield { v: 10, s: 0, hold: 1500 + Math.random() * 500 };
      yield { v: 0, s: 0, hold: Math.max(200, gap) };
      yield { v: 0, s: 10, hold: 1500 + Math.random() * 500 };
      yield { v: 0, s: 0, hold: Math.max(200, gap) };
      yield { v: 10, s: 10, hold: 2000 + Math.random() * 1000 };
      yield { v: 0, s: 0, hold: Math.max(150, gap * 0.5) };
      gap = Math.max(100, gap - 50);
    }
    aiDomRatchetBump();
    const fl = aiDomRatchetFloor();
    yield { v: Math.max(fl, 4), s: Math.max(fl, 3), hold: 3000 };
    while (true) yield { v: Math.max(fl, 3), s: Math.max(fl, 2), hold: 5000 };
  });
}

function aiDomTrapRun(type) {
  const fl = aiDomRatchetFloor();
  if (type === 'mercy') {
    aiDomRunCustomGen('TRAP', '陷阱·假慈悲', function*() {
      // 假放松
      yield { v: Math.max(fl, 2), s: Math.max(fl, 1), hold: 2500 + Math.random() * 1500 };
      yield { v: Math.max(fl, 1), s: Math.max(fl, 1), hold: 2500 + Math.random() * 1000 };
      // 突袭
      yield { v: 10, s: 10, hold: 3000 + Math.random() * 2000 };
      yield { v: 9, s: 8, hold: 2000 };
      aiDomRatchetBump();
      const nfl = aiDomRatchetFloor();
      while (true) yield { v: Math.max(nfl, 5), s: Math.max(nfl, 4), hold: 4000 };
    });
  } else if (type === 'soothe') {
    aiDomRunCustomGen('TRAP', '陷阱·假安抚', function*() {
      // 模仿下降曲线
      const cur = aiDomScene.last.v || 5;
      for (let lv = cur; lv >= Math.max(fl, cur - 3); lv--) {
        yield { v: lv, s: Math.max(fl, lv - 1), hold: 2000 };
      }
      // 半路反转：比之前高 2 档
      for (let lv = Math.max(fl, cur - 2); lv <= Math.min(10, cur + 2); lv++) {
        yield { v: lv, s: Math.max(fl, lv - 1), hold: 600 };
      }
      const peak = Math.min(10, cur + 2);
      yield { v: peak, s: Math.max(fl, peak - 2), hold: 3000 + Math.random() * 2000 };
      aiDomRatchetBump();
      const nfl = aiDomRatchetFloor();
      while (true) yield { v: Math.max(nfl, peak - 1), s: Math.max(nfl, peak - 2), hold: 5000 };
    });
  } else if (type === 'reward') {
    aiDomRunCustomGen('TRAP', '陷阱·假奖赏', function*() {
      // 给舒服的上升节奏
      for (let lv = Math.max(fl, 4); lv <= 8; lv++) {
        yield { v: lv, s: Math.max(fl, Math.round(lv * 0.7)), hold: 1200 + Math.random() * 600 };
      }
      yield { v: 8, s: 6, hold: 2000 + Math.random() * 1000 };
      // 快到时切断
      yield { v: 0, s: 0, hold: 800 };
      // DENY 效果
      yield { v: fl, s: fl, hold: 5000 + Math.random() * 3000 };
      aiDomDebtAdd(0.3);
      while (true) yield { v: Math.max(fl, 2), s: Math.max(fl, 1), hold: 6000 };
    });
  }
}

function aiDomBreakRun(demand) {
  // 清理之前的 BREAK
  if (aiDomBreakActive?.escalateTimer) clearInterval(aiDomBreakActive.escalateTimer);
  const fl = aiDomRatchetFloor();
  let currentLv = Math.max(fl, 5);
  aiDomSceneStop();
  toyDriver.play({ v: currentLv, s: Math.round(currentLv * 0.7) });
  aiDomScene.last = { v: currentLv, s: Math.round(currentLv * 0.7) };
  aiDomLog(`逼供 · 「${demand}」`, 'sys');
  const escalateTimer = setInterval(() => {
    if (!aiDomMode || !aiDomBreakActive) { clearInterval(escalateTimer); return; }
    currentLv = Math.min(10, currentLv + 1);
    const s = Math.min(10, Math.round(currentLv * 0.7));
    toyDriver.play({ v: currentLv, s });
    aiDomScene.last = { v: currentLv, s };
    if (currentLv >= 10) {
      clearInterval(escalateTimer);
      aiDomBreakActive.escalateTimer = null;
      // 满档后转 GRIND
      aiDomRunCustomGen('BREAK', '逼供·满档碾磨', function*() {
        while (true) {
          yield { v: 10, s: 10, hold: 2000 + Math.random() * 2000 };
        }
      });
    }
  }, 10000);
  aiDomBreakActive = { demand, startAt: Date.now(), escalateTimer };
}

// ── DILEMMA（抉择）──
let _aiDomDilemmaTimer = null;
let _aiDomDilemmaCountdown = null;

function aiDomDilemmaCleanup() {
  if (_aiDomDilemmaTimer) { clearTimeout(_aiDomDilemmaTimer); _aiDomDilemmaTimer = null; }
  if (_aiDomDilemmaCountdown) { clearInterval(_aiDomDilemmaCountdown); _aiDomDilemmaCountdown = null; }
  const el = document.getElementById('aiDomDilemmaOverlay');
  if (el) el.remove();
}

function aiDomDilemmaRun(optA, optB) {
  aiDomDilemmaCleanup();
  aiDomLog(`抉择 · A:${optA} / B:${optB}`, 'sys');

  // 创建浮层
  const overlay = document.createElement('div');
  overlay.id = 'aiDomDilemmaOverlay';
  overlay.style.cssText = 'position:fixed;bottom:80px;left:50%;transform:translateX(-50%);z-index:9999;background:rgba(30,0,10,0.95);border:1px solid #e8537a;border-radius:12px;padding:16px 20px;min-width:280px;max-width:90vw;text-align:center;color:#fff;font-size:14px;box-shadow:0 4px 20px rgba(232,83,122,0.4);';

  const title = document.createElement('div');
  title.style.cssText = 'color:#e8537a;font-weight:bold;margin-bottom:10px;font-size:15px;';
  title.textContent = '选。';
  overlay.appendChild(title);

  const countdown = document.createElement('div');
  countdown.style.cssText = 'color:#ff6b8a;font-size:24px;font-weight:bold;margin-bottom:12px;';
  let remaining = 10;
  countdown.textContent = remaining;
  overlay.appendChild(countdown);

  const btnWrap = document.createElement('div');
  btnWrap.style.cssText = 'display:flex;gap:10px;justify-content:center;';

  const makeBtn = (label, cb) => {
    const btn = document.createElement('button');
    btn.textContent = label;
    btn.style.cssText = 'flex:1;padding:8px 12px;border:1px solid #e8537a;background:transparent;color:#e8537a;border-radius:8px;cursor:pointer;font-size:13px;max-width:180px;word-break:break-all;';
    btn.onmouseenter = () => btn.style.background = 'rgba(232,83,122,0.2)';
    btn.onmouseleave = () => btn.style.background = 'transparent';
    btn.onclick = cb;
    return btn;
  };

  const executeA = () => {
    aiDomDilemmaCleanup();
    aiDomLog('抉择·选A', 'sys');
    // A 通常是物理惩罚类
    aiDomOverloadRun(30);
  };

  const executeB = () => {
    aiDomDilemmaCleanup();
    aiDomLog('抉择·选B', 'sys');
    // B 通常是言语/心理类，给一个 REWARD 但低档
    aiDomRewardRun(Math.max(aiDomRatchetFloor(), 5));
    aiDomDebtAdd(-0.5);
  };

  const executeBoth = () => {
    aiDomDilemmaCleanup();
    aiDomLog('抉择·超时·全部执行', 'sys');
    aiDomDebtAdd(1);
    aiDomOverloadRun(30);
  };

  btnWrap.appendChild(makeBtn('A: ' + optA, executeA));
  btnWrap.appendChild(makeBtn('B: ' + optB, executeB));
  overlay.appendChild(btnWrap);

  const warn = document.createElement('div');
  warn.style.cssText = 'color:#666;font-size:11px;margin-top:8px;';
  warn.textContent = '不选？两个都来。';
  overlay.appendChild(warn);

  document.body.appendChild(overlay);

  _aiDomDilemmaCountdown = setInterval(() => {
    remaining -= 1;
    countdown.textContent = remaining;
    if (remaining <= 3) countdown.style.color = '#ff2050';
    if (remaining <= 0) executeBoth();
  }, 1000);

  _aiDomDilemmaTimer = setTimeout(() => {
    // fallback safety
    if (document.getElementById('aiDomDilemmaOverlay')) executeBoth();
  }, 11000);
}

// ── 分发 ──

function _clamp10(x) { return Math.max(0, Math.min(10, parseInt(x) || 0)); }
function _clampSec(x, lo, hi) { return Math.max(lo, Math.min(hi, parseInt(x) || lo)); }

async function aiDomSceneDispatch(cmd) {
  const c = (cmd || '').trim();
  const up = c.toUpperCase();
  aiDomHistoryPush(c);

  if (up === 'STOP') { aiDomSceneStop(); if (aiDomBreakActive?.escalateTimer) clearInterval(aiDomBreakActive.escalateTimer); aiDomBreakActive = null; const ok = await aiDomStopToy('toy_stop_command'); aiDomScene.last = { v:0, s:0 }; aiDomLog('停止', 'sys'); return ok !== false; }
  if (up === 'EDGE') { aiDomEdgeRun(); return true; }
  if (up === 'PUNISH') { aiDomPunishRun(); return true; }

  const parts = c.split(':');
  const verb = (parts[0] || '').toUpperCase();

  if (verb === 'SCENE' && parts[1]) { aiDomSceneStart(parts[1].toLowerCase()); return true; }

  if (verb === 'HOLD' && parts[1] != null) {
    aiDomSceneStop();
    const v = _clamp10(parts[1]);
    const s = parts[2] != null ? _clamp10(parts[2]) : Math.round(v * 0.7);
    aiDomScene.last = { v, s };
    const ok = await toyDriver.play({ v, s });
    aiDomLog(`维持 · v${v}/s${s}`, 'sys');
    return ok !== false;
  }

  if (verb === 'SPIKE' && parts[1] != null && parts[2] != null) {
    let v, s, sec;
    if (parts[3] != null) { v = _clamp10(parts[1]); s = _clamp10(parts[2]); sec = _clampSec(parts[3], 1, 30); }
    else { v = _clamp10(parts[1]); s = Math.round(v * 0.7); sec = _clampSec(parts[2], 1, 30); }
    const prev = { v: aiDomScene.last.v || 0, s: aiDomScene.last.s || 0 };
    if (aiDomScene.timer) { clearTimeout(aiDomScene.timer); aiDomScene.timer = null; }
    if (aiDomScene.spikeTimer) clearTimeout(aiDomScene.spikeTimer);
    const ok = await toyDriver.play({ v, s });
    aiDomScene.last = { v, s };
    aiDomLog(`脉冲 · v${v}/s${s} · ${sec}秒`, 'sys');
    aiDomScene.spikeTimer = setTimeout(() => {
      aiDomScene.spikeTimer = null;
      const fl = aiDomRatchetFloor();
      const rv = Math.max(fl, prev.v), rs = Math.max(fl, prev.s);
      aiDomScene.last = { v: rv, s: rs };
      toyDriver.play({ v: rv, s: rs });
      if (aiDomScene.gen && !aiDomScene.timer) aiDomSceneTick();
    }, sec * 1000);
    return ok !== false;
  }

  if (verb === 'DENY' && parts[1] != null) { aiDomDenyRun(_clampSec(parts[1], 2, 60)); return true; }
  if (verb === 'REWARD' && parts[1] != null) { aiDomRewardRun(_clamp10(parts[1])); return true; }
  if (verb === 'TEASE' && parts[1] != null) { aiDomTeaseRun(_clampSec(parts[1], 10, 120)); return true; }
  if (verb === 'OVERLOAD' && parts[1] != null) { aiDomOverloadRun(_clampSec(parts[1], 3, 30)); return true; }

  // v2 新指令
  if (verb === 'GRIND') {
    if (parts[3] != null) { aiDomGrindRun(_clamp10(parts[1]), _clamp10(parts[2]), _clampSec(parts[3], 30, 180)); }
    else if (parts[2] != null) { const v = _clamp10(parts[1]); aiDomGrindRun(v, Math.round(v * 0.7), _clampSec(parts[2], 30, 180)); }
    return true;
  }
  if (verb === 'SIEGE') {
    if (parts[2] != null) { aiDomSiegeRun(_clamp10(parts[1]), _clamp10(parts[2])); }
    else if (parts[1] != null) { const v = _clamp10(parts[1]); aiDomSiegeRun(v, Math.round(v * 0.7)); }
    else { aiDomSiegeRun(8, 6); }
    return true;
  }
  if (verb === 'HUNT') { aiDomHuntRun(_clampSec(parts[1] || '5', 3, 8)); return true; }
  if (verb === 'SHATTER') { aiDomShatterRun(_clampSec(parts[1] || '30', 15, 60)); return true; }
  if (verb === 'TRAP' && parts[1]) { aiDomTrapRun(parts[1].toLowerCase()); return true; }
  if (verb === 'BREAK' && parts[1]) { aiDomBreakRun(parts.slice(1).join(':')); return true; }
  if (verb === 'DILEMMA' && parts[1] && parts[2]) { aiDomDilemmaRun(parts[1], parts.slice(2).join(':')); return true; }

  aiDomLog('无效指令: ' + c, 'err');
  return false;
}

// ══════════════════════════════════════════════════════════
// ── 面板 / 生命周期 / 安全词 ──
// ══════════════════════════════════════════════════════════

function openAiDom() {
  closeSidebar();
  const sw = document.getElementById('aiDomSafewordInput');
  sw.value = aiDomSafeword || '';
  const cncBox = document.getElementById('aiDomCncToggle');
  if (cncBox) cncBox.checked = !!aiDomCncEnabled;
  const wk = document.getElementById('aiDomWeaknessInput');
  if (wk) wk.value = (aiDomCncWeakness || []).join('\n');
  aiDomRefreshUI();
  document.getElementById('aiDomModal').classList.add('show');
}

function closeAiDom() {
  document.getElementById('aiDomModal').classList.remove('show');
}

function onAiDomSafewordInput() { aiDomRefreshUI(); }

async function onAiDomDeviceChange() {
  if (toyDriver.isConnected()) await toyDriver.disconnect();
  aiDomRefreshUI();
}

async function aiDomToggleConnect() {
  if (toyDriver.isConnected()) {
    await toyDriver.disconnect();
    aiDomRefreshUI();
    return;
  }
  const sel = document.querySelector('input[name="aiDomDev"]:checked');
  if (!sel) return;
  try { await toyDriver.connect(sel.value); }
  catch (e) { aiDomLog('连接失败: ' + (e.message || e), 'err'); }
  // 真正连接成功由 gattserverdisconnected 反向 / toyNativeBle.onConnected 触发 UI 刷新
  aiDomRefreshUI();
}

function aiDomRefreshUI() {
  const dot = document.getElementById('aiDomDot');
  if (!dot) return;
  const lab = document.getElementById('aiDomConnLabel');
  const btn = document.getElementById('aiDomConnBtn');
  const enterBtn = document.getElementById('aiDomEnterBtn');
  const conn = toyDriver.isConnected();
  dot.className = 'whisper-dot ' + (conn ? 'on' : 'off');
  const sel = document.querySelector('input[name="aiDomDev"]:checked');
  const devKey = sel ? sel.value : null;
  const devName = devKey ? AI_DOM_DEVICES[devKey].label : '';
  if (lab) lab.textContent = conn ? (devName + ' 已连接') : '未连接';
  if (btn) btn.textContent = conn ? '断开' : '连接';
  const swv = (document.getElementById('aiDomSafewordInput')?.value || '').trim();
  if (enterBtn) enterBtn.disabled = !conn || !swv;
}

async function aiDomEnter() {
  const sw = (document.getElementById('aiDomSafewordInput').value || '').trim();
  if (!sw) { aiDomToast('请先设置安全词'); return; }
  if (!toyDriver.active || !toyDriver.isConnected()) { aiDomToast('请先连接设备'); return; }
  aiDomSafeword = sw;
  localStorage.setItem('aion_safeword', sw);
  aiDomCncEnabled = !!(document.getElementById('aiDomCncToggle')?.checked);
  localStorage.setItem('aion_cnc_enabled', aiDomCncEnabled ? '1' : '0');
  const wkRaw = (document.getElementById('aiDomWeaknessInput')?.value || '').trim();
  aiDomCncWeakness = wkRaw.split('\n').map(s=>s.trim()).filter(Boolean);
  localStorage.setItem('aion_cnc_weakness', wkRaw);
  aiDomSessionStartAt = Date.now();
  aiDomLastPunishAt = 0;
  aiDomComplianceStreak = 0;
  aiDomShortStreak = 0;
  aiDomLastResistHits = 0;
  aiDomLastReplyDelayMs = 0;
  aiDomRatchetValley = 0;
  aiDomDebt = 0;
  aiDomStubbornStreak = 0;
  aiDomBreakActive = null;
  aiDomPostOCooldown = 0;
  aiDomMode = true;
  if (window.ControlRuntime) {
    await ControlRuntime.start('dom', {
      deviceId: AI_DOM_BACKEND_DEVICE_ID,
      safewordSet: true,
      snapshot: aiDomBuildControlSnapshot(),
    });
    if (!ControlRuntime.isOwner()) {
      aiDomMode = false;
      aiDomToast('当前对话已由其他标签页控制');
      return;
    }
  }
  document.getElementById('aiDomPillDev').textContent = AI_DOM_DEVICES[toyDriver.active].label;
  document.getElementById('aiDomPillSafe').textContent = sw;
  document.getElementById('aiDomPill').classList.add('show');
  closeAiDom();
  aiDomToast('🔒 已进入主控');
  aiDomFidgetStart();
  aiDomInitiativeStart();
}

function aiDomExit(fromPanic) {
  if (!aiDomMode) return;
  if (!fromPanic && !confirm('退出 AI 主控？玩具会立即停止。')) return;
  if (window.ControlRuntime) ControlRuntime.end(fromPanic ? 'panic' : 'normal');
  aiDomFidgetStop();
  aiDomInitiativeStop();
  aiDomSceneStop();
  aiDomStopToy(fromPanic ? 'panic' : 'normal_exit');
  aiDomScene.last = { v: 0, s: 0 };
  aiDomHistoryClear();
  aiDomSessionStartAt = 0;
  aiDomLastPunishAt = 0;
  aiDomShortStreak = 0;
  aiDomComplianceStreak = 0;
  aiDomLastResistHits = 0;
  aiDomLastReplyDelayMs = 0;
  aiDomLastAiDoneAt = 0;
  aiDomRatchetValley = 0;
  aiDomDebt = 0;
  aiDomStubbornStreak = 0;
  if (aiDomBreakActive?.escalateTimer) clearInterval(aiDomBreakActive.escalateTimer);
  aiDomBreakActive = null;
  aiDomPostOCooldown = 0;
  aiDomMode = false;
  document.getElementById('aiDomPill').classList.remove('show');
}

function aiDomCheckSafeword(text) {
  if (!aiDomSafeword) return false;
  return String(text).toLowerCase().includes(aiDomSafeword.toLowerCase());
}

function aiDomLocalPanicStop(reason) {
  aiDomFidgetStop();
  aiDomInitiativeStop();
  aiDomSceneStop();
  if (reason !== 'device_emergency_stop') aiDomStopToy('panic');
  aiDomScene.last = { v: 0, s: 0 };
  aiDomHistoryClear();
  aiDomSessionStartAt = 0;
  aiDomLastPunishAt = 0;
  aiDomShortStreak = 0;
  aiDomComplianceStreak = 0;
  aiDomLastResistHits = 0;
  aiDomLastReplyDelayMs = 0;
  aiDomLastAiDoneAt = 0;
  aiDomRatchetValley = 0;
  aiDomDebt = 0;
  aiDomStubbornStreak = 0;
  if (aiDomBreakActive?.escalateTimer) clearInterval(aiDomBreakActive.escalateTimer);
  aiDomBreakActive = null;
  aiDomPostOCooldown = 0;
  aiDomDilemmaCleanup();
  aiDomToast('🚨 安全词触发，已退出主控');
  aiDomMode = false;
  const pill = document.getElementById('aiDomPill');
  if (pill) pill.classList.remove('show');
}

function aiDomPanic(reason) {
  const closeReason = reason || 'safeword';
  if (window.ControlEmergencyStop) {
    ControlEmergencyStop.panic(closeReason, aiDomLocalPanicStop);
  } else {
    aiDomLocalPanicStop(closeReason);
  }
}

// ── AI 主动出击（initiative timer） ──
let _aiDomInitTimer = null;
let _aiDomInitFiring = false;

function aiDomInitiativeStart() {
  aiDomInitiativeStop();
  const delay = (60 + Math.random() * 120) * 1000;
  _aiDomInitTimer = setTimeout(aiDomInitiativeCheck, delay);
}

function aiDomInitiativeStop() {
  if (_aiDomInitTimer) { clearTimeout(_aiDomInitTimer); _aiDomInitTimer = null; }
}

function aiDomInitiativeReset() {
  if (!aiDomMode) return;
  aiDomInitiativeStart();
}

function aiDomInitiativeCheck() {
  _aiDomInitTimer = null;
  if (!aiDomMode || !currentConvId) return;
  if (sending || _aiDomInitFiring) { aiDomInitiativeStart(); return; }
  const sinceLast = Date.now() - (aiDomLastAiDoneAt || 0);
  if (sinceLast < 30000) { aiDomInitiativeStart(); return; }
  aiDomFireInitiative();
}

async function aiDomFireInitiative() {
  if (_aiDomInitFiring || !aiDomMode || !currentConvId) return;
  _aiDomInitFiring = true;
  try {
    const now = Date.now();
    const bodyData = {
      context_limit: 15,
      safeword: aiDomSafeword,
      dom_history: aiDomHistorySnapshot(),
      cnc_enabled: aiDomCncEnabled,
      cnc_weakness: aiDomCncWeakness || [],
      resist_hits: aiDomLastResistHits,
      short_streak: aiDomShortStreak,
      reply_delay_ms: aiDomLastReplyDelayMs,
      compliance_streak: aiDomComplianceStreak,
      session_elapsed: aiDomSessionStartAt ? Math.floor((now - aiDomSessionStartAt) / 1000) : 0,
      scene_name: aiDomScene.name || null,
      scene_elapsed: aiDomScene.startAt ? Math.floor((now - aiDomScene.startAt) / 1000) : 0,
      since_last_punish: aiDomLastPunishAt ? Math.floor((now - aiDomLastPunishAt) / 1000) : null,
      ratchet_valley: aiDomRatchetValley,
      debt: aiDomDebt,
      stubborn_streak: aiDomStubbornStreak,
    };
    if (window.ControlRuntime) await ControlRuntime.updateSnapshot(aiDomBuildControlSnapshot());

    const res = await fetch(`/api/conversations/${currentConvId}/dom-initiative`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(bodyData),
    });

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let aiMsgId = null;
    let aiContent = '';
    let buf = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      const lines = buf.split('\n');
      buf = lines.pop();
      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        try {
          const data = JSON.parse(line.slice(6));
          if (data.type === 'start') {
            aiMsgId = data.id;
            currentMessages.push({ id: aiMsgId, conv_id: currentConvId, role: 'assistant', content: '...', created_at: Date.now()/1000 });
            renderMessages();
          } else if (data.type === 'chunk') {
            if (data.content.includes('\x00RETRY\x00')) { aiContent = ''; continue; }
            aiContent += data.content;
            const display = cleanAssistantContent(aiContent);
            const mi = currentMessages.findIndex(m => m.id === aiMsgId);
            if (mi >= 0) currentMessages[mi].content = display;
            const container = document.getElementById(`m_${aiMsgId}`);
            if (container) {
              const target = container.querySelector('.msg-bubbles') || container.querySelector('.msg-bubble');
              if (target) target.innerHTML = formatMsg(display);
            }
            scrollBottom();
          } else if (data.type === 'toy_command') {
            ControlToyRouter.execute(data);
          } else if (data.type === 'toy_command_rejected') {
            ControlToyRouter.reportRejected(data);
          }
        } catch {}
      }
    }
    if (aiMsgId && aiContent) {
      aiDomLastAiDoneAt = Date.now();
      if (typeof _detectAiMood === 'function') {
        _msgMoods[aiMsgId] = _detectAiMood(aiContent);
        _applyMoodGlow(aiMsgId);
      }
      const cleanText = cleanAssistantContent(aiContent);
      ttsSpeak(cleanText, aiMsgId);
    }
  } catch (e) {
    console.error('[DomInitiative] error:', e);
  } finally {
    _aiDomInitFiring = false;
    if (aiDomMode) aiDomInitiativeStart();
  }
}

function aiDomRestoreControlRuntimeSession() {
  const s = window.ControlRuntime?.currentSession?.();
  if (!s || s.kind !== 'dom' || !window.ControlRuntime?.isOwner?.()) return;
  aiDomMode = true;
  aiDomSessionStartAt = s.started_at ? Number(s.started_at) * 1000 : Date.now();
  const pill = document.getElementById('aiDomPill');
  if (pill) {
    document.getElementById('aiDomPillDev').textContent = s.device_id || '设备';
    document.getElementById('aiDomPillSafe').textContent = s.safeword_set ? '已设置' : '未设置';
    pill.classList.add('show');
  }
}

aiDomRestoreControlRuntimeSession();

ChatApp.registerModule("aiDom", {
  open: openAiDom,
  close: closeAiDom,
  onSafewordInput: onAiDomSafewordInput,
  onDeviceChange: onAiDomDeviceChange,
  toggleConnect: aiDomToggleConnect,
  enter: aiDomEnter,
  exit: aiDomExit,
  panic: aiDomPanic,
  guard: aiDomGuard,
  isActive: () => aiDomMode,
  historySnapshot: aiDomHistorySnapshot,
  buildSendContext: aiDomBuildSendContext,
  buildControlSnapshot: aiDomBuildControlSnapshot,
  restoreControlRuntimeSession: aiDomRestoreControlRuntimeSession,
});
