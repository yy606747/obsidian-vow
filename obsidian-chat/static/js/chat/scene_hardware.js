(function (global) {
  'use strict';
  const bridge = () => global.ObsidianSceneDevices;
  const runtime = () => global.SceneRuntime;
  const ready = new Map();
  let reporting = false;
  function nativeStatus() {
    try { return JSON.parse(bridge()?.status() || '{}'); } catch (_) { return {}; }
  }
  function supported() { const s=nativeStatus(); return s.version >= 2 && s.background_player === true; }
  function supportsDevice(device) {
    if (device.driver === 'mock') return true;
    if (!supported()) return false;
    const status = nativeStatus();
    return status.drivers ? Object.prototype.hasOwnProperty.call(status.drivers, device.driver) :
      ['muse_ble', 'sosexy_ble'].includes(device.driver);
  }
  function driverReady(status, driver) {
    if (status.drivers) return status.drivers[driver] === true;
    if (driver === 'muse_ble') return status.muse_supported === true;
    if (driver === 'sosexy_ble') return status.sosexy_connected === true;
    return false;
  }
  function requireNative() {
    if (!supported()) throw new Error('真机小剧场需要安装支持后台播放的新测试 APK。');
  }
  // Reattach a view to the already-authorized native run. Never call begin here.
  const attached = nativeStatus();
  if (attached.active) for (const device of attached.devices || []) ready.set(device.id, device);
  function connected() {
    const status = nativeStatus();
    return [...ready.values()].filter(d => driverReady(status, d.driver)).map(d => d.id);
  }
  function backgroundManaged(state = runtime()?.current?.()) {
    return supported() && !!state && (state.devices || []).some(d => d.driver && d.driver !== 'mock');
  }
  function legacyBusy() {
    const old = global.ControlRuntime?.currentSession?.();
    return (old && old.status !== 'ended') || (typeof whisperMode !== 'undefined' && whisperMode) ||
      (typeof aiDomMode !== 'undefined' && aiDomMode);
  }
  async function report() {
    if (!ready.size || reporting) return null;
    reporting = true;
    try { return await runtime().request('POST', '/api/scenes/bridge', {owner_id:runtime().ownerId(), connected:connected()}); }
    finally { reporting = false; }
  }
  async function connect(device, enabled) {
    if (device.driver === 'mock') return runtime().request('POST', '/api/scenes/devices/'+encodeURIComponent(device.id)+'/'+(enabled ? 'connect' : 'disconnect'));
    requireNative();
    if (!supportsDevice(device)) throw new Error('这台设备需要支持五台设备的新 APK，请先更新手机应用。');
    if (legacyBusy()) throw new Error('请先结束密语、Tide 或主控模式。');
    if (enabled) {
      if (!nativeStatus().foreground) throw new Error('请把应用留在前台再连接。');
      if (nativeStatus().version >= 3) {
        if (!bridge().connectDevice(device.driver)) throw new Error('无法准备设备，请检查蓝牙状态，或先结束当前场景。');
        if (['sosexy_ble', 'cx492b_ble'].includes(device.driver)) {
          const until = Date.now() + 24000;
          while (!driverReady(nativeStatus(), device.driver) && Date.now() < until) await new Promise(resolve => setTimeout(resolve, 250));
          if (!driverReady(nativeStatus(), device.driver)) throw new Error(`未连接到 ${device.name || device.id}，请检查开机状态和蓝牙占用。`);
        }
      } else if (device.driver === 'muse_ble') {
        if (!nativeStatus().muse_supported) throw new Error('手机蓝牙广播不可用。');
      } else if (device.driver === 'sosexy_ble') {
        if (!bridge().connectSosexy()) throw new Error('无法开始蓝牙连接。');
        const until = Date.now() + 24000;
        while (!nativeStatus().sosexy_connected && Date.now() < until) await new Promise(resolve => setTimeout(resolve, 250));
        if (!nativeStatus().sosexy_connected) throw new Error('未连接到 SOSEXY，请检查开机状态和蓝牙占用。');
      } else throw new Error('尚不支持这台设备的真机驱动。');
      ready.set(device.id, device);
    } else {
      stop();
      ready.delete(device.id);
      if (nativeStatus().version >= 3) bridge().disconnectDevice(device.driver);
      else if (device.driver === 'sosexy_ble') bridge().disconnectSosexy();
    }
    return runtime().request('POST', '/api/scenes/bridge', {owner_id:runtime().ownerId(), connected:connected()});
  }
  function stop(action = 'pause') {
    const state = runtime()?.current?.();
    if (state && supported()) bridge().stop(state.id, state.generation, action);
  }
  function panic() { if (supported()) bridge().panic(); }
  function stateChanged(state) {
    if (state && supported()) bridge().observe(state.id, state.generation, state.status);
  }
  function authorize(state) {
    const physical = (state.devices || []).filter(d => d.driver && d.driver !== 'mock');
    if (!physical.length || !['preparing','active'].includes(state.status)) return;
    requireNative();
    if (!physical.every(d => connected().includes(d.id))) throw new Error('本场真机需要在这个窗口重新确认连接。');
    if (!bridge().begin(JSON.stringify({id:state.id, generation:state.generation, owner_id:runtime().ownerId(),
      devices:physical.map(d => ({id:d.id, driver:d.driver}))}))) throw new Error('本地后台播放器未获授权，请暂停后重试。');
  }
  global.SceneHardware = {supported, supportsDevice, connect, connected, report, authorize, stateChanged, stop, panic, backgroundManaged,
    isArmed:()=>!!nativeStatus().active};
  document.addEventListener('DOMContentLoaded', () => {
    setInterval(() => {
      if (!nativeStatus().active && ready.size && nativeStatus().foreground) report().catch(() => {});
    }, 5000);
  });
  // No pagehide/visibility/socket hooks stop the native player. Its own bounded
  // output exchange and local lease handle actual connection failures.
})(window);
