'use strict';

const REVIEW_TABS = [['active', '待审核'], ['deferred', '稍后处理'], ['low_priority', '低优先级'], ['sensitive', '敏感队列'], ['runs', '提炼批次']];
const SKIP_LABELS = {sensitive: '敏感', deferred: '已稍后处理', low_priority: '低优先级', conflict_suggestion: '有时间冲突建议',
  needs_supersede: '需选择取代对象', unverified: '证据未核验', not_user: '非用户来源'};
const ACTION_LABELS = {approve: '批准', reject: '驳回', defer: '稍后处理'};
const SENSITIVE_LABELS = {keep_sensitive: '保留为敏感记忆', to_private: '改为普通候选', reject: '驳回并清除正文', defer: '稍后处理'};
const SENSITIVE_CONFIRM = {keep_sensitive: '保留为敏感记忆后只在本机 Web 可见，不进入模型上下文和 MCP。确认？',
  to_private: '改为普通候选后会进入常规审核队列，批准后可供模型读取。确认？', reject: '驳回后会删除隔离文件中的候选正文，只保留哈希。确认？'};
const KEY_HELP = '快捷键：J/↓ 下一条 · K/↑ 上一条 · A 批准 · R 驳回 · L 稍后处理 · E 编辑 · O/Enter 来源与修订 · X 选中';
let reviewState = {};

async function reviewPage() {
  reviewState = {tab: 'active', offset: 0, focus: 0, threshold: 0.5, memoryType: '', runId: '',
    source: reviewMode === 'chatgpt_export' ? 'chatgpt_export' : ''};
  content.innerHTML = `<section class="card"><div class="section-head"><h2>每一条事实，都由你确认。</h2><button id="new-memory" class="secondary">手工提议记忆</button></div>
    <p class="subtle">自动提炼只产生候选；批量批准前会显示数量与抽样证据，敏感与冲突项不参与批量批准。按 <kbd>?</kbd> 查看快捷键。</p>
    <nav class="tabs" id="review-tabs" aria-label="审核视图"></nav><div id="review-body"></div></section>`;
  bind('new-memory', 'click', () => memoryEditor(null, loadReview));
  document.addEventListener('keydown', reviewKeys);
  pageCleanup = () => document.removeEventListener('keydown', reviewKeys);
  await loadReview();
}

function queueQuery(view, limit, offset) {
  const params = new URLSearchParams({user_id: scope.user_id, project_id: scope.project_id, view, limit, offset,
    threshold: reviewState.threshold});
  if (reviewState.memoryType) params.set('memory_type', reviewState.memoryType);
  if (reviewState.source) params.set('source_type', reviewState.source);
  if (reviewState.runId) params.set('run_id', reviewState.runId);
  return '/v1/review/queue?' + params.toString();
}

async function loadReview() {
  const queueView = ['active', 'deferred', 'low_priority'].includes(reviewState.tab);
  const [data, files] = await Promise.all([
    api(queueQuery(queueView ? reviewState.tab : 'active', queueView ? 30 : 1, queueView ? reviewState.offset : 0)),
    api('/v1/review/sensitive?' + qs()),
  ]);
  if (page !== 'review') return;
  renderTabs({...data.counts, sensitive: data.counts.sensitive + files.items.length});
  if (reviewState.tab === 'runs') return renderRuns();
  if (reviewState.tab === 'sensitive') return renderSensitive(files);
  renderQueue(data);
}

function renderTabs(counts) {
  $('review-tabs').innerHTML = REVIEW_TABS.map(([key, label]) => `<button type="button" class="tab${reviewState.tab === key ? ' active' : ''}"
    data-review-tab="${key}">${esc(label)}${key in counts ? ` (${counts[key]})` : ''}</button>`).join('');
  $('review-tabs').querySelectorAll('[data-review-tab]').forEach(button => button.onclick = async () => {
    reviewState = {...reviewState, tab: button.dataset.reviewTab, offset: 0, focus: 0};
    try { await loadReview(); } catch (error) { notify(error.message, true); }
  });
}

