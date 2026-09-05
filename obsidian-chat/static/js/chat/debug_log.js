// ── Debug / 系统日志 ──
function renderDebugMemories(mems) {
  if (!mems || mems.length === 0) return '<h4>🧠 召回记忆</h4><div class="debug-empty">本次未召回任何记忆</div>';
  const items = mems.map(m => `<div class="debug-mem-item"><span class="score">${m.score.toFixed(4)}</span><span class="type">${escHtml(m.type)}</span><span class="content">${escHtml(m.content)}</span></div>`).join('');
  return `<h4>🧠 召回记忆 (${mems.length} 条，按相似度排序)</h4>${items}`;
}

function renderDebugPrompt(msgs, count) {
  if (!msgs || msgs.length === 0) return '';
  const items = msgs.map(m => {
    const roleCls = m.role === 'user' ? 'user' : 'assistant';
    return `<div class="debug-prompt-item"><span class="debug-prompt-role ${roleCls}">[${escHtml(m.role)}]</span> <span class="debug-prompt-text">${escHtml(m.content)}</span></div>`;
  }).join('');
  return `<h4>📝 完整 Prompt (${count} 条消息)</h4><div class="debug-prompt-list">${items}</div>`;
}

function _debugV2Value(value) {
  if (value === null || value === undefined || value === '') return '-';
  if (Array.isArray(value)) return value.length ? value.join(', ') : '-';
  if (typeof value === 'boolean') return value ? 'true' : 'false';
  if (typeof value === 'object') return JSON.stringify(value);
  return String(value);
}

function _debugV2Badge(label, value, cls = '') {
  return `<span class="debug-v2-badge ${cls}"><b>${escHtml(label)}</b>${escHtml(_debugV2Value(value))}</span>`;
}

function _debugV2Number(value, digits = 3) {
  const n = Number(value);
  return Number.isFinite(n) ? n.toFixed(digits) : '-';
}

function _renderDebugV2Runtime(runtime) {
  if (!runtime) return '';
  const cls = runtime.prompt_injection_enabled ? 'warn' : 'ok';
  return `<div class="debug-v2-badges">
    ${_debugV2Badge('mode', runtime.mode || '-')}
    ${_debugV2Badge('effective', runtime.effective_mode || '-')}
    ${_debugV2Badge('enabled', !!runtime.v2_enabled, runtime.v2_enabled ? 'ok' : 'warn')}
    ${_debugV2Badge('trace', !!runtime.include_trace, runtime.include_trace ? 'ok' : 'warn')}
    ${_debugV2Badge('prompt', !!runtime.prompt_injection_enabled, cls)}
  </div>`;
}

function _renderDebugV2SummaryBadges(data) {
  if (!data) return '';
  const selected = data.selected_count ?? (data.selected ? data.selected.length : 0);
  return `<div class="debug-v2-badges">
    ${_debugV2Badge('query_type', data.classification?.query_type || data.query_type || 'normal')}
    ${_debugV2Badge('needs_memory', data.turn_plan?.needs_memory ?? data.needs_memory ?? false, (data.turn_plan?.needs_memory ?? data.needs_memory) ? 'ok' : 'warn')}
    ${_debugV2Badge('namespace', data.turn_plan?.namespace || data.namespace || 'normal')}
    ${_debugV2Badge('allowed', data.allowed_namespaces || [])}
    ${_debugV2Badge('candidates', data.candidate_count ?? 0)}
    ${_debugV2Badge('selected', selected, selected > 0 ? 'ok' : 'warn')}
    ${_debugV2Badge('abstain', data.abstain_reason || '-', data.abstain_reason ? 'warn' : 'ok')}
  </div>`;
}

function _renderDebugV2Query(data) {
  const query = data?.query_preview || data?.query || '';
  const keywords = data?.keywords || data?.classification?.keywords || [];
  let html = '';
  if (query) html += `<div class="debug-recall-query">${escHtml(query)}</div>`;
  if (keywords.length) html += `<div class="debug-recall-keywords">🏷️ V2 关键词: ${escHtml(keywords.join('、'))}</div>`;
  return html;
}

