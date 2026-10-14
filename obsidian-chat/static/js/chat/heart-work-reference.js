(() => {
  const validId = id => /^hw_[a-f0-9]{32}$/.test(String(id || ''));
  const pendingKey = 'obsidian_pending_heart_work';
  function card(item) {
    if (!validId(item?.id)) return '';
    return `<button type="button" class="heart-work-reference" data-heart-work-id="${escHtml(item.id)}"><span>心语 · 聊聊这份</span><strong>${escHtml(item.name || '一份心语')}</strong></button>`;
  }
  async function attach(id) {
    if (!validId(id) || !currentConvId) return false;
    if (pendingAttachments.some(item => item?.type === 'heart_work' && item.id === id)) {
      closeSubPage();
      $('input')?.focus();
      return true;
    }
    if (pendingAttachments.filter(item => item?.type === 'heart_work').length >= 3) {
      showToast('每条消息最多引用 3 份心语');
      return false;
    }
    const work = await api('GET', `/api/heart-whispers/works/${encodeURIComponent(id)}`);
    const title = work.cover?.find(block => block.type === 'text')?.text?.trim().slice(0, 80) || '一份心语';
    pendingAttachments.push({type: 'heart_work', id: work.id, name: title});
    renderPreview();
    closeSubPage();
    $('input')?.focus();
    return true;
  }
  let consuming = false;
  async function consumePending() {
    if (consuming || !currentConvId) return;
    let id;
    try { id = sessionStorage.getItem(pendingKey); } catch (_) { return; }
    if (!validId(id)) return;
    consuming = true;
    try {
      if (await attach(id)) sessionStorage.removeItem(pendingKey);
    } catch (_) {
      sessionStorage.removeItem(pendingKey);
      showToast('这份心语暂时无法读取');
    } finally { consuming = false; }
  }
  document.addEventListener('click', event => {
    const button = event.target.closest('[data-heart-work-id]');
    if (button && validId(button.dataset.heartWorkId)) {
      openSubPage('/heart-whispers?work=' + encodeURIComponent(button.dataset.heartWorkId));
    }
  });
  window.ChatHeartWorks = {attach, card, consumePending};
})();