function reviewCard(item, index) {
  const review = item.review || {};
  const badges = [`<span class="tag">优先级 ${Number(review.priority || 0).toFixed(2)}</span>`];
  if (review.run) badges.push(`<span class="tag">${review.run.kind === 'pilot' ? '试点' : '批次'} ${esc(review.run.run_id.slice(0, 8))}</span>`);
  if (review.links?.evidence) badges.push(`<span class="tag badge-a">关联旧记忆 ${review.links.evidence}</span>`);
  if (review.links?.conflict) badges.push(`<span class="tag badge-c">时间冲突 ${review.links.conflict}</span>`);
  if (review.low_priority) badges.push('<span class="tag">低优先级</span>');
  return `<div class="review-card${index === reviewState.focus ? ' focused' : ''}" data-review-index="${index}">
    <div class="review-badges">${badges.join('')}</div>${renderMemory(item, true)}</div>`;
}

function renderQueue(data) {
  const deferredTab = reviewState.tab === 'deferred';
  currentItems = data.items;
  reviewState = {...reviewState, focus: Math.min(reviewState.focus, Math.max(0, data.items.length - 1))};
  $('review-body').innerHTML = `<div class="filters"><select id="review-type" aria-label="记忆类型">${options(types, reviewState.memoryType, true)}</select>
      <input id="review-source" placeholder="来源类型" aria-label="来源类型" value="${esc(reviewState.source)}">
      <input id="review-run" placeholder="批次编号（64 位）" aria-label="批次编号" value="${esc(reviewState.runId)}">
      <label class="threshold">低优先级阈值<input id="review-threshold" type="number" min="0" max="1" step="0.05" value="${reviewState.threshold}"></label></div>
    <div class="actions"><button id="review-search">筛选</button><label class="check"><input id="select-all" type="checkbox">全选此页</label>
      ${deferredTab ? '<button class="secondary" id="selection-undefer">取消稍后处理所选</button>'
        : '<button id="selection-approve">批准所选</button><button class="secondary" id="selection-reject">驳回所选</button><button class="secondary" id="selection-defer">稍后处理所选</button>'}</div>
    <div class="actions"><span class="subtle">按当前类别：</span><button class="secondary" id="category-approve">批准此类别</button>
      <button class="secondary" id="category-reject">驳回此类别</button><button class="secondary" id="category-defer">稍后处理此类别</button></div>
    <div id="review-results">${data.items.length ? data.items.map(reviewCard).join('') : '<div class="empty">这里没有待审核的候选。</div>'}</div>
    <div class="actions"><button id="review-prev" class="secondary" ${reviewState.offset ? '' : 'disabled'}>上一页</button>
      <button id="review-next" class="secondary" ${data.items.length < 30 ? 'disabled' : ''}>下一页</button></div>`;
  bindMemoryActions(loadReview);
  bind('review-search', 'click', async () => {
    reviewState = {...reviewState, memoryType: $('review-type').value, source: $('review-source').value.trim(),
      runId: $('review-run').value.trim(), threshold: Number($('review-threshold').value) || 0, offset: 0, focus: 0};
    await loadReview();
  });
  bind('select-all', 'change', event => document.querySelectorAll('[data-select]').forEach(box => box.checked = event.target.checked));
  bind('review-prev', 'click', async () => { reviewState = {...reviewState, offset: Math.max(0, reviewState.offset - 30)}; await loadReview(); });
  bind('review-next', 'click', async () => { reviewState = {...reviewState, offset: reviewState.offset + 30}; await loadReview(); });
  for (const action of ['approve', 'reject', 'defer']) {
    bind('selection-' + action, 'click', async () => {
      const ids = selectedIds();
      if (!ids.length) return notify('请先选择候选', true);
      await batchFlow({action, memory_ids: ids});
    });
    bind('category-' + action, 'click', async () => {
      if (!reviewState.memoryType) return notify('请先在筛选中选择记忆类型，再按类别操作', true);
      await batchFlow({action, memory_type: reviewState.memoryType, source_type: reviewState.source || null,
        run_id: reviewState.runId || null, threshold: reviewState.threshold});
    });
  }
  bind('selection-undefer', 'click', async () => {
    const ids = selectedIds();
    if (!ids.length) return notify('请先选择候选', true);
    const result = await api('/v1/review/defer', {method: 'POST', body: {...scope, memory_ids: ids, undo: true}});
    notify(`已取消 ${result.updated} 条稍后处理`);
    await loadReview();
  });
}