function _renderDebugV2Steps(trace) {
  const steps = trace?.steps || [];
  if (!steps.length) return '';
  const rows = steps.map(step => {
    const statusCls = step.status === 'done' || step.status === 'selected' ? 'ok' : (step.status === 'abstained' || step.status === 'skipped' ? 'warn' : '');
    return `<div class="debug-v2-step">
      <div class="debug-v2-step-head">
        <span class="debug-v2-step-name">${escHtml(step.name || '-')}</span>
        ${_debugV2Badge('status', step.status || '-', statusCls)}
      </div>
      <div class="debug-v2-step-data">${escHtml(JSON.stringify(step.data || {}, null, 2))}</div>
    </div>`;
  }).join('');
  return `<div class="debug-v2-section"><div class="debug-v2-section-title">决策步骤</div>${rows}</div>`;
}

function _renderDebugV2Items(title, items, emptyText) {
  if (!items || !items.length) {
    return `<div class="debug-v2-section"><div class="debug-v2-section-title">${escHtml(title)}</div><div class="debug-v2-empty">${escHtml(emptyText)}</div></div>`;
  }
  const rows = items.map(item => {
    const meta = [
      item.namespace || '-',
      item.kind || '-',
      item.legacy_memory_id ? `legacy:${item.legacy_memory_id}` : ''
    ].filter(Boolean).join(' · ');
    const preview = item.preview || item.content || '';
    const reason = item.reason ? `<div class="debug-v2-reason">reason: ${escHtml(item.reason)}</div>` : '';
    return `<div class="debug-v2-item">
      <div class="debug-v2-item-head">
        <span class="debug-v2-score">${_debugV2Number(item.score)}</span>
        <span class="debug-v2-meta">${escHtml(meta)}</span>
      </div>
      <div class="debug-v2-preview">${escHtml(preview || '-')}</div>
      ${reason}
    </div>`;
  }).join('');
  return `<div class="debug-v2-section"><div class="debug-v2-section-title">${escHtml(title)} (${items.length})</div><div class="debug-v2-items">${rows}</div></div>`;
}

function _renderDebugV2Trace(trace) {
  if (!trace) return '';
  return [
    _renderDebugV2Query(trace),
    _renderDebugV2SummaryBadges(trace),
    _renderDebugV2Steps(trace),
    _renderDebugV2Items('V2 实际召回', trace.selected || [], '没有选中记忆'),
    _renderDebugV2Items('V2 候选排名 debug_top', trace.debug_top || [], '没有候选记忆'),
    `<details class="debug-v2-section"><summary class="debug-v2-section-title debug-v2-raw-summary">Raw JSON</summary><pre class="debug-v2-json">${escHtml(JSON.stringify(trace, null, 2))}</pre></details>`
  ].join('');
}

function _renderDebugV2Summary(summary) {
  if (!summary) return '';
  return [
    _renderDebugV2Query(summary),
    _renderDebugV2SummaryBadges(summary),
    _renderDebugV2Items('V2 实际召回', summary.selected || [], '没有选中记忆'),
    _renderDebugV2Items('V2 候选排名 debug_top', summary.debug_top || [], '没有候选记忆')
  ].join('');
}

function _renderDebugV2PromptBlock(block, decision) {
  if (!block && !decision) return '';
  block = block || {};
  decision = decision || {};
  const inject = !!decision.inject;
  let html = `<div class="debug-v2-section"><div class="debug-v2-section-title">V2 Prompt Block</div>`;
  html += `<div class="debug-v2-badges">
    ${_debugV2Badge('block', !!block.enabled, block.enabled ? 'ok' : 'warn')}
    ${_debugV2Badge('inject', inject, inject ? 'ok' : 'warn')}
    ${_debugV2Badge('reason', decision.reason || block.skipped_reason || '-')}
    ${_debugV2Badge('items', block.item_count ?? 0)}
  </div>`;
  if (block.warnings && block.warnings.length) {
    html += `<div class="debug-recall-keywords">${block.warnings.map(escHtml).join('<br>')}</div>`;
  }
  if (block.content) {
    html += `<pre class="debug-v2-prompt">${escHtml(block.content)}</pre>`;
  } else {
    html += '<div class="debug-v2-empty">本轮没有生成可注入的 V2 prompt block。</div>';
  }
  html += '</div>';
  return html;
}

