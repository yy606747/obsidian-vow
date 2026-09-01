// ══════════════════════════════════════════════════
// ── 密语时刻：BLE 玩具控制 ──
// ══════════════════════════════════════════════════
let whisperMode = false;
let toyActivePreset = -1;
const TOY_PNAMES = ['微风轻拂','春水初生','暗流涌动','如梦似幻','情潮渐涨','烈焰焚身','极乐之巅','魂飞魄散','失控'];
const TOY_PICONS = ['🌸','💧','🌊','✨','🔥','💥','⚡','💀','🌀'];
const TOY_DEF_PRESETS = [
  { motors:[{on:0,mode:1,speed:10},{on:0,mode:1,speed:0},{on:1,mode:1,speed:10}] },
  { motors:[{on:0,mode:1,speed:20},{on:0,mode:1,speed:10},{on:1,mode:3,speed:20}] },
  { motors:[{on:0,mode:2,speed:30},{on:0,mode:1,speed:20},{on:1,mode:2,speed:30}] },
  { motors:[{on:0,mode:2,speed:45},{on:0,mode:2,speed:25},{on:1,mode:4,speed:40}] },
  { motors:[{on:0,mode:3,speed:60},{on:1,mode:2,speed:20},{on:1,mode:2,speed:50}] },
  { motors:[{on:1,mode:3,speed:10},{on:1,mode:3,speed:30},{on:1,mode:4,speed:60}] },
  { motors:[{on:1,mode:2,speed:20},{on:1,mode:4,speed:40},{on:1,mode:4,speed:80}] },
  { motors:[{on:1,mode:1,speed:30},{on:1,mode:3,speed:80},{on:1,mode:3,speed:100}] },
  { motors:[{on:1,mode:4,speed:40},{on:1,mode:3,speed:90},{on:1,mode:3,speed:100}] },
];

let toyPresets = [];
function toyLoadPresets() {
  try { const s = localStorage.getItem('sosexy_presets_v3'); if (s) { toyPresets = JSON.parse(s); return; } } catch(e) {}
  toyPresets = JSON.parse(JSON.stringify(TOY_DEF_PRESETS));
}
function toySavePresets() { localStorage.setItem('sosexy_presets_v3', JSON.stringify(toyPresets)); }

function toyLog(msg, cls='') {
  const a = $('toyLogArea'); if (!a) return;
  const d = document.createElement('div'); d.className = cls;
  d.textContent = `[${new Date().toLocaleTimeString('zh-CN',{hour12:false})}] ${msg}`;
  a.appendChild(d); a.scrollTop = a.scrollHeight;
}

async function toyApplyPreset(p) {
  for (let i = 0; i < 3; i++) {
    const m = p.motors[i], mo = TOY_MOTORS[i];
    if (!await toySendData2(toyBuildDualCmd(mo.modeSpec, m.mode||1, mo.gearsSpec, m.on ? m.speed : 0))) return false;
    await toySleep(80);
  }
  return true;
}

async function toyActivatePreset(idx) {
  if (typeof whisperAutoRetreatCancel === 'function') whisperAutoRetreatCancel();
  toyActivePreset = idx; toyRenderGrid();
  const p = toyPresets[idx];
  toyLog('⚡ ' + TOY_PNAMES[idx], 'wl-sys');
  return await toyApplyPreset(p);
}

function toyStopAll() {
  toyActivePreset = -1;
  const sent = toySendData2(toyBuildStopCmd());
  toyLog('⏹ 停止', 'wl-sys');
  toyRenderGrid();
  return sent;
}

// 处理 AI 发送的 [TOY:x] 指令
function toyExecCmd(cmd) {
  cmd = (cmd || '').trim();
  // AI Dom 模式：交给 scene 引擎（SCENE:xxx / HOLD:n / SPIKE:n:sec / STOP）
  if (aiDomMode) {
    return aiDomSceneDispatch(cmd);
  }
  // 密语时刻老模式：旧 [TOY:1..9 | STOP]
  const up = cmd.toUpperCase();
  if (up === 'STOP' || cmd === '0') return toyStopAll();
  const n = parseInt(cmd);
  if (n >= 1 && n <= 9) return toyActivatePreset(n - 1);
  toyLog('无效指令:' + cmd, 'wl-err');
  return false;
}

