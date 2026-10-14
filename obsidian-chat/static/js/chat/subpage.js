const _subPageNames = {'/':'小屋','/settings':'设置','/memory':'回忆','/worldbook':'世界书','/schedule':'日程','/camera':'摄像头','/monitor-logs':'监控日志','/activity-logs':'活动日志','/devices':'设备','/scenes':'小剧场','/location':'定位','/heart-whispers':'心语'};
function openSubPage(url) {
  if (typeof aiDomGuard === 'function' && aiDomGuard('跳转子页面')) return;
  closeSidebar();
  const f = $('subPageFrame');
  f.src = url;
  $('subPageTitle').textContent = _subPageNames[new URL(url, location.origin).pathname] || '';
  const wasOpen = $('subPageOverlay').classList.contains('show');
  $('subPageOverlay').classList.add('show');
  document.body.classList.add('vow-covered');
  $('subPageOverlay').dataset.page = new URL(url, location.origin).pathname === '/' ? 'home' : 'subpage';
  $('subPageOverlay').dataset.route = new URL(url, location.origin).pathname;
  window.SceneRuntime?.navigationChanged?.();
  document.body.classList.toggle('vow-visiting-home', $('subPageOverlay').dataset.page === 'home');
  window.VowUI?.setActive(url);
  callChatNative('syncStarted');
  if (wasOpen) history.replaceState({subPage: url}, '', '/chat');
  else history.pushState({subPage: url}, '', '/chat');
}
function closeSubPage() {
  const ov = $('subPageOverlay');
  if (!ov.classList.contains('show')) return;
  ov.classList.remove('show');
  window.SceneRuntime?.navigationChanged?.();
  document.body.classList.remove('vow-visiting-home', 'vow-covered');
  window.VowUI?.setActive('/chat');
  $('subPageFrame').src = 'about:blank';
  // 回到聊天页后重新加载消息列表（拿到后台生成完成的新消息）
  refreshChat();
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
