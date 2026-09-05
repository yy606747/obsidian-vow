// ── AI 输入等待动画 ──
let _typingTimer = null;
let _typingShownAt = 0;
const _TYPING_MIN_MS = 600;  // 泡泡最小显示时长，防止首字节太快导致闪现
async function _awaitTypingFloor() {
  if (!_typingShownAt) return;
  const wait = _TYPING_MIN_MS - (Date.now() - _typingShownAt);
  _typingShownAt = 0;  // 只对首个 chunk 生效
  if (wait > 0) await new Promise(r => setTimeout(r, wait));
}
function _startTypingAnim(msgId) {
  _stopTypingAnim();
  _typingShownAt = Date.now();
  const container = document.getElementById(`m_${msgId}`);
  if (!container) return;
  const bubble = container.querySelector('.msg-bubble');
  if (!bubble) return;
  bubble.classList.add('typing-bubble');
  const texts = _typingMoodTexts[_lastUserMood] || _typingMoodTexts.neutral;
  bubble.innerHTML = `<span class="typing-text">${texts[0]}</span><span class="typing-dots"><span></span><span></span><span></span></span>`;
  let idx = 0;
  _typingTimer = setInterval(() => {
    const label = bubble.querySelector('.typing-text');
    if (!label) { _stopTypingAnim(); return; }
    idx = (idx + 1) % texts.length;
    label.style.opacity = '0';
    setTimeout(() => { if (label.parentNode) { label.textContent = texts[idx]; label.style.opacity = '1'; } }, 200);
  }, 1200);
}
function _stopTypingAnim() {
  if (_typingTimer) { clearInterval(_typingTimer); _typingTimer = null; }
  _typingShownAt = 0;
}

// ── [CAM_CHECK] 监控查看处理 ──
let camCheckSafetyTimer = null;
let _camCheckInProgress = false;
function dismissCamCheckIndicator() {
  camCheckMsgId = null;
  _camCheckInProgress = false;
  if (camCheckSafetyTimer) { clearTimeout(camCheckSafetyTimer); camCheckSafetyTimer = null; }
  const el = document.getElementById('cam_check_loading');
  if (el) el.remove();
}
function handleCamCheck(convId, modelKey, msgId) {
  // 去重：防止 SSE + WebSocket 双通道重复触发 UI
  if (_camCheckInProgress) return;
  _camCheckInProgress = true;
  // 通知语音模块：AI 触发了 CAM_CHECK，保持 AI 说话状态
  if (voiceInCall) {
    notifyVoiceCamCheckStart();
  }
  // 设置全局跟踪，确保 renderMessages 后能恢复
  camCheckMsgId = msgId;
  // 在当前 AI 消息下方显示加载指示器
  const aiName = worldBook.ai_name || 'AI';
  const msgRow = msgId ? document.getElementById('m_' + msgId) : null;
  if (msgRow) {
    const indicator = document.createElement('div');
    indicator.className = 'cam-check-indicator';
    indicator.id = 'cam_check_loading';
    indicator.innerHTML = `📷 ${escHtml(aiName)} 正在查看监控<span class="cam-dots"><span></span><span></span><span></span></span>`;
    const msgBody = msgRow.querySelector('.msg-body');
    (msgBody || msgRow).appendChild(indicator);
    scrollBottom();
  }

  // 保底30秒安全超时：防止AI无响应时提示一直卡着
  camCheckSafetyTimer = setTimeout(() => dismissCamCheckIndicator(), 30000);

  const audio = new Audio('/public/monitor-alert.mp3');
  audio.play().catch(() => {});
  // 监控查看由服务端直接触发，前端只负责 UI 显示
}

// ── [查看动态:n] 活动动态查看处理 ──
let activityCheckMsgId = null;
let activityCheckSafetyTimer = null;
let _activityCheckInProgress = false;
let activityCheckN = 0;
function dismissActivityCheckIndicator() {
  activityCheckMsgId = null;
  activityCheckN = 0;
  _activityCheckInProgress = false;
  if (activityCheckSafetyTimer) { clearTimeout(activityCheckSafetyTimer); activityCheckSafetyTimer = null; }
  const el = document.getElementById('activity_check_loading');
  if (el) el.remove();
}
function handleActivityCheck(convId, n, msgId) {
  if (_activityCheckInProgress) return;
  _activityCheckInProgress = true;
  activityCheckMsgId = msgId;
  activityCheckN = n || 6;
  const aiName = worldBook.ai_name || 'AI';
  const minutes = activityCheckN * 10;
  const msgRow = msgId ? document.getElementById('m_' + msgId) : null;
  if (msgRow) {
    const indicator = document.createElement('div');
    indicator.className = 'activity-check-indicator';
    indicator.id = 'activity_check_loading';
    indicator.innerHTML = `📊 ${escHtml(aiName)} 正在查看过去${minutes}分钟的动态<span class="activity-dots"><span></span><span></span><span></span></span>`;
    const msgBody = msgRow.querySelector('.msg-body');
    (msgBody || msgRow).appendChild(indicator);
    scrollBottom();
  }
  activityCheckSafetyTimer = setTimeout(() => dismissActivityCheckIndicator(), 30000);
}