function toyRenderGrid() {
  const g = $('toyPresetGrid'); if (!g) return;
  g.innerHTML = '';
  for (let i = 0; i < 9; i++) {
    const d = document.createElement('div');
    d.className = 'whisper-p-btn' + (i === toyActivePreset ? ' active' : '');
    d.dataset.toyPreset = String(i);
    d.innerHTML = `<span class="wp-icon">${TOY_PICONS[i]}</span><span class="wp-name">${TOY_PNAMES[i]}</span><button class="wp-edit" data-toy-edit="${i}">⚙</button>`;
    g.appendChild(d);
  }
}

async function toyToggleConnect() {
  if (toyConnected) { toyDisconnect(); return; }
  // 原生 BLE 桥接（Android APK）：总是 configure 成 SOSEXY，避免被 AI Dom 模式切到别的设备后残留
  if (window.AionBle) {
    if (window.AionBle.configure && typeof AI_DOM_DEVICES !== 'undefined') {
      window.AionBle.configure(JSON.stringify(AI_DOM_DEVICES.sosexy.native));
    }
    window.AionBle.connect();
    return;
  }
  // Web Bluetooth（浏览器）
  if (!navigator.bluetooth) { toyLog('此浏览器不支持 Web Bluetooth','wl-err'); return; }
  try {
    toyLog('搜索中...', 'wl-sys');
    toyDevice = await navigator.bluetooth.requestDevice({ filters: [{ namePrefix: 'SOSEXY' }], optionalServices: [TOY_SERVICE_UUID] });
    toyLog(toyDevice.name || '已找到设备', 'wl-sys');
    toyDevice.addEventListener('gattserverdisconnected', () => {
      toyConnected = false; toyWriteChar = null; toyUpdateUI(); toyLog('断开','wl-err');
      toyReportBridgeState(false, { source_event: 'web_bluetooth_disconnected' });
    });
    toyServer = await toyDevice.gatt.connect();
    const svc = await toyServer.getPrimaryService(TOY_SERVICE_UUID);
    toyWriteChar = await svc.getCharacteristic(TOY_WRITE_UUID);
    try {
      const notifyChar = await svc.getCharacteristic(TOY_NOTIFY_UUID);
      await notifyChar.startNotifications();
    } catch(e) {}
    toyConnected = true;
    toyUpdateUI();
    toyLog('已连接 ♡', 'wl-sys');
    toyReportBridgeState(true, { source_event: 'web_bluetooth_connected' });
  } catch(e) { toyLog('连接失败:'+e.message, 'wl-err'); }
}

function toyDisconnect() {
  toyStopAll();
  if (window.AionBle) {
    window.AionBle.disconnect();
  } else if (toyDevice && toyDevice.gatt.connected) {
    toyDevice.gatt.disconnect();
  }
  toyConnected = false; toyWriteChar = null;
  toyUpdateUI(); toyLog('已断开', 'wl-sys');
  toyReportBridgeState(false, { source_event: 'manual_disconnect' });
}

function toyUpdateUI() {
  const dot = $('toyDot'), label = $('toyConnLabel'), btn = $('toyConnBtn');
  if (dot) { dot.className = 'whisper-dot ' + (toyConnected ? 'on' : 'off'); }
  if (label) { label.textContent = toyConnected ? (toyDevice?.name || '已连接') : '未连接'; }
  if (btn) { btn.textContent = toyConnected ? '断开' : '连接'; }
}

function whisperBuildControlSnapshot() {
  const toyOnline = (typeof toyBridgeIsConnected === 'function') ? toyBridgeIsConnected() : !!toyConnected;
  return {
    whisper_mode: !!whisperMode,
    toy_connected: !!toyOnline,
    active_preset: toyActivePreset,
    initiative_enabled: !!($('whisperInitToggle')?.checked),
  };
}

function openWhisper() {
  if (typeof aiDomGuard === 'function' && aiDomGuard('打开密语时刻')) return;
  closeSidebar();
  toyLoadPresets();
  toyRenderGrid();
  toyUpdateUI();
  $('whisperModeToggle').checked = whisperMode;
  $('whisperModal').classList.add('show');
}
function closeWhisper() { $('whisperModal').classList.remove('show'); }

