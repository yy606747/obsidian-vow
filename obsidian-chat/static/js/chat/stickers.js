// Human-side picker. Selecting stages one sticker; the send button delivers it.
(function () {
  let items = [];
  let loadVersion = 0;
  let importing = false;
  const collecting = new Set();

  function close() {
    $("stickerPanel").hidden = true;
    $("stickerToggle").setAttribute("aria-expanded", "false");
  }

  function render() {
    $("stickerCount").textContent = items.length ? String(items.length) : "";
    $("stickerGrid").innerHTML = items.length
      ? items.map(item => {
          const label = item.summary?.visual || "表情";
          return `<button class="sticker-tile" type="button" data-sticker-id="${escHtml(item.id)}" title="${escHtml(label)}" aria-label="选择表情：${escHtml(label)}"><img src="${escHtml(item.url)}" alt="${escHtml(label)}" loading="lazy"></button>`;
        }).join("")
      : '<div class="sticker-panel-empty">还没有表情<br>点「添加表情」存几张喜欢的，之后就能直接选。</div>';
  }

  async function readResponse(response) {
    const data = await response.json();
    if (!response.ok || data.error) throw new Error(data.detail || data.error || "表情加载失败");
    return data;
  }

  async function refresh() {
    const version = ++loadVersion;
    if (!items.length) $("stickerGrid").innerHTML = '<div class="sticker-panel-empty">正在加载表情…</div>';
    try {
      const data = await readResponse(await fetch("/api/stickers", { cache: "no-store" }));
      if (!Array.isArray(data.stickers)) throw new Error("表情列表暂时不可用");
      if (version !== loadVersion) return;
      items = data.stickers.filter(item => item?.type === "sticker" && String(item.url || "").startsWith("/uploads/"));
      render();
    } catch (error) {
      if (version !== loadVersion) return;
      if (items.length) render();
      else $("stickerGrid").innerHTML = '<div class="sticker-panel-empty">表情加载失败，请点右上角刷新重试。</div>';
      showToast(error.message || "表情加载失败", 2600);
    }
  }

  function toggle() {
    if (!$("stickerPanel").hidden) return close();
    $("input")?.blur?.();
    $("stickerPanel").hidden = false;
    $("stickerToggle").setAttribute("aria-expanded", "true");
    return refresh();
  }

  function select(id) {
    const item = items.find(value => value.id === id);
    if (!item) return;
    if (!currentConvId) return showToast("请先选择一个对话", 2200);
    pendingAttachments = pendingAttachments.filter(value => value?.type !== "sticker");
    pendingAttachments.push({ ...item });
    renderPreview();
    close();
    showToast("已选表情，可配文字后发送", 1600);
  }

  async function collectMessage(messageId) {
    if (collecting.has(messageId)) return;
    collecting.add(messageId);
    try {
      const data = await readResponse(await fetch("/api/stickers/collect", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message_id: messageId }),
      }));
      const item = data.sticker;
      if (item?.type !== "sticker" || !item.id || !String(item.url || "").startsWith("/uploads/")) throw new Error("表情收藏失败");
      ++loadVersion;
      items = [item, ...items.filter(value => value.id !== item.id)];
      render();
      showToast("已收藏到表情面板", 2200);
    } catch (error) {
      showToast(error.message || "表情收藏失败，请重试", 2600);
    } finally {
      collecting.delete(messageId);
    }
  }

  async function addFiles(input) {
    if (importing) return;
    const files = Array.from(input.files || []);
    input.value = "";
    if (!files.length) return;
    importing = true;
    $("stickerAddBtn").disabled = true;
    $("stickerAddBtn").textContent = "添加中…";
    let added = 0;
    try {
      for (const file of files) {
        try {
          if (!file.size || file.size > 20 * 1024 * 1024) throw new Error("表情文件须在 20 MB 以内且不能为空");
          const body = new FormData();
          body.append("file", file);
          const data = await readResponse(await fetch("/api/stickers", { method: "POST", body }));
          const item = data.sticker;
          if (item?.type !== "sticker" || !String(item.url || "").startsWith("/uploads/")) throw new Error("表情添加失败");
          ++loadVersion; // An earlier GET must not erase the freshly imported asset.
          items = [item, ...items.filter(value => value.id !== item.id)];
          render();
          added++;
        } catch (error) {
          showToast(`${file.name}：${error.message || "添加失败"}`, 3500);
        }
      }
      if (added === files.length) showToast(`已添加 ${added} 张表情，点选后发送`, 2400);
    } finally {
      importing = false;
      $("stickerAddBtn").disabled = false;
      $("stickerAddBtn").textContent = "＋ 添加表情";
    }
  }

  $("stickerPanel")?.addEventListener("click", event => {
    const tile = event.target.closest("[data-sticker-id]");
    if (tile) return select(tile.dataset.stickerId);
    const button = event.target.closest("[data-sticker-action]");
    if (!button) return;
    if (button.dataset.stickerAction === "close") close();
    if (button.dataset.stickerAction === "refresh") refresh();
    if (button.dataset.stickerAction === "add") $("stickerFileInput").click();
  });
  $("stickerFileInput")?.addEventListener("change", event => addFiles(event.target));
  document.addEventListener("keydown", event => {
    if (event.key === "Escape" && !$("stickerPanel").hidden) close();
  });
  ChatApp.registerModule("stickers", { toggle, close, refresh, select, addFiles, collectMessage });
})();