function _renderDebugV2Usage(usage) {
  if (!usage) return '';
  const cls = usage.status === 'recorded' ? 'ok' : (usage.status === 'error' ? 'error' : 'warn');
  return `<div class="debug-v2-section"><div class="debug-v2-section-title">V2 Usage</div>
    <div class="debug-v2-badges">
      ${_debugV2Badge('status', usage.status || '-', cls)}
      ${_debugV2Badge('type', usage.usage_type || '-')}
      ${_debugV2Badge('count', usage.count ?? 0)}
      ${_debugV2Badge('touch_last_used', usage.touch_last_used ?? false, usage.touch_last_used ? 'ok' : 'warn')}
      ${_debugV2Badge('reason', usage.reason || '-')}
    </div>
  </div>`;
}

function renderMemoryV2RecallDebug(v2) {
  if (!v2) return '';
  const runtime = v2.runtime || {};
  let body = _renderDebugV2Runtime(runtime);
  if (v2.error) {
    body += `<div class="debug-v2-badges">${_debugV2Badge('error', v2.error, 'error')}</div>`;
  }
  body += _renderDebugV2PromptBlock(v2.prompt_block, v2.prompt_decision);
  body += _renderDebugV2Usage(v2.usage);
  if (v2.trace) {
    body += _renderDebugV2Trace(v2.trace);
  } else if (v2.summary) {
    body += _renderDebugV2Summary(v2.summary);
  } else if (!runtime.v2_enabled) {
    body += '<div class="debug-v2-empty">V2 recall 当前关闭，聊天 prompt 只使用 legacy recall。</div>';
  } else if (!runtime.include_trace) {
    body += '<div class="debug-v2-empty">V2 recall 已运行但未开启 trace；切到 debug 模式可查看完整决策步骤。</div>';
  } else {
    body += '<div class="debug-v2-empty">本轮没有 V2 recall 数据。</div>';
  }
  if (runtime.notes && runtime.notes.length) {
    body += `<div class="debug-recall-keywords">${runtime.notes.map(escHtml).join('<br>')}</div>`;
  }
  return `<div class="debug-v2-panel"><h4>🧬 Memory V2 Recall</h4>${body}</div>`;
}

function toggleDebugDetail(msgId) {
  const el = document.getElementById(`debugDetail_${msgId}`);
  if (!el) return;
  el.classList.toggle('show');
  const btn = el.previousElementSibling?.querySelector('.debug-toggle');
  if (btn) btn.textContent = el.classList.contains('show') ? '收起 ▴' : '详情 ▾';
}

// ── 系统日志 ──
let sysLogHasUnreadError = false;  // 是否有未读的错误日志

function addSystemLog(d) {
  // 按 msg_id 去重，避免 SSE + WebSocket 双通道导致重复
  if (d.msg_id && systemLogs.some(log => log.msg_id === d.msg_id)) return;
  const now = new Date();
  const ts = String(now.getHours()).padStart(2,'0') + ':' + String(now.getMinutes()).padStart(2,'0') + ':' + String(now.getSeconds()).padStart(2,'0');
  systemLogs.unshift({ ...d, _ts: ts, _id: 'slog_' + Date.now() + '_' + Math.random().toString(36).slice(2,6) });
  // 如果是错误日志，闪烁系统日志按钮
  if (d.has_error) {
    sysLogHasUnreadError = true;
    const btn = $("sysLogBtn");
    if (btn && !btn.classList.contains('syslog-btn-flash')) {
      btn.classList.add('syslog-btn-flash');
    }
  }
  renderSystemLogList();
}

