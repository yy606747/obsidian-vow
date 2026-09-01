/* ── Obsidian Vow Common JS — 共享工具函数 ── */

const $ = id => document.getElementById(id);

// 在 iframe 子页面浮层中时，返回按钮改为关闭浮层回到聊天页
if (window.parent !== window) {
  document.addEventListener('DOMContentLoaded', () => {
    const backBtn = document.querySelector('.top-bar .back-btn');
    if (backBtn) backBtn.onclick = () => window.parent.closeSubPage();
  });
}

// 琥珀光场（轻量版：3 orbs + 4 sparks，省电）
(function spawnAmbient() {
  const orbs = document.createElement('div');
  orbs.className = 'vow-orbs';
  for (let i = 0; i < 3; i++) {
    const o = document.createElement('div');
    o.className = 'orb';
    const sz = 30 + Math.random() * 40;
    const duration = 18 + Math.random() * 18;
    o.style.cssText = `left:${Math.random()*100}%;bottom:-5%;width:${sz}px;height:${sz}px;animation-duration:${duration}s;animation-delay:${-Math.random()*duration}s;`;
    orbs.appendChild(o);
  }
  for (let i = 0; i < 4; i++) {
    const s = document.createElement('div');
    s.className = 'spark';
    const sz = 2 + Math.random() * 4;
    const duration = 12 + Math.random() * 14;
    s.style.cssText = `left:${Math.random()*100}%;bottom:-3%;width:${sz}px;height:${sz}px;animation-duration:${duration}s;animation-delay:${-Math.random()*duration}s;`;
    orbs.appendChild(s);
  }
  document.body.appendChild(orbs);

  ['tr','bl'].forEach(pos => {
    const h = document.createElement('div');
    h.className = `vow-halo ${pos}`;
    document.body.appendChild(h);
  });

  ['vow-streak','vow-vignette'].forEach(cls => {
    const el = document.createElement('div');
    el.className = cls;
    document.body.appendChild(el);
  });

  // 页面不可见时暂停所有 CSS 动画，省电
  document.addEventListener('visibilitychange', () => {
    document.body.classList.toggle('vow-paused', document.hidden);
  });
})();

async function api(method, url, body) {
  const opts = { method, headers: {"Content-Type": "application/json"} };
  if (body) opts.body = JSON.stringify(body);
  const res = await fetch(url, opts);
  return res.json();
}

function escHtml(s) {
  const d = document.createElement("div");
  d.textContent = s;
  return d.innerHTML;
}

/* ── Toast ── */
let _toastTimer = null;
function showToast(msg, duration) {
  let t = document.getElementById('commonToast');
  if (!t) {
    t = document.createElement('div');
    t.id = 'commonToast';
    t.className = 'toast-msg';
    document.body.appendChild(t);
  }
  t.textContent = msg;
  t.classList.add('show');
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => t.classList.remove('show'), duration || 2000);
}

/* ── WebSocket（闹铃弹窗等全局事件） ── */
let _commonWs = null;
let _wsHandlers = {};

function _setWsDot(state) {
  // state: "connecting" | "open" | "closed"
  let dot = document.getElementById('wsStatusDot');
  if (!dot) {
    dot = document.createElement('div');
    dot.id = 'wsStatusDot';
    dot.title = '服务器连接状态';
    dot.style.cssText = `
      position:fixed; top:10px; right:10px; z-index:10000;
      width:8px; height:8px; border-radius:50%;
      transition:background 0.3s, box-shadow 0.3s;
      pointer-events:none;
    `;
    document.body.appendChild(dot);
  }
  const colors = {
    open:       { bg: '#22c55e', glow: 'rgba(34,197,94,0.4)',  title: '已连接' },
    connecting: { bg: '#eab308', glow: 'rgba(234,179,8,0.5)',  title: '重连中...' },
    closed:     { bg: '#ef4444', glow: 'rgba(239,68,68,0.5)',  title: '连接断开' },
  };
  const c = colors[state] || colors.closed;
  dot.style.background = c.bg;
  dot.style.boxShadow = `0 0 6px ${c.glow}`;
  dot.title = '服务器连接：' + c.title;
}

