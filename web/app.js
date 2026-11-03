'use strict';
const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const types = {user_profile:'用户画像',agent_experience:'零的经验',relationship:'关系',episodic:'事件',semantic:'知识',procedural:'方法',working_context:'工作上下文'};
const statuses = {approved:'已批准',pending_review:'待审核',rejected:'已驳回',revoked:'已撤销',superseded:'历史事实'};
let scope = {user_id:'default-user',agent_id:'zero-001',project_id:'default'};
let page = 'overview', currentIdentity, currentItems = [], currentJob, messageOffset=0, pollTimer;
let pageCleanup;
const content = $('content');
function notify(text, error=false) { $('notice').textContent = text; $('notice').className=error?'error':''; }
async function api(path, options={}) {
  const headers = {'X-Zero-Request':'1',...options.headers};
  if(options.body && !(options.body instanceof FormData)) {headers['Content-Type']='application/json';options.body=JSON.stringify(options.body);}
  const r=await fetch(path,{...options,headers});
  const data=await r.json(); if(!r.ok) throw new Error(data.error?.message || `请求失败 (${r.status})`); return data;
}
const qs = () => new URLSearchParams({user_id:scope.user_id,project_id:scope.project_id}).toString();
function options(map, selected='', all=false) {return (all?'<option value="">全部</option>':'')+Object.entries(map).map(([k,v])=>`<option value="${esc(k)}" ${k===selected?'selected':''}>${esc(v)}</option>`).join('');}
function jsonView(value) {return `<pre>${esc(JSON.stringify(value,null,2))}</pre>`;}
function showDetail(title, value) { $('detail-body').innerHTML=`<h2>${esc(title)}</h2>${typeof value==='string'?value:jsonView(value)}`; $('detail').showModal(); }
function bind(id, event, callback) {const el=$(id);if(el) el.addEventListener(event, async ev=>{try{await callback(ev);}catch(e){notify(e.message,true);}});}
async function navigate(next, evidence) {
  clearTimeout(pollTimer); if(pageCleanup){pageCleanup();pageCleanup=null;} page=next; notify('');
  document.querySelectorAll('nav button').forEach(b=>b.classList.toggle('active',b.dataset.page===page));
  $('page-title').textContent={overview:'身份与状态',observatory:'记忆星海',activity:'持续目标与经历',memories:'记忆档案',imports:'导入对话',rawHistory:'原始对话',review:'审核工作台',chat:'与零对话',settings:'模型与系统'}[page];
  content.innerHTML='<p class="subtle">正在读取本地数据…</p>';
  try {await ({overview,observatory:()=>observatory(),activity:()=>activityPage(),memories:()=>memoryPage(false),imports,rawHistory:()=>rawHistoryPage(evidence),review:()=>memoryPage(true),chat,settings})[page]();}
  catch(e){content.innerHTML='<div class="empty">暂时无法加载。请检查本地服务后重试。</div>';notify(e.message,true);}
}
async function overview() {
  const [id,system,health,versions]=await Promise.all([api('/v1/identity?'+qs()),api('/v1/system?'+qs()),api('/health'),api('/v1/identity/versions')]); currentIdentity=id;
  content.innerHTML=`<section class="hero card"><img class="portrait" src="/v1/identity/avatar" alt="零的初始形象"><div class="hero-copy"><span class="pill"><i></i> PERSISTENT IDENTITY / ${esc(id.soul.agent_id)}</span><h2>${esc(id.soul.name)}<span>${esc(id.soul.english_name)}</span></h2><p>${esc(id.style.description)}</p><div><span class="tag">18 岁 · 女性形象 · 虚构角色</span><span class="tag">身份版本 ${id.version}</span><span class="tag">中文</span></div><p>模型可以更换。名字、性格和经过你审核的记忆，会留在这里。</p></div></section>
  <div class="stats"><div class="stat"><span>持久记忆</span><strong>${system.counts.memories}</strong></div><div class="stat"><span>原始消息</span><strong>${system.counts.messages}</strong></div><div class="stat"><span>等待模型提炼</span><strong>${system.counts.extractions.waiting_llm||0}</strong></div></div>
  <div class="grid"><section class="card"><div class="section-head"><h2>人格基线</h2><span class="subtle">0 — 100</span></div><form id="identity-form">${Object.entries({curiosity:'好奇心',independence:'独立判断',directness:'直率',patience:'耐心基线',humor:'幽默'}).map(([k,v])=>`<label class="trait">${v}<input type="range" min="0" max="100" name="${k}" value="${id.soul.traits[k]}"><output>${id.soul.traits[k]}</output></label>`).join('')}<label class="field">表达风格<textarea id="identity-style" maxlength="2000">${esc(id.style.description)}</textarea></label><label class="check field"><input type="checkbox" id="identity-authorized" required>我授权修改零的核心人格，并保存新版本</label><button type="submit">保存人格版本</button></form></section>
  <div><section class="card"><h2>此刻的零</h2><div class="row"><span class="tag">${esc(id.state.mood)}</span><span class="subtle">确定性模拟状态</span></div><p class="subtle">耐心 ${id.state.patience_current} / 100　参与度 ${id.state.engagement} / 100</p><p class="subtle">情绪不代表真实感受，也不改变任何权限。</p><button id="state-reset" class="secondary">恢复状态基线</button></section><section class="card"><h2>运行状态</h2><p class="subtle">记忆后端　SQLite FTS5<br>本地模型　${esc(health.llm.reason)}<br>向量检索　未启用（关键词检索可用）<br>数据目录　${esc(system.home)}</p></section></div></div>
  <section class="card"><h2>身份版本</h2>${versions.map(v=>`<div class="version"><span>v${v.version} · ${esc(v.reason)} <small class="subtle">${esc(v.updated_at)}</small></span><button class="ghost" data-rollback="${v.version}">恢复为新版本</button></div>`).join('')}</section>`;
  document.querySelectorAll('input[type=range]').forEach(x=>x.oninput=()=>x.nextElementSibling.textContent=x.value);
  bind('identity-form','submit',async ev=>{ev.preventDefault(); const traits=Object.fromEntries(new FormData(ev.target));for(const k of Object.keys(traits)) traits[k]=Number(traits[k]);await api('/v1/identity',{method:'PATCH',body:{authorized:$('identity-authorized').checked,expected_version:id.version,soul:{...id.soul,traits},style:{...id.style,description:$('identity-style').value}}});await overview();notify('人格已保存为新版本。所有模型下次读取时使用相同设定。');});
  bind('state-reset','click',async()=>{await api('/v1/persona-state',{method:'POST',body:{...scope,event:'reset'}});await overview();});
  content.querySelectorAll('[data-rollback]').forEach(b=>b.onclick=async()=>{try{if(!confirm('授权将此历史人格恢复为一个新版本？'))return;await api('/v1/identity/rollback',{method:'POST',body:{version:Number(b.dataset.rollback),expected_version:id.version,authorized:true}});await overview();}catch(e){notify(e.message,true);}});
}
const isDeepSeekPilot = m => m.source_type === "chatgpt_export" && m.review_note?.startsWith("DeepSeek 试点 ");
function deepSeekLabel(memory) {
  if (isDeepSeekPilot(memory)) return '<span class="tag" data-review-origin="deepseek-pilot">DeepSeek 试点</span>';
  if (memory.source_type === 'chatgpt_export' && memory.review_note?.startsWith('DeepSeek 批次 ')) {
    return '<span class="tag">DeepSeek 批次</span>';
  }
  return '';
}
function renderMemory(m,review=false) {return `<article class="memory"><div class="checkbox-row">${review?`<input type="checkbox" data-select="${esc(m.memory_id)}" aria-label="选择此记忆">`:''}<div><span class="tag">${esc(types[m.memory_type])}</span><span class="tag ${esc(m.status)}">${esc(statuses[m.status])}</span><span class="tag">${esc(m.source_type)} · ${esc(m.source_role)}</span>${deepSeekLabel(m)}<p class="memory-content">${esc(m.content)}</p><p class="meta">${esc(m.event_time||m.event_time_original||m.extracted_at)} · v${m.version} · ${esc(m.memory_id)}</p>${m.evidence?`<div class="evidence">证据：${esc(m.evidence)}</div>`:''}${m.conflicts.length?`<p class="warn subtle">存在冲突：${esc(m.conflicts.join(', '))}</p>`:''}<div class="actions"><button class="ghost" data-detail="${esc(m.memory_id)}">来源与修订</button>${m.status==='pending_review'?`<button data-approve="${esc(m.memory_id)}">批准</button><button class="secondary" data-reject="${esc(m.memory_id)}">驳回</button>`:''}<button class="ghost" data-edit="${esc(m.memory_id)}">编辑</button><button class="ghost" data-merge="${esc(m.memory_id)}">合并</button>${m.status==='approved'?`<button class="ghost" data-revoke="${esc(m.memory_id)}">撤销</button>`:''}<button class="ghost" data-delete="${esc(m.memory_id)}">删除</button></div></div></div></article>`;}
async function memoryPage(review) {
  content.innerHTML=`<section class="card"><div class="section-head"><h2>${review?'每一条事实，都由你确认。':'属于零的长期记忆'}</h2><button id="new-memory" class="secondary">手工提议记忆</button></div><p class="subtle">${review?'自动提炼只产生候选；旧 Assistant 内容保留外部推断标记。批量批准遇到冲突时会报告并跳过。':'只向模型提供已批准记忆。修改会生成新版本，并重新进入审核。'}</p><div class="filters"><input id="memory-query" placeholder="关键词，支持中文子串" aria-label="搜索记忆"><select id="memory-type" aria-label="记忆类型">${options(types,'',true)}</select><select id="memory-status" aria-label="记忆状态">${options(statuses,review?'pending_review':'approved',true)}</select><input id="memory-source" placeholder="来源类型" aria-label="来源类型"></div><div class="filter-dates"><label>起始日期<input type="date" id="memory-since"></label><label>结束日期<input type="date" id="memory-until"></label><button id="memory-search">检索</button></div>${review?'<div class="actions"><label class="check"><input id="select-all" type="checkbox">全选此页</label><button id="batch-approve">批准所选</button><button class="secondary" id="batch-reject">驳回所选</button></div>':''}<div id="memory-results"></div><div class="actions"><button id="memory-prev" class="secondary">上一页</button><button id="memory-next" class="secondary">下一页</button></div></section>`;
  if (review) {
    $("memory-search").insertAdjacentHTML("beforebegin", '<label class="check"><input id="review-deepseek-pilot" type="checkbox">只看早期 DeepSeek 试点</label>');
    $("review-deepseek-pilot").checked = location.hash === "#deepseek-pilot";
    if (reviewImportedMemories) $("memory-source").value = "chatgpt_export";
    if (reviewImportedMemories || location.hash === "#deepseek-pilot") {
      $("memory-search").insertAdjacentHTML("afterend", '<a href="/?review=all">查看全部待审核</a>');
    }
  }
  let offset=0;
  async function load() {
    const params={...scope,query:$('memory-query').value,status:$('memory-status').value||null,memory_type:$('memory-type').value||null,source_type:$('memory-source').value||null,limit:30,offset,since:$('memory-since').value?$('memory-since').value+'T00:00:00+00:00':null,until:$('memory-until').value?$('memory-until').value+'T23:59:59+00:00':null};
    if ($("review-deepseek-pilot")?.checked) params.review_origin = "deepseek_pilot";
    const data=await api('/v1/memories/search',{method:'POST',body:params});currentItems=data.items;$('memory-results').innerHTML=data.items.length?data.items.map(m=>renderMemory(m,review)).join(''):'<div class="empty">这里还没有符合条件的记忆。可以导入对话，再引用原文或使用模型提炼候选。</div>';
    $('memory-prev').disabled=offset===0;$('memory-next').disabled=data.items.length<30; bindMemoryActions(load);
  }
  bind('review-deepseek-pilot','change',async()=>{offset=0;await load();});
  bind('memory-search','click',async()=>{offset=0;await load();});bind('memory-prev','click',async()=>{offset=Math.max(0,offset-30);await load();});bind('memory-next','click',async()=>{offset+=30;await load();});
  bind('new-memory','click',()=>memoryEditor(null,load));
  bind('select-all','change',ev=>document.querySelectorAll('[data-select]').forEach(x=>x.checked=ev.target.checked));
  for(const action of ['approve','reject']) bind('batch-'+action,'click',async()=>{const ids=[...document.querySelectorAll('[data-select]:checked')].map(x=>x.dataset.select);if(!ids.length)return;let success=0,errors=[];for(const id of ids){try{const m=currentItems.find(x=>x.memory_id===id);await approveOrReject(m,action);success++;}catch(e){errors.push(e.message);}}await load();notify(`完成 ${success} 条${errors.length?'；'+errors.join('；'):''}`,!!errors.length);});
  await load();
}
async function approveOrReject(m,action) {const body={...scope};if(action==='approve'){if(m.conflicts.length || m.summary_links?.length){const id=prompt('请选择取代的旧 memory_id（较早事件不能取代较新事件）',m.conflicts[0] || m.summary_links[0]);if(!id)throw Error('未指定冲突处理');body.supersedes=id;}if(!m.evidence_verified||m.source_role!=='user'){if(!confirm('这是一条手工陈述或外部推断，不能视为已验证的用户亲述。仍批准为可引用资料？'))throw Error('未确认外部资料');body.confirm_external=true;}}return api(`/v1/memories/${m.memory_id}/${action}`,{method:'POST',body});}
function evidenceControls(memory) {
  if (memory.source_type !== 'chatgpt_export') return '';
  const keys = [...new Set(memory.source_message_keys || [])];
  return keys.map((_, index) => `<button type="button" class="secondary" data-evidence-index="${index}">打开原始消息 ${index + 1}</button>`).join('')
    + '<button type="button" class="secondary" id="evidence-associations">对照旧记忆</button><div id="evidence-matches"></div>';
}
function bindEvidenceActions(memory) {
  const keys = [...new Set(memory.source_message_keys || [])];
  bind('evidence-associations', 'click', async event => {
    const button = event.currentTarget;
    const result = await api(`/v1/raw-history/associations/${encodeURIComponent(memory.memory_id)}?${qs()}`);
    if (!$('detail').open || !button.isConnected) return;
    const signal = result.has_time_change_wording ? '检测到时间变化措辞，需结合双方时间判断。' : '';
    $('evidence-matches').innerHTML = `<p class="subtle">词面重合 ${esc(result.total_matches)} 条，最多展示 3 条。关联与冲突由人工判断。${signal}</p>`
      + result.items.map(({overlap, memory: old}) => `<details><summary>${esc(statuses[old.status])} · v${esc(old.version)} · ${esc(overlap)} 个重合片段</summary>
        <p class="memory-content">${esc(old.content)}</p><p class="meta">事件时间 ${esc(old.event_time || old.event_time_original || '未知')}</p>
        <p>旧记忆证据：${esc(old.evidence || '暂无逐字引文')}</p><p>新记忆证据：${esc(memory.evidence || '暂无逐字引文')}</p>
        ${jsonView({memory_id: old.memory_id, source_type: old.source_type, source_artifact: old.source_artifact,
          source_conversation_id: old.source_conversation_id, source_message_ids: old.source_message_ids})}</details>`).join('');
  });
  $('detail-body').querySelectorAll('[data-evidence-index]').forEach(button => {
    button.onclick = async () => {
      try {
        const messageKey = keys[Number(button.dataset.evidenceIndex)];
        const target = await api(`/v1/raw-history/evidence/${encodeURIComponent(messageKey)}?${qs()}`);
        if (!$('detail').open || !button.isConnected) return;
        $('detail').close();
        await navigate('rawHistory', {...target, messageKey, quote: memory.evidence,
          eventTime: memory.event_time || memory.event_time_original});
      } catch (error) {
        const notice = $('evidence-error');
        if (button.isConnected && notice) notice.textContent = error.message;
      }
    };
  });
}
function bindMemoryActions(reload) {
  for(const action of ['approve','reject','revoke','delete','detail','edit','merge']) content.querySelectorAll(`[data-${action}]`).forEach(b=>b.onclick=async()=>{try{const id=b.dataset[action],m=currentItems.find(x=>x.memory_id===id);if(action==='detail'){const versions=await api(`/v1/memories/${id}/versions?`+qs());showDetail('来源与版本',`<p class="subtle">导入证据表示原文存在；语义是否成立仍须人工判断。旧备份不会随活动记忆删除。</p>${evidenceControls(m)}<p id="evidence-error" role="alert"></p>${jsonView(m)}<h3>修订历史</h3>${versions.map(v=>`<details><summary>v${v.version} · ${esc(statuses[v.status])}</summary>${jsonView(v)}<button data-undo="${v.version}">撤回到此内容（重新审核）</button></details>`).join('')}`);bindEvidenceActions(m);$('detail-body').querySelectorAll('[data-undo]').forEach(x=>x.onclick=async()=>{try{await api(`/v1/memories/${id}/undo`,{method:'POST',body:{...scope,version:Number(x.dataset.undo)}});$('detail').close();await reload();}catch(e){notify(e.message,true);}});return;}if(action==='merge'){const other=prompt('输入要合并的另一条 memory_id（须同一作用域）',m.duplicate_candidates[0]||'');if(!other)return;const text=prompt('输入合并后的内容（完整来源会被保留，保存后仍待审核）',m.content);if(!text)return;await api('/v1/memories/merge',{method:'POST',body:{...scope,memory_ids:[id,other],content:text}});await reload();notify('合并候选已保存；批准后旧记录转为历史。');return;}if(action==='edit'){memoryEditor(m,reload);return;}if(action==='delete'){if(!confirm('删除此活动记忆和修订？原始对话与旧备份仍保留。'))return;await api('/v1/memories/'+id,{method:'DELETE',body:scope});}else if(action==='revoke')await api(`/v1/memories/${id}/revoke`,{method:'POST',body:scope});else await approveOrReject(m,action);await reload();notify('记忆已更新');}catch(e){notify(e.message,true);}});
}
const proposalFields=['memory_type','content','source_type','source_conversation_id','source_message_ids','source_message_keys','source_role','evidence','event_time','valid_from','valid_to','confidence','inference_type','sensitivity','fact_key','claim_kind','origin_quality','topic','review_note','dedup_key','source_artifact','summary_links','event_time_original','event_time_precision','activity_goal_id','activity_episode_ids','activity_artifact_ids'];
function memoryEditor(m,reload) {
  showDetail(m?'编辑记忆 · 保存后重新审核':'手工提议记忆',`<form id="memory-editor"><label class="field">内容<textarea id="edit-content" maxlength="12000" required>${esc(m?.content||'')}</textarea></label><div class="form-grid"><label>类别<select id="edit-type">${options(types,m?.memory_type||'episodic')}</select></label><label>事实键（用于发现同一事实冲突）<input id="edit-key" value="${esc(m?.fact_key||'')}" placeholder="例如 user.current_device"></label><label>敏感度<select id="edit-sensitive">${options({private:'私有',public:'公开',sensitive:'敏感'},m?.sensitivity||'private')}</select></label><label>事件时间（带时区，可留空）<input id="edit-time" value="${esc(m?.event_time||'')}" placeholder="2026-10-29T12:00:00+08:00"></label></div><p class="subtle">导入记忆的原文证据和角色会保留。来源不可伪造；手工提议需要明确确认后才能批准。</p><button type="submit">保存待审核版本</button></form>`);
  bind('memory-editor','submit',async ev=>{ev.preventDefault();const body={...scope};if(m) for(const k of proposalFields)body[k]=m[k];Object.assign(body,{content:$('edit-content').value,memory_type:$('edit-type').value,fact_key:$('edit-key').value||null,sensitivity:$('edit-sensitive').value,event_time:$('edit-time').value||null});if(m)body.expected_version=m.version;await api(m?'/v1/memories/'+m.memory_id:'/v1/memories/propose',{method:m?'PATCH':'POST',body});$('detail').close();await reload();notify('已保存，等待审核');});
}
async function imports() {
  content.innerHTML=`<section class="card"><h2>把历史带回来</h2><p class="subtle">支持 ChatGPT 官方 ZIP、conversations.json、分卷 JSON 和通用 JSONL。文件先留存到本机进行预览，点击开始后才入库；不会自动向模型或云端发送。</p><form id="upload-form"><div class="upload"><p>选择本地对话导出文件</p><input type="file" id="import-file" accept=".zip,.json,.jsonl" required aria-label="导入对话文件"><p class="subtle">上传 ≤ 64 MiB · ZIP 展开 ≤ 256 MiB · 原始内容视为不可信资料</p></div><div class="actions field"><button type="submit">解析并预览</button><span class="subtle">无需本地模型</span></div></form><div id="import-preview"></div></section><section class="card"><h2>导入任务</h2><div id="import-jobs"></div></section><section class="card"><div class="section-head"><h2>原始消息与人工引用</h2><div class="actions"><button id="messages-prev" class="secondary">上一页</button><button id="messages-next" class="secondary">下一页</button></div></div><p class="subtle">每条消息保留节点和父节点，不将不同分支拼成同一轮对话。无模型时可手工将用户原文引用为待审核候选。</p><div class="filters"><input id="message-query" placeholder="检索本地原始消息" aria-label="检索原始消息"><button id="message-search">检索原文</button></div><div id="source-messages" class="messages-list"></div></section>`;
  bind('upload-form','submit',async ev=>{ev.preventDefault();notify('正在本地解析文件…');const form=new FormData();form.append('file',$('import-file').files[0]);form.append('scope',JSON.stringify(scope));currentJob=await api('/v1/imports',{method:'POST',body:form});renderPreview(currentJob);await loadJobs();notify('预览完成，尚未写入会话和记忆。');});
  async function loadJobs(){if(page!=='imports')return;const jobs=await api('/v1/imports?'+qs());$('import-jobs').innerHTML=jobs.length?jobs.map(j=>`<div class="job"><div class="row"><strong>${esc(j.filename)}</strong><span class="tag">${esc(j.status)}</span></div><progress value="${j.processed}" max="${j.total_messages||1}"></progress><p class="subtle">已处理 ${j.processed} / ${j.total_messages} · 新入库 ${j.inserted} · 重复 ${j.duplicates} · 失败 ${j.failed} · 导入时待提炼 ${j.waiting_extraction}</p>${j.errors.length?jsonView(j.errors):''}${['interrupted','failed','preview'].includes(j.status)?`<button class="secondary" data-resume="${j.id}">查看 / 继续</button>`:''}</div>`).join(''):'<p class="empty">还没有导入任务。</p>';$('import-jobs').querySelectorAll('[data-resume]').forEach(b=>b.onclick=async()=>{try{currentJob=jobs.find(j=>j.id===b.dataset.resume);renderPreview(currentJob);}catch(e){notify(e.message,true);}});clearTimeout(pollTimer);if(jobs.some(j=>['running','queued'].includes(j.status)))pollTimer=setTimeout(async()=>{try{await loadJobs();await loadMessages();}catch(e){notify(e.message,true);}},1500);}
  function renderPreview(job){$('import-preview').innerHTML=`<div class="note-box">${job.conversations.length} 个会话 · ${job.total_messages} 条消息 · ${(job.size/1024).toFixed(1)} KiB · 预计新增 ${job.estimated_new} 条</div><details><summary>选择会话（不勾选表示全部；断点重试沿用原选择）</summary><div class="messages-list">${job.conversations.map(c=>`<label class="check field"><input type="checkbox" name="conversation" value="${esc(c.id)}">${esc(c.title)} (${c.messages})</label>`).join('')}</div></details><div class="form-grid"><label>从（可选，按 UTC）<input id="import-since" type="date"></label><label>至（可选，按 UTC）<input id="import-until" type="date"></label></div><label class="check field"><input id="skip-sensitive" type="checkbox" checked>跳过敏感消息的自动提炼（原始来源仍本地保留）</label><button id="import-start">开始 / 继续离线导入</button>`;bind('import-start','click',async()=>{const selected=[...document.querySelectorAll('[name=conversation]:checked')].map(x=>x.value);const body=job.cursor?{...job.selection,skip_sensitive:job.skip_sensitive}:{conversation_ids:selected.length?selected:null,since:$('import-since').value?$('import-since').value+'T00:00:00+00:00':null,until:$('import-until').value?$('import-until').value+'T23:59:59+00:00':null,skip_sensitive:$('skip-sensitive').checked};await api(`/v1/imports/${job.id}/start?`+qs(),{method:'POST',body});$('import-preview').innerHTML='';await loadJobs();await loadMessages();notify('导入已启动，原文与待提炼任务保存在本机。');});}
  async function loadMessages(){if(page!=='imports')return;const data=await api(`/v1/messages?${qs()}&offset=${messageOffset}&limit=30&query=${encodeURIComponent($("message-query").value)}`);$('source-messages').innerHTML=data.items.length?data.items.map(m=>`<div class="message"><span class="tag">${esc(m.role)}</span><span class="subtle">${esc(m.timestamp||'时间缺失')} · ${esc(m.conversation_id)}</span><p>${esc(m.content)}</p><small class="subtle">节点 ${esc(m.node_id)} ← ${esc(m.parent_id||'root')} · 消息 ${esc(m.message_id)}</small><div class="actions field">${m.role==='user'&&m.content.trim()?`<button class="secondary" data-quote="${m.key}">引用原文为候选</button>`:''}<button class="ghost" data-source="${m.key}">原始证据</button></div></div>`).join(''):'<p class="empty">导入后将在这里显示原始消息。</p>';$('messages-prev').disabled=messageOffset===0;$('messages-next').disabled=data.items.length<30;$('source-messages').querySelectorAll('[data-quote]').forEach(b=>b.onclick=async()=>{try{await api('/v1/extractions/quote/'+b.dataset.quote,{method:'POST',body:scope});notify('原文已引用为待审核候选。这是人工引用，不是模型提炼。');}catch(e){notify(e.message,true);}});$('source-messages').querySelectorAll('[data-source]').forEach(b=>b.onclick=()=>showDetail('原始消息版本',data.items.find(m=>m.key===b.dataset.source)));}
  bind('message-search','click',async()=>{messageOffset=0;await loadMessages();});bind('messages-prev','click',async()=>{messageOffset=Math.max(0,messageOffset-30);await loadMessages();});bind('messages-next','click',async()=>{messageOffset+=30;await loadMessages();});await Promise.all([loadJobs(),loadMessages()]);
}
async function chat() {
  const model=await api('/v1/model');let preview;
  content.innerHTML=`<div class="note-box">${model.provider==='none'?'当前没有配置模型。身份、导入、人工审核和关键词检索仍然可用。':`模型：${esc(model.model)} · ${model.local?'本机连接':'云端连接：每次发送需明确确认上下文'}`}</div><section class="card"><div id="chat-log"><div class="chat-line">我是零。我的身份和你确认过的记忆保存在这台电脑里。</div></div><form id="chat-form"><label class="field">当前消息<textarea id="chat-message" maxlength="12000" required placeholder="聊聊正在做的项目…"></textarea></label><div class="actions"><button id="chat-preview" type="button" class="secondary">预览发送上下文</button><button type="submit">发送给模型</button><label class="check"><input type="checkbox" id="chat-consent">我确认向配置的云端地址发送已预览内容</label></div></form></section>`;
  const request=()=>({...scope,message:$('chat-message').value,session_id:'web-default',budget:8000});
  bind('chat-preview','click',async()=>{preview=await api('/v1/chat/preview',{method:'POST',body:request()});showDetail('本次完整发送上下文',preview);});
  bind('chat-form','submit',async ev=>{ev.preventDefault();const body=request();Object.assign(body,{consent:$('chat-consent').checked,preview_hash:preview?.preview_hash||null});const answer=await api('/v1/chat',{method:'POST',body});$('chat-log').insertAdjacentHTML('beforeend',`<div class="chat-line user">${esc(body.message)}</div><div class="chat-line">${esc(answer.answer)}</div><p class="subtle">身份 v${answer.identity_version} · 记忆 ${esc(answer.memory_ids.join(', ')||'无匹配')}</p>`);$('chat-message').value='';preview=null;$('chat-consent').checked=false;});
}
async function settings() {
  const [model,system,clients]=await Promise.all([api('/v1/model'),api('/v1/system?'+qs()),api('/v1/clients')]);let extractionPreview;
  content.innerHTML=`<div class="grid"><section class="card"><h2>模型连接</h2><p class="subtle">${esc(model.reason)}。不会自动下载模型。密钥只读 ZERO_LLM_API_KEY 环境变量。</p><form id="model-form"><label class="field">提供方<select id="model-provider">${options({none:'无模型模式',ollama:'本地 Ollama',openai:'OpenAI-compatible endpoint'},model.provider)}</select></label><label class="field">模型名称<input id="model-name" value="${esc(model.model)}"></label><label class="field">基础地址<input id="model-url" value="${esc(model.base_url)}"></label><label class="check field"><input id="model-cloud" type="checkbox" ${model.allow_cloud?'checked':''}>明确启用云端连接（每次发送还需预览确认）</label><button type="submit">保存模型配置</button></form></section><section class="card"><h2>备份与恢复</h2><p class="subtle">数据目录：${esc(system.home)}<br>备份包含原始会话、记忆、证据、人格版本和可重建索引元数据；不包含令牌和密钥。</p><button id="backup-create">生成本地备份</button><div id="backup-result" class="field"></div><form id="restore-form"><label class="field">恢复备份到一个全新目录<input type="file" id="restore-file" accept=".zip" required></label><p class="subtle">不会覆盖或自动切换当前数据。恢复后会显示新目录和启动方法。</p><button class="secondary" type="submit">验证并恢复</button></form><button id="index-rebuild" class="ghost field">重建关键词索引</button></section>
  <section class="card"><h2>自动提炼队列</h2><p class="subtle">${esc(JSON.stringify(system.counts.extractions))}<br>一次最多处理 10 条用户消息；只产生待审核候选。无模型时保留等待状态。</p><div class="actions"><button id="extraction-preview" class="secondary">预览这一批上下文</button><button id="extraction-run">提炼这一批</button></div><label class="check field"><input id="extraction-consent" type="checkbox">授权向云端发送这一批已预览消息</label><div id="extraction-result"></div></section>
  <section class="card"><h2>MCP 客户端权限</h2><p class="subtle">客户端令牌仅允许一个用户 / 项目，默认只读已批准记忆。令牌仅创建时显示一次，请妥善保存。</p><form id="client-form"><label class="field">客户端名称<input id="client-name" required placeholder="例如 Codex / 当前项目"></label><p class="subtle">授权作用域：${esc(scope.user_id)} / ${esc(scope.project_id)}</p><label class="check field"><input id="client-write" type="checkbox">允许提议待审核记忆（不允许批准或改人格）</label><button type="submit">创建作用域令牌</button></form><div id="client-token" class="field"></div>${clients.map(c=>`<div class="version"><span>${esc(c.name)} · ${esc(c.scope.user_id)}/${esc(c.scope.project_id)} · ${c.writable?'可提议':'只读'}</span><button class="ghost" data-client-revoke="${c.token_id}">撤销</button></div>`).join('')}</section></div>`;
  bind('model-form','submit',async ev=>{ev.preventDefault();await api('/v1/model',{method:'PATCH',body:{provider:$('model-provider').value,model:$('model-name').value,base_url:$('model-url').value,allow_cloud:$('model-cloud').checked}});notify('模型配置已保存。保存配置本身不会发送个人数据。');});
  bind('backup-create','click',async()=>{const b=await api('/v1/backup',{method:'POST'});$('backup-result').innerHTML=`<a href="${esc(b.download_url)}" download>下载备份 (${(b.size/1024).toFixed(1)} KiB)</a><p class="subtle">${esc(b.note)}</p>`;});
  bind('restore-form','submit',async ev=>{ev.preventDefault();const f=new FormData();f.append('file',$('restore-file').files[0]);showDetail('恢复结果',await api('/v1/restore',{method:'POST',body:f}));});
  bind('index-rebuild','click',async()=>{await api('/v1/index/rebuild',{method:'POST'});notify('索引已根据活动事实重建。');});
  bind('extraction-preview','click',async()=>{extractionPreview=await api('/v1/extractions/preview',{method:'POST',body:{...scope,limit:10}});showDetail('提炼发送上下文',extractionPreview);});
  bind('extraction-run','click',async()=>{$('extraction-result').innerHTML=jsonView(await api('/v1/extractions/run',{method:'POST',body:{...scope,limit:10,consent:$('extraction-consent').checked,preview_hash:extractionPreview?.preview_hash||null}}));extractionPreview=null;});
  bind('client-form','submit',async ev=>{ev.preventDefault();const c=await api('/v1/clients',{method:'POST',body:{...scope,name:$('client-name').value,writable:$('client-write').checked}});$('client-token').innerHTML=`<p class="subtle">只显示这一次。不要将令牌提交 Git。</p><div class="token">${esc(c.token)}</div><p class="subtle">设置 ZERO_API_TOKEN；ZERO_MCP_USER_ID=${esc(scope.user_id)}；ZERO_MCP_PROJECT_ID=${esc(scope.project_id)}。详见 docs/MCP_SETUP.md。</p>`;});
  content.querySelectorAll('[data-client-revoke]').forEach(b=>b.onclick=async()=>{try{await api('/v1/clients/'+b.dataset.clientRevoke,{method:'DELETE'});await settings();notify('客户端令牌已撤销');}catch(e){notify(e.message,true);}});
}
document.querySelectorAll('nav button').forEach(b=>b.onclick=()=>{
  if (b.dataset.page === 'review' && (reviewImportedMemories || location.hash === '#deepseek-pilot')) {
    location.assign('/?review=all');
    return;
  }
  navigate(b.dataset.page);
});
$('detail-close').onclick=()=>$('detail').close();
$('scope-apply').onclick=()=>{scope={...scope,user_id:$('scope-user').value.trim(),project_id:$('scope-project').value.trim()};messageOffset=0;navigate(page);};
const reviewMode = new URLSearchParams(location.search).get('review');
const reviewImportedMemories = reviewMode === 'chatgpt_export';
navigate(reviewImportedMemories || reviewMode === 'all' || location.hash === '#deepseek-pilot' ? 'review' : 'overview');
