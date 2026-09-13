/* Document cards and an on-demand, plain-source reader. */
(() => {
  'use strict';
  const uploading = new Map();
  let serial = 0, reader = null, request = null, previousFocus = null, addedCover = false;
  const isDocument = item => item?.type === 'document';
  const number = value => Math.max(0, Number(value) || 0).toLocaleString();
  function kind(item) { return String(item.name || '').split('.').pop().toUpperCase().slice(0, 5) || 'FILE'; }
  function info(item) {
    return [kind(item), item.page_count ? number(item.page_count) + ' 页' : '', number(item.char_count) + ' 字'].filter(Boolean).join(' · ');
  }
  function card(item) {
    return `<button type="button" class="document-card" data-document-open="${escHtml(item.id)}" aria-label="查看文件 ${escHtml(item.name)}">
      <span class="document-icon" aria-hidden="true">${window.VowUI?.icon('log') || '▤'}<small>${escHtml(kind(item))}</small></span>
      <span class="document-details"><strong>${escHtml(item.name)}</strong><span>${escHtml(info(item))}</span>${item.warnings?.length ? '<span class="document-warning">部分页面未提取到文字</span>' : ''}</span>
      <span class="document-open-icon" aria-hidden="true">${window.VowUI?.icon('arrow') || '›'}</span></button>`;
  }
  function progress() {
    return [...uploading.values()].map(name => `<div class="document-progress" role="status"><span>${escHtml(name)}</span><small>正在上传并读取…</small></div>`).join('');
  }
  async function upload(file) {
    if (file.size > 20 * 1024 * 1024) throw new Error('文件太大，最大 20 MB。');
    const id = ++serial;
    uploading.set(id, file.name); renderPreview();
    try {
      const form = new FormData(); form.append('file', file);
      const response = await fetch('/api/upload', {method: 'POST', body: form});
      const data = await response.json();
      if (!response.ok || data.error) throw new Error(data.error || '上传失败，请重试。');
      return data;
    } finally { uploading.delete(id); renderPreview(); }
  }
  function close() {
    request?.abort(); request = null;
    if (reader?.open) reader.close();
    if (addedCover) document.body.classList.remove('vow-covered');
    addedCover = false;
    previousFocus?.focus?.(); previousFocus = null;
  }
  function ensureReader() {
    if (reader) return reader;
    reader = document.createElement('dialog');
    reader.className = 'document-reader'; reader.setAttribute('aria-labelledby', 'documentReaderTitle');
    reader.innerHTML = '<header><div><h2 id="documentReaderTitle"></h2><p data-document-info></p></div><button type="button" data-document-close aria-label="关闭文件预览">✕</button></header>'
      + '<div class="document-reader-body"><p class="document-reader-warning" hidden></p><pre tabindex="0" data-document-text></pre></div>'
      + '<footer><span>提取正文</span><a data-document-download target="_blank" rel="noopener noreferrer">打开原文件</a></footer>';
    reader.querySelector('[data-document-close]').addEventListener('click', close);
    reader.addEventListener('cancel', event => { event.preventDefault(); close(); });
    reader.addEventListener('click', event => { if (event.target === reader) close(); });
    document.body.appendChild(reader);
    return reader;
  }
  async function open(id) {
    if (!/^doc_[a-f0-9]{32}$/.test(id)) return;
    const dialog = ensureReader();
    request?.abort(); const controller = new AbortController(); request = controller;
    previousFocus = document.activeElement;
    dialog.querySelector('h2').textContent = '正在读取…';
    dialog.querySelector('[data-document-info]').textContent = '';
    dialog.querySelector('[data-document-text]').textContent = '';
    dialog.querySelector('.document-reader-warning').hidden = true;
    const download = dialog.querySelector('[data-document-download]'); download.hidden = true;
    addedCover = !document.body.classList.contains('vow-covered');
    document.body.classList.add('vow-covered');
    dialog.showModal();
    try {
      const response = await fetch('/api/documents/' + id, {signal:controller.signal});
      const data = await response.json();
      if (!response.ok || data.error) throw new Error(data.error || '文件读取失败。');
      if (request !== controller) return;
      dialog.querySelector('h2').textContent = data.name;
      dialog.querySelector('[data-document-info]').textContent = info(data);
      dialog.querySelector('[data-document-text]').textContent = data.text;
      const warning = dialog.querySelector('.document-reader-warning');
      warning.textContent = (data.warnings || []).join('\n'); warning.hidden = !warning.textContent;
      if (/^\/uploads\/doc_[a-f0-9]{32}\.(pdf|txt|md|docx)$/.test(data.url)) {
        download.href = data.url; download.hidden = false;
      }
      dialog.querySelector('.document-reader-body').scrollTop = 0;
    } catch (error) {
      if (error.name === 'AbortError') return;
      dialog.querySelector('h2').textContent = '暂时无法读取';
      dialog.querySelector('[data-document-text]').textContent = error.message;
    }
  }
  function discard(item) {
    if (!isDocument(item) || !/^doc_[a-f0-9]{32}$/.test(item.id)) return;
    fetch('/api/documents/' + item.id, {method:'DELETE'}).catch(() => {});
  }
  document.addEventListener('click', event => {
    const button = event.target.closest('[data-document-open]');
    if (button) { event.preventDefault(); open(button.dataset.documentOpen); }
  });
  window.ChatDocuments = { isDocument, card, info, progress, upload, open, close, discard, busy: () => uploading.size > 0 };
  ChatApp.registerModule('documents', window.ChatDocuments);
})();