// 添加前端网络错误到系统日志
function addErrorToSystemLog(errorMsg, model) {
  const d = {
    type: 'debug',
    model: model || '?',
    msg_id: null,
    has_error: true,
    error_text: errorMsg,
    usage: null,
    recalled_memories: null,
    prompt_messages: null,
  };
  addSystemLog(d);
}

function _buildTokenHtml(u) {
  if (!u) return '🔤 token 无数据';
  const raw = u.raw;
  let parts = [];
  // 基础 token 信息（使用服务器返回的原始数据）
  if (raw) {
    // Gemini 格式
    if ('promptTokenCount' in raw) {
      parts.push(`<span class="tok-label">输入:</span><span class="tok-value">${raw.promptTokenCount || 0}</span>`);
      if (raw.thoughtsTokenCount) parts.push(`<span class="tok-label">思考:</span><span class="tok-value tok-thinking">${raw.thoughtsTokenCount}</span>`);
      if ('cachedContentTokenCount' in raw) parts.push(`<span class="tok-label">缓存:</span><span class="tok-value tok-cached">${raw.cachedContentTokenCount || 0}</span>`);
      parts.push(`<span class="tok-label">输出:</span><span class="tok-value">${raw.candidatesTokenCount || 0}</span>`);
      if (raw.toolUsePromptTokenCount) parts.push(`<span class="tok-label">工具:</span><span class="tok-value">${raw.toolUsePromptTokenCount}</span>`);
      parts.push(`<span class="tok-label">总计:</span><span class="tok-value">${raw.totalTokenCount || 0}</span>`);
    }
    // SiliconFlow / OpenAI 格式
    else if ('prompt_tokens' in raw) {
      parts.push(`<span class="tok-label">输入:</span><span class="tok-value">${raw.prompt_tokens || 0}</span>`);
      if (raw.prompt_tokens_details) {
        if ('cached_tokens' in raw.prompt_tokens_details) parts.push(`<span class="tok-label">缓存:</span><span class="tok-value tok-cached">${raw.prompt_tokens_details.cached_tokens || 0}</span>`);
        if ('cache_write_tokens' in raw.prompt_tokens_details) parts.push(`<span class="tok-label">缓存写入:</span><span class="tok-value">${raw.prompt_tokens_details.cache_write_tokens || 0}</span>`);
      }
      parts.push(`<span class="tok-label">输出:</span><span class="tok-value">${raw.completion_tokens || 0}</span>`);
      if (raw.completion_tokens_details) {
        if (raw.completion_tokens_details.reasoning_tokens) parts.push(`<span class="tok-label">推理:</span><span class="tok-value tok-thinking">${raw.completion_tokens_details.reasoning_tokens}</span>`);
      }
      parts.push(`<span class="tok-label">总计:</span><span class="tok-value">${raw.total_tokens || 0}</span>`);
    }
  }
  // 无 raw 数据时使用简化格式
  if (parts.length === 0) {
    parts.push(`<span class="tok-label">输入:</span><span class="tok-value">${u.prompt_tokens || 0}</span>`);
    parts.push(`<span class="tok-label">输出:</span><span class="tok-value">${u.completion_tokens || 0}</span>`);
    parts.push(`<span class="tok-label">总计:</span><span class="tok-value">${u.total_tokens || 0}</span>`);
  }
  return '🔤 ' + parts.join(' ');
}

function _buildTokenDetailHtml(u) {
  if (!u || !u.raw) return '';
  const raw = u.raw;
  let html = '<h4>🔤 Token 用量详情（服务器原始数据）</h4><div class="syslog-token-raw">';
  // 直接展示服务器返回的所有字段
  for (const [k, v] of Object.entries(raw)) {
    if (v === null || v === undefined) continue;
    if (typeof v === 'object') {
      html += `<div><span class="tok-label">${escHtml(k)}:</span> <span class="tok-value">${escHtml(JSON.stringify(v))}</span></div>`;
    } else {
      html += `<div><span class="tok-label">${escHtml(k)}:</span> <span class="tok-value">${v}</span></div>`;
    }
  }
  html += '</div>';
  return html;
}