// ── 预设编辑器 ──
function toyOpenEditor(idx) {
  const p = toyPresets[idx], isLoop = idx === 8;
  let h = `<h3>${TOY_PICONS[idx]} ${TOY_PNAMES[idx]}</h3>`;
  for (let mi = 0; mi < 3; mi++) {
    const ms = p.motors[mi], mo = TOY_MOTORS[mi];
    h += `<div class="toy-me-block"><div class="toy-me-head"><span>${mo.label}</span>
    <label class="toggle-switch toy-me-switch"><input type="checkbox" id="teo${mi}" ${ms.on?'checked':''}><span class="toggle-slider"></span></label>
    </div><div class="toy-chip-row" id="tem${mi}">
    ${mo.modes.map(md => `<button class="toy-chip${md.id===ms.mode?' sel':''}" data-mid="${md.id}" data-toy-chip data-motor-index="${mi}" data-mode-id="${md.id}">${md.name}</button>`).join('')}
    </div><div class="toy-ed-speed"><label>速度</label>
    <input type="range" min="0" max="100" value="${ms.speed}" id="tes${mi}" data-toy-speed data-motor-index="${mi}">
    <span class="toy-ed-sv" id="tev${mi}">${ms.speed}</span></div></div>`;
  }
  if (isLoop) {
    h += `<div class="toy-loop-section"><div class="toy-me-head"><span>🌀 循环步骤</span></div><div id="toyLsc"></div>
    <button class="toy-add-step" data-toy-editor-action="add-loop-step">+ 添加步骤</button></div>`;
  }
  h += `<div class="toy-sheet-btns"><button class="toy-sb-cancel" data-toy-editor-action="cancel">取消</button><button class="toy-sb-save" data-toy-editor-action="save" data-preset-index="${idx}">保存</button></div>`;
  $('toyEditContent').innerHTML = h;
  $('toyEditorOverlay').classList.add('show');
  if (isLoop) { window._toyLS = JSON.parse(JSON.stringify(p.loopSteps || [])); toyRenderLS(); }
}

function toyESel(mi, mid) {
  document.querySelectorAll(`#tem${mi} .toy-chip`).forEach(c => c.classList.toggle('sel', parseInt(c.dataset.mid) === mid));
}

function toyRenderLS() {
  const c = $('toyLsc'); if (!c) return;
  c.innerHTML = window._toyLS.map((s, i) => `<div class="toy-ls"><span class="sn">${i+1}</span>
  <select data-toy-loop-preset data-loop-index="${i}">${[0,1,2,3,4,5,6,7].map(j => `<option value="${j}"${s.presetIdx===j?' selected':''}>${j+1}.${TOY_PNAMES[j]}</option>`).join('')}</select>
  <input type="number" min="1" max="60" value="${s.durationSec}" data-toy-loop-duration data-loop-index="${i}">s
  <button class="del" data-toy-editor-action="delete-loop-step" data-loop-index="${i}">×</button></div>`).join('');
}

function toyAddLS() { window._toyLS.push({ presetIdx: 0, durationSec: 3 }); toyRenderLS(); }

function toySaveEd(idx) {
  const p = toyPresets[idx];
  for (let mi = 0; mi < 3; mi++) {
    p.motors[mi].on = document.getElementById(`teo${mi}`).checked ? 1 : 0;
    const sc = document.querySelector(`#tem${mi} .toy-chip.sel`);
    if (sc) p.motors[mi].mode = parseInt(sc.dataset.mid);
    p.motors[mi].speed = parseInt(document.getElementById(`tes${mi}`).value);
  }
  if (idx === 8) p.loopSteps = window._toyLS.filter(s => s.durationSec > 0);
  toySavePresets(); toyCloseEditor(); toyRenderGrid();
  toyLog(`预设${idx+1}已保存`, 'wl-sys');
}

function toyCloseEditor() { $('toyEditorOverlay').classList.remove('show'); }