// 父页面在自己的 WS onmessage 里调用：把事件转发给子页面 iframe
// （iframe 内不建 WS，记忆库誓约 tab、心语等靠转发保持实时）
function forwardWsToSubPage(msg) {
  const frame = document.getElementById('subPageFrame');
  if (!frame || !frame.contentWindow) return;
  if (!frame.src || frame.src === 'about:blank') return;
  try { frame.contentWindow.postMessage({ __aionWs: true, payload: msg }, location.origin); } catch (e) {}
}

function connectCommonWS(extraHandler) {
  // iframe 子页面不建自己的 WS 连接：改为监听父页转发的 WS 事件
  if (window.parent !== window) {
    _setWsDot('open');
    window.addEventListener('message', e => {
      if (e.origin !== location.origin) return;
      const msg = e.data && e.data.__aionWs ? e.data.payload : null;
      if (!msg || !extraHandler) return;
      try { extraHandler(msg); } catch (err) { console.error('转发事件处理失败:', err); }
    });
    return;
  }
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  _setWsDot('connecting');
  _commonWs = new WebSocket(`${proto}//${location.host}/ws`);
  _commonWs.onopen = () => {
    _setWsDot('open');
    try { if (typeof window.onWsReopen === 'function') window.onWsReopen(); } catch (e) {}
  };
  _commonWs.onerror = () => _setWsDot('closed');
  _commonWs.onmessage = e => {
    const msg = JSON.parse(e.data);
    forwardWsToSubPage(msg);
    // 闹铃弹窗 — 全局
    if (msg.type === "schedule_alarm") {
      showAlarmPopup(msg.data);
      return;
    }
    // 监控提示音 — 全局
    if (msg.type === "monitor_alert") {
      const audio = new Audio('/public/AionMonitoralart.mp3');
      audio.play().catch(() => {});
      sendSystemNotification('📷 监控提醒', msg.data?.content || '哨兵监控即将分析');
      return;
    }
    // 端点调用失败 — 全局 toast，避免后台任务静默挂掉
    if (msg.type === "endpoint_error") {
      const d = msg.data || {};
      const slot = d.slot ? `[${d.slot}]` : '';
      const ep = d.endpoint ? `(${d.endpoint})` : '';
      const status = d.status ? ` HTTP ${d.status}` : '';
      const elapsed = d.elapsed_ms ? ` · ${d.elapsed_ms}ms` : '';
      const kind = d.error_type ? ` · ${d.error_type}` : '';
      const rid = d.request_id ? ` · ${d.request_id}` : '';
      showToast(`⚠️ ${slot}${ep}${status}${kind}${elapsed}${rid} ${d.error || '端点调用失败'}`, 7000);
      return;
    }
    // 错过的闹铃汇总（启动时触发）；与下方 REST 兜底二选一，先到先弹
    if (msg.type === "missed_alarms") {
      if (window.__missedAlarmsChecked) return;
      window.__missedAlarmsChecked = true;
      const d = msg.data || {};
      const items = d.items || [];
      const lines = items.slice(0, 5).map(it =>
        `• ${it.trigger_at} ${it.content}`).join('\n');
      const more = d.count > items.length ? `\n（还有 ${d.count - items.length} 条）` : '';
      alert(`⏰ 你错过了 ${d.count} 条提醒\n\n${lines}${more}`);
      return;
    }
    // 页面自定义处理
    if (extraHandler) extraHandler(msg);
  };
  _commonWs.onclose = () => {
    _setWsDot('closed');
    setTimeout(() => connectCommonWS(extraHandler), 2000);
  };
}

/* ── 闹铃弹窗 ── */
let _alarmQueue = [];
const _seenAlarmDeliveries = new Map();

function _alarmDeliveryKey(data) {
  const ids = Array.isArray(data?.ids) ? [...data.ids].sort().join(',') : '';
  return ids || data?.id || `${data?.trigger_at || ''}|${data?.content || ''}`;
}

function _claimAlarmDelivery(data) {
  const now = Date.now();
  for (const [key, seenAt] of _seenAlarmDeliveries) {
    if (now - seenAt > 10 * 60_000) _seenAlarmDeliveries.delete(key);
  }
  const key = _alarmDeliveryKey(data);
  if (key && _seenAlarmDeliveries.has(key)) return false;
  if (key) _seenAlarmDeliveries.set(key, now);
  return true;
}

