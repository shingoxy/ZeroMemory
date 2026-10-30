'use strict';

async function observatory() {
  let data, selected, cursor=0, disposed=false, requestId=0, zoom=1, pan={x:0,y:0}, drag, points=[];
  const rehearsed=new Set();
  const colors={episodic:'#a7d8ff',agent_experience:'#f5c978',user_profile:'#b9dca4',semantic:'#c5bbef',procedural:'#83d7cd',relationship:'#e5b9d2',working_context:'#e4cf8e'};
  const edgeColors={semantic_association:'#748ea5',same_source:'#b7dad3',time_near:'#bfa583',human_confirmed:'#dce6a2',source_link:'#bda7d9'};
  content.innerHTML=`<section class="card"><div class="section-head"><div><h2>零的记忆星海</h2><p class="subtle">现有数据库 · 真实记忆 · 只读观测</p></div><button id="obs-refresh" class="secondary">重新读取</button></div>
  <details id="obs-filters" ${matchMedia("(min-width:651px)").matches?"open":""}><summary>筛选与来源范围</summary><div class="filters"><input id="obs-query" aria-label="星海线索" placeholder="线索检索，不调用模型"><select id="obs-type" aria-label="星海记忆类型">${options(types,'',true)}</select><select id="obs-status" aria-label="星海审核状态">${options(statuses,'',true)}</select><button id="obs-search">检索星海</button></div>
  <div class="filter-dates"><label>起始日期（UTC）<input type="date" id="obs-since"></label><label>结束日期（UTC）<input type="date" id="obs-until"></label><label class="check"><input type="checkbox" id="obs-sensitive">明确查看当前作用域的敏感记忆</label></div>
  </details><p id="obs-summary" role="status" class="note-box"></p><div class="actions"><button id="obs-star" class="secondary">星图</button><button id="obs-time" class="secondary">时间线</button><button id="obs-zoom-in" class="ghost" aria-label="放大星图">＋</button><button id="obs-zoom-out" class="ghost" aria-label="缩小星图">－</button><button id="obs-center" class="ghost">居中</button></div>
  <div class="obs-legend"><span>浅绿实线：同源</span><span>紫色实线：原文/摘要候选关联</span><span>虚线：共同主题推导</span><span>点线：时间相近</span><span>粗线：人工批准关系</span><span>空心圆：待审或非活动记录</span></div>
  <div id="obs-chart" class="star-surface"><canvas id="obs-canvas" tabindex="0" role="img" aria-label="当前页记忆关系星图；也可使用下方列表选择记忆。方向键平移，加减键缩放。"></canvas><p>每个星点对应一条真实记忆；距离仅为布局，不表示语义距离。</p></div><div id="obs-timeline" hidden></div>
  <div class="obs-lab"><label>仅视图模拟未来天数 <output id="obs-day-label">0</output><input type="range" id="obs-days" min="0" max="730" value="0"></label><button id="obs-rehearse" class="secondary">模拟巩固选中记忆</button><button id="obs-reset" class="ghost">重置模拟</button><p class="subtle">模拟亮度使用假设半衰期，从提炼时间计算；不会修改真实置信度、审核状态或数据库。</p></div>
  <div class="obs-columns"><div><h3>可访问记忆列表</h3><div id="obs-list" class="obs-list"></div></div><section id="obs-detail" class="obs-detail" aria-live="polite"><p>选择一条记忆，查看来源。</p></section></div>
  <div class="actions"><button id="obs-prev" class="secondary">上一页</button><button id="obs-next" class="secondary">下一页</button><span class="subtle">每页最多 100 条；关系只在当前授权页内计算。</span></div></section>`;
  const canvas=$('obs-canvas'), ctx=canvas.getContext('2d');
  const resize=new ResizeObserver(draw); resize.observe(canvas);
  pageCleanup=()=>{disposed=true;resize.disconnect();};
  const age=m=>Math.max(0,(Date.parse(data.as_of)-Date.parse(m.created_at))/86400000)+Number($('obs-days').value);
  const accessibility=m=>Math.pow(2,-age(m)/(m.half_life_days*(rehearsed.has(m.id)?1.6:1)));
  function draw() {
    if(disposed||!data||!canvas.clientWidth)return;
    const w=canvas.clientWidth,h=canvas.clientHeight,dpr=Math.min(devicePixelRatio||1,2);
    canvas.width=Math.round(w*dpr);canvas.height=Math.round(h*dpr);ctx.setTransform(dpr,0,0,dpr,0,0);
    ctx.clearRect(0,0,w,h);const groups=Object.keys(types), byId=new Map();
    points=[];
    for(const [groupIndex,type] of groups.entries()) {
      const nodes=data.memories.filter(m=>m.memory_type===type).sort((a,b)=>a.id.localeCompare(b.id));
      const anchor=groupIndex*Math.PI*2/groups.length-Math.PI/2;
      const ax=w/2+Math.cos(anchor)*w*.29,ay=h/2+Math.sin(anchor)*h*.27;
      nodes.forEach((m,i)=>{
        const angle=i*2.39996323, radius=10+Math.sqrt(i)*8;
        const point={...m,x:w/2+(ax+Math.cos(angle)*radius-w/2)*zoom+pan.x,y:h/2+(ay+Math.sin(angle)*radius-h/2)*zoom+pan.y};
        points.push(point);byId.set(m.id,point);
      });
      if(nodes.length){ctx.fillStyle='#cad7e6';ctx.font='12px system-ui';ctx.fillText(types[type],w/2+(ax-w/2)*zoom+pan.x-22,h/2+(ay-h/2)*zoom+pan.y-22-Math.sqrt(nodes.length)*7);}
    }
    for(const edge of data.edges) {
      const a=byId.get(edge.source),b=byId.get(edge.target);if(!a||!b)continue;
      ctx.strokeStyle=edgeColors[edge.kind]||'#8794aa';ctx.globalAlpha=edge.source===selected||edge.target===selected?.85:.22;
      ctx.lineWidth=edge.kind==='human_confirmed'?3:1;
      ctx.setLineDash(edge.kind==='semantic_association'?[5,5]:edge.kind==='time_near'?[1,5]:[]);
      ctx.beginPath();ctx.moveTo(a.x,a.y);ctx.lineTo(b.x,b.y);ctx.stroke();
    }
    ctx.setLineDash([]);ctx.globalAlpha=1;
    for(const m of points) {
      ctx.globalAlpha=.3+.7*accessibility(m);ctx.fillStyle=colors[m.memory_type];ctx.strokeStyle=colors[m.memory_type];ctx.lineWidth=1.5;
      ctx.beginPath();ctx.arc(m.x,m.y,m.id===selected?8:5,0,Math.PI*2);
      if(m.status==='approved')ctx.fill();else ctx.stroke();
      if(m.id===selected){ctx.globalAlpha=1;ctx.strokeStyle='#fff';ctx.beginPath();ctx.arc(m.x,m.y,12,0,Math.PI*2);ctx.stroke();}
    }
    ctx.globalAlpha=1;
  }
  function params(){return new URLSearchParams({...scope,query:$('obs-query').value,memory_type:$('obs-type').value,status:$('obs-status').value,include_sensitive:$('obs-sensitive').checked,limit:100,cursor,since:$('obs-since').value?$('obs-since').value+'T00:00:00Z':'',until:$('obs-until').value?$('obs-until').value+'T23:59:59.999999Z':''});}
  function cleanParams(){const p=params();for(const [k,v] of [...p])if(v==='')p.delete(k);return p;}
  async function load(){
    const serial=++requestId;const result=await api('/v1/observatory/snapshot?'+cleanParams());if(disposed||serial!==requestId)return;
    data=result;selected=data.memories.some(m=>m.id===selected)?selected:data.memories[0]?.id;
    $('obs-summary').textContent=`本页 ${data.count} 条真实记忆 · 待审核 ${data.memories.filter(m=>m.status==='pending_review').length} 条 · 敏感记忆${data.sensitive_hidden?'已隐藏':'已按管理员选择显示'} · 快照 ${data.as_of}`;
    $('obs-list').innerHTML=data.memories.length?data.memories.map(m=>`<button class="obs-memory" data-obs-id="${esc(m.id)}"><span>${esc(types[m.memory_type])} · ${esc(statuses[m.status])} · ${esc(m.sensitivity)}</span><strong>${esc(m.title)}</strong><small>${esc(m.event_time_original||m.event_time||'事件时间未知')} · ${esc(m.id)}</small></button>`).join(''):'<p class="empty">没有符合当前授权与筛选的记忆。不会用演示数据替代。</p>';
    $('obs-list').querySelectorAll('[data-obs-id]').forEach(b=>b.onclick=()=>select(b.dataset.obsId));
    const timeline=[...data.memories].sort((a,b)=>a.timeline_at.localeCompare(b.timeline_at));
    $('obs-timeline').innerHTML=timeline.map(m=>`<article class="obs-event"><time>${esc(m.event_time_original||m.timeline_at)}</time><p>${esc(m.title)}</p><small>${esc(m.time_basis==='event_time'?'来源事件时间':'按提炼时间排列；不是事件发生时间')} · ${esc(statuses[m.status])}</small></article>`).join('');
    $('obs-prev').disabled=cursor===0;$('obs-next').disabled=data.next_cursor===null;
    draw();if(selected)await select(selected);else $('obs-detail').textContent='当前页没有可显示的来源。';
  }
  async function select(id){
    selected=id;draw();const serial=requestId;
    const m=await api('/v1/observatory/memories/'+encodeURIComponent(id)+'?'+cleanParams());
    if(disposed||serial!==requestId||selected!==id)return;
    $('obs-detail').innerHTML=`<h3>${esc(m.content)}</h3><p><span class="tag">${esc(statuses[m.status])}</span><span class="tag">${esc(m.sensitivity)}</span><span class="tag">${esc(m.claim_kind||m.inference_type)}</span></p><p class="warn">${esc(m.source_verification)}</p><p>置信度：${esc(m.confidence)}（来自原记录）</p><p>来源：${esc(m.source_type)} / ${esc(m.source_role)}<br>事件时间：${esc(m.event_time_original||m.event_time||'未知')}<br>提炼时间：${esc(m.extracted_at)}</p><p>证据：${esc(m.evidence||'没有原文证据')}</p><p class="subtle">${esc(m.review_note||'')}${m.source_artifact?`<br>摘要原始行：${esc(m.source_artifact.line)}<br>源文件：${esc(m.source_artifact.source_file)}`:''}</p><details><summary>完整来源元数据</summary>${jsonView(m)}</details><p id="obs-projection" class="subtle"></p>`;
    updateProjection();
  }
  function updateProjection(){
    const m=data?.memories.find(m=>m.id===selected);if(m&&$('obs-projection'))$('obs-projection').textContent=`模拟可及度 ${Math.round(accessibility(m)*100)}% · 假设半衰期 ${m.half_life_days} 天${rehearsed.has(m.id)?' × 1.6（模拟巩固）':''}；置信度保持 ${m.confidence}`;
    $('obs-day-label').value=$('obs-days').value;draw();
  }
  function scale(f){zoom=Math.max(.5,Math.min(4,zoom*f));draw();}
  canvas.onwheel=e=>{e.preventDefault();scale(e.deltaY<0?1.1:1/1.1);};
  canvas.onpointerdown=e=>{drag={x:e.clientX,y:e.clientY,moved:0};canvas.setPointerCapture(e.pointerId);};
  canvas.onpointermove=e=>{if(drag){const dx=e.clientX-drag.x,dy=e.clientY-drag.y;drag.moved+=Math.abs(dx)+Math.abs(dy);pan.x+=dx;pan.y+=dy;drag.x=e.clientX;drag.y=e.clientY;draw();}};
  canvas.onpointerup=e=>{if(drag&&drag.moved<5){const r=canvas.getBoundingClientRect();const hit=points.find(m=>Math.hypot(m.x-e.clientX+r.left,m.y-e.clientY+r.top)<16);if(hit)select(hit.id).catch(e=>notify(e.message,true));}drag=null;};
  canvas.onpointercancel=canvas.onlostpointercapture=()=>{drag=null;};
  canvas.onkeydown=e=>{if(['+','-','ArrowUp','ArrowDown','ArrowLeft','ArrowRight'].includes(e.key)){e.preventDefault();if(e.key==='+')scale(1.2);if(e.key==='-')scale(1/1.2);if(e.key==='ArrowUp')pan.y+=25;if(e.key==='ArrowDown')pan.y-=25;if(e.key==='ArrowLeft')pan.x+=25;if(e.key==='ArrowRight')pan.x-=25;draw();}};
  bind('obs-search','click',async()=>{cursor=0;await load();});bind('obs-refresh','click',load);
  bind('obs-prev','click',async()=>{cursor=Math.max(0,cursor-100);await load();});bind('obs-next','click',async()=>{cursor=data.next_cursor;await load();});
  bind('obs-days','input',updateProjection);bind('obs-rehearse','click',()=>{if(selected){if(rehearsed.has(selected))rehearsed.delete(selected);else rehearsed.add(selected);updateProjection();}});
  bind('obs-reset','click',()=>{rehearsed.clear();$('obs-days').value=0;updateProjection();});
  bind('obs-center','click',()=>{zoom=1;pan={x:0,y:0};draw();});bind('obs-zoom-in','click',()=>scale(1.2));bind('obs-zoom-out','click',()=>scale(1/1.2));
  for(const view of ['star','time'])bind('obs-'+view,'click',()=>{$('obs-chart').hidden=view!=='star';$('obs-timeline').hidden=view!=='time';if(view==='star')draw();});
  await load();
}