function selectedIds() {
  return [...document.querySelectorAll('[data-select]:checked')].map(box => box.dataset.select);
}

async function batchFlow(request) {
  const body = {...scope, ...request};
  const preview = await api('/v1/review/batch/preview', {method: 'POST', body});
  const label = ACTION_LABELS[request.action];
  const skipped = Object.entries(preview.skipped).map(([key, count]) => `${SKIP_LABELS[key] || key} ${count}`).join('，') || '无';
  const samples = preview.samples.map(sample => `<li><p class="memory-content">${esc(sample.content)}</p>
    <div class="evidence">证据：${esc(sample.evidence || '暂无逐字引文')}</div></li>`).join('');
  showDetail(`${label}预览`, `<p>匹配 ${preview.matched} 条，可${esc(label)} ${preview.eligible} 条（单次上限 ${preview.limit}）。跳过：${esc(skipped)}。</p>
    ${samples ? `<p class="subtle">抽样证据：</p><ol class="samples">${samples}</ol>` : ''}
    <div class="actions"><button id="batch-confirm" ${preview.eligible ? '' : 'disabled'}>确认${esc(label)} ${preview.eligible} 条</button></div>`);
  bind('batch-confirm', 'click', async () => {
    const result = await api('/v1/review/batch/apply', {method: 'POST', body: {...body, confirm: preview.confirm}});
    $('detail').close();
    notify(`已${label} ${result.applied} 条${result.errors.length ? '；未完成：' + result.errors.join('；') : ''}`, !!result.errors.length);
    await loadReview();
  });
}

async function renderRuns() {
  const data = await api('/v1/review/runs?' + qs());
  const percent = value => value === null || value === undefined ? '—' : `${Math.round(value * 100)}%`;
  const seconds = value => value === null || value === undefined ? '—' : `${Math.round(value)} 秒`;
  const reasons = journal => Object.entries(journal?.reasons || {}).map(([key, count]) => `${esc(key)} ${count}`).join('，') || '—';
  const rows = data.runs.map(run => `<tr><td>${run.kind === 'pilot' ? '试点' : '批次'} <code>${esc(run.run_id.slice(0, 12))}</code></td>
    <td>${run.journal ? run.journal.requests : '—'}</td><td>${run.candidates}</td><td>${run.approved}</td>
    <td>${run.rejected + run.batch_rejected}</td><td>${run.run_revoked}</td><td>${run.pending}</td><td>${run.deferred}</td>
    <td>${percent(run.pass_rate)}</td><td>${seconds(run.avg_review_seconds)}</td><td>${reasons(run.journal)}</td>
    <td class="run-actions"><button class="ghost" data-run-filter="${esc(run.run_id)}">查看</button>
      <button class="ghost" data-run-revoke="${esc(run.run_id)}" ${run.pending ? '' : 'disabled'}>撤销待审核</button></td></tr>`).join('');
  $('review-body').innerHTML = `<div class="actions"><button id="links-suggest" class="secondary">重新计算关联建议</button><span id="links-result" class="subtle"></span></div>
    <div class="table-wrap"><table class="runs"><thead><tr><th>批次</th><th>请求</th><th>候选</th><th>批准</th><th>驳回</th><th>撤销</th>
      <th>待审</th><th>稍后</th><th>通过率</th><th>平均审核耗时</th><th>失败原因</th><th></th></tr></thead>
      <tbody>${rows || '<tr><td colspan="12">暂无提炼批次</td></tr>'}</tbody></table></div>
    <p class="subtle">合计 ${data.overall.candidates} 条候选，通过率 ${percent(data.overall.pass_rate)}。审核耗时按相邻两次审核操作的间隔估算，间隔超过 10 分钟视为中断。</p>`;
  bind('links-suggest', 'click', async () => {
    const result = await api('/v1/review/links/suggest', {method: 'POST', body: {...scope}});
    $('links-result').textContent = `检查 ${result.candidates} 条候选，新增 ${result.created} 条建议（关联 ${result.evidence}，冲突 ${result.conflict}）。`;
  });
  $('review-body').querySelectorAll('[data-run-filter]').forEach(button => button.onclick = async () => {
    reviewState = {...reviewState, tab: 'active', runId: button.dataset.runFilter, offset: 0, focus: 0};
    try { await loadReview(); } catch (error) { notify(error.message, true); }
  });
  $('review-body').querySelectorAll('[data-run-revoke]').forEach(button => button.onclick = async () => {
    try { await revokeRun(button.dataset.runRevoke); } catch (error) { notify(error.message, true); }
  });
}