function _buildProviderMiniHtml(u) {
  const calls = u && u.provider_calls;
  if (!calls || !calls.length) return '';
  const last = calls[calls.length - 1] || {};
  const state = last.ok ? '✅' : '❌';
  const status = last.http_status ? ` HTTP ${last.http_status}` : '';
  const kind = last.error_type ? ` ${last.error_type}` : '';
  const cost = last.elapsed_ms ? ` ${last.elapsed_ms}ms` : '';
  const rid = last.request_id ? ` ${last.request_id}` : '';
  return `<span class="syslog-tokens">${state} ${escHtml(last.endpoint_name || last.provider_label || '?')}${status}${kind}${cost}${rid}</span>`;
}

function _buildV2MemoryMiniHtml(v2) {
  if (!v2) return '';
  if (v2.error) return `<span class="syslog-mem none">🧬 V2 error</span>`;
  const runtime = v2.runtime || {};
  if (!runtime.v2_enabled) return `<span class="syslog-mem none">🧬 V2 off</span>`;
  const data = v2.trace || v2.summary;
  const block = v2.prompt_block || {};
  if (!data && !block.enabled) return `<span class="syslog-mem none">🧬 V2 无数据</span>`;
  const selected = data ? (data.selected_count ?? (data.selected ? data.selected.length : 0)) : (block.item_count || 0);
  const blockText = block.enabled ? ` · block ${block.item_count || 0}` : '';
  const injectText = v2.prompt_decision?.inject ? ' · inject' : '';
  const usageText = v2.usage?.status === 'recorded' ? ` · usage ${v2.usage.count || 0}` : '';
  const abstain = data?.abstain_reason ? ` · ${data.abstain_reason}` : '';
  const cls = selected > 0 ? 'syslog-mem' : 'syslog-mem none';
  return `<span class="${cls}">🧬 V2 ${selected > 0 ? `召回 ${selected}` : '未召回'}${blockText}${injectText}${usageText}${abstain}</span>`;
}

function _debugModeCapabilities(d) {
  const caps = Array.isArray(d?.capabilities) ? d.capabilities.map(String).filter(Boolean) : [];
  return caps;
}

function _buildModeMiniHtml(d) {
  const caps = _debugModeCapabilities(d);
  if (!d?.chat_mode && caps.length === 0) return '';
  const mode = d?.chat_mode || '-';
  const source = d?.mode_source ? ` · ${d.mode_source}` : '';
  const hasDevice = caps.includes('device.toy');
  const cls = hasDevice ? 'syslog-mode device' : 'syslog-mode';
  const deviceText = hasDevice ? ' · device' : '';
  return `<span class="${cls}">🎛 ${escHtml(mode)}${escHtml(source)} · ${caps.length} caps${deviceText}</span>`;
}

function _buildModeDetailHtml(d) {
  const caps = _debugModeCapabilities(d);
  if (!d?.chat_mode && caps.length === 0) return '';
  const hasDevice = caps.includes('device.toy');
  const chips = caps.length
    ? caps.map(cap => `<span class="debug-cap-chip ${cap === 'device.toy' ? 'device' : ''}">${escHtml(cap)}</span>`).join('')
    : '<span class="debug-v2-empty">本轮没有 capability 数据。</span>';
  return `<h4>🎛 Mode / Capability</h4>
    <div class="debug-v2-badges">
      ${_debugV2Badge('mode', d.chat_mode || '-')}
      ${_debugV2Badge('source', d.mode_source || '-')}
      ${_debugV2Badge('capabilities', caps.length)}
      ${_debugV2Badge('device.toy', hasDevice, hasDevice ? 'warn' : 'ok')}
    </div>
    <div class="debug-cap-list">${chips}</div>`;
}

