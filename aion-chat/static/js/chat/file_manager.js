// ── 聊天记录文件管理器 ──
let fmCurrentConvId = null;

async function openFileManagerPanel() {
  $("fmList").style.display = "";
  $("fmEditor").style.display = "none";
  $("fileModal").classList.add("show");
  await loadFiles();
}

function closeFileManagerPanel() {
  $("fileModal").classList.remove("show");
}

async function loadFiles() {
  const files = await api("GET", "/api/files");
  $("fmFileList").innerHTML = files.length === 0
    ? '<div class="fm-empty">暂无文件，发送消息后自动生成</div>'
    : files.map(f => `
      <button class="fm-file-item" data-fm-open="${escHtml(f.conv_id)}">
        <span class="fm-title">${escHtml(f.filename)}</span>
        <span class="fm-size">${(f.size/1024).toFixed(1)}KB</span>
      </button>
    `).join("");
}

async function fmOpen(convId) {
  fmCurrentConvId = convId;
  const data = await api("GET", `/api/files/${convId}`);
  if (data.error) { alert(data.error); return; }
  $("fmEditorTitle").textContent = "编辑: " + data.filename;
  $("fmContent").value = data.content;
  $("fmList").style.display = "none";
  $("fmEditor").style.display = "flex";
}

function fmBack() {
  $("fmList").style.display = "";
  $("fmEditor").style.display = "none";
}

async function fmSave() {
  if (!fmCurrentConvId) return;
  const res = await api("PUT", `/api/files/${fmCurrentConvId}`, { content: $("fmContent").value });
  if (res.ok) {
    alert("保存成功，已同步到对话！");
    if (fmCurrentConvId === currentConvId) {
      currentMessages = await api("GET", `/api/conversations/${currentConvId}/messages?limit=${MSG_PAGE_SIZE}`);
      hasMoreMessages = currentMessages.length >= MSG_PAGE_SIZE;
      renderMessages();
      conversations = await api("GET", "/api/conversations");
      renderConvList();
      const conv = conversations.find(c => c.id === currentConvId);
      if (conv) $("chatTitle").textContent = conv.title;
    }
    fmBack();
    await loadFiles();
  }
}

let fileManagerEventsBound = false;

function bindFileManagerEvents() {
  if (fileManagerEventsBound) return;
  fileManagerEventsBound = true;
  document.addEventListener("click", event => {
    const item = event.target.closest("[data-fm-open]");
    if (!item || !$("fileModal")?.contains(item)) return;
    event.preventDefault();
    fmOpen(item.dataset.fmOpen);
  });
}

bindFileManagerEvents();

ChatApp.registerModule("fileManager", {
  open: openFileManagerPanel,
  close: closeFileManagerPanel,
  load: loadFiles,
  openConversationFile: fmOpen,
  back: fmBack,
  save: fmSave,
  bindEvents: bindFileManagerEvents,
});
