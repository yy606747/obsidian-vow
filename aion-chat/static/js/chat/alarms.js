// ── 日程管理 → 已拆分为独立页面 ──
let _alarmQueue = [];
const _seenAlarmDeliveries = new Map();

function _alarmDeliveryKey(data) {
  const ids = Array.isArray(data?.ids) ? [...data.ids].sort().join(',') : '';
  return ids || data?.id || `${data?.trigger_at || ''}|${data?.content || ''}`;
}

function _claimAlarmDelivery(data) {
  const now = Date.now();
  for (const [key, seenAt] of _seenAlarmDeliveries) {
    if (now - seenAt > 10 * 60_000) _seenAlarmDeliveries.delete(key);
  }
  const key = _alarmDeliveryKey(data);
  if (key && _seenAlarmDeliveries.has(key)) return false;
  if (key) _seenAlarmDeliveries.set(key, now);
  return true;
}

function showAlarmPopup(data) {
  if (!_claimAlarmDelivery(data)) return;
  _alarmQueue.push(data);
  if (_alarmQueue.length === 1) _showNextAlarm();
}
function _showNextAlarm() {
  if (!_alarmQueue.length) return;
  const data = _alarmQueue[0];
  $("alarmContent").textContent = data.content || "日程提醒";
  $("alarmTime").textContent = data.trigger_at || "";
  $("alarmOverlay").classList.add("show");
}
function dismissAlarm() {
  $("alarmOverlay").classList.remove("show");
  _alarmQueue.shift();
  if (_alarmQueue.length) setTimeout(_showNextAlarm, 300);
}
