(function (global) {
  let avatarVer = { ai: 0, user: 0 };
  let toastTimer = null;
  const ChatApp = global.ChatApp || {};
  const modules = ChatApp.modules || {};

  function registerModule(name, api) {
    if (!name || !api || typeof api !== "object") {
      throw new Error("ChatApp.registerModule requires a module name and api object");
    }
    modules[name] = Object.freeze({ ...api });
    return modules[name];
  }

  function getModule(name) {
    return modules[name] || null;
  }

  ChatApp.modules = modules;
  ChatApp.registerModule = registerModule;
  ChatApp.getModule = getModule;
  global.ChatApp = ChatApp;

  function $(id) {
    return document.getElementById(id);
  }

  async function api(method, url, body) {
    const opts = { method, headers: { "Content-Type": "application/json" } };
    if (body) opts.body = JSON.stringify(body);
    const res = await fetch(url, opts);
    return res.json();
  }

  function escHtml(s) {
    const d = document.createElement("div");
    d.textContent = s == null ? "" : String(s);
    return d.innerHTML;
  }

  function formatMsg(s) {
    return escHtml(s).replace(/\n/g, "<br>");
  }

  const CHAT_VALUE_CONTROL_NAMES = [
    "MOBILE_SCREEN_CHECK", "SCREEN_CHECK", "POI_SEARCH", "MUSIC",
    "ALARM", "REMINDER", "Monitor", "SCHEDULE_DEL", "TOY", "HEART",
    "RING", "REMEMBER", "VOW", "UPDATE_MODEL", "查看动态",
    "PRESENCE_DRAW", "PRESENCE_SHOW", "SELF_WAKE",
  ];
  const CHAT_LITERAL_CONTROL_NAMES = [
    "CAM_CHECK", "SCHEDULE_LIST", "SELF_WAKE_CANCEL",
    "OPPORTUNITY_NONE", "OPPORTUNITY_REFLECT", "SELF_WAKE_NONE",
  ];
  const CHAT_PAIRED_CONTROL_NAMES = [
    "WORKING_MODEL_REQUEST", "RECALL_INTENT", "WEB_SEARCH_INTENT",
  ];
  const CHAT_ALL_CONTROL_NAMES = [
    ...CHAT_VALUE_CONTROL_NAMES,
    ...CHAT_LITERAL_CONTROL_NAMES,
    ...CHAT_PAIRED_CONTROL_NAMES,
    "TIDE_INTENT",
  ];
  const valueNames = CHAT_VALUE_CONTROL_NAMES.join("|");
  const literalNames = CHAT_LITERAL_CONTROL_NAMES.join("|");
  const pairedNames = CHAT_PAIRED_CONTROL_NAMES.join("|");

  const CHAT_CONTROL_PATTERNS = [
    /<meta\b[^>]*>[\s\S]*?<\/meta>/gi,
    /<(think|thinking|thought|analysis|reasoning)\b[^>]*>[\s\S]*?<\/\1>/gi,
    /```(?:think|thinking|thought|analysis|reasoning)\b[\s\S]*?```/gi,
    new RegExp(
      `[\\[【]\\s*(${pairedNames})\\s*[\\]】][\\s\\S]*?`
      + `[\\[【]\\s*\\/\\s*\\1\\s*[\\]】]`,
      "gi",
    ),
    /[\[【]\s*TIDE_INTENT\s*[:：][\s\S]*?[\[【]\s*\/\s*TIDE_INTENT\s*[\]】]/gi,
    new RegExp(
      `[\\[【]\\s*(?:${valueNames})\\s*[:：][^\\]】]*[\\]】]`,
      "gi",
    ),
    new RegExp(
      `[\\[【]\\s*(?:${literalNames})\\s*[\\]】]`,
      "gi",
    ),
    new RegExp(
      `[\\[【]\\s*\\/\\s*(?:${pairedNames}|TIDE_INTENT)\\s*[\\]】]`,
      "gi",
    ),
  ];

  const CHAT_UNFINISHED_PRIVATE_PATTERN = /<(?:meta|think|thinking|thought|analysis|reasoning)\b[^>]*>[\s\S]*$/i;
  const CHAT_UNFINISHED_BLOCK_PATTERNS = [
    new RegExp(`[\\[【]\\s*(?:${pairedNames})\\s*[\\]】][\\s\\S]*$`, "i"),
    /[\[【]\s*TIDE_INTENT\s*[:：][\s\S]*$/i,
  ];

  function stripTrailingControlPrefix(text) {
    const left = Math.max(text.lastIndexOf("["), text.lastIndexOf("【"));
    if (left < 0) return text;
    let tail = text.slice(left + 1).trimStart();
    if (tail.startsWith("/")) tail = tail.slice(1).trimStart();
    const name = tail.split(/[\s:：\]】]/, 1)[0].toUpperCase();
    if (!name || CHAT_ALL_CONTROL_NAMES.some(item => item.toUpperCase().startsWith(name))) {
      return text.slice(0, left);
    }
    return text;
  }

  function cleanAssistantContent(text) {
    let cleaned = CHAT_CONTROL_PATTERNS.reduce(
      (value, pattern) => value.replace(pattern, ""),
      String(text || ""),
    );
    cleaned = cleaned.replace(CHAT_UNFINISHED_PRIVATE_PATTERN, "");
    cleaned = CHAT_UNFINISHED_BLOCK_PATTERNS.reduce(
      (value, pattern) => value.replace(pattern, ""),
      cleaned,
    );
    cleaned = stripTrailingControlPrefix(cleaned);
    return cleaned.trim();
  }

  function avatarUrl(kind) {
    const v = avatarVer[kind] || 0;
    return `/api/avatar/${kind}?v=${v}`;
  }

  async function refreshAvatarVersions() {
    try {
      const info = await api("GET", "/api/avatar/info");
      avatarVer = { ai: info.ai || 0, user: info.user || 0 };
    } catch (e) {}
  }

  function showToast(msg, duration) {
    let t = document.getElementById("commonToast");
    if (!t) {
      t = document.createElement("div");
      t.id = "commonToast";
      t.className = "toast-msg";
      document.body.appendChild(t);
    }
    t.textContent = msg;
    t.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => t.classList.remove("show"), duration || 2000);
  }

  global.$ = $;
  global.api = api;
  global.escHtml = escHtml;
  global.formatMsg = formatMsg;
  global.cleanAssistantContent = cleanAssistantContent;
  global.avatarUrl = avatarUrl;
  global.refreshAvatarVersions = refreshAvatarVersions;
  global.showToast = showToast;
  global.ChatApp.registerModule("core", {
    $,
    api,
    escHtml,
    formatMsg,
    cleanAssistantContent,
    avatarUrl,
    refreshAvatarVersions,
    showToast,
  });
})(window);