async function revokeRun(runId) {
  const url = `/v1/review/runs/${runId}/revoke`;
  const dry = await api(url, {method: 'POST', body: {...scope}});
  if (!confirm(`撤销批次 ${runId.slice(0, 12)}：将驳回 ${dry.pending} 条待审核候选和 ${dry.sensitive_items} 条敏感条目，已批准的 ${dry.approved} 条不受影响。确认？`)) return;
  const result = await api(url, {method: 'POST', body: {...scope, confirm: dry.confirm}});
  notify(`已驳回 ${result.rejected} 条候选、${result.sensitive_rejected} 条敏感条目`);
  await loadReview();
}

async function renderSensitive(files) {
  const db = await api(queueQuery('sensitive', 100, 0));
  currentItems = db.items;
  const fileCards = files.items.map(item => `<article class="memory sensitive-item"><div>
      <span class="tag">隔离文件</span>${item.deferred ? '<span class="tag">稍后处理</span>' : ''}
      ${item.rule_hits.map(hit => `<span class="tag rejected">命中：${esc(hit)}</span>`).join('')}
      <p class="memory-content">${esc(item.candidate.content)}</p><div class="evidence">证据：${esc(item.candidate.evidence)}</div>
      <p class="meta">${esc(item.observed_at || '时间未知')} · 批次 ${esc((item.run_id || '').slice(0, 8))}</p>
      <div class="actions">${Object.entries(SENSITIVE_LABELS).map(([action, label]) => `<button class="${action === 'keep_sensitive' ? '' : 'secondary'}"
        data-sensitive-action="${action}" data-item="${esc(item.item_id)}">${label}</button>`).join('')}</div></div></article>`).join('');
  $('review-body').innerHTML = `<div class="sensitive-banner" role="note">敏感候选只在本机 Web 可见，不进入模型上下文和 MCP，这里只能逐条处理。</div>
    ${files.skipped_files ? `<p class="subtle">有 ${files.skipped_files} 个隔离文件无法读取，已跳过。</p>` : ''}
    ${fileCards}${db.items.map(memory => renderMemory(memory, false)).join('')}
    ${fileCards || db.items.length ? '' : '<div class="empty">敏感队列为空。</div>'}`;
  bindMemoryActions(loadReview);
  $('review-body').querySelectorAll('[data-sensitive-action]').forEach(button => button.onclick = async () => {
    try {
      const action = button.dataset.sensitiveAction;
      if (SENSITIVE_CONFIRM[action] && !confirm(SENSITIVE_CONFIRM[action])) return;
      const result = await api(`/v1/review/sensitive/${button.dataset.item}`, {method: 'POST', body: {...scope, action}});
      notify(action === 'keep_sensitive' && !result.approved ? '已转为敏感候选，需先处理冲突后再批准' : `已${SENSITIVE_LABELS[action]}`);
      await loadReview();
    } catch (error) { notify(error.message, true); }
  });
}

