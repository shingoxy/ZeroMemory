'use strict';

function highlightEvidence(text, quote) {
  const start = quote ? text.indexOf(quote) : -1;
  if (start < 0) return esc(text);
  return esc(text.slice(0, start)) + '<mark>' + esc(quote) + '</mark>' + esc(text.slice(start + quote.length));
}

async function rawHistoryPage(evidence) {
  let active = true;
  let timer;
  let conversationOffset = 0;
  let messageOffset = 0;
  let selected = '';
  let generation = 0;
  const params = qs();
  pageCleanup = () => { active = false; clearTimeout(timer); };
  content.innerHTML = `<section class="card"><h2>原始导入批次</h2><div id="raw-batches"></div></section>
    <section class="card"><h2>历史会话</h2><form id="raw-search" class="row">
      <input id="raw-query" aria-label="搜索标题或消息" maxlength="300" placeholder="搜索标题或消息"><button>搜索</button></form>
      <div id="raw-conversations"></div><div class="row">
        <button class="secondary" id="raw-prev">上一页</button><button class="secondary" id="raw-next">下一页</button></div></section>
    <section class="card" id="raw-reader" hidden><h2 id="raw-title"></h2><div class="row">
      <label>分支 <select id="raw-branch" aria-label="分支"><option value="main">主分支</option></select></label>
      <label><input type="checkbox" id="raw-hidden">显示隐藏及系统消息</label></div>
      <div id="raw-messages"></div><div class="row"><button class="secondary" id="raw-message-prev">上一页</button>
        <button class="secondary" id="raw-message-next">下一页</button></div></section>`;

  async function batches() {
    const {items} = await api(`/v1/raw-history/batches?${params}`);
    if (!active) return;
    $('raw-batches').innerHTML = items.map((job, index) => {
      const messages = job.messages;
      const state = {running:'进行中',completed:'完成',interrupted:'已中断',rolled_back:'已回滚'}[job.status] || job.status;
      return `<div class="job"><strong>${esc(state)}</strong> · ${esc(job.cursor)} 个会话
        · 新增 ${esc(messages.inserted || 0)} · 重复 ${esc(messages.duplicates || 0)}
        · 失败 ${esc(messages.failed || 0)} · 未解析 ${esc(messages.unsupported || 0)}
        <small>${esc(job.id)} ${esc(job.error || '')}</small>
        <button class="secondary" id="raw-issues-${index}">查看异常</button></div>`;
    }).join('') || '<p class="subtle">暂无已确认的原始导入批次。</p>';
    items.forEach((job, index) => bind(`raw-issues-${index}`, 'click', async () => {
      const issues = await api(`/v1/raw-history/batches/${encodeURIComponent(job.id)}/issues?${params}`);
      if (active) showDetail('批次异常（前 50 条）', issues.items);
    }));
    if (items.some(job => job.status === 'running')) {
      timer = setTimeout(() => batches().catch(e => { if (active) notify(e.message, true); }), 2000);
    }
  }

  async function read(target) {
    const current = ++generation;
    const branch = target?.branch || $('raw-branch').value || 'main';
    const hidden = target?.show_hidden ?? $('raw-hidden').checked;
    const data = await api(`/v1/raw-history/conversations/${encodeURIComponent(selected)}/messages?${params}`
      + `&branch=${encodeURIComponent(branch)}&show_hidden=${hidden}&offset=${messageOffset}`);
    if (!active || current !== generation) return;
    $('raw-reader').hidden = false;
    $('raw-title').textContent = data.conversation.title || '无标题';
    $('raw-branch').innerHTML = '<option value="main">主分支</option>' + data.branches.filter(b => b.id !== 'main')
      .map((b, index) => `<option value="${esc(b.id)}">其他分支 ${index + 1}（${esc(b.count)} 条）</option>`).join('');
    $('raw-branch').value = branch;
    $('raw-hidden').checked = hidden;
    $('raw-messages').innerHTML = data.items.map(m => {
      const focused = m.message_key === evidence?.messageKey;
      const text = m.content_text || '';
      return `<article class="job"${focused ? ' id="raw-evidence" tabindex="-1"' : ''}>
      <strong>${esc(m.role)}</strong> · ${esc(m.observed_at || '时间未知')} · v${esc(m.message_version)}
      ${m.is_hidden || m.is_system ? ' · 隐藏或系统消息' : ''}${m.unsupported ? ' · 含未解析内容' : ''}
      ${focused ? `<p class="subtle">记忆证据 · 事件时间 ${esc(evidence.eventTime || '未知')}${evidence.quote && !text.includes(evidence.quote) ? ' · 本条消息未找到逐字引文' : ''}</p>` : ''}
      <pre class="raw-content">${focused ? highlightEvidence(text, evidence.quote) : esc(text)}</pre>
      <details><summary>来源与附件元数据</summary>${jsonView({source_ref:m.source_ref,
        message_id:m.message_id,parent_message_id:m.parent_message_id,attachments:m.attachments})}</details></article>`;
    }).join('') || '<p class="subtle">该分支当前没有可显示的消息。</p>';
    $('raw-message-prev').disabled = messageOffset === 0;
    $('raw-message-next').disabled = data.items.length < 20;
  }

  async function conversations() {
    const query = encodeURIComponent($('raw-query').value);
    const {items} = await api(`/v1/raw-history/conversations?${params}&query=${query}&offset=${conversationOffset}`);
    if (!active) return;
    $('raw-conversations').innerHTML = items.map((c, index) => `<div class="job">
      <button class="secondary" id="raw-conversation-${index}">${esc(c.title || '无标题')}</button>
      <small>${esc(c.updated_at || c.created_at || '时间未知')}</small></div>`).join('') || '<p class="subtle">没有匹配的会话。</p>';
    items.forEach((c, index) => bind(`raw-conversation-${index}`, 'click', async () => {
      selected = c.snapshot_id;
      $('raw-branch').value = 'main';
      $('raw-hidden').checked = false;
      messageOffset = 0;
      await read();
      if (active) $('raw-reader').scrollIntoView({block:'start'});
    }));
    $('raw-prev').disabled = conversationOffset === 0;
    $('raw-next').disabled = items.length < 30;
  }

  bind('raw-search', 'submit', async e => { e.preventDefault(); conversationOffset = 0; await conversations(); });
  bind('raw-prev', 'click', async () => { conversationOffset -= 30; await conversations(); });
  bind('raw-next', 'click', async () => { conversationOffset += 30; await conversations(); });
  for (const id of ['raw-branch', 'raw-hidden']) bind(id, 'change', async () => { messageOffset = 0; await read(); });
  bind('raw-message-prev', 'click', async () => { messageOffset -= 20; await read(); });
  bind('raw-message-next', 'click', async () => { messageOffset += 20; await read(); });
  await Promise.all([batches(), conversations()]);
  if (active && evidence) {
    selected = evidence.snapshot_id;
    messageOffset = Math.floor(evidence.position / 20) * 20;
    await read(evidence);
    const target = $('raw-evidence');
    if (active && target) {
      target.focus({preventScroll: true});
      target.scrollIntoView({block: 'center'});
    }
  }
}
