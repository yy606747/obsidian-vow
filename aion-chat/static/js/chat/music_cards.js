// ── 音乐卡片 ──
function renderMusicCards(msgId) {
  const cards = msgMusicCards[msgId];
  if (!cards || !cards.length) return;
  const row = document.getElementById('m_' + msgId);
  if (!row) return;
  // 有完整卡片时隐藏胶囊
  row.querySelectorAll('.music-capsule').forEach(e => e.style.display = 'none');
  // 移除旧的音乐卡片容器
  row.querySelectorAll('.music-cards-container').forEach(e => e.remove());
  const container = document.createElement('div');
  container.className = 'music-cards-container';
  cards.forEach(song => {
    container.innerHTML += buildMusicCardHtml(song);
  });
  const msgBody = row.querySelector('.msg-body');
  msgBody.appendChild(container);
}

function buildMusicCardHtml(song) {
  const cover = song.cover ? escHtml(song.cover) : '';
  const coverImg = cover ? `<img class="music-cover" src="${cover}" alt="">` : `<div class="music-cover music-cover-fallback">🎵</div>`;
  const name = escHtml(song.name || '未知歌曲');
  const artist = escHtml(song.artist || '未知歌手');
  const album = song.album ? `<div class="music-album">💿 ${escHtml(song.album)}</div>` : '';
  const songId = escHtml(song.id);

  // 在线播放按钮（统一走服务端代理推流）
  const onlineBtn = `<button class="music-btn secondary" data-music-action="play" data-song-id="${songId}">▶ 在线播放</button>`;

  // 备选歌曲
  let candidatesHtml = '';
  if (song.candidates && song.candidates.length) {
    const items = song.candidates.map(c =>
      `<button class="cand-item" data-music-action="open" data-song-id="${escHtml(c.id)}">🎵 ${escHtml(c.name)} - ${escHtml(c.artist)}</button>`
    ).join('');
    candidatesHtml = `<details class="music-candidates"><summary>不是这首？看看其他结果</summary>${items}</details>`;
  }

  return `
    <div class="music-card">
      ${coverImg}
      <div class="music-info">
        <div class="music-name">${name}</div>
        <div class="music-artist">${artist}</div>
        ${album}
        <div class="music-btns">
          <button class="music-btn primary" data-music-action="open" data-song-id="${songId}">🎶 网易云播放</button>
          ${onlineBtn}
        </div>
        ${candidatesHtml}
      </div>
    </div>`;
}

function openInNetease(songId) {
  window.open('https://music.163.com/song?id=' + songId, '_blank');
}

function playMusicOnline(songId) {
  let wrap = document.getElementById('globalMusicWrap');
  if (!wrap) {
    wrap = document.createElement('div');
    wrap.id = 'globalMusicWrap';
    wrap.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:999;display:none;align-items:center;gap:8px;background:var(--surface,#1e1e1e);padding:0 12px;height:36px;box-shadow:0 2px 8px rgba(0,0,0,0.25);border-bottom:1px solid var(--border,#333);';

    const playBtn = document.createElement('button');
    playBtn.id = 'globalMusicPlayBtn';
    playBtn.textContent = '⏸';
    playBtn.style.cssText = 'background:none;border:none;font-size:16px;cursor:pointer;color:var(--text,#eee);padding:0 4px;line-height:1;flex-shrink:0;';

    const bar = document.createElement('input');
    bar.id = 'globalMusicBar';
    bar.type = 'range'; bar.min = 0; bar.max = 1000; bar.value = 0;
    bar.style.cssText = 'flex:1;height:4px;accent-color:#e53935;cursor:pointer;';

    const audio = document.createElement('audio');
    audio.id = 'globalMusicAudio';

    playBtn.onclick = () => { if (audio.paused) { audio.play(); playBtn.textContent = '⏸'; } else { audio.pause(); playBtn.textContent = '▶'; } };
    audio.ontimeupdate = () => { if (audio.duration) bar.value = (audio.currentTime / audio.duration) * 1000; };
    bar.oninput = () => { if (audio.duration) audio.currentTime = (bar.value / 1000) * audio.duration; };
    audio.onended = () => { wrap.style.display = 'none'; playBtn.textContent = '▶'; };
    audio.onplay = () => { playBtn.textContent = '⏸'; };
    audio.onpause = () => { if (!audio.ended) playBtn.textContent = '▶'; };

    const closeBtn = document.createElement('button');
    closeBtn.textContent = '✕';
    closeBtn.style.cssText = 'background:none;border:none;font-size:14px;cursor:pointer;color:var(--text2,#888);padding:0 4px;line-height:1;flex-shrink:0;';
    closeBtn.onclick = () => { audio.pause(); audio.src = ''; wrap.style.display = 'none'; bar.value = 0; };

    wrap.appendChild(playBtn);
    wrap.appendChild(bar);
    wrap.appendChild(audio);
    wrap.appendChild(closeBtn);
    document.body.appendChild(wrap);
  }
  const audio = document.getElementById('globalMusicAudio');
  audio.src = '/api/music/stream/' + songId;
  wrap.style.display = 'flex';
  document.getElementById('globalMusicBar').value = 0;
  document.getElementById('globalMusicPlayBtn').textContent = '⏸';
  audio.play().catch(() => {});
}

let musicCardEventsBound = false;

function bindMusicCardEvents() {
  if (musicCardEventsBound) return;
  musicCardEventsBound = true;
  document.addEventListener('click', event => {
    const action = event.target.closest('[data-music-action]');
    if (!action) return;
    event.preventDefault();
    const songId = action.dataset.songId;
    if (action.dataset.musicAction === 'open') openInNetease(songId);
    if (action.dataset.musicAction === 'play') playMusicOnline(songId);
  });
}

bindMusicCardEvents();

ChatApp.registerModule("musicCards", {
  render: renderMusicCards,
  open: openInNetease,
  play: playMusicOnline,
  bindEvents: bindMusicCardEvents,
});
