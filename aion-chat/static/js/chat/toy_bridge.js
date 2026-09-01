// ── Toy bridge core：BLE 状态、DeviceService 上报、底层发送 ──
const TOY_SERVICE_UUID = 0xEE01, TOY_WRITE_UUID = 0xEE03, TOY_NOTIFY_UUID = 0xEE02;
let toyDevice = null, toyServer = null, toyWriteChar = null, toyConnected = false;

const TOY_MOTORS = [
  { label:'震动', gearsSpec:'0001', modeSpec:'0002',
    modes:[{id:1,name:'全身酥麻'},{id:2,name:'渐入佳境'},{id:3,name:'循序渐进'},{id:4,name:'欢呼雀跃'}] },
  { label:'电流', gearsSpec:'0003', modeSpec:'0004',
    modes:[{id:1,name:'温柔涟漪'},{id:2,name:'娇舌搅动'},{id:3,name:'风驰快感'},{id:4,name:'浪潮不断'}] },
  { label:'吮吸', gearsSpec:'0007', modeSpec:'0008',
    modes:[{id:1,name:'连绵不绝'},{id:2,name:'深海暗涌'},{id:3,name:'爆裂冲刺'},{id:4,name:'浪潮不断'}] },
];

function toyBridgeLog(msg, cls='') {
  if (typeof toyLog === 'function') toyLog(msg, cls);
}

// 原生 BLE 回调（Android APK 的 BleBridge.java 通过 evaluateJavascript 调用）
// 桥只有单实例，按当前 toyDriver.active 分派到对应子驱动；未激活 AI Dom 时走老 SOSEXY 路径
window.toyNativeBle = {
  onConnected() {
    if (typeof toyDriver !== 'undefined' && toyDriver._isAdv && toyDriver._isAdv() && typeof skConnected !== 'undefined') {
      skConnected = true;
    } else if (typeof toyDriver !== 'undefined' && toyDriver.active === 'cx492b' && typeof cxConnected !== 'undefined') {
      cxConnected = true;
    } else {
      toyConnected = true;
      if (typeof toyUpdateUI === 'function') toyUpdateUI();
      toyBridgeLog('已连接 ♡', 'wl-sys');
      if (typeof whisperMode !== 'undefined' && whisperMode && typeof whisperInitStart === 'function' && $('whisperInitToggle')?.checked) whisperInitStart();
    }
    if (typeof aiDomRefreshUI === 'function') aiDomRefreshUI();
    if (typeof aiDomLog === 'function') aiDomLog('已连接 ♡', 'sys');
    if (typeof aiDomMode !== 'undefined' && aiDomMode && typeof aiDomSceneResume === 'function') aiDomSceneResume();
    toyReportBridgeState(true, { source_event: 'native_connected' });
  },
  onDisconnected() {
    const _inDom = (typeof aiDomMode !== 'undefined' && aiDomMode);
    toyConnected = false;
    if (typeof cxConnected !== 'undefined') cxConnected = false;
    if (typeof skConnected !== 'undefined') skConnected = false;
    toyWriteChar = null;
    if (!_inDom && typeof toyDriver !== 'undefined') toyDriver.active = null;
    if (typeof whisperInitStop === 'function') whisperInitStop();
    if (typeof toyUpdateUI === 'function') toyUpdateUI();
    if (typeof aiDomRefreshUI === 'function') aiDomRefreshUI();
    toyBridgeLog('断开', 'wl-err');
    if (typeof aiDomLog === 'function') aiDomLog('断开','err');
    if (_inDom && typeof aiDomScenePause === 'function') aiDomScenePause();
    toyReportBridgeState(false, { source_event: 'native_disconnected' });
  },
  onError(msg) {
    toyBridgeLog(msg, 'wl-err');
    if (typeof aiDomLog === 'function') aiDomLog(msg, 'err');
  },
  onLog(msg) {
    toyBridgeLog(msg, 'wl-sys');
    if (typeof aiDomLog === 'function') aiDomLog(msg, 'sys');
  }
};

