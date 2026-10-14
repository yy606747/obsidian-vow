(function () {
  'use strict';
  if (window.parent === window || !window.parent.SceneRuntime) {
    location.replace('/chat?scene=1');
    return;
  }
  const runtime = window.parent.SceneRuntime;
  const $ = id => document.getElementById(id);
  let config, devices = [], view = null, rating = null, feedbackFor = null, requestId = null;
  let unsubscribe, feedbackTimer = null, feedbackDirty = false;
  let dismissedReveal = null, entering = false, sending = false, disposed = false, messageSignature = null;
  const chosen = new Set();
  const api = runtime.request;
  const broadcast = device => ['muse_ble', 'sk30_ble', 'sk40_ble'].includes(device.driver);
  function error(err) { $('sceneError').textContent = err.message || String(err); $('sceneError').hidden = false; }
  async function run(work) { $('sceneError').hidden = true; try { return await work(); } catch (err) { error(err); } }
  function text(tag, content, cls) { const el = document.createElement(tag); el.textContent = content; if (cls) el.className = cls; return el; }
  function ready() {
    const count = devices.filter(d => d.connected && chosen.has(d.id)).length;
    $('sceneSelectedCount').textContent = `已选 ${count} 台`;
    $('sceneEnter').disabled = entering || !config?.enabled || !config?.compiler_ready || !count || !$('sceneSafeword').value.trim();
    $('sceneEntryHint').textContent = entering ? '正在准备…' : !config?.enabled ? '先启用小剧场' :
      !config.compiler_ready ? '先配置设备编译模型' : !count ? '先连接并选择设备' :
      !$('sceneSafeword').value.trim() ? '请填写本场安全词' : `本场授权 ${count} 台设备 · 准备就绪`;
  }
  function renderDevices() {
    const physical = devices.some(d => d.driver !== 'mock');
    $('sceneDeviceKind').textContent = physical ? '真机 / 模拟' : '模拟设备版';
    $('sceneOutputNote').textContent = physical ? '数值是手机播放器采用的协议输出，不等于实测物理强度。枕趣炮机与失控系列通过广播控制，没有设备回执。' : '以下为实际模拟输出，不代表真实设备强度。';
    $('sceneDeviceFootnote').textContent = physical ? '真机由手机后台播放器执行，切换应用或锁屏后继续；通知栏可停下设备，不会揭卡。' : '仅模拟输出 · 不连接真实设备';
    $('sceneDevices').replaceChildren();
    for (const device of devices) {
      const row = text('div', '', 'scene-device');
      const label = document.createElement('label');
      label.className = 'scene-device-choice';
      const check = document.createElement('input'); check.type = 'checkbox';
      check.checked = chosen.has(device.id); check.disabled = !device.connected;
      check.setAttribute('aria-label', `本场允许使用${device.name}`);
      row.dataset.selected = String(check.checked);
      check.addEventListener('change', () => {
        check.checked ? chosen.add(device.id) : chosen.delete(device.id);
        row.dataset.selected = String(check.checked); ready();
      });
      label.append(check, document.createTextNode(` ${device.name}`));
      const needsUpdate = window.parent.SceneHardware?.supportsDevice && !window.parent.SceneHardware.supportsDevice(device);
      const button = text('button', needsUpdate ? '需更新 APK' : device.connected ? '断开' : broadcast(device) ? '已开机，准备广播' : '连接');
      button.disabled = !!needsUpdate;
      button.type = 'button';
      button.setAttribute('aria-label', `${device.connected ? '断开' : '连接'}${device.name}`);
      button.addEventListener('click', () => run(async () => {
        button.disabled = true;
        try {
          devices = window.parent.SceneHardware ? await window.parent.SceneHardware.connect(device, !device.connected) :
            await api('POST', `/api/scenes/devices/${encodeURIComponent(device.id)}/${device.connected ? 'disconnect' : 'connect'}`);
          if (device.connected) chosen.delete(device.id);
          renderDevices();
        } finally { button.disabled = false; }
      }));
      const top = text('div', '', 'scene-row'); top.append(label, button);
      row.append(top, text('p', `${device.connected ? (broadcast(device) ? '广播已准备，无设备回执' : '已连接') : '未连接'} · ${device.description || '简介尚未填写。'}`));
      if (device.driver === 'sk30_ble' || device.driver === 'sk40_ble') row.append(text('p', '开机后请先在这里准备广播，暂时不要用 Cachito 原厂应用连接；若已被原厂应用占用，请重启设备。', 'hint'));
      $('sceneDevices').append(row);
    }
    if (!devices.length) $('sceneDevices').append(text('p', '还没有可用设备。', 'hint'));
    ready();
  }
  function renderDescriptions() {
    $('sceneDeviceDescriptions').replaceChildren();
    for (const device of config.devices) {
      const field = document.createElement('textarea'); field.className = 'form-input'; field.rows = 3;
      field.dataset.deviceId = device.id; field.value = device.description; field.maxLength = 8000;
      const label = text('label', device.name, 'form-label'); label.append(field);
      $('sceneDeviceDescriptions').append(label);
    }
  }
  function render(state) {
    view = state;
    const active = !!state && state.status !== 'ended';
    const revealed = !!state?.card && !active && dismissedReveal !== state.id;
    const waitingReveal = !active && !!state?.has_card && !state.card && dismissedReveal !== state.id;
    const showConversation = active || revealed || waitingReveal;
    $('sceneSetup').hidden = showConversation;
    $('sceneLive').hidden = !showConversation;
    $('sceneReveal').hidden = !revealed;
    $('scenePage').dataset.stage = active || waitingReveal ? 'live' : revealed ? 'reveal' : 'setup';
    $('scenePage').dataset.status = state?.status || 'idle';
    for (let step = 1; step <= 3; step++) $('sceneStep' + step).setAttribute('aria-current', step === (active ? 2 : revealed ? 3 : 1) ? 'step' : 'false');
    $('sceneIntro').textContent = active ? '有想说的，随时说。' : revealed ? '看看开始前的想法。' : '按彼此的了解，准备这一次。';
    $('sceneHeroNote').textContent = revealed ? '这次的感受，可以留给以后参考。' : '不用一直回应，想调整时再说。';
    $('sceneEnabled').checked = !!config?.enabled;
    $('sceneConfigSave').disabled = active;
    $('sceneAddHardware').disabled = active || !window.parent.SceneHardware?.supported();
    ready();
    if (!revealed) $('sceneCardText').textContent = '';
    if (showConversation) {
      if (active) dismissedReveal = null;
      $('sceneStatus').textContent = state.status === 'active' && state.phase === 'aftercare' ? '收尾中' :
        ({preparing:'正在准备', active:'场景进行中', paused:'场景已暂停', ended:'本场已结束'})[state.status];
      const reasons = {output_link_lost:'手机输出连接已中断，设备已暂停。恢复连接后可手动继续。', heartbeat_lost:'连接心跳已中断，设备已暂停。恢复连接后可以继续。', device_disconnected:'设备已断开，重新连接后可以继续。', owner_paused:'设备已暂停，仍可以聊天。只有手动继续才恢复设备。', owner_stopped:'设备已停止，仍可以聊天。只有手动继续才恢复设备。', safeword:'安全词已生效，设备保持暂停。仍可以聊天，只有手动继续才恢复设备。', panic:'设备已急停，仍可以聊天。只有手动继续才恢复设备。'};
      $('sceneReason').textContent = reasons[state.reason] || (state.reason?.startsWith('compile_failed') ? '设备安排未能完成，已暂停。可以重试或结束。' :
        state.reason?.startsWith('model_failed') ? '这次响应未能完成，已暂停。可以重试或结束。' : state.reason) ||
        (state.status === 'preparing' ? '正在抽取匹配卡；没有合适的卡时会即时准备。' : '切回聊天、切换应用或锁屏都可继续。需要停下设备时，可用通知栏或这里的暂停。');
      if (!active) $('sceneReason').textContent = waitingReveal ? '本场已中断，设备不会自动恢复。准备卡还没有揭开。' : '这次的交流暂留在这里，稍后会清理。';
      if (state.status === 'paused' && (state.devices || []).some(d => d.driver && d.driver !== 'mock')) {
        $('sceneReason').textContent = '设备已暂停，仍可以聊天。请确认实物已停止；若仍运行，使用设备上的停止或电源键。确认连接后，可手动继续。';
      }
      $('sceneReason').hidden = state.status === 'active' && !state.reason;
      const visible = (state.messages || []).filter(m => m.content);
      const signature = JSON.stringify([state.id, visible]);
      if (signature !== messageSignature) {
        const atBottom = $('sceneMessages').scrollHeight - $('sceneMessages').scrollTop - $('sceneMessages').clientHeight < 70;
        $('sceneMessages').replaceChildren(...visible.map(m => text('div', m.content, `scene-message ${m.role}`)));
        if (atBottom) $('sceneMessages').scrollTop = $('sceneMessages').scrollHeight;
        messageSignature = signature;
      }
      $('sceneQuiet').hidden = visible.length > 0;
      $('sceneQuiet').textContent = !active ? '本场临时消息已清理，准备卡仍会保留。' : state.status === 'preparing' ? '正在准备这次的安排。' : '有想说的，直接说就好。';
      renderPlayback(state.devices || []);
      $('sceneOutputPanel').hidden = !active;
      $('scenePrivacy').hidden = !active;
      $('scenePause').textContent = state.status === 'paused' ? '继续 / 重试' : '暂停';
      $('scenePause').hidden = !active;
      $('scenePause').disabled = state.status === 'preparing' || !runtime.isOwner();
      $('sceneReconnect').hidden = state.status !== 'paused' || !runtime.isOwner() || !(state.devices || []).some(d => d.driver !== 'mock');
      $('sceneEnd').textContent = !runtime.isOwner() ? '停止设备' : state.status === 'preparing' ? '取消准备' : '结束并揭卡';
      $('sceneEnd').hidden = revealed;
      $('sceneEnd').disabled = !active && !runtime.isOwner();
      $('sceneMessageForm').hidden = !active || state.status === 'preparing' || !runtime.isOwner();
    }
    if (revealed) {
      $('sceneCardText').textContent = state.card.raw_text;
      if (feedbackFor !== state.id) {
        feedbackFor = state.id;
        feedbackDirty = false;
        rating = state.feedback?.rating || null;
        $('sceneFeedbackReason').value = state.feedback?.reason || '';
        $('sceneFeedbackStatus').textContent = '';
        paintVotes();
        loadFeedback(state.id);
        loadHistory();
      }
    } else if (state?.reason?.startsWith('preparation_failed')) {
      error(new Error('准备失败，请检查模型配置后重新进入。'));
      requestId = null;
    }
    elapsed();
    runtime.sceneViewReady?.(window, active ? state.id : null);
  }

  function resizeComposer() {
    const field = $('sceneMessage');
    field.style.height = 'auto';
    field.style.height = `${Math.min(field.scrollHeight, 112)}px`;
  }
  function renderPlayback(items) {
    $('sceneOutputStatus').textContent = items.length ? `${items.filter(d => d.position?.playing).length} / ${items.length} 台运行中` : '等待安排';
    $('scenePlayback').replaceChildren();
    for (const device of items) {
      const description = config?.devices.find(d => d.id === device.id);
      const tile = text('div', '', 'scene-output-device');
      tile.append(text('h3', description?.name || device.id), text('p', `${device.label || device.id} · ${device.connected ? (broadcast(device) ? '广播已准备' : '已连接') : '已断开'} · ${device.position?.playing ? `第 ${device.position.step + 1} 步` : '输出已停止'}`, 'hint'));
      for (const [channel, value] of Object.entries(device.output || {})) {
        const row = text('div', '', 'scene-channel');
        row.append(text('span', channel), text('output', String(value)));
        const bounds = description?.capabilities?.channels?.[channel];
        if (bounds && bounds.max > bounds.min && Number.isFinite(value)) {
          const meter = document.createElement('meter');
          meter.min = bounds.min; meter.max = bounds.max; meter.value = value;
          meter.setAttribute('aria-label', `${description.name} ${channel} ${description.driver === 'mock' ? '模拟' : '协议'}输出`);
          row.append(meter);
        }
        tile.append(row);
      }
      $('scenePlayback').append(tile);
    }
  }
  function elapsed() {
    const seconds = Math.max(0, Math.floor(Date.now()/1000 - (view?.started_at || Date.now()/1000)));
    $('sceneElapsed').textContent = view?.started_at && view.status !== 'ended' ? `${String(Math.floor(seconds/60)).padStart(2, '0')}:${String(seconds%60).padStart(2, '0')}` : '';
  }
  function paintVotes() {
    for (const button of document.querySelectorAll('[data-scene-vote]')) button.setAttribute('aria-pressed', String((button.dataset.sceneVote || null) === rating));
  }
  async function loadFeedback(id) {
    try {
      const state = await api('GET', `/api/scenes/sessions/${encodeURIComponent(id)}`);
      if (disposed || feedbackFor !== id) return;
      if (!feedbackDirty) {
        rating = state.feedback?.rating || null;
        $('sceneFeedbackReason').value = state.feedback?.reason || '';
        paintVotes();
      }
      const status = state.feedback?.note_status;
      $('sceneFeedbackStatus').textContent = ({pending:'评价已保存，正在考虑是否更新笔记。', processed:'评价已保存，笔记处理完成。', failed:'评价已保存；笔记更新失败，可再次保存以重试。', skipped:'未提交评价，不更新笔记。'})[status] || '可以跳过评价。';
      clearTimeout(feedbackTimer);
      if (status === 'pending') feedbackTimer = setTimeout(() => loadFeedback(id), 2000);
    } catch (err) { error(err); }
  }
  async function loadHistory() {
    try {
      const items = await api('GET', '/api/scenes/sessions');
      if (disposed) return;
      $('sceneHistory').replaceChildren();
      for (const item of items) {
        const details = document.createElement('details');
        details.append(text('summary', new Date(item.ended_at*1000).toLocaleString()));
        if (item.card) details.append(text('pre', item.card.raw_text, 'scene-prose'));
        else {
          const reveal = text('button', '揭开这张卡');
          reveal.type = 'button'; reveal.disabled = item.owner_id !== runtime.ownerId();
          reveal.addEventListener('click', () => run(async () => {
            const state = await api('POST', `/api/scenes/sessions/${encodeURIComponent(item.id)}/finish`, {owner_id:runtime.ownerId()});
            if (runtime.current()?.id === item.id) runtime.accept(state);
            await loadHistory();
          }));
          details.append(text('p', '本场已中断，尚未揭卡。', 'hint'), reveal);
        }
        $('sceneHistory').append(details);
      }
      if (!items.length) $('sceneHistory').append(text('p', '还没有结束的场景。结束后，原卡会保存在这里。', 'scene-empty-history'));
    } catch (err) { error(err); }
  }
  $('sceneEnter').addEventListener('click', () => run(async () => {
    if (entering) return;
    const safe = $('sceneSafeword').value.trim();
    if (!safe) throw new Error('请设置安全词。');
    const selected = devices.filter(d => d.connected && chosen.has(d.id)).map(d => d.id);
    if (!selected.length) throw new Error('请连接并勾选本场设备。');
    if (!config?.enabled || !config?.compiler_ready) throw new Error('请先启用功能并配置设备编译模型。');
    requestId ||= `scene_request_${Date.now()}_${Math.random().toString(36).slice(2)}`;
    entering = true; ready();
    try {
      localStorage.setItem('obsidian_safeword', safe);
      await runtime.start({request_id: requestId, device_ids: selected, safeword: safe});
      requestId = null;
    } finally { entering = false; ready(); }
  }));
  $('scenePause').addEventListener('click', () => run(() => runtime.action(view?.status === 'paused' ? 'resume' : 'pause')));
  $('sceneReconnect').addEventListener('click', () => run(async () => {
    $('sceneReconnect').disabled = true;
    try {
      for (const device of view.devices || []) {
        if (device.driver && device.driver !== 'mock') devices = await window.parent.SceneHardware.connect(device, true);
      }
      await runtime.recover();
    } finally { $('sceneReconnect').disabled = false; }
  }));
  $('sceneEnd').addEventListener('click', () => run(() => runtime.isOwner() ? runtime.action('finish') : runtime.panic()));
  $('sceneMessageForm').addEventListener('submit', event => {
    event.preventDefault();
    const content = $('sceneMessage').value.trim();
    if (content && !sending) run(async () => {
      const id = view?.id;
      sending = true; $('sceneSend').disabled = true;
      try {
        await runtime.send(content);
        if (view?.id === id && $('sceneMessage').value.trim() === content) {
          $('sceneMessage').value = ''; resizeComposer();
        }
      } finally { sending = false; $('sceneSend').disabled = false; }
    });
  });
  $('sceneMessage').addEventListener('input', resizeComposer);
  $('sceneEnabled').addEventListener('change', () => run(async () => {
    try { config = {...config, ...await api('PUT', '/api/scenes/config', {...config, enabled: $('sceneEnabled').checked})}; }
    finally { render(runtime.current()); }
  }));
  $('sceneConfigSave').addEventListener('click', () => run(async () => {
    const descriptions = new Map([...document.querySelectorAll('[data-device-id]')].map(el => [el.dataset.deviceId, el.value]));
    config = {...config, ...await api('PUT', '/api/scenes/config', {...config, devices: config.devices.map(d => ({...d, description: descriptions.get(d.id) ?? d.description}))})};
    devices = await api('GET', '/api/scenes/devices'); renderDevices();
    $('sceneConfigStatus').textContent = '简介已保存，下次入场生效。';
  }));
  $('sceneAddHardware').addEventListener('click', () => run(async () => {
    const profiles = await api('GET', '/api/scenes/hardware');
    const missing = profiles.filter(p => !config.devices.some(d => d.driver === p.driver));
    config = {...config, ...await api('PUT', '/api/scenes/config', {...config, devices:[...config.devices, ...missing]})};
    devices = await api('GET', '/api/scenes/devices');
    renderDevices(); renderDescriptions();
    $('sceneConfigStatus').textContent = `已补入 ${missing.length} 台设备。现支持枕趣炮机、啵啵贝、失控 3.0、失控 4.0 与野行 Pro，入场前分别连接并勾选。原有自填简介和范围保持不变。`;
  }));
  $('sceneFeedbackReason').addEventListener('input', () => { feedbackDirty = true; });
  for (const button of document.querySelectorAll('[data-scene-vote]')) button.addEventListener('click', () => { feedbackDirty = true; rating = button.dataset.sceneVote || null; paintVotes(); });
  $('sceneFeedbackSave').addEventListener('click', () => run(async () => {
    if (!view?.card) return;
    const id = view.id;
    await api('PUT', `/api/scenes/sessions/${encodeURIComponent(id)}/feedback`, {rating, reason: $('sceneFeedbackReason').value});
    clearTimeout(feedbackTimer); await loadFeedback(id);
  }));
  $('sceneAgain').addEventListener('click', () => { dismissedReveal = view?.id; render(view); $('sceneSetup').scrollIntoView({block: 'start'}); });
  $('sceneSafeword').addEventListener('input', ready);
  $('sceneOpenSettings').addEventListener('click', () => window.parent.openSubPage('/settings'));
  $('sceneSafeword').value = localStorage.getItem('obsidian_safeword') || '';
  const timer = setInterval(elapsed, 1000);
  window.addEventListener('pagehide', () => {
    disposed = true; unsubscribe?.(); clearInterval(timer); clearTimeout(feedbackTimer);
    runtime.sceneViewReady?.(window, null);
  });
  run(async () => {
    [config, devices] = await Promise.all([api('GET', '/api/scenes/config'), api('GET', '/api/scenes/devices')]);
    $('sceneModelHint').textContent = config.compiler_ready ? '使用当前聊天的主模型；设备编译槽位已配置。' : '设备编译槽位尚未配置。可先保存设置，再到“槽位 / 模型”配置 scene_compiler。';
    renderDevices(); renderDescriptions();
    unsubscribe = runtime.subscribe(render);
    await runtime.recover(); await loadHistory();
  });
})();