async function loadLinkSuggestions(memory) {
  // 来源详情中的已保存 A/C 建议，确认或忽略都只写建议表
  const box = $('link-suggestions');
  if (!box) return;
  const data = await api(`/v1/review/links?memory_id=${encodeURIComponent(memory.memory_id)}&${qs()}`);
  if (!box.isConnected) return;
  box.innerHTML = data.items.length ? '<p class="subtle">已保存的关联建议（本地规则，需人工判断）：</p>' + data.items.map(link => `<div class="link-item">
      <span class="tag ${link.kind === 'conflict' ? 'badge-c' : 'badge-a'}">${link.kind === 'conflict' ? '时间冲突' : '证据关联'}</span>
      <span class="subtle">目标 ${esc(link.target_memory_id)}（建议时为${esc(statuses[link.target_status] || link.target_status)}）· 重合 ${link.signals.overlap} · ${esc(link.status)}</span>
      ${link.time_version ? `<p class="subtle">时间版本建议：新记忆自 ${esc(link.time_version.new_valid_from || '未知')} 起生效，旧记忆截至同一时间。</p>` : ''}
      ${link.status === 'suggested' ? `<button class="secondary" data-link-decision="confirm" data-link="${esc(link.link_id)}">确认</button>
        <button class="ghost" data-link-decision="dismiss" data-link="${esc(link.link_id)}">忽略</button>` : ''}</div>`).join('') : '';
  box.querySelectorAll('[data-link-decision]').forEach(button => button.onclick = async () => {
    try {
      await api(`/v1/review/links/${button.dataset.link}`, {method: 'POST', body: {...scope, decision: button.dataset.linkDecision}});
      await loadLinkSuggestions(memory);
    } catch (error) { notify(error.message, true); }
  });
}

function reviewKeys(event) {
  if (page !== 'review' || event.ctrlKey || event.metaKey || event.altKey || document.querySelector('dialog[open]')) return;
  const target = event.target;
  if (target.closest?.('input, textarea, select, [contenteditable="true"]')) return;
  const key = event.key.length === 1 ? event.key.toLowerCase() : event.key;
  if (key === 'Enter' && target.closest?.('button, a')) return;
  if (key === '?') { event.preventDefault(); notify(KEY_HELP); return; }
  const cards = [...document.querySelectorAll('[data-review-index]')];
  if (!cards.length) return;
  const card = cards[Math.min(reviewState.focus, cards.length - 1)];
  const click = selector => card.querySelector(selector)?.click();
  const move = step => {
    reviewState = {...reviewState, focus: Math.max(0, Math.min(cards.length - 1, reviewState.focus + step))};
    cards.forEach((item, index) => item.classList.toggle('focused', index === reviewState.focus));
    cards[reviewState.focus].scrollIntoView({block: 'nearest'});
  };
  const toggle = () => { const box = card.querySelector('[data-select]'); if (box) box.checked = !box.checked; };
  const actions = {j: () => move(1), ArrowDown: () => move(1), k: () => move(-1), ArrowUp: () => move(-1),
    a: () => click('[data-approve]'), r: () => click('[data-reject]'), e: () => click('[data-edit]'),
    o: () => click('[data-detail]'), Enter: () => click('[data-detail]'), x: toggle, l: () => deferCard(card)};
  if (!actions[key]) return;
  event.preventDefault();
  Promise.resolve(actions[key]()).catch(error => notify(error.message, true));
}

async function deferCard(card) {
  const id = card.querySelector('[data-select]')?.dataset.select;
  if (!id) return;
  const undo = reviewState.tab === 'deferred';
  await api('/v1/review/defer', {method: 'POST', body: {...scope, memory_ids: [id], undo}});
  notify(undo ? '已取消稍后处理' : '已移到稍后处理');
  await loadReview();
}
