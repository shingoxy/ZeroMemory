'use strict';

async function observatory() {
  let data, selected, cursor=0, disposed=false, requestId=0, drag, points=[];
  let scene=[], groups=[], focus, flight, frame=0;
  const overview={yaw:-.16,pitch:.27,zoom:1,x:0,y:0,z:0};
  let camera={...overview};
  const reducedMotion=matchMedia('(prefers-reduced-motion: reduce)');
  const rehearsed=new Set();
  const colors={episodic:'#a7d8ff',agent_experience:'#f5c978',user_profile:'#b9dca4',semantic:'#c5bbef',procedural:'#83d7cd',relationship:'#e5b9d2',working_context:'#e4cf8e'};
  const edgeColors={semantic_association:'#748ea5',same_source:'#b7dad3',time_near:'#bfa583',human_confirmed:'#dce6a2',source_link:'#bda7d9'};
  content.innerHTML=`<section class="card"><div class="section-head"><div><h2>零的记忆星海</h2><p class="subtle">现有数据库 · 真实记忆 · 只读观测</p></div><button id="obs-refresh" class="secondary">重新读取</button></div>
  <details id="obs-filters" ${matchMedia("(min-width:651px)").matches?"open":""}><summary>筛选与来源范围</summary><div class="filters"><input id="obs-query" aria-label="星海线索" placeholder="线索检索，不调用模型"><select id="obs-type" aria-label="星海记忆类型">${options(types,'',true)}</select><select id="obs-status" aria-label="星海审核状态">${options(statuses,'',true)}</select><button id="obs-search">检索星海</button></div>
  <div class="filter-dates"><label>起始日期（UTC）<input type="date" id="obs-since"></label><label>结束日期（UTC）<input type="date" id="obs-until"></label><label class="check"><input type="checkbox" id="obs-sensitive">明确查看当前作用域的敏感记忆</label></div>
  </details><p id="obs-summary" role="status" class="note-box"></p><div class="actions"><button id="obs-star" class="secondary">星图</button><button id="obs-time" class="secondary">时间线</button><button id="obs-zoom-in" class="ghost" aria-label="放大星图">＋</button><button id="obs-zoom-out" class="ghost" aria-label="缩小星图">－</button><button id="obs-center" class="ghost">居中</button></div>
  <div class="obs-legend"><span>浅绿实线：同源</span><span>紫色实线：原文/摘要候选关联</span><span>虚线：共同主题推导</span><span>点线：时间相近</span><span>粗线：人工批准关系</span><span>空心圆：待审或非活动记录</span></div>
  <div id="obs-chart" class="star-surface"><canvas id="obs-canvas" tabindex="0" role="img" aria-label="当前页三维记忆星海。方向键旋转，加减键缩放，也可使用下方列表选择记忆。"></canvas><p>拖动旋转 · 滚轮缩放 · 点选星点查看来源 · 星点间距仅用于布局</p></div><div id="obs-timeline" hidden></div>
  <div class="obs-lab"><label>仅视图模拟未来天数 <output id="obs-day-label">0</output><input type="range" id="obs-days" min="0" max="730" value="0"></label><button id="obs-rehearse" class="secondary">模拟巩固选中记忆</button><button id="obs-reset" class="ghost">重置模拟</button><p class="subtle">模拟亮度使用假设半衰期，从提炼时间计算；不会修改真实置信度、审核状态或数据库。</p></div>
  <div class="obs-columns"><div><h3>可访问记忆列表</h3><div id="obs-list" class="obs-list"></div></div><section id="obs-detail" class="obs-detail" aria-live="polite"><p>选择一条记忆，查看来源。</p></section></div>
  <div class="actions"><button id="obs-prev" class="secondary">上一页</button><button id="obs-next" class="secondary">下一页</button><span class="subtle">每页最多 100 条；关系只在当前授权页内计算。</span></div></section>`;
  const canvas=$('obs-canvas'), ctx=canvas.getContext('2d');
  const chart=$('obs-chart');
  const resize=new ResizeObserver(schedule); resize.observe(canvas);
  pageCleanup=()=>{disposed=true;resize.disconnect();cancelAnimationFrame(frame);};
  const age=m=>Math.max(0,(Date.parse(data.as_of)-Date.parse(m.created_at))/86400000)+Number($('obs-days').value);
  const accessibility=m=>Math.pow(2,-age(m)/(m.half_life_days*(rehearsed.has(m.id)?1.6:1)));
  const turn=2.39996323;
  let seed=3819;
  const random=()=>{
    seed=(Math.imul(seed,1664525)+1013904223)>>>0;
    return seed/4294967296;
  };
  const dust=Array.from({length:1050},(_,i)=>{
    const distant=i<450;
    const radius=2.1*Math.sqrt(random());
    const angle=distant?random()*Math.PI*2:i%3*Math.PI*2/3+radius*2.1+random()*.9;
    return {
      x:Math.cos(angle)*radius,
      y:Math.sin(angle)*radius*(distant?.83:.37)+(random()-.5)*.26,
      z:distant?-.95+random()*.55:(random()-.5)*.52,
      size:.35+random()**4*1.55,
      light:.12+random()**2*.7,
      color:['#a0c8ff','#ffe2c4','#e2bdff'][i%3],
    };
  });
  function sprite(size,paint){
    const image=document.createElement('canvas');
    image.width=image.height=size;
    paint(image.getContext('2d'),size/2);
    return image;
  }
  function glow(target,x,y,r,stops){
    const gradient=target.createRadialGradient(x,y,0,x,y,r);
    for(const [offset,color] of stops)gradient.addColorStop(offset,color);
    target.fillStyle=gradient;
    target.fillRect(x-r,y-r,r*2,r*2);
  }
  const nebula=sprite(384,(target,half)=>{
    const palette=['#7254eb','#3e74e0','#9e439c','#32a9ce'];
    target.globalCompositeOperation='lighter';
    for(let i=0;i<330;i++){
      const x=(random()-.5)*half*1.55;
      const ridge=.22*x+Math.sin(x/45)*half*.16;
      const y=ridge+(random()-.5)*half*.92;
      const r=6+random()**2*47;
      const color=palette[Math.floor(random()*palette.length)];
      glow(target,half+x,half+y,r,[[0,color+'20'],[.42,color+'0c'],[1,color+'00']]);
    }
    target.globalCompositeOperation='destination-out';
    for(let i=0;i<110;i++){
      const x=(random()-.5)*half*1.7;
      const y=.22*x+Math.sin(x/45)*half*.16+(random()-.5)*half*.26;
      glow(target,half+x,half+y,8+random()*28,[[0,'#0004'],[1,'#0000']]);
    }
  });
  const flares=new Map(Object.values(colors).map(color=>[color,sprite(128,(target,half)=>{
    glow(target,half,half,half,[[0,color+'e8'],[.10,color+'76'],[.32,color+'25'],[.64,color+'07'],[1,color+'00']]);
    target.translate(half,half);
    for(let i=0;i<4;i++){
      const length=i%2===0?half:half*.42;
      const beam=target.createLinearGradient(0,0,length,0);
      beam.addColorStop(0,'#ffffffe0');beam.addColorStop(.22,color+'75');beam.addColorStop(1,color+'00');
      target.fillStyle=beam;
      target.beginPath();target.moveTo(4,-1.7);target.lineTo(length,0);target.lineTo(4,1.7);target.fill();
      target.rotate(Math.PI/2);
    }
  })]));
  const clouds=[
    {x:-.48,y:-.22,z:-.69,size:2.5,angle:-.38,light:.78},
    {x:.48,y:.23,z:-.34,size:2.1,angle:2.7,light:.64},
    {x:.02,y:-.05,z:.13,size:1.58,angle:-.58,light:.48},
  ];
  function layout() {
    scene=[];groups=[];
    const keys=Object.keys(types);
    for(const [index,type] of keys.entries()) {
      const nodes=data.memories.filter(m=>m.memory_type===type).sort((a,b)=>a.id.localeCompare(b.id));
      if(!nodes.length)continue;
      const angle=index*Math.PI*2/keys.length-.9;
      const group={type,x:.92*Math.cos(angle),y:.63*Math.sin(angle),z:.29*Math.sin(angle*2)};
      groups.push(group);
      nodes.forEach((m,i)=>{
        const radius=.046*Math.sqrt(i);
        scene.push({...m,x:group.x+Math.cos(i*turn)*radius,y:group.y+Math.sin(i*turn)*radius,z:group.z+Math.sin(i*1.7)*radius});
      });
    }
  }
  function schedule() {
    if(!disposed&&!chart.hidden&&!frame)frame=requestAnimationFrame(draw);
  }
  function draw(now) {
    frame=0;
    if(disposed||!data||chart.hidden||!canvas.clientWidth)return;
    if(flight){
      const progress=reducedMotion.matches?1:Math.min(1,(now-flight.start)/720);
      const eased=1-(1-Math.max(0,progress))**3;
      for(const key of ['x','y','z','zoom'])camera[key]=flight.from[key]+(flight.to[key]-flight.from[key])*eased;
      if(progress===1)flight=null;
    }
    const w=canvas.clientWidth,h=canvas.clientHeight,dpr=Math.min(devicePixelRatio||1,2);
    const width=Math.round(w*dpr),height=Math.round(h*dpr);
    if(canvas.width!==width||canvas.height!==height){canvas.width=width;canvas.height=height;}
    ctx.setTransform(dpr,0,0,dpr,0,0);
    const space=ctx.createRadialGradient(w*.52,h*.46,0,w*.52,h*.46,Math.max(w,h)*.66);
    space.addColorStop(0,'#10132e');space.addColorStop(.55,'#060a1d');space.addColorStop(1,'#02030b');
    ctx.fillStyle=space;ctx.fillRect(0,0,w,h);
    const cy=Math.cos(camera.yaw),sy=Math.sin(camera.yaw);
    const cp=Math.cos(camera.pitch),sp=Math.sin(camera.pitch);
    const unit=Math.min(w,h)*.37*camera.zoom;
    function project(point) {
      const x=point.x-camera.x,y=point.y-camera.y,z=point.z-camera.z;
      const depth=z*cy-x*sy;
      const rotated=depth*cp+y*sp;
      const perspective=4/Math.max(1,4-rotated);
      return {x:w/2+(x*cy+z*sy)*unit*perspective,y:h/2+(y*cp-depth*sp)*unit*perspective,perspective};
    }
    ctx.globalCompositeOperation='lighter';
    for(const cloud of clouds){
      const p=project(cloud),size=unit*cloud.size*p.perspective;
      ctx.save();ctx.translate(p.x,p.y);ctx.rotate(cloud.angle+Math.sin(camera.yaw)*.4);
      ctx.globalAlpha=cloud.light;
      ctx.drawImage(nebula,-size,-size,size*2,size*2);
      ctx.restore();
    }
    for(const star of dust){
      const p=project(star);
      const size=star.size*p.perspective;
      if(p.x<0||p.x>w||p.y<0||p.y>h)continue;
      ctx.fillStyle=star.color;ctx.globalAlpha=star.light;
      ctx.fillRect(p.x,p.y,size,size);
      if(star.size>1.65){
        ctx.globalAlpha=star.light*.36;
        ctx.drawImage(flares.get(colors.episodic),p.x-7,p.y-7,14,14);
      }
    }
    ctx.globalAlpha=1;ctx.globalCompositeOperation='source-over';
    for(const group of groups){
      const p=project(group),r=unit*.47*p.perspective;
      const nebula=ctx.createRadialGradient(p.x,p.y,0,p.x,p.y,r);
      nebula.addColorStop(0,colors[group.type]+'2b');nebula.addColorStop(1,colors[group.type]+'00');
      ctx.fillStyle=nebula;ctx.fillRect(p.x-r,p.y-r,r*2,r*2);
    }
    points=scene.map(m=>({...m,...project(m)})).sort((a,b)=>a.perspective-b.perspective);
    const byId=new Map(points.map(m=>[m.id,m]));
    const neighbors=new Set();
    let flowing=false;
    for(const edge of data.edges) {
      const a=byId.get(edge.source),b=byId.get(edge.target);if(!a||!b)continue;
      const active=a.id===focus||b.id===focus;
      const color=edgeColors[edge.kind]||'#8794aa';
      const width=edge.kind==='human_confirmed'?2.5:1;
      ctx.strokeStyle=color;ctx.globalAlpha=active?.94:focus?.07:.19;
      ctx.lineWidth=width;
      ctx.setLineDash(edge.kind==='semantic_association'?[5,5]:edge.kind==='time_near'?[1,5]:[]);
      ctx.beginPath();ctx.moveTo(a.x,a.y);ctx.lineTo(b.x,b.y);
      if(active){
        ctx.globalAlpha=.12;ctx.lineWidth=width+8;ctx.stroke();
        ctx.globalAlpha=.28;ctx.lineWidth=width+3;ctx.stroke();
        ctx.globalAlpha=.93;ctx.lineWidth=width;
      }
      ctx.stroke();
      if(active){
        const [origin,target]=a.id===focus?[a,b]:[b,a];
        neighbors.add(target.id);
        if(!reducedMotion.matches){
          const distance=Math.hypot(target.x-origin.x,target.y-origin.y);
          if(distance<2)continue;
          const t=(now/1900+distance*.0017)%1;
          const x=origin.x+(target.x-origin.x)*t,y=origin.y+(target.y-origin.y)*t;
          const tail=Math.min(t,42/distance);
          const tx=x-(target.x-origin.x)*tail,ty=y-(target.y-origin.y)*tail;
          const trail=ctx.createLinearGradient(tx,ty,x,y);
          trail.addColorStop(0,color+'00');trail.addColorStop(.75,color);trail.addColorStop(1,'#fff');
          ctx.setLineDash([]);ctx.lineWidth=2.6;ctx.strokeStyle=trail;
          ctx.beginPath();ctx.moveTo(tx,ty);ctx.lineTo(x,y);ctx.stroke();
          ctx.drawImage(flares.get(colors.episodic),x-11,y-11,22,22);
        }
      }
    }
    ctx.setLineDash([]);
    for(const m of points) {
      const color=colors[m.memory_type],active=m.id===focus;
      const radius=(active?7:4)*m.perspective;
      ctx.globalAlpha=(.34+.66*accessibility(m))*(focus&&!active&&!neighbors.has(m.id)?.32:1);
      const flareSize=radius*(active?15:9);
      ctx.globalCompositeOperation='lighter';
      ctx.drawImage(flares.get(color),m.x-flareSize/2,m.y-flareSize/2,flareSize,flareSize);
      ctx.globalCompositeOperation='source-over';
      ctx.fillStyle=color;ctx.strokeStyle=color;ctx.lineWidth=1.5;
      if(m.status!=='approved'){
        ctx.fillStyle='#080e22';ctx.beginPath();ctx.arc(m.x,m.y,radius,0,Math.PI*2);ctx.fill();
      }
      ctx.beginPath();ctx.arc(m.x,m.y,radius,0,Math.PI*2);
      ctx.fillStyle='#f5f8ff';
      if(m.status==='approved')ctx.fill();else ctx.stroke();
      if(m.id===selected){
        ctx.globalAlpha=.9;ctx.strokeStyle='#fff';ctx.beginPath();ctx.arc(m.x,m.y,radius+5,0,Math.PI*2);ctx.stroke();
      }
      if(active){
        ctx.strokeStyle=color;ctx.lineWidth=1;
        const phase=reducedMotion.matches?0:now/1800;
        for(let i=0;i<3;i++){
          const angle=phase+i*Math.PI*2/3;
          ctx.globalAlpha=.67-i*.14;
          ctx.beginPath();ctx.arc(m.x,m.y,radius+12+i*4,angle,angle+1.55);ctx.stroke();
        }
        flowing=!reducedMotion.matches;
      }
    }
    ctx.shadowBlur=0;ctx.globalAlpha=1;
    ctx.font='12px system-ui';ctx.textAlign='center';ctx.fillStyle='#d5e3ff';
    if(focus){
      const m=byId.get(focus);
      if(m)ctx.fillText(m.title,m.x,Math.max(18,m.y-41),Math.min(240,w-24));
    }else{
      for(const group of groups){
        const p=project(group);
        ctx.fillText(types[group.type],p.x,p.y-24);
      }
    }
    if(flight||flowing)schedule();
  }
  function params(){return new URLSearchParams({...scope,query:$('obs-query').value,memory_type:$('obs-type').value,status:$('obs-status').value,include_sensitive:$('obs-sensitive').checked,limit:100,cursor,since:$('obs-since').value?$('obs-since').value+'T00:00:00Z':'',until:$('obs-until').value?$('obs-until').value+'T23:59:59.999999Z':''});}
  function cleanParams(){const p=params();for(const [k,v] of [...p])if(v==='')p.delete(k);return p;}
  async function load(){
    const serial=++requestId;const result=await api('/v1/observatory/snapshot?'+cleanParams());if(disposed||serial!==requestId)return;
    data=result;selected=data.memories.some(m=>m.id===selected)?selected:data.memories[0]?.id;
    resetCamera();
    layout();
    $('obs-summary').textContent=`本页 ${data.count} 条真实记忆 · 待审核 ${data.memories.filter(m=>m.status==='pending_review').length} 条 · 敏感记忆${data.sensitive_hidden?'已隐藏':'已按管理员选择显示'} · 快照 ${data.as_of}`;
    $('obs-list').innerHTML=data.memories.length?data.memories.map(m=>`<button class="obs-memory" data-obs-id="${esc(m.id)}"><span>${esc(types[m.memory_type])} · ${esc(statuses[m.status])} · ${esc(m.sensitivity)}</span><strong>${esc(m.title)}</strong><small>${esc(m.event_time_original||m.event_time||'事件时间未知')} · ${esc(m.id)}</small></button>`).join(''):'<p class="empty">没有符合当前授权与筛选的记忆。不会用演示数据替代。</p>';
    $('obs-list').querySelectorAll('[data-obs-id]').forEach(b=>b.onclick=()=>select(b.dataset.obsId));
    const timeline=[...data.memories].sort((a,b)=>a.timeline_at.localeCompare(b.timeline_at));
    $('obs-timeline').innerHTML=timeline.map(m=>`<article class="obs-event"><time>${esc(m.event_time_original||m.timeline_at)}</time><p>${esc(m.title)}</p><small>${esc(m.time_basis==='event_time'?'来源事件时间':'按提炼时间排列；不是事件发生时间')} · ${esc(statuses[m.status])}</small></article>`).join('');
    $('obs-prev').disabled=cursor===0;$('obs-next').disabled=data.next_cursor===null;
    schedule();if(selected)await select(selected,false);else $('obs-detail').textContent='当前页没有可显示的来源。';
  }
  async function select(id,fly=true){
    selected=id;
    const target=scene.find(m=>m.id===id);
    if(fly&&target){
      focus=id;
      flight={start:performance.now(),from:{...camera},to:{x:target.x,y:target.y,z:target.z,zoom:1.8}};
    }
    schedule();const serial=requestId;
    const m=await api('/v1/observatory/memories/'+encodeURIComponent(id)+'?'+cleanParams());
    if(disposed||serial!==requestId||selected!==id)return;
    $('obs-detail').innerHTML=`<h3>${esc(m.content)}</h3><p><span class="tag">${esc(statuses[m.status])}</span><span class="tag">${esc(m.sensitivity)}</span><span class="tag">${esc(m.claim_kind||m.inference_type)}</span></p><p class="warn">${esc(m.source_verification)}</p><p>置信度：${esc(m.confidence)}（来自原记录）</p><p>来源：${esc(m.source_type)} / ${esc(m.source_role)}<br>事件时间：${esc(m.event_time_original||m.event_time||'未知')}<br>提炼时间：${esc(m.extracted_at)}</p><p>证据：${esc(m.evidence||'没有原文证据')}</p><p class="subtle">${esc(m.review_note||'')}${m.source_artifact?`<br>摘要原始行：${esc(m.source_artifact.line)}<br>源文件：${esc(m.source_artifact.source_file)}`:''}</p><details><summary>完整来源元数据</summary>${jsonView(m)}</details><p id="obs-projection" class="subtle"></p>`;
    updateProjection();
  }
  function updateProjection(){
    const m=data?.memories.find(m=>m.id===selected);if(m&&$('obs-projection'))$('obs-projection').textContent=`模拟可及度 ${Math.round(accessibility(m)*100)}% · 假设半衰期 ${m.half_life_days} 天${rehearsed.has(m.id)?' × 1.6（模拟巩固）':''}；置信度保持 ${m.confidence}`;
    $('obs-day-label').value=$('obs-days').value;schedule();
  }
  function resetCamera(){camera={...overview};focus=flight=null;schedule();}
  function scale(f){flight=null;camera.zoom=Math.max(.55,Math.min(4,camera.zoom*f));schedule();}
  function rotate(dx,dy){
    flight=null;
    camera.yaw+=dx;
    camera.pitch=Math.max(-1.5,Math.min(1.5,camera.pitch+dy));
    schedule();
  }
  canvas.onwheel=e=>{e.preventDefault();scale(e.deltaY<0?1.1:1/1.1);};
  canvas.onpointerdown=e=>{
    if(!e.isPrimary||e.button!==0)return;
    drag={id:e.pointerId,x:e.clientX,y:e.clientY,moved:0};
    canvas.setPointerCapture(e.pointerId);
  };
  canvas.onpointermove=e=>{
    if(drag?.id!==e.pointerId)return;
    const dx=e.clientX-drag.x,dy=e.clientY-drag.y;
    drag.moved+=Math.abs(dx)+Math.abs(dy);
    if(drag.moved>=5){
      rotate(dx*.008,dy*.008);
      drag.x=e.clientX;drag.y=e.clientY;
    }
  };
  canvas.onpointerup=e=>{
    if(drag?.id!==e.pointerId)return;
    if(drag.moved<5){
      const rect=canvas.getBoundingClientRect();
      let hit,closest=16;
      for(const point of points){
        const distance=Math.hypot(point.x-e.clientX+rect.left,point.y-e.clientY+rect.top);
        if(distance<=closest){hit=point;closest=distance;}
      }
      if(hit)select(hit.id).catch(e=>notify(e.message,true));
    }
    drag=null;
  };
  canvas.onpointercancel=canvas.onlostpointercapture=e=>{if(drag?.id===e.pointerId)drag=null;};
  canvas.onkeydown=e=>{
    const directions={ArrowUp:[0,-.14],ArrowDown:[0,.14],ArrowLeft:[-.14,0],ArrowRight:[.14,0]};
    if(e.key==='+'||e.key==='-'||Object.hasOwn(directions,e.key)){
      e.preventDefault();
      if(e.key==='+')scale(1.2);
      else if(e.key==='-')scale(1/1.2);
      else rotate(...directions[e.key]);
    }
  };
  bind('obs-search','click',async()=>{cursor=0;await load();});bind('obs-refresh','click',load);
  bind('obs-prev','click',async()=>{cursor=Math.max(0,cursor-100);await load();});bind('obs-next','click',async()=>{cursor=data.next_cursor;await load();});
  bind('obs-days','input',updateProjection);bind('obs-rehearse','click',()=>{if(selected){if(rehearsed.has(selected))rehearsed.delete(selected);else rehearsed.add(selected);updateProjection();}});
  bind('obs-reset','click',()=>{rehearsed.clear();$('obs-days').value=0;updateProjection();});
  bind('obs-center','click',resetCamera);bind('obs-zoom-in','click',()=>scale(1.2));bind('obs-zoom-out','click',()=>scale(1/1.2));
  for(const view of ['star','time'])bind('obs-'+view,'click',()=>{chart.hidden=view!=='star';$('obs-timeline').hidden=view!=='time';schedule();});
  await load();
}
