// Only the pure prepare/plainText entry points of the shared renderer are used.
// Daily chat owns its own streaming state; this view receives native snapshots.
window.ChatApp = { registerModule() {} };
window.escHtml = value => String(value ?? '').replace(/[&<>"']/g, ch =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[ch]);
