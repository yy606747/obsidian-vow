// ── 情绪检测 ──
function _detectAiMood(text) {
  if (!text) return 'neutral';
  const m = {
    happy:  /[哈嘻嘿]{2,}|开心|高兴|太好了|好耶|万岁|真棒|好棒|棒棒|厉害|太厉害|太可爱|可爱死了|喜欢|好喜欢|超喜欢|哇|好哇|耶|yeah|哈哈|嘻嘻|嘿嘿|嗯嗯|好的呀|好呀|当然|没问题|乐|开朗|积极|😊|😄|😁|🎉|🥳|✨|🌟|💪/,
    sad:    /难过|伤心|抱歉|对不起|可惜|遗憾|心酸|心疼|心情不好|不开心|低落|沮丧|担心|担忧|害怕|恐惧|焦虑|压力大|撑不住|好累|很累|受伤|痛|难受|难熬|煎熬|委屈|失落|无奈|遗憾|叹气|唉|哎|😢|😭|💔|😔|😟|😞|🥺/,
    angry:  /生气|讨厌[^啦]|好烦|烦死了|哼[！!]|不行|可恶|气死|愤怒|不满|不爽|抓狂|崩溃|受不了|忍不了|😤|😠|💢|🤬/,
    shy:    /害羞|脸红|嘤|人家|讨厌啦|不要看|捂脸|有点不好意思|有些不好意思|其实吧|其实我|嗯…|呀…|啊…|那个…|😳|🙈|😶|🫣/,
    tender: /抱抱|陪你|陪着你|在呢|在的|乖|安心|别怕|守护|保护你|晚安|早安|晚安呀|早安呀|好好休息|加油|支持你|理解你|懂你|心疼|珍惜|温柔|温暖|暖|关心|关怀|好好的|照顾好自己|💗|🤗|♥|💕|🥰|💓|💝|🫂|❤/,
  };
  for (const [mood, re] of Object.entries(m)) { if (re.test(text)) return mood; }
  return 'neutral';
}
function _detectUserMood(text) {
  if (!text) return 'neutral';
  const m = {
    sweet:   /爱你|喜欢你|想你|亲亲|抱抱|宝贝|么么|mua|❤|💕|🥰|好喜欢|想见你|陪我|哄我/,
    sad:     /难过|不开心|伤心|哭了|烦死|压力|累了|好累|丧|emo|😢|😭|撑不住|好难|崩了|哭|难受|唉|哎|好烦|很烦|焦虑|担心|害怕/,
    playful: /[哈]{2,}|嘻嘻|笑死|hh|233|逗|搞笑|你猜|🤣|😂|哈哈哈|有意思|好好笑|噗|哎哟|哇哦|绝了|太绝|厉害了/,
    angry:   /生气|烦|讨厌|滚|气死|不理你|哼|😤|😠|可恶|好气|气死我了|无语|无语子/,
    shy:     /害羞|不好意思|脸红|嘤嘤|那个|其实我|😳|🙈|有点想|其实想|好像想|我想/,
  };
  for (const [mood, re] of Object.entries(m)) { if (re.test(text)) return mood; }
  return 'neutral';
}
const _typingMoodTexts = {
  neutral: ['思考中', '正在输入'],
  sweet:   ['开心地想着', '心跳加速中'],
  sad:     ['认真思考中', '想安慰你'],
  playful: ['憋笑中', '嘿嘿~'],
  angry:   ['小心翼翼地想着', '在反省中'],
  shy:     ['有点害羞地想着', '脸红中'],
};

// ── 情绪光圈 ──
const _moodClasses = ['mood-happy', 'mood-sad', 'mood-angry', 'mood-shy', 'mood-tender'];
const _moodReducedMotion = matchMedia('(prefers-reduced-motion: reduce)');
let _moodTextCache = new Map();
let _moodPulseFrame = null;
let _moodPulseObserver = null;
let _moodPulseStartedAt = null;
let _lastMoodPulseKey = '';

function _moodForMessage(message) {
  if (!message || message.role !== 'assistant' || message.inherited || message.transient_error
      || (typeof isErrorLikeAssistantMessage === 'function' && isErrorLikeAssistantMessage(message))) {
    return 'neutral';
  }
  const cached = _moodTextCache.get(message.id);
  if (cached && cached.content === message.content) return cached.mood;
  const mood = _detectAiMood(cleanAssistantContent(message.content));
  _moodTextCache.set(message.id, { content: message.content, mood });
  return mood;
}

function _latestMoodMessage() {
  for (let i = currentMessages.length - 1; i >= 0; i--) {
    const message = currentMessages[i];
    if (message.role === 'assistant' && !message.inherited) return message;
  }
  return null;
}

