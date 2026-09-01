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
function _applyMoodGlow(msgId) {
  const mood = _msgMoods[msgId];
  if (!mood || mood === 'neutral') return;
  const row = document.getElementById('m_' + msgId);
  if (!row) return;
  const avatar = row.querySelector('.msg-avatar');
  if (!avatar) return;
  avatar.classList.remove('mood-happy','mood-sad','mood-angry','mood-shy','mood-tender');
  avatar.classList.add('mood-' + mood);
}

// ── 心语飘字 ──
let _whisperTimer = null;
async function _initWhisperFloat() {
  try {
    const r = await api("GET", "/api/heart-whispers?page=1&page_size=50");
    // merge，而不是覆盖：WS 可能在 API 返回前先推了若干新心语进池
    for (const it of (r.items || [])) {
      if (it.content && !_whisperPool.includes(it.content)) _whisperPool.push(it.content);
    }
  } catch(e) {}
  if (_whisperPool.length && !_whisperTimer) _scheduleNextWhisper();
}
function _scheduleNextWhisper() {
  const delay = 45000 + Math.random() * 60000; // 45~105 秒
  _whisperTimer = setTimeout(_floatOneWhisper, delay);
}
function _floatOneWhisper() {
  const el = $("messages");
  if (!el || !_whisperPool.length || !currentConvId) { _scheduleNextWhisper(); return; }
  let text = _whisperPool[Math.floor(Math.random() * _whisperPool.length)];
  const maxLen = window.innerWidth < 480 ? 18 : 26;
  if (text.length > maxLen) text = text.slice(0, maxLen) + '…';
  const span = document.createElement('span');
  span.className = 'whisper-float';
  span.textContent = `"${text}"`;
  // 随机水平位置：中心在 35~65%，配合 translateX(-50%) 在窄屏也不溢出
  const leftPct = 35 + Math.random() * 30;
  span.style.left = leftPct + '%';
  span.style.bottom = '140px';
  document.body.appendChild(span);
  const removeSpan = () => { if (span.parentNode) span.remove(); };
  span.addEventListener('animationend', removeSpan, { once: true });
  setTimeout(removeSpan, 11000);
  _scheduleNextWhisper();
}