async function onWhisperModeChange() {
  whisperMode = $('whisperModeToggle').checked;
  toyLog(whisperMode ? '🔮 密语模式已开启' : '🔮 密语模式已关闭', 'wl-sys');
  api("PUT", "/api/settings/whisper", { active: whisperMode }).catch(() => {});
  if (!whisperMode) {
    whisperInitStop();
    const t = $('whisperInitToggle'); if (t) t.checked = false;
    if (window.ControlRuntime) ControlRuntime.end('normal');
  } else if ($('whisperInitToggle')?.checked && toyConnected) {
    if (window.ControlRuntime) {
      await ControlRuntime.start('whisper', { snapshot: whisperBuildControlSnapshot() });
      if (!ControlRuntime.isOwner()) {
        whisperMode = false;
        $('whisperModeToggle').checked = false;
        toyLog('当前对话已由其他标签页控制', 'wl-err');
        return;
      }
    }
    whisperInitStart();
  } else if (window.ControlRuntime) {
    await ControlRuntime.start('whisper', { snapshot: whisperBuildControlSnapshot() });
    if (!ControlRuntime.isOwner()) {
      whisperMode = false;
      $('whisperModeToggle').checked = false;
      toyLog('当前对话已由其他标签页控制', 'wl-err');
      return;
    }
  }
}

// ── AI 随机突袭（whisper initiative） ──
let _whisperInitTimer = null;
let _whisperInitFiring = false;
let _whisperInitLastAiDoneAt = 0;
let _whisperAutoRetreatTimer = null;

function onWhisperInitChange() {
  const on = $('whisperInitToggle').checked;
  toyLog(on ? '🎯 AI随机突袭已开启' : '🎯 AI随机突袭已关闭', 'wl-sys');
  if (on) { whisperInitStart(); } else { whisperInitStop(); }
}

function whisperInitStart() {
  whisperInitStop();
  if (!whisperMode || !toyConnected || !$('whisperInitToggle')?.checked) return;
  const delay = (120 + Math.random() * 1680) * 1000;
  _whisperInitTimer = setTimeout(whisperInitCheck, delay);
}

function whisperInitStop() {
  if (_whisperInitTimer) { clearTimeout(_whisperInitTimer); _whisperInitTimer = null; }
  whisperAutoRetreatCancel();
}

function whisperInitCheck() {
  _whisperInitTimer = null;
  if (!whisperMode || !toyConnected || !currentConvId) return;
  if (!$('whisperInitToggle')?.checked) return;
  if (sending || _whisperInitFiring) { whisperInitStart(); return; }
  const sinceLast = Date.now() - (_whisperInitLastAiDoneAt || 0);
  if (sinceLast < 60000) { whisperInitStart(); return; }
  whisperFireInit();
}

async function whisperFireInit() {
  if (_whisperInitFiring || !whisperMode || !toyConnected || !currentConvId) return;
  _whisperInitFiring = true;
  try {
    if (window.ControlRuntime) await ControlRuntime.updateSnapshot(whisperBuildControlSnapshot());
    const res = await fetch(`/api/conversations/${currentConvId}/whisper-initiative`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ context_limit: 15 }),
    });
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let aiMsgId = null, aiContent = '', buf = '';
    let hasToyCmd = false;

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
            if (ControlToyRouter.execute(data)) hasToyCmd = true;
          } else if (data.type === 'toy_command_rejected') {
            ControlToyRouter.reportRejected(data);
          }
        } catch {}
      }
    }

    if (aiMsgId && aiContent) {
      _whisperInitLastAiDoneAt = Date.now();
      if (typeof _detectAiMood === 'function') {
        _msgMoods[aiMsgId] = _detectAiMood(aiContent);
        _applyMoodGlow(aiMsgId);
      }
      const cleanText = cleanAssistantContent(aiContent);
      ttsSpeak(cleanText, aiMsgId);
    }

    if (hasToyCmd) whisperAutoRetreatSchedule();
  } catch (e) {
    console.error('[WhisperInit] error:', e);
  } finally {
    _whisperInitFiring = false;
    if (whisperMode && toyConnected && $('whisperInitToggle')?.checked) whisperInitStart();
  }
}

function whisperAutoRetreatSchedule() {
  whisperAutoRetreatCancel();
  const delay = (15 + Math.random() * 75) * 1000;
  _whisperAutoRetreatTimer = setTimeout(() => {
    _whisperAutoRetreatTimer = null;
    toyStopAll();
    toyLog('🎯 突袭结束', 'wl-sys');
  }, delay);
}