function _detachMoodPulse() {
  _moodPulseFrame?.classList.remove('mood-pulse');
  _moodPulseFrame?.style.removeProperty('--mood-pulse-delay');
  _moodPulseFrame = null;
  _moodPulseObserver?.disconnect();
  _moodPulseObserver = null;
}

function _stopMoodPulse() {
  _detachMoodPulse();
  _moodPulseStartedAt = null;
}

function _pulseMoodGlow(msgId, mood, frame) {
  const key = JSON.stringify([currentConvId, msgId, mood]);
  const now = performance.now();
  if (key !== _lastMoodPulseKey) {
    _lastMoodPulseKey = key;
    _stopMoodPulse();
    _moodPulseStartedAt = now;
  }
  // Replacing the message DOM may resume an unfinished breath, never restart its budget.
  if (_moodPulseStartedAt === null || now - _moodPulseStartedAt >= 5200) {
    _stopMoodPulse();
    return;
  }
  if (!frame || !_moodClasses.includes('mood-' + mood) || _moodReducedMotion.matches
      || document.hidden || (typeof isChatForeground === 'function' && !isChatForeground())
      || document.body.classList.contains('vow-covered')
      || document.getElementById('subPageOverlay')?.classList.contains('show')) {
    _stopMoodPulse();
    return;
  }
  if (_moodPulseFrame === frame) return;
  _detachMoodPulse();
  _moodPulseFrame = frame;
  frame.style.setProperty('--mood-pulse-delay', (-(now - _moodPulseStartedAt)) + 'ms');
  frame.classList.add('mood-pulse');
  frame.addEventListener('animationend', event => {
    if (event.animationName === 'moodGlow' && _moodPulseFrame === frame) _stopMoodPulse();
  }, { once: true });
  // Observe only the one active ring; scrolling it away ends the effect.
  if ('IntersectionObserver' in window) {
    _moodPulseObserver = new IntersectionObserver(entries => {
      if (entries.some(entry => entry.target === _moodPulseFrame && !entry.isIntersecting)) _stopMoodPulse();
    }, { root: document.getElementById('messages') });
    _moodPulseObserver.observe(frame);
  }
}

function _applyMoodGlow(msgId, { pulse = true } = {}) {
  if (pulse) {
    _msgMoods[msgId] = _moodForMessage(currentMessages.find(message => message.id === msgId));
  }
  const mood = _msgMoods[msgId];
  const row = document.getElementById('m_' + msgId);
  if (!row) return;
  const avatar = row.querySelector('.msg-avatar');
  if (!avatar) return;
  const frame = avatar.closest('.msg-avatar-frame');
  avatar.classList.remove(..._moodClasses);
  if (frame) delete frame.dataset.mood;
  if (_moodClasses.includes('mood-' + mood)) {
    avatar.classList.add('mood-' + mood);
    if (frame) frame.dataset.mood = mood;
  } else if (_moodPulseFrame === frame) {
    _stopMoodPulse();
  }
  if (pulse && _latestMoodMessage()?.id === msgId) _pulseMoodGlow(msgId, mood, frame);
}

function _restoreMoodGlows() {
  _detachMoodPulse();
  const cache = new Map();
  _msgMoods = Object.create(null);
  for (const message of currentConvId ? currentMessages : []) {
    if (message.role !== 'assistant' || message.inherited) continue;
    _msgMoods[message.id] = _moodForMessage(message);
    if (_moodTextCache.has(message.id)) cache.set(message.id, _moodTextCache.get(message.id));
    _applyMoodGlow(message.id, { pulse: false });
  }
  // Keep only the messages in the current loaded window, including neutral results.
  _moodTextCache = cache;
  const latest = currentConvId ? _latestMoodMessage() : null;
  if (!latest) {
    _stopMoodPulse();
    _lastMoodPulseKey = '';
  } else if (latest.id !== streamingAiId || latest.persisted === true) {
    _pulseMoodGlow(latest.id, _msgMoods[latest.id],
      document.getElementById('m_' + latest.id)?.querySelector('.msg-avatar-frame'));
  }
}

_moodReducedMotion.addEventListener('change', () => {
  if (_moodReducedMotion.matches) _stopMoodPulse();
});
document.addEventListener('visibilitychange', () => {
  if (document.hidden) _stopMoodPulse();
});
window.addEventListener('pagehide', _stopMoodPulse);
document.addEventListener('DOMContentLoaded', () => {
  const cover = new MutationObserver(() => {
    if (document.body.classList.contains('vow-covered')
        || document.getElementById('subPageOverlay')?.classList.contains('show')) _stopMoodPulse();
  });
  cover.observe(document.body, { attributes: true, attributeFilter: ['class'] });
  const overlay = document.getElementById('subPageOverlay');
  if (overlay) cover.observe(overlay, { attributes: true, attributeFilter: ['class'] });
}, { once: true });
