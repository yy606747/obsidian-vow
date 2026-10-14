// Human-side picker. Selecting stages one sticker; the send button delivers it.
(function () {
  let items = [];
  let loadVersion = 0;
  let importing = false;
  let managing = false;
  let removing = false;
  const collecting = new Set();
  const selected = new Set();

  function close() {
    $("stickerPanel").hidden = true;
    $("stickerToggle").setAttribute("aria-expanded", "false");
    managing = false;
    selected.clear();
    render();
  }

  function updateControls() {
    const busy = importing || removing || collecting.size > 0;
    $("stickerAddBtn").disabled = busy || managing;
    $("stickerAddBtn").textContent = importing ? "添加中…" : "＋ 添加表情";
    $("stickerManageBtn").disabled = busy || (!managing && !items.length);
    $("stickerManageBtn").textContent = managing ? "完成" : "管理";
    $("stickerManageBtn").setAttribute("aria-pressed", String(managing));
    $("stickerRefreshBtn").disabled = removing;
    $("stickerManageBar").hidden = !managing;
    $("stickerSelectedCount").textContent = `已选 ${selected.size} 张`;
    $("stickerSelectAllBtn").disabled = busy || !items.length;
    $("stickerSelectAllBtn").textContent = items.length && selected.size === items.length ? "取消全选" : "全选";
    $("stickerRemoveBtn").disabled = busy || !selected.size;
    $("stickerRemoveBtn").textContent = removing ? "移除中…" : selected.size ? `移除（${selected.size}）` : "移除";
  }

  function render() {
    const available = new Set(items.map(item => item.id));
    for (const id of selected) if (!available.has(id)) selected.delete(id);
    if (!items.length) managing = false;
    updateControls();
    $("stickerCount").textContent = items.length ? String(items.length) : "";
    $("stickerGrid").innerHTML = items.length
      ? items.map(item => {
          const label = item.summary?.visual || "表情";
          const checked = managing && selected.has(item.id);
          const selection = managing ? `<span class="sticker-select-indicator" aria-hidden="true">✓</span>` : "";
          return `<button class="sticker-tile${checked ? " is-selected" : ""}" type="button" data-sticker-id="${escHtml(item.id)}" title="${escHtml(label)}" aria-label="${managing ? "勾选" : "选择"}表情：${escHtml(label)}"${managing ? ` aria-pressed="${checked}"` : ""}${removing ? " disabled" : ""}><img src="${escHtml(item.url)}" alt="${escHtml(label)}" loading="lazy">${selection}</button>`;
        }).join("")
      : '<div class="sticker-panel-empty">还没有表情<br>点「添加表情」存几张喜欢的，之后就能直接选。</div>';
  }

  async function readResponse(response) {
    const data = await response.json();
    if (!response.ok || data.error) throw new Error(data.detail || data.error || "表情加载失败");
    return data;
  }

  async function refresh() {
    if (removing) return;
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
    if (removing) return;
    const item = items.find(value => value.id === id);
    if (!item) return;
    if (managing) {
      if (selected.has(id)) selected.delete(id);
      else selected.add(id);
      return render();
    }
    if (!currentConvId) return showToast("请先选择一个对话", 2200);
    pendingAttachments = pendingAttachments.filter(value => value?.type !== "sticker");
    pendingAttachments.push({ ...item });
    renderPreview();
    close();
    showToast("已选表情，可配文字后发送", 1600);
  }

  function toggleManage() {
    if (removing || importing || collecting.size || !items.length) return;
    managing = !managing;
    selected.clear();
    render();
  }

  function selectAll() {
    if (!managing || removing || importing || collecting.size) return;
    if (selected.size === items.length) selected.clear();
    else for (const item of items) selected.add(item.id);
    render();
  }

  async function removeSelected() {
    if (!managing || !selected.size || removing || importing || collecting.size) return;
    const ids = Array.from(selected);
    removing = true;
    ++loadVersion; // Discard listings started before the removal.
    render();
    try {
      const data = await readResponse(await fetch("/api/stickers/remove", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ids }),
      }));
      if (!Array.isArray(data.removed_ids)) throw new Error("invalid_removal_response");
      const acknowledged = new Set(data.removed_ids);
      if (!ids.every(id => acknowledged.has(id))) throw new Error("incomplete_removal_response");
      ++loadVersion;
      const removed = new Set(ids);
      items = items.filter(item => !removed.has(item.id));
      for (const id of removed) selected.delete(id);
      const remaining = pendingAttachments.filter(item => item?.type !== "sticker" || !removed.has(item.id));
      if (remaining.length !== pendingAttachments.length) {
        pendingAttachments = remaining;
        renderPreview();
      }
      showToast(`已从我的表情移除 ${ids.length} 张`, 2200);
    } catch (error) {
      showToast("表情移除失败，请重试", 2600);
    } finally {
      removing = false;
      render();
    }
  }

  async function collectMessage(messageId) {
    if (removing) return showToast("正在移除表情，请稍后再收藏", 2200);
    if (collecting.has(messageId)) return;
    collecting.add(messageId);
    updateControls();
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
      updateControls();
    }
  }

  async function addFiles(input) {
    if (importing || managing || removing) return;
    const files = Array.from(input.files || []);
    input.value = "";
    if (!files.length) return;
    importing = true;
    updateControls();
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
      updateControls();
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
    if (button.dataset.stickerAction === "manage") toggleManage();
    if (button.dataset.stickerAction === "select-all") selectAll();
    if (button.dataset.stickerAction === "remove") removeSelected();
  });
  $("stickerFileInput")?.addEventListener("change", event => addFiles(event.target));
  document.addEventListener("keydown", event => {
    if (event.key === "Escape" && !$("stickerPanel").hidden) close();
  });
  ChatApp.registerModule("stickers", { toggle, close, refresh, select, addFiles, collectMessage,
    toggleManage, selectAll, removeSelected });
})();
