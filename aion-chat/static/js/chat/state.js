// ── 聊天页运行态 ──
let conversations = [];
let currentConvId = null;
let currentMessages = [];
let models = [];
let sending = false;
let streamingAiId = null;
let _recoverStreamMsgId = null;  // 流式中断后待恢复的 AI 消息 id
let camCheckMsgId = null;
let screenCheckMsgId = null;
let poiSearchMsgId = null;
let poiSearchCategories = null;
let ws = null;
let pendingAttachments = [];  // [{url, type, name}]
let worldBook = { ai_persona: "", user_persona: "", ai_name: "AI", user_name: "你" };
let msgDebugData = {};  // { msgId: { model, recalled_memories, prompt_messages, prompt_count, usage } }
let systemLogs = [];    // 系统日志（会话级，刷新清空）
let _msgMoods = {};     // { msgId: 'happy'|'sad'|... } AI 消息情绪
let _pendingRetract = false;  // 用户撤回了消息，下次 send 通知 AI
let _whisperPool = [];  // 心语池，定时飘字用
let _lastUserMood = 'neutral'; // 用户最近消息的情绪，影响打字动画
let msgMusicCards = {}; // { msgId: [{ id, name, artist, album, cover, audio_url, candidates }] }
let hasMoreMessages = false;   // 是否还有更早的消息可加载
let loadingMore = false;       // 防止重复加载
const MSG_PAGE_SIZE = 50;

const CHAT_STATE_OWNERS = Object.freeze({
  conversations: "conversations/realtime/render",
  currentConvId: "conversations/send/realtime",
  currentMessages: "conversations/render/send/messages/realtime",
  models: "boot/render/config_panel",
  sending: "send",
  streamingAiId: "send/realtime",
  pendingAttachments: "ui/attachments/send",
  msgDebugData: "debug_log_facade/debug_log/render",
  systemLogs: "debug_log_facade/debug_log",
  msgMusicCards: "music_cards/send/realtime",
  voiceState: "voice_facade/voice",
  whisperState: "whisper_toy_facade/whisper_toy",
  aiDomState: "ai_dom_facade/ai_dom",
  toyBridgeState: "toy_bridge/whisper_toy/ai_dom",
});

function getChatStateSnapshot() {
  return {
    conversationCount: conversations.length,
    currentConvId,
    messageCount: currentMessages.length,
    modelCount: models.length,
    sending,
    streamingAiId,
    pendingAttachmentCount: pendingAttachments.length,
    systemLogCount: systemLogs.length,
    musicCardMessageCount: Object.keys(msgMusicCards).length,
    hasMoreMessages,
    loadingMore,
  };
}

ChatApp.registerModule("state", {
  snapshot: getChatStateSnapshot,
  owners: () => CHAT_STATE_OWNERS,
});
