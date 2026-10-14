(function (global) {
  'use strict';
  const ownerKey = 'obsidian_scene_owner_id';
  let ownerId = sessionStorage.getItem(ownerKey);
  // A recreated page attaches to the phone's existing run without reauthorizing it.
  try {
    const native = JSON.parse(global.ObsidianSceneDevices?.status?.() || '{}');
    if (native.version >= 2 && native.owner_id) {
      ownerId = native.owner_id; sessionStorage.setItem(ownerKey, ownerId);
      if (native.session_id) sessionStorage.setItem('obsidian_scene_last_id', native.session_id);
    }
  } catch (_) {}
  if (!ownerId) {
    ownerId = `scene_client_${Date.now()}_${Math.random().toString(36).slice(2)}`;
    sessionStorage.setItem(ownerKey, ownerId);
  }
  let current = null, heartbeatBusy = false, panicPromise = null;
  let safeword = '';
  let socketConnected = true, pendingPause = null, pausePromise = null;
  let sceneView = null;
  const listeners = new Set();
  const revisions = new Map();
  const endedIds = new Set();
  const errorLabels = {
    scene_control_busy: '请先结束当前设备控制模式，再进入场景。',
    scene_disabled: '请先启用 AI 主导场景。',
    scene_safeword_required: '请先设置安全词。',
    scene_device_unavailable: '本场设备未全部连接。',
    scene_owner_mismatch: '这个场景由另一个窗口持有，可使用全局停止设备。',
    scene_device_config_in_use: '请先结束场景，再修改设备简介。',
    scene_native_bridge_required: '请在安装了真机播放器的手机上重新确认设备连接。',
  };

  async function request(method, path, body, options = {}) {
    const response = await fetch(path, { method, headers: {'Content-Type': 'application/json'},
      ...options,
      ...(body === undefined ? {} : {body: JSON.stringify(body)}) });
    const data = await response.json();
    if (!response.ok) throw new Error(errorLabels[data.detail] || (typeof data.detail === 'string' ? data.detail : `请求失败（${response.status}）`));
    return data;
  }

  function active() { return !!current && current.status !== 'ended'; }
  function own() { return current?.owner_id === ownerId; }
  function foreground() {
    return typeof global.isChatForeground === 'function' ? global.isChatForeground() : !document.hidden;
  }
  function nativeManaged() { return !!global.SceneHardware?.backgroundManaged?.(current); }
  function rememberDisconnectedScene() {
    if (nativeManaged()) return;
    if (!socketConnected && active() && own() && current.status !== 'paused' && pendingPause?.id !== current.id) {
      pendingPause = {id: current.id, generation:current.generation};
    }
  }
  function notify() {
    global.SceneHardware?.stateChanged(current);
    renderBar();
    for (const listener of listeners) {
      try { listener(current); } catch (e) { console.warn('[scene] view update failed', e); }
    }
  }
  function accept(value) {
    if (!value?.id) return false;
    const old = revisions.get(value.id);
    const next = [Number(value.generation || 0), Number(value.revision || 0)];
    if (old && (next[0] < old[0] || (next[0] === old[0] && next[1] < old[1]))) return false;
    if (endedIds.has(value.id) && value.status !== 'ended') return false;
    if (current && current.id !== value.id && active() && (value.status === 'ended' || value.created_at < current.created_at)) return false;
    revisions.set(value.id, next);
    if (value.status === 'ended') endedIds.add(value.id);
    current = value;
    safeword = sessionStorage.getItem('obsidian_scene_safeword_' + value.id) || safeword;
    if (value.status === 'ended') { sessionStorage.removeItem('obsidian_scene_safeword_' + value.id); safeword = ''; }
    if (pendingPause && (pendingPause.id !== value.id || !own() || ['paused', 'ended'].includes(value.status))) pendingPause = null;
    rememberDisconnectedScene();
    sessionStorage.setItem('obsidian_scene_last_id', value.id);
    notify();
    return true;
  }

  function handleEvent(event) {
    if (event.type === 'scene_state') return accept(event.data);
    if (event.type !== 'scene_playback' || !current || event.data?.id !== current.id || !active()) return false;
    const value = event.data;
    if (value.generation !== current.generation || value.revision < current.revision) return false;
    current = {...current, devices: value.devices, revision: value.revision};
    revisions.set(current.id, [current.generation, current.revision]);
    notify();
    return true;
  }

  async function recover() {
    const state = await request('GET', '/api/scenes/sessions/current');
    if (state) accept(state);
    else {
      const id = current?.id || sessionStorage.getItem('obsidian_scene_last_id');
      if (id) {
        try { accept(await request('GET', `/api/scenes/sessions/${encodeURIComponent(id)}`)); }
        catch (_) { if (!active()) current = null; }
      }
    }
    return current;
  }

  async function start(options) {
    const legacy = global.ControlRuntime?.currentSession?.();
    if (legacy && legacy.status !== 'ended') throw new Error(errorLabels.scene_control_busy);
    const convId = options.conv_id || (typeof currentConvId !== 'undefined' ? currentConvId : null);
    if (!convId) throw new Error('请先选择一段聊天，作为本场使用的主模型来源。');
    await global.SceneHardware?.report();
    const result = await request('POST', '/api/scenes/sessions', {...options, conv_id: convId, owner_id: ownerId});
    safeword = String(options.safeword || '').trim();
    sessionStorage.setItem('obsidian_scene_safeword_' + result.id, safeword);
    accept(result);
    try { global.SceneHardware?.authorize(result); }
    catch (error) { outputFailed(); throw error; }
    return result;
  }

  async function action(name) {
    if (!current) return null;
    if (name === 'resume' && ((!socketConnected && !nativeManaged()) || pendingPause)) throw new Error('请等待连接恢复并确认暂停，再继续场景。');
    if (name === 'resume') await global.SceneHardware?.report();
    if (name === 'pause' || name === 'end' || name === 'finish') {
      if (global.SceneHardware?.isArmed()) pendingPause = {id:current.id, generation:current.generation};
      // Native end/panic is stop-only. Revealing belongs exclusively to the
      // explicit finish request below, even if the native pause wins the race.
      global.SceneHardware?.stop(name === 'finish' ? 'pause' : name);
    }
    const result = await request('POST', `/api/scenes/sessions/${encodeURIComponent(current.id)}/${name}`, {owner_id: ownerId, generation: current.generation});
    accept(result);
    if (name === 'resume') {
      try { global.SceneHardware?.authorize(result); }
      catch (error) { outputFailed(); throw error; }
    }
    return result;
  }

  async function send(text) {
    if (!current) throw new Error('尚未进入场景。');
    if (safeword && String(text).toLocaleLowerCase().includes(safeword.toLocaleLowerCase())) global.SceneHardware?.stop();
    const result = await request('POST', `/api/scenes/sessions/${encodeURIComponent(current.id)}/messages`, {owner_id: ownerId, text});
    accept(result);
    return result;
  }

  async function heartbeat() {
    if (current?.status === 'ended' && current.messages?.length && Date.now()/1000 - current.ended_at > 1800) {
      current = {...current, messages: []};
      notify();
    }
    rememberDisconnectedScene();
    if (pendingPause) { await retryPendingPause(); return; }
    if (nativeManaged() || !socketConnected || heartbeatBusy || !active() || !own() || !foreground() || current.status === 'paused') return;
    heartbeatBusy = true;
    const id = current.id;
    try {
      const state = await request('POST', `/api/scenes/sessions/${encodeURIComponent(id)}/heartbeat`, {owner_id: ownerId});
      if (state.status !== current?.status || state.generation !== current?.generation) await recover();
    } catch (_) {
      // The backend independently pauses after 45 seconds without heartbeats.
      global.showToast?.('场景连接中断；后台将在心跳超时后暂停。');
    } finally { heartbeatBusy = false; }
  }

  async function panic() {
    if (global.SceneHardware?.isArmed() && own()) pendingPause = {id:current.id, generation:current.generation};
    if (global.SceneHardware?.panic) global.SceneHardware.panic();
    else global.SceneHardware?.stop();
    if (panicPromise) return panicPromise;
    panicPromise = request('POST', '/api/scenes/panic').then(() => recover()).finally(() => { panicPromise = null; });
    return panicPromise;
  }

  function connectionChanged(connected) {
    socketConnected = !!connected;
    if (!connected && !nativeManaged()) global.SceneHardware?.stop();
    rememberDisconnectedScene();
    if (connected) recover().catch(() => {}).then(retryPendingPause);
    else retryPendingPause();
  }

  function outputFailed() {
    global.SceneHardware?.stop();
    if (active() && own() && current.status !== 'paused') {
      pendingPause = {id:current.id, generation:current.generation};
      retryPendingPause();
    }
  }

  function retryPendingPause() {
    if (!pendingPause) return Promise.resolve();
    if (pausePromise) return pausePromise;
    const target = pendingPause;
    pausePromise = (async () => {
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 5000);
      try {
        const result = await request('POST', `/api/scenes/sessions/${encodeURIComponent(target.id)}/pause`,
          {owner_id: ownerId, generation: target.generation}, {signal: controller.signal});
        // A late retry belongs only to its original session, never a new one.
        if (pendingPause === target && current?.id === target.id) accept(result);
      } catch (_) {
        // Keep the pending pause across HTTP failures and WS reconnects. The
        // heartbeat timer retries, but cannot renew the backend's 45s lease.
      } finally {
        clearTimeout(timeout);
        pausePromise = null;
      }
    })();
    return pausePromise;
  }

  function renderBar() {
    const bar = document.getElementById('sceneGlobalBar');
    if (!bar) return;
    const overlay = document.getElementById('subPageOverlay');
    const inSubpage = !!overlay?.classList.contains('show');
    const slot = document.getElementById(inSubpage ? 'sceneGlobalSubpageSlot' : 'sceneGlobalChatSlot');
    if (slot && bar.parentElement !== slot) slot.append(bar);
    // Keep a fallback stop while the scene page is loading or fails to render.
    // Only the actual mounted child may take over the visible controls.
    const localControls = inSubpage && overlay.dataset.route === '/scenes' &&
      sceneView?.id === current?.id && sceneView?.source === document.getElementById('subPageFrame')?.contentWindow;
    bar.hidden = !active() || localControls;
    if (!active()) return;
    const status = document.getElementById('sceneGlobalStatus');
    if (status) status.textContent = current.status === 'active' && current.phase === 'aftercare' ? '收尾中' :
      ({preparing:'正在准备场景', active:'场景进行中', paused:'场景已暂停'})[current.status] || current.status;
    const pause = document.getElementById('sceneGlobalPause');
    if (pause) { pause.disabled = !own() || current.status === 'preparing'; pause.textContent = current.status === 'paused' ? '继续' : '暂停'; }
  }

  function navigationChanged() { sceneView = null; renderBar(); }
  function sceneViewReady(source, id) {
    if (source !== document.getElementById('subPageFrame')?.contentWindow) return;
    sceneView = id ? {source, id} : null;
    renderBar();
  }

  global.SceneRuntime = {
    request, start, action, send, panic, recover, heartbeat, handleEvent, accept, connectionChanged,
    outputFailed, navigationChanged, sceneViewReady,
    canOutput: () => active() && own() && ['preparing','active'].includes(current.status) && !pendingPause &&
      (nativeManaged() ? global.SceneHardware.isArmed() : socketConnected && foreground()),
    background: () => { if (!nativeManaged() && global.SceneHardware?.isArmed()) outputFailed(); },
    current: () => current, isActive: active, isOwner: own, ownerId: () => ownerId,
    subscribe(fn) { listeners.add(fn); fn(current); return () => listeners.delete(fn); },
  };
  global.ChatApp?.registerModule?.('sceneRuntime', global.SceneRuntime);
  document.addEventListener('DOMContentLoaded', () => {
    const fail = error => global.showToast?.(error.message);
    document.getElementById('sceneGlobalOpen')?.addEventListener('click', () => global.openSubPage('/scenes'));
    document.getElementById('sceneGlobalPause')?.addEventListener('click', () => action(current?.status === 'paused' ? 'resume' : 'pause').catch(fail));
    document.getElementById('sceneGlobalEnd')?.addEventListener('click', () => panic().catch(fail));
    recover().catch(() => {});
    setInterval(heartbeat, 15000);
    if (new URLSearchParams(location.search).has('scene')) global.openSubPage('/scenes');
  });
  document.addEventListener('visibilitychange', () => { if (foreground()) recover().then(heartbeat).catch(() => {}); });
})(window);