function _buildProviderDetailHtml(u) {
  const calls = u && u.provider_calls;
  if (!calls || !calls.length) return '';
  const rows = calls.map(c => {
    const state = c.ok ? '成功' : '失败';
    const status = c.http_status || '-';
    const proxy = c.proxy_enabled ? `proxy:${c.proxy_url || 'on'}` : 'direct';
    const req = c.request_id || '-';
    const msg = c.message ? `<div class="debug-provider-message">${escHtml(c.message)}</div>` : '';
    return `<div class="debug-mem-item">
      <span class="score">${escHtml(state)}</span>
      <span class="type">${escHtml(c.scope || '')}</span>
      <span class="content">${escHtml(c.endpoint_name || c.provider_label || '')} · ${escHtml(c.model || '')} · HTTP ${escHtml(status)} · ${escHtml(c.error_type || 'ok')} · ${escHtml(c.elapsed_ms || 0)}ms · ${escHtml(proxy)} · req:${escHtml(req)}${msg}</span>
    </div>`;
  }).join('');
  return `<h4>🧪 模型调用诊断</h4>${rows}`;
}

function renderSystemLogList() {
  const el = $("sysLogList");
  const countEl = $("sysLogCount");
  if (!el) return;
  if (countEl) countEl.textContent = `共 ${systemLogs.length} 条（刷新后清空）`;
  if (systemLogs.length === 0) {
    el.innerHTML = '<div class="syslog-empty">暂无日志</div>';
    return;
  }
  el.innerHTML = systemLogs.map(d => {
    const u = d.usage;
    const tokenText = _buildTokenHtml(u);
    const isError = d.has_error;
    const memCount = d.recalled_memories ? d.recalled_memories.length : 0;
    const memText = memCount > 0 ? `🧠 召回 ${memCount} 条记忆` : '🧠 无相关记忆';
    const memCls = memCount > 0 ? 'syslog-mem' : 'syslog-mem none';
    const v2Mini = _buildV2MemoryMiniHtml(d.memory_v2_recall);
    const modeMini = _buildModeMiniHtml(d);
    const detailId = 'sd_' + d._id;
    // 详情内容
    let detailHtml = '';
    // 错误信息
    if (isError && d.error_text) {
      detailHtml += `<div class="debug-error-text">⚠️ ${escHtml(d.error_text)}</div>`;
    }
    // Token 详情（服务器原始数据）
    detailHtml += _buildTokenDetailHtml(u);
    detailHtml += _buildProviderDetailHtml(u);
    detailHtml += _buildModeDetailHtml(d);
    // 即时哨兵结果
    if (d.is_search_needed !== undefined) {
      const searchTag = d.is_search_needed ? '<span class="debug-search-needed">✅ 需要搜索</span>' : '<span class="debug-search-skip">⏭️ 无需搜索</span>';
      detailHtml += `<div class="debug-recall-keywords">即时哨兵判断: ${searchTag}</div>`;
    }
    if (d.recall_topic) {
      detailHtml += `<div class="debug-recall-keywords">📌 话题: <span class="debug-topic">${escHtml(d.recall_topic)}</span></div>`;
    }
    if (d.recall_keywords) {
      detailHtml += `<div class="debug-recall-keywords">🏷️ 关键词: ${escHtml(d.recall_keywords)}</div>`;
    }
    // 向量匹配查询句
    if (d.recall_query) {
      detailHtml += `<h4>🔍 向量匹配查询</h4><div class="debug-recall-query">${escHtml(d.recall_query)}</div>`;
    }
    // 得分最高的 Top6（含未达标）
    if (d.debug_top6 && d.debug_top6.length > 0) {
      const topItems = d.debug_top6.map((m, i) => {
        const passed = m.score >= 0.45;
        return `<div class="debug-mem-item ${passed ? '' : 'below-threshold'}"><span class="score">${m.score.toFixed(4)}</span><span class="score-detail">vec:${m.vec_sim.toFixed(3)} kw:${m.kw_score.toFixed(3)} imp:${m.importance.toFixed(2)}</span><span class="content">${escHtml(m.content)}</span>${!passed ? '<span class="threshold-tag">未达标</span>' : ''}</div>`;
      }).join('');
      detailHtml += `<h4>📊 记忆库 Top6 得分 (阈值 0.45)</h4>${topItems}`;
    }
    if (d.recalled_memories && d.recalled_memories.length > 0) {
      const memItems = d.recalled_memories.map(m => `<div class="debug-mem-item"><span class="score">${m.score.toFixed(4)}</span><span class="type">${escHtml(m.type)}</span><span class="content">${escHtml(m.content)}</span></div>`).join('');
      detailHtml += `<h4>🧠 实际召回记忆 (${d.recalled_memories.length} 条)</h4>${memItems}`;
    }
    detailHtml += renderMemoryV2RecallDebug(d.memory_v2_recall);
    if (d.prompt_messages && d.prompt_messages.length > 0) {
      const pmItems = d.prompt_messages.map(m => {
        const roleCls = m.role === 'user' ? 'user' : 'assistant';
        return `<div class="debug-prompt-item"><span class="debug-prompt-role ${roleCls}">[${escHtml(m.role)}]</span> <span class="debug-prompt-text">${escHtml(m.content)}</span></div>`;
      }).join('');
      detailHtml += `<h4>📝 完整 Prompt (${d.prompt_count || d.prompt_messages.length} 条)</h4><div class="debug-prompt-list">${pmItems}</div>`;
    }
    const hasDetail = detailHtml.length > 0;
    const errorTag = isError ? '<span class="syslog-error-tag">❌ 错误</span>' : '';
    const entryCls = isError ? 'syslog-entry error-entry' : 'syslog-entry';
    const providerMini = _buildProviderMiniHtml(u);
    return `<div class="${entryCls}">
      <span class="syslog-time">${d._ts}</span>
      ${errorTag}
      <span class="syslog-model">📦 ${escHtml(d.model || '?')}</span>
      ${providerMini}
      ${modeMini}
      <span class="syslog-tokens">${tokenText}</span>
      <span class="${memCls}">${memText}</span>
      ${v2Mini}
      ${hasDetail ? `<button class="syslog-detail-toggle" data-syslog-detail="${detailId}">详情 ▾</button>` : ''}
      ${hasDetail ? `<div class="syslog-detail" id="${detailId}">${detailHtml}</div>` : ''}
    </div>`;
  }).join('');
}