// ── [SCREEN_CHECK] PC 屏幕查看处理 ──
let screenCheckSafetyTimer = null;
let _screenCheckInProgress = false;
function dismissScreenCheckIndicator() {
  screenCheckMsgId = null;
  _screenCheckInProgress = false;
  if (screenCheckSafetyTimer) { clearTimeout(screenCheckSafetyTimer); screenCheckSafetyTimer = null; }
  const el = document.getElementById('screen_check_loading');
  if (el) el.remove();
}
function handleScreenCheck(payload, msgId) {
  if (_screenCheckInProgress) return;
  _screenCheckInProgress = true;
  screenCheckMsgId = msgId;
  const aiName = worldBook.ai_name || 'AI';
  const data = (payload && typeof payload === 'object') ? payload : { reason: payload || '' };
  const targetName = (data.target_device_name || '').trim();
  const targetType = (data.target_device_type || '').trim();
  const isMobile = !!(data.target_device_id || targetName || targetType);
  const icon = isMobile ? (targetType === 'tablet' || /平板|tablet|pad/i.test(targetName) ? '▣' : '▯') : '🖥️';
  const targetText = isMobile ? `正在等待${escHtml(targetName || (targetType === 'tablet' ? '平板' : '手机'))}屏幕确认` : '正在等待屏幕确认';
  const msgRow = msgId ? document.getElementById('m_' + msgId) : null;
  if (msgRow) {
    const indicator = document.createElement('div');
    indicator.className = 'screen-check-indicator';
    indicator.id = 'screen_check_loading';
    indicator.innerHTML = `${icon} ${escHtml(aiName)} ${targetText}<span class="screen-dots"><span></span><span></span><span></span></span>`;
    const msgBody = msgRow.querySelector('.msg-body');
    (msgBody || msgRow).appendChild(indicator);
    scrollBottom();
  }
  screenCheckSafetyTimer = setTimeout(() => dismissScreenCheckIndicator(), 120000);
}

// ── [HEART] 心语提示 ──
const _heartWhisperMsgIds = new Set();
function showHeartWhisperHint(msgId, content) {
  if (!msgId) return;
  _heartWhisperMsgIds.add(msgId);
  _applyHeartHint(msgId);
  // 新心语加入飘字池；若飘字定时器未启动则启动
  if (content && !_whisperPool.includes(content)) {
    const wasEmpty = !_whisperPool.length;
    _whisperPool.push(content);
    if (wasEmpty && !_whisperTimer) _scheduleNextWhisper();
  }
}
function _applyHeartHint(msgId) {
  const msgRow = document.getElementById('m_' + msgId);
  if (!msgRow) return;
  const avatarCol = msgRow.querySelector('.msg-avatar-col');
  if (!avatarCol || avatarCol.querySelector('.heart-whisper-hint')) return;
  const hint = document.createElement('span');
  hint.className = 'heart-whisper-hint';
  hint.textContent = '💭';
  hint.addEventListener('animationend', () => { hint.style.opacity = '1'; hint.style.animation = 'none'; }, { once: true });
  avatarCol.appendChild(hint);
}

function showCamOfflineNotice() {
  const notice = { id: 'notice_cam_' + Date.now(), conv_id: currentConvId, role: 'assistant',
    content: '📷 摄像头未开启，Core无法查看监控信息。请先在设置中开启摄像头。', created_at: Date.now()/1000 };
  currentMessages.push(notice);
  renderMessages();
  scrollBottom();
}

// ── [POI_SEARCH] 周边搜索处理 ──
let poiSearchSafetyTimer = null;
function dismissPoiSearchIndicator() {
  poiSearchMsgId = null;
  poiSearchCategories = null;
  if (poiSearchSafetyTimer) { clearTimeout(poiSearchSafetyTimer); poiSearchSafetyTimer = null; }
  const el = document.getElementById('poi_search_loading');
  if (el) el.remove();
}
function handlePoiSearch(categories, msgId) {
  const aiName = worldBook.ai_name || 'AI';
  const catText = categories.map(c => c.trim()).join('、');
  poiSearchMsgId = msgId;
  poiSearchCategories = categories;
  const msgRow = msgId ? document.getElementById('m_' + msgId) : null;
  if (msgRow) {
    const indicator = document.createElement('div');
    indicator.className = 'poi-search-indicator';
    indicator.id = 'poi_search_loading';
    indicator.innerHTML = `📍 ${escHtml(aiName)} 正在搜索附近${escHtml(catText)}<span class="poi-dots"><span></span><span></span><span></span></span>`;
    const msgBody = msgRow.querySelector('.msg-body');
    (msgBody || msgRow).appendChild(indicator);
    scrollBottom();
  }
  poiSearchSafetyTimer = setTimeout(() => dismissPoiSearchIndicator(), 45000);
}