function whisperAutoRetreatCancel() {
  if (_whisperAutoRetreatTimer) { clearTimeout(_whisperAutoRetreatTimer); _whisperAutoRetreatTimer = null; }
}

let whisperToyEventsBound = false;

function bindWhisperToyEvents() {
  if (whisperToyEventsBound) return;
  whisperToyEventsBound = true;

  document.addEventListener('click', event => {
    const edit = event.target.closest('[data-toy-edit]');
    if (edit && $('toyPresetGrid')?.contains(edit)) {
      event.preventDefault();
      event.stopPropagation();
      toyOpenEditor(parseInt(edit.dataset.toyEdit, 10));
      return;
    }

    const preset = event.target.closest('[data-toy-preset]');
    if (preset && $('toyPresetGrid')?.contains(preset)) {
      event.preventDefault();
      const idx = parseInt(preset.dataset.toyPreset, 10);
      if (toyConnected) toyActivatePreset(idx);
      else toyLog('请先连接', 'wl-err');
      return;
    }

    const chip = event.target.closest('[data-toy-chip]');
    if (chip && $('toyEditorOverlay')?.contains(chip)) {
      event.preventDefault();
      toyESel(parseInt(chip.dataset.motorIndex, 10), parseInt(chip.dataset.modeId, 10));
      return;
    }

    const action = event.target.closest('[data-toy-editor-action]');
    if (!action || !$('toyEditorOverlay')?.contains(action)) return;
    event.preventDefault();
    switch (action.dataset.toyEditorAction) {
      case 'add-loop-step':
        toyAddLS();
        break;
      case 'cancel':
        toyCloseEditor();
        break;
      case 'save':
        toySaveEd(parseInt(action.dataset.presetIndex, 10));
        break;
      case 'delete-loop-step':
        window._toyLS.splice(parseInt(action.dataset.loopIndex, 10), 1);
        toyRenderLS();
        break;
    }
  });

  document.addEventListener('input', event => {
    const speed = event.target.closest('[data-toy-speed]');
    if (!speed || !$('toyEditorOverlay')?.contains(speed)) return;
    const val = $('tev' + speed.dataset.motorIndex);
    if (val) val.textContent = speed.value;
  });

  document.addEventListener('change', event => {
    const preset = event.target.closest('[data-toy-loop-preset]');
    if (preset && $('toyEditorOverlay')?.contains(preset)) {
      window._toyLS[parseInt(preset.dataset.loopIndex, 10)].presetIdx = parseInt(preset.value, 10);
      return;
    }

    const duration = event.target.closest('[data-toy-loop-duration]');
    if (duration && $('toyEditorOverlay')?.contains(duration)) {
      window._toyLS[parseInt(duration.dataset.loopIndex, 10)].durationSec = parseInt(duration.value, 10) || 3;
    }
  });
}

bindWhisperToyEvents();

function whisperRestoreControlRuntimeSession() {
  const s = window.ControlRuntime?.currentSession?.();
  if (!s || s.kind !== 'whisper' || !window.ControlRuntime?.isOwner?.()) return;
  whisperMode = true;
  const toggle = $('whisperModeToggle');
  if (toggle) toggle.checked = true;
}

whisperRestoreControlRuntimeSession();

// URL 参数检查：从主页点击密语时刻跳转
(function checkWhisperParam() {
  const params = new URLSearchParams(location.search);
  if (params.get('whisper') === '1') {
    setTimeout(() => openWhisper(), 500);
    history.replaceState(null, '', '/chat');
  }
})();

ChatApp.registerModule("whisperToy", {
  open: openWhisper,
  close: closeWhisper,
  toggleConnect: toyToggleConnect,
  stopAll: toyStopAll,
  execCommand: toyExecCmd,
  onModeChange: onWhisperModeChange,
  onInitChange: onWhisperInitChange,
  startInitiative: whisperInitStart,
  stopInitiative: whisperInitStop,
  cancelAutoRetreat: whisperAutoRetreatCancel,
  bindEvents: bindWhisperToyEvents,
  isModeEnabled: () => whisperMode,
  buildControlSnapshot: whisperBuildControlSnapshot,
  restoreControlRuntimeSession: whisperRestoreControlRuntimeSession,
});