function showAlarmPopup(data) {
  if (!_claimAlarmDelivery(data)) return;
  _alarmQueue.push(data);
  if (_alarmQueue.length === 1) _showNextAlarm();
}
function _showNextAlarm() {
  if (!_alarmQueue.length) return;
  // 确保 DOM 中有闹铃弹窗
  _ensureAlarmOverlay();
  const data = _alarmQueue[0];
  $("alarmContent").textContent = data.content || "日程提醒";
  $("alarmTime").textContent = data.trigger_at || "";
  $("alarmOverlay").classList.add("show");
}
function dismissAlarm() {
  $("alarmOverlay").classList.remove("show");
  _alarmQueue.shift();
  if (_alarmQueue.length) setTimeout(_showNextAlarm, 300);
}

function _ensureAlarmOverlay() {
  if ($("alarmOverlay")) return;
  const div = document.createElement('div');
  div.innerHTML = `
    <div class="alarm-overlay" id="alarmOverlay">
      <div class="alarm-box">
        <div class="alarm-icon">⏰</div>
        <h3>日程提醒</h3>
        <div class="alarm-content" id="alarmContent"></div>
        <div class="alarm-time" id="alarmTime"></div>
        <button onclick="dismissAlarm()">确认</button>
      </div>
    </div>`;
  document.body.appendChild(div.firstElementChild);
}

/* ── 系统通知 ── */
function sendSystemNotification(title, body) {
  if (!('Notification' in window)) return;
  if (Notification.permission !== 'granted') return;
  try { new Notification(title, { body, icon: '/public/icon-192.png' }); } catch(e) {}
}

/* ── 配置健康检查：没端点/没 key 时顶部横幅 ── */
/* iframe 子页面跳过：父页面已经检查过了，避免重复请求拖慢加载 */
(async function checkConfigHealth() {
  if (window.parent !== window) return;
  if (window.__configHealthChecked) return;
  window.__configHealthChecked = true;
  if (/\/settings(\?|$|\.html)/.test(location.pathname + location.search)) return;
  try {
    const r = await fetch('/api/settings/health').then(x => x.json());
    if (r && !r.ok && r.issues && r.issues.length) {
      _showConfigBanner(r.issues);
    }
  } catch (e) {}
})();

function _showConfigBanner(issues) {
  if (document.getElementById('configBanner')) return;
  const b = document.createElement('div');
  b.id = 'configBanner';
  b.style.cssText = `
    position:fixed; top:0; left:0; right:0; z-index:9999;
    background:rgba(36,33,32,0.95); border-bottom:1px solid rgba(212,148,58,0.3); color:#e8ddd4;
    padding:10px 14px; font-size:13px; display:flex; align-items:center; gap:10px;
    box-shadow:0 2px 12px rgba(0,0,0,0.3), 0 0 20px rgba(212,148,58,0.08);
    backdrop-filter:blur(12px); -webkit-backdrop-filter:blur(12px);
  `;
  const summary = issues.map(i => i.msg).join('；');
  b.innerHTML = `
    <span style="flex:1;color:#d4943a">⚠️ 配置未完成：<span style="color:#a09388">${escHtml(summary)}</span></span>
    <button style="background:#d4943a; color:#fff; border:none; border-radius:6px; padding:5px 12px; cursor:pointer; font-size:12px; font-weight:600; box-shadow:0 0 8px rgba(212,148,58,0.3);"
            onclick="location.href='/settings'">去设置</button>
    <button style="background:transparent; border:none; color:#6a5f58; cursor:pointer; font-size:16px; padding:2px 4px;"
            onclick="this.parentElement.remove()">×</button>
  `;
  document.body.appendChild(b);
}
/* iframe 子页面跳过错过闹铃检查 */
(async function checkMissedOnLoad() {
  if (window.parent !== window) return;
  if (window.__missedAlarmsChecked) return;
  window.__missedAlarmsChecked = true;
  try {
    const r = await fetch('/api/schedule/missed-recent').then(x => x.json());
    if (r && r.count > 0) {
      const lines = (r.items || []).map(it => `• ${it.trigger_at} ${it.content}`).join('\n');
      const tail = r.count > (r.items || []).length ? `\n（还有 ${r.count - r.items.length} 条）` : '';
      alert(`⏰ 你错过了 ${r.count} 条提醒\n\n${lines}${tail}`);
    }
  } catch (e) {}
})();