const TOY_BRIDGE_DEVICE_ID = 'browser_toy_bridge';
function toyBridgeMetadata(extra) {
  let active = null;
  try { active = (typeof toyDriver !== 'undefined' && toyDriver.active) ? toyDriver.active : null; } catch(e) {}
  return Object.assign({
    page: 'chat',
    transport: window.AionBle ? 'android_ble' : 'web_bluetooth',
    ai_dom_mode: !!(typeof aiDomMode !== 'undefined' && aiDomMode),
    active_driver: active || 'sosexy',
    device_name: toyDevice && toyDevice.name ? toyDevice.name : ''
  }, extra || {});
}
function toyReportBridgeState(connected, extra) {
  api('POST', `/api/devices/${TOY_BRIDGE_DEVICE_ID}/state`, {
    status: connected ? 'online' : 'offline',
    name: (toyDevice && toyDevice.name) || 'Browser Toy Bridge',
    kind: 'toy_bridge',
    capabilities: ['status.read', 'notify.pulse', 'toy.legacy_command'],
    metadata: toyBridgeMetadata(extra)
  }).catch(() => {});
}
function toyBridgeIsConnected() {
  try {
    if (typeof toyDriver !== 'undefined' && toyDriver && typeof toyDriver.isConnected === 'function' && toyDriver.isConnected()) return true;
  } catch(e) {}
  try { if (typeof toyConnected !== 'undefined' && toyConnected) return true; } catch(e) {}
  try { if (typeof cxConnected !== 'undefined' && cxConnected) return true; } catch(e) {}
  try { if (typeof skConnected !== 'undefined' && skConnected) return true; } catch(e) {}
  try { if (window.AionBle && typeof window.AionBle.isConnected === 'function' && window.AionBle.isConnected()) return true; } catch(e) {}
  return false;
}
setInterval(() => {
  if (toyBridgeIsConnected()) toyReportBridgeState(true, { source_event: 'heartbeat' });
}, 15000);

function toyHexToBytes(h) { const b=[]; for(let i=0;i<h.length;i+=2) b.push(parseInt(h.substr(i,2),16)); return b; }
function toyToHex2(n) { return n.toString(16).padStart(2,'0'); }
function toyBuildDualCmd(s1,v1,s2,v2) { return '02'+s1+'11'+toyToHex2(v1)+s2+'11'+toyToHex2(v2); }
function toyBuildStopCmd() { return '03000111000003110000071100'; }
function toySleep(ms) { return new Promise(r => setTimeout(r, ms)); }

async function toySendData2(hexCmd) {
  if (window.AionBle && window.AionBle.isConnected()) {
    toyBridgeLog('→ ' + hexCmd, 'wl-send');
    try {
      window.AionBle.sendData(hexCmd);
      return true;
    } catch(e) {
      toyBridgeLog('写入失败:' + (e.message || e), 'wl-err');
      return false;
    }
  }
  if (!toyWriteChar) { toyBridgeLog('未连接','wl-err'); return false; }
  const full = '00' + hexCmd;
  toyBridgeLog('→ ' + hexCmd, 'wl-send');
  const data = toyHexToBytes(full), chunks = [];
  for (let i = 0; i < data.length; i += 18) chunks.push(data.slice(i, i+18));
  const rnd = Math.floor(Math.random() * 255), pkts = [];
  for (let i = 0; i < chunks.length; i++) pkts.push([rnd, i+1, ...chunks[i]]);
  if (chunks.length > 0 && chunks[chunks.length-1].length === 18) pkts.push([rnd, chunks.length+1]);
  for (let i = 0; i < pkts.length; i++) {
    const p = new Uint8Array(pkts[i]);
    try {
      if (toyWriteChar.properties.write) await toyWriteChar.writeValueWithResponse(p);
      else await toyWriteChar.writeValueWithoutResponse(p);
    } catch(e) { toyBridgeLog('写入失败:'+e.message,'wl-err'); return false; }
    if (pkts.length > 1 && i < pkts.length-1) await toySleep(30);
  }
  return true;
}

ChatApp.registerModule("toyBridge", {
  isConnected: toyBridgeIsConnected,
  reportState: toyReportBridgeState,
  sendData: toySendData2,
  buildStopCommand: toyBuildStopCmd,
  buildDualCommand: toyBuildDualCmd,
});
