const _subPageNames = {'/':'主页','/settings':'设置','/memory':'记忆库','/worldbook':'世界书','/schedule':'日程','/camera':'摄像头','/monitor-logs':'监控日志','/devices':'设备','/location':'定位','/heart-whispers':'心语'};
function openSubPage(url) {
  if (typeof aiDomGuard === 'function' && aiDomGuard('跳转子页面')) return;
  closeSidebar();
  const f = $('subPageFrame');
  f.src = url;
  $('subPageTitle').textContent = _subPageNames[url] || '';
  $('subPageOverlay').classList.add('show');
  history.pushState({subPage: url}, '', '/chat');
}
function closeSubPage() {
  const ov = $('subPageOverlay');
  if (!ov.classList.contains('show')) return;
  ov.classList.remove('show');
  $('subPageFrame').src = 'about:blank';
  // 回到聊天页后重新加载消息列表（拿到后台生成完成的新消息）
  if (currentConvId) {
    api("GET", `/api/conversations/${currentConvId}/messages?limit=${MSG_PAGE_SIZE}`).then(msgs => {
      currentMessages = msgs;
      hasMoreMessages = msgs.length >= MSG_PAGE_SIZE;
      renderMessages();
    });
  }
  // 设置页可能新增/删除了端点或自定义主脑模型，刷新模型下拉
  api("GET", "/api/models").then(m => {
    models = m;
    renderModelSelect();
    const conv = conversations.find(c => c.id === currentConvId);
    if (conv && models.some(x => x.key === conv.model)) {
      $("modelSelect").value = conv.model;
    }
  }).catch(() => {});
}
window.addEventListener('popstate', function(e) {
  if ($('subPageOverlay').classList.contains('show')) { closeSubPage(); }
});