function toggleSysLogDetail(id) {
  const el = document.getElementById(id);
  if (!el) return;
  el.classList.toggle('show');
  const btn = el.previousElementSibling;
  if (btn && btn.classList.contains('syslog-detail-toggle')) {
    btn.textContent = el.classList.contains('show') ? '收起 ▴' : '详情 ▾';
  }
}

function openSystemLog() {
  // 清除红色闪烁
  sysLogHasUnreadError = false;
  const btn = $("sysLogBtn");
  if (btn) btn.classList.remove('syslog-btn-flash');
  renderSystemLogList();
  $("sysLogModal").classList.add("show");
}
function closeSystemLog() {
  $("sysLogModal").classList.remove("show");
}
function clearSystemLog() {
  systemLogs = [];
  sysLogHasUnreadError = false;
  const btn = $("sysLogBtn");
  if (btn) btn.classList.remove('syslog-btn-flash');
  renderSystemLogList();
}

let debugLogEventsBound = false;

function bindDebugLogEvents() {
  if (debugLogEventsBound) return;
  debugLogEventsBound = true;
  document.addEventListener("click", event => {
    const btn = event.target.closest("[data-syslog-detail]");
    if (!btn || !$("sysLogModal")?.contains(btn)) return;
    event.preventDefault();
    toggleSysLogDetail(btn.dataset.syslogDetail);
  });
}

bindDebugLogEvents();

window.ChatApp?.registerModule?.("debugLog", {
  renderSystemLogList,
  addSystemLog,
  addErrorToSystemLog,
  open: openSystemLog,
  close: closeSystemLog,
  clear: clearSystemLog,
  toggleDetail: toggleSysLogDetail,
  toggleDebugDetail,
  bindEvents: bindDebugLogEvents,
});
