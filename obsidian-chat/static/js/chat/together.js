(function () {
  'use strict';
  let active = null, opening = false, syncing = null;
  const byId = id => document.getElementById(id);

  function apply(session) {
    const previous = active?.id === session?.id ? active : null;
    active = session?.id ? {...previous, ...session, ai_name: session.ai_name || previous?.ai_name || ''} : null;
    document.body?.classList.toggle('together-active', Boolean(active));
    const input = byId('input');
    if (input) input.readOnly = Boolean(active);
    const notice = byId('togetherChatNotice');
    if (notice) notice.hidden = !active;
    const text = byId('togetherChatStatus');
    if (text) text.textContent = active?.ending ? '已经结束，正在同步。写好的话还在。'
      : active?.ai_name ? `和 ${active.ai_name} 相伴中。` : '正在相伴中。';
    return Boolean(active);
  }
  function localState() {
    try {
      const local = JSON.parse(window.ObsidianTogether?.state?.() || '{}');
      if (local.id) apply(local);
    } catch (_) {}
  }
  async function sync() {
    localState();
    if (typeof fetch !== 'function') return Boolean(active);
    if (!syncing) {
      syncing = (async () => {
        try {
          const response = await fetch('/api/together/sessions/current', {cache: 'no-store'});
          if (response.ok) apply(await response.json());
          localState();
        } catch (_) { /* Preserve a known active session while offline. */ }
        finally { syncing = null; }
        return Boolean(active);
      })();
    }
    return syncing;
  }
  async function guard() {
    if (!await sync()) return false;
    window.showToast?.('回到相伴里接着聊吧，这里的草稿会留着。');
    return true;
  }
  function resume() {
    if (window.ObsidianTogether?.resume) window.ObsidianTogether.resume();
    else window.showToast?.('这次陪伴在手机上继续，结束后就可以在这里聊了。');
  }
  async function openTogether() {
    const bridge = window.ObsidianTogether;
    if (!bridge?.open) { window.showToast?.('请在支持“相伴”的 Android 客户端中打开'); return; }
    localState();
    if (active && bridge.resume) { resume(); return; }
    const conv = typeof currentConvId === 'string' ? currentConvId : '';
    if (!conv) { window.showToast?.('先打开一段聊天，再一起待会儿'); return; }
    if (opening) return;
    opening = true;
    try {
      if (typeof sending !== 'undefined' && sending) {
        window.showToast?.('等这句回完，就一起待会儿。');
        while (sending) await new Promise(resolve => setTimeout(resolve, 200));
      }
      const model = byId('modelSelect')?.value || '';
      const value = Number(byId('contextSlider')?.value ?? 0);
      const count = Number.isFinite(value) ? Math.max(0, Math.min(100, Math.trunc(value))) : 0;
      bridge.open(conv, model, count);
      if (typeof closeSidebar === 'function') closeSidebar();
    } finally { opening = false; }
  }
  byId('togetherOpen')?.addEventListener('click', openTogether);
  byId('togetherResume')?.addEventListener('click', resume);
  window.ChatTogether = {open: openTogether, resume, sync, guard, apply};
  window.ChatApp?.registerModule?.('together', window.ChatTogether);
  if (typeof fetch === 'function') {
    sync();
    document.addEventListener('visibilitychange', () => { if (!document.hidden) sync(); });
    window.addEventListener('focus', sync);
    setInterval(() => { if (!document.hidden) sync(); }, 10000);
  }
})();
