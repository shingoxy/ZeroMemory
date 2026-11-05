'use strict';

async function observatory() {
  let data, selected, hovered, focus, flight, drag, pinch, sky, skyTimer, skyPending='', cursor=0, requestId=0, frame=0;
  let disposed=false, visible=true, pointerInside=false, overStar=false, simDays=0, lastDraw=0, introAt=-1e9, lockAt=-1e9, swayClock=0, pixel=1, fitAt=0;
  let scene=[], groups=[], links=[], adjacency=[], order=[], byId=new Map(), extent={x:1,y:1};
  // 投影坐标、亮度和半径写进复用的类型化数组，几百个星点也不会每帧新建对象
  let starX=new Float32Array(0), starY=starX, starScale=starX, starDepth=starX, starAlpha=starX, starRadius=starX;
  const mix={hover:0,focus:0,sway:1};
  const pointers=new Map(), fitted=new Map(), rehearsed=new Set();
  const overview={yaw:-.16,pitch:.27,zoom:1,x:0,y:0,z:0};
  let camera={...overview};
  const reducedMotion=matchMedia('(prefers-reduced-motion: reduce)');
  const still=()=>reducedMotion.matches;
  const colors={episodic:'#86c4ff',agent_experience:'#ffc46e',user_profile:'#9fe39a',semantic:'#b49fff',procedural:'#67dfd0',relationship:'#ff93c4',working_context:'#ff9e7a'};
  // dash 用于点亮的宽线；thin 用于普通关联的发丝线（1 个物理像素，GPU 走快速路径，几千条也不卡）
  const edgeStyles={
    same_source:{color:'#93e2cd',dash:[],thin:[],width:1.1,label:'相同来源',note:'同一来源会话；不表示观点一致'},
    source_link:{color:'#c4a5f7',dash:[],thin:[],width:1.1,label:'原文/摘要候选',note:'摘要与原文候选关联，仍需审核'},
    semantic_association:{color:'#8db3df',dash:[5,5],thin:[5,4],width:1.1,label:'共同主题推测',note:'共同主题推导；不是向量相似度或事实证明'},
    time_near:{color:'#e9c58c',dash:[.1,4.6],thin:[1.4,3.2],width:1.8,label:'事件时间相近',note:'事件时间相近（7 天内）；不表示因果关系'},
    human_confirmed:{color:'#f0f6bd',dash:[],thin:[],width:2.6,label:'人工批准关系',note:'人工批准的取代/合并关系；不等于语义事实核验'},
  };
  const fallbackEdge={color:'#8794aa',dash:[],thin:[],width:1};
  const font='"Inter","Segoe UI","Microsoft YaHei",sans-serif';
  const turn=2.39996323;
  const clamp=(value,low=0,high=1)=>Math.min(high,Math.max(low,value));
  const easeOut=t=>1-(1-t)**3;
  const hash=text=>{let h=2166136261;for(let i=0;i<text.length;i++)h=Math.imul(h^text.charCodeAt(i),16777619);return (h>>>0)/4294967296;};
  const seeded=seed=>()=>{seed=(Math.imul(seed,1664525)+1013904223)>>>0;return seed/4294967296;};
  const indexOf=id=>id==null?-1:byId.get(id)??-1;
  const legend=Object.entries(edgeStyles).map(([kind,style])=>`<li title="${esc(style.note)}"><i class="obs-swatch" data-kind="${kind}"></i>${esc(style.label)}</li>`).join('');
  content.innerHTML=`<section class="card"><div class="section-head"><div><h2>零的记忆星海</h2><p class="subtle">现有数据库 · 真实记忆 · 只读观测</p></div><button id="obs-refresh" class="secondary">重新读取</button></div>
  <details id="obs-filters" ${matchMedia("(min-width:651px)").matches?"open":""}><summary>筛选与来源范围</summary><div class="filters"><input id="obs-query" aria-label="星海线索" placeholder="线索检索，不调用模型"><select id="obs-type" aria-label="星海记忆类型">${options(types,'',true)}</select><select id="obs-status" aria-label="星海审核状态">${options(statuses,'',true)}</select><button id="obs-search">检索星海</button></div>
  <div class="filter-dates"><label>起始日期（UTC）<input type="date" id="obs-since"></label><label>结束日期（UTC）<input type="date" id="obs-until"></label><label class="check"><input type="checkbox" id="obs-sensitive">明确查看当前作用域的敏感记忆</label></div>
  </details><p id="obs-summary" role="status" class="note-box"></p><div class="actions"><button id="obs-star" class="secondary obs-view" aria-pressed="true">星图</button><button id="obs-time" class="secondary obs-view" aria-pressed="false">时间线</button><button id="obs-zoom-in" class="ghost" aria-label="放大星图">＋</button><button id="obs-zoom-out" class="ghost" aria-label="缩小星图">－</button><button id="obs-center" class="ghost">居中</button></div>
  <div id="obs-chart" class="star-surface"><canvas id="obs-canvas" tabindex="0" role="img" aria-label="当前页三维记忆星海。方向键旋转，加减键缩放，Esc 返回全景，也可使用下方列表选择记忆。"></canvas>
  <div class="obs-hud"><ul id="obs-types" class="obs-key" aria-label="星点颜色对应的记忆类型"></ul><ul class="obs-key" aria-label="关联线样式">${legend}<li><i class="obs-swatch" data-kind="hollow"></i>空心：待审或非活动</li></ul><p>拖动旋转 · 滚轮或双指缩放 · 悬停看标题 · 点选星点查看来源 · 同源记忆就近成团，其余间距仅用于布局</p></div></div><div id="obs-timeline" class="obs-timeline" hidden></div>
  <div class="obs-lab"><label>仅视图模拟未来天数 <output id="obs-day-label">0</output><input type="range" id="obs-days" min="0" max="730" value="0"></label><button id="obs-rehearse" class="secondary">模拟巩固选中记忆</button><button id="obs-reset" class="ghost">重置模拟</button><p class="subtle">模拟亮度使用假设半衰期，从提炼时间计算；不会修改真实置信度、审核状态或数据库。</p></div>
  <div class="obs-columns"><div><h3>可访问记忆列表</h3><div id="obs-list" class="obs-list"></div></div><section id="obs-detail" class="obs-detail" aria-live="polite"><p>选择一条记忆，查看来源。</p></section></div>
  <div class="actions"><button id="obs-prev" class="secondary">上一页</button><button id="obs-next" class="secondary">下一页</button><span class="subtle">每页最多 100 条；关系只在当前授权页内计算。</span></div></section>`;
  const canvas=$('obs-canvas'), ctx=canvas.getContext('2d');
  const chart=$('obs-chart');
  // 画布尺寸变化（窗口缩放、手机横竖屏）时按新的适配比例同步缩放，保留用户当前的相对缩放
  const resize=new ResizeObserver(()=>{
    if(data&&fitAt){const next=fitZoom();camera={...camera,zoom:clamp(camera.zoom*next/fitAt,.55,4)};fitAt=next;}
    schedule();
  });
  resize.observe(canvas);
  // 星图滚出视口或切到时间线时停止渲染循环
  const sight=new IntersectionObserver(entries=>{visible=entries[entries.length-1].isIntersecting;schedule();}); sight.observe(chart);
  reducedMotion.addEventListener('change',schedule);
  pageCleanup=()=>{disposed=true;resize.disconnect();sight.disconnect();reducedMotion.removeEventListener('change',schedule);clearTimeout(skyTimer);cancelAnimationFrame(frame);};
  tint(chart);
  const ageOf=m=>Math.max(0,(Date.parse(data.as_of)-Date.parse(m.created_at))/86400000);
  const accessibility=(m,age=ageOf(m))=>Math.pow(2,-(age+simDays)/(m.half_life_days*(rehearsed.has(m.id)?1.6:1)));
  function tint(root){
    root.querySelectorAll('[data-type],[data-kind]').forEach(el=>el.style.setProperty('--tone',colors[el.dataset.type]||edgeStyles[el.dataset.kind]?.color||'#c9d8ea'));
  }
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
  const byColor=paint=>new Map(Object.values(colors).map(color=>[color,paint(color)]));
  // 星点贴图：加色光晕、衍射芒、清晰星核、失焦散景；空心环表示待审或非活动记录
  const halos=byColor(color=>sprite(96,(c,r)=>glow(c,r,r,r,[[0,color+'d8'],[.09,color+'90'],[.24,color+'38'],[.5,color+'10'],[1,color+'00']])));
  const flares=byColor(color=>sprite(160,(c,r)=>{
    glow(c,r,r,r*.55,[[0,'#ffffffc0'],[.18,color+'70'],[.5,color+'1c'],[1,color+'00']]);
    c.translate(r,r);
    for(let i=0;i<4;i++){
      const length=i%2?r*.5:r, beam=c.createLinearGradient(0,0,length,0);
      beam.addColorStop(0,'#ffffffd0');beam.addColorStop(.3,color+'66');beam.addColorStop(1,color+'00');
      c.fillStyle=beam;c.beginPath();c.moveTo(2,-1.3);c.lineTo(length,0);c.lineTo(2,1.3);c.fill();
      c.rotate(Math.PI/2);
    }
  }));
  const cores=byColor(color=>sprite(32,(c,r)=>glow(c,r,r,r,[[0,'#ffffff'],[.3,'#f7fbff'],[.52,color],[.78,color+'55'],[1,color+'00']])));
  const blurs=byColor(color=>sprite(48,(c,r)=>glow(c,r,r,r,[[0,color+'3c'],[.62,color+'46'],[.84,color+'6e'],[.93,color+'26'],[1,color+'00']])));
  const rings=byColor(color=>sprite(32,(c,r)=>{
    c.fillStyle='#070d1f';c.beginPath();c.arc(r,r,r*.6,0,Math.PI*2);c.fill();
    glow(c,r,r,r,[[0,color+'00'],[.44,color+'00'],[.6,color],[.74,color+'44'],[1,color+'00']]);
  }));
  const blurRings=byColor(color=>sprite(48,(c,r)=>glow(c,r,r,r,[[0,'#070d1f99'],[.5,'#070d1f88'],[.66,color+'70'],[.84,color+'26'],[1,color+'00']])));
  // 每个类型簇底下的一团同色星云，由多片拉长的薄雾叠成
  const clouds=byColor(color=>sprite(256,(c,r)=>{
    const random=seeded(parseInt(color.slice(1),16));
    c.globalCompositeOperation='lighter';
    for(let i=0;i<36;i++){
      const angle=random()*Math.PI*2, distance=random()**.8*r*.5;
      c.save();c.translate(r+Math.cos(angle)*distance,r+Math.sin(angle)*distance);c.rotate(random()*Math.PI);c.scale(1,.3+random()*.5);
      glow(c,0,0,r*(.16+random()*.3),[[0,color+'18'],[.5,color+'08'],[1,color+'00']]);
      c.restore();
    }
    c.globalCompositeOperation='destination-in';
    glow(c,r,r,r,[[0,'#000'],[.5,'#000b'],[1,'#0000']]);
  }));
  const spark=sprite(32,(c,r)=>glow(c,r,r,r,[[0,'#ffffff'],[.25,'#ffffffa0'],[.6,'#ffffff20'],[1,'#ffffff00']]));
  const glint=sprite(32,(c,r)=>{
    glow(c,r,r,r*.5,[[0,'#ffffffe0'],[.3,'#cfe0ff60'],[1,'#cfe0ff00']]);
    c.fillStyle='#e6efff80';c.fillRect(r-.5,2,1,r*2-4);c.fillRect(2,r-.5,r*2-4,1);
  });
  const dustTints=['#a6c9ff','#ffe4c8','#e2c4ff','#f4f7ff'];
  // 环绕星海的三维星尘：一部分在远处铺开，其余沿三条旋臂分布；按颜色排序以减少画布状态切换
  const dust=(()=>{
    const random=seeded(3819);
    return Array.from({length:620},(_,i)=>{
      const distant=i<240, radius=2.1*Math.sqrt(random());
      const angle=distant?random()*Math.PI*2:i%3*Math.PI*2/3+radius*2.1+random()*.9;
      return {
        x:Math.cos(angle)*radius,
        y:Math.sin(angle)*radius*(distant?.83:.37)+(random()-.5)*.26,
        z:distant?-.95+random()*.55:(random()-.5)*.52,
        size:.45+random()**4*1.6, light:.16+random()**2*.62, tint:i%4, phase:random()*Math.PI*2, speed:.5+random()*1.5,
      };
    }).sort((a,b)=>a.tint-b.tint);
  })();
  function layer(width,height,resolution,paint){
    const image=document.createElement('canvas');
    image.width=Math.ceil(width*resolution);image.height=Math.ceil(height*resolution);
    const c=image.getContext('2d');
    c.scale(resolution,resolution);
    paint(c,width,height);
    return image;
  }
  // 远景只在尺寸变化时重绘：深空渐变、斜向银河、远星和暗角；星云单独一层，拖动时产生视差
  function paintSky(w,h){
    const margin=Math.round(Math.min(w,h)*.08)+8, W=w+margin*2, H=h+margin*2, band=x=>H*(.82-.64*x/W);
    const far=layer(W,H,Math.min(devicePixelRatio||1,1.5),c=>{
      const random=seeded(1907), base=c.createRadialGradient(W*.55,H*.45,0,W*.55,H*.45,Math.hypot(W,H)*.6);
      base.addColorStop(0,'#13203d');base.addColorStop(.5,'#09122a');base.addColorStop(1,'#03060f');
      c.fillStyle=base;c.fillRect(0,0,W,H);
      for(let i=0;i<60;i++){const x=random()*W;glow(c,x,band(x)+(random()-.5)*H*.2,H*(.1+random()*.16),[[0,'#3b4f8c12'],[1,'#3b4f8c00']]);}
      const tints=['#ffffff','#cfe0ff','#ffe6cc','#e7d6ff'];
      for(let i=0,count=Math.round(W*H/480);i<count;i++){
        const x=random()*W, y=random()<.45?band(x)+(random()+random()+random()-1.5)*H*.14:random()*H;
        c.globalAlpha=.14+random()**2*.72;c.fillStyle=tints[i%4];
        c.beginPath();c.arc(x,y,.3+random()**3*1.05,0,Math.PI*2);c.fill();
      }
      c.globalAlpha=1;
      const vignette=c.createRadialGradient(W/2,H/2,Math.min(W,H)*.3,W/2,H/2,Math.hypot(W,H)*.56);
      vignette.addColorStop(0,'#03060f00');vignette.addColorStop(1,'#03060fd0');
      c.fillStyle=vignette;c.fillRect(0,0,W,H);
    });
    return {key:w+'x'+h,margin,far,nebula:layer(W,H,.6,c=>paintNebula(c,W,H,band))};
  }
  function paintNebula(c,W,H,band){
    const random=seeded(4721), unit=Math.min(W,H);
    const regions=[[.16,.74,['#2b86b5','#3a4ec2','#1d9d93']],[.82,.24,['#7a4ed2','#b24b92','#4656c9']],[.5,.48,['#36509f','#6247b8']]];
    c.globalCompositeOperation='lighter';
    for(const [rx,ry,hues] of regions){
      for(let i=0;i<120;i++){
        const angle=random()*Math.PI*2, distance=random()**.7*unit*.42, hue=hues[Math.floor(random()*hues.length)];
        c.save();c.translate(rx*W+Math.cos(angle)*distance*1.6,ry*H+Math.sin(angle)*distance*.7);
        c.rotate(-.5+(random()-.5)*.8);c.scale(1,.25+random()*.45);
        glow(c,0,0,unit*(.04+random()**2*.2),[[0,hue+(random()<.12?'36':'1b')],[.45,hue+'0a'],[1,hue+'00']]);
        c.restore();
      }
    }
    for(let i=0;i<12;i++)glow(c,random()*W,random()*H,unit*(.006+random()*.014),[[0,'#e4ecff26'],[1,'#e4ecff00']]);
    c.globalCompositeOperation='destination-out';
    for(let i=0;i<80;i++){
      const x=random()*W;
      c.save();c.translate(x,band(x)+(random()-.5)*H*.1);c.rotate(-.55);c.scale(1,.3);
      glow(c,0,0,unit*(.02+random()*.07),[[0,'#00000060'],[1,'#00000000']]);
      c.restore();
    }
    // 中心稍淡，给记忆星点留出对比
    c.globalCompositeOperation='destination-in';
    const mask=c.createRadialGradient(W/2,H/2,0,W/2,H/2,Math.hypot(W,H)*.55);
    mask.addColorStop(0,'#0009');mask.addColorStop(.45,'#000');mask.addColorStop(1,'#0003');
    c.fillStyle=mask;c.fillRect(0,0,W,H);
  }
  // 拖拽改变尺寸时先拉伸旧图层，尺寸稳定后再重绘，避免每帧重新生成
  function backdrop(w,h){
    const key=w+'x'+h;
    if(!sky)sky=paintSky(w,h);
    else if(sky.key!==key&&skyPending!==key){
      skyPending=key;clearTimeout(skyTimer);
      skyTimer=setTimeout(()=>{skyPending='';if(!disposed){sky=paintSky(w,h);schedule();}},180);
    }
    return sky;
  }
  // 每个记忆类型是一个倾斜的星盘；同一来源会话的记忆聚成小星座，没有来源的各自成组
  function cluster(type,index,total,spacing){
    const members=data.memories.filter(m=>m.memory_type===type).sort((a,b)=>a.id.localeCompare(b.id));
    const angle=index*Math.PI*2/total-.9;
    const center=total===1?{x:0,y:0,z:0}:{x:.92*spacing*Math.cos(angle),y:.63*spacing*Math.sin(angle),z:.34*spacing*Math.sin(angle*2)};
    const radius=.05*Math.sqrt(members.length)+.04;
    const tiltX=.42*Math.sin(index*2.1+.4), tiltY=.38*Math.cos(index*1.7+.2);
    const place=(x,y,z)=>{
      const y1=y*Math.cos(tiltX)-z*Math.sin(tiltX), z1=y*Math.sin(tiltX)+z*Math.cos(tiltX);
      return {x:center.x+x*Math.cos(tiltY)+z1*Math.sin(tiltY),y:center.y+y1,z:center.z-x*Math.sin(tiltY)+z1*Math.cos(tiltY)};
    };
    const sources=new Map();
    for(const m of members){const key=m.source_conversation_id||m.id;sources.set(key,[...(sources.get(key)||[]),m]);}
    let filled=0;
    const stars=[...sources.values()].sort((a,b)=>b.length-a.length||a[0].id.localeCompare(b[0].id)).flatMap((family,j)=>{
      const hub=.05*Math.sqrt(filled+family.length/2);
      filled+=family.length;
      return family.map((memory,i)=>{
        const spread=.026*Math.sqrt(i);
        const x=Math.cos(j*turn)*hub+Math.cos(i*turn+j)*spread, y=Math.sin(j*turn)*hub+Math.sin(i*turn+j)*spread;
        const z=(hash(memory.id)-.5)*.16*Math.max(.3,1-Math.hypot(x,y)/radius);
        return {memory,...place(x,y,z)};
      });
    });
    const ring=new Float32Array(41*3);
    for(let k=0;k<=40;k++){const p=place(Math.cos(k*Math.PI/20)*radius*1.12,Math.sin(k*Math.PI/20)*radius*1.12,0);ring.set([p.x,p.y,p.z],k*3);}
    return {type,...center,radius,count:members.length,stars,ring};
  }
  // 关联线是三维二次曲线：跨簇的向外、向镜头拱起，簇内的只轻微弯曲
  function arc(edge){
    const a=byId.get(edge.source), b=byId.get(edge.target);
    if(a===undefined||b===undefined)return [];
    const A=scene[a], B=scene[b], mx=(A.x+B.x)/2, my=(A.y+B.y)/2, mz=(A.z+B.z)/2;
    const bow=Math.hypot(B.x-A.x,B.y-A.y,B.z-A.z)*(A.group===B.group?.16:.3), out=Math.hypot(mx,my)||1;
    return [{a,b,style:edgeStyles[edge.kind]||fallbackEdge,phase:hash(edge.source+edge.target),cx:mx+mx/out*bow*.7,cy:my+my/out*bow*.7-bow*.3,cz:mz+bow*.5}];
  }
  function layout() {
    const present=Object.keys(types).filter(type=>data.memories.some(m=>m.memory_type===type));
    const timeline=[...data.memories].sort((a,b)=>a.timeline_at.localeCompare(b.timeline_at));
    const ranks=new Map(timeline.map((m,i)=>[m.id,i/Math.max(1,timeline.length-1)]));
    const asOf=Date.parse(data.as_of);
    // 大簇把环拉开，簇与簇之间留出空隙；全景缩放随后按范围自适应
    const largest=Math.max(0,...present.map(type=>data.memories.filter(m=>m.memory_type===type).length));
    const spacing=Math.max(1,(.05*Math.sqrt(largest)+.04)*2.6);
    groups=present.map((type,index)=>cluster(type,index,present.length,spacing));
    scene=groups.flatMap(group=>group.stars.map(({memory:m,x,y,z})=>({
      ...m,x,y,z,group,color:colors[m.memory_type],hollow:m.status!=='approved',rank:ranks.get(m.id),
      age:Math.max(0,(asOf-Date.parse(m.created_at))/86400000),phase:hash(m.id)*Math.PI*2,speed:.7+hash(m.id+'~')*.9,
    })));
    byId=new Map(scene.map((m,i)=>[m.id,i]));
    links=data.edges.flatMap(arc);
    adjacency=scene.map(()=>new Set());
    for(const {a,b} of links){adjacency[a].add(b);adjacency[b].add(a);}
    order=scene.map((_,i)=>i);
    [starX,starY,starScale,starDepth,starAlpha,starRadius]=Array.from({length:6},()=>new Float32Array(scene.length));
    extent={x:Math.max(.3,...scene.map(m=>Math.abs(m.x)))+.08,y:Math.max(.3,...scene.map(m=>Math.abs(m.y)))+.16};
    fitted.clear();
  }
  // 全景缩放按当前页的星盘范围自适应，单个类型的页面也能铺满画面
  function fitZoom(){
    const w=canvas.clientWidth||900, h=canvas.clientHeight||480, unit=Math.min(w,h)*.37;
    return clamp(Math.min(w*.45/(unit*extent.x),h*.42/(unit*extent.y)),.55,2.6);
  }
  const focusZoom=()=>clamp(fitZoom()*1.7,1.8,3.6);
  function flyTo(target,duration=760){
    const to={...target};
    if('yaw' in to)to.yaw=camera.yaw+Math.atan2(Math.sin(to.yaw-camera.yaw),Math.cos(to.yaw-camera.yaw));
    flight=still()?null:{start:performance.now(),from:{...camera},to,duration};
    if(!flight)camera={...camera,...to};
    schedule();
  }
  function schedule() {
    if(!disposed&&!chart.hidden&&visible&&!frame)frame=requestAnimationFrame(draw);
  }
  const swayTarget=()=>pointerInside||pointers.size?0:focus?.45:1;
  function settling(now){
    return Boolean(flight||drag||pinch||focus)||now-introAt<1800||now-lockAt<600
      ||Math.abs(mix.hover-(hovered?1:0))>.01||Math.abs(mix.focus-(focus?1:0))>.01||Math.abs(mix.sway-swayTarget())>.01;
  }
  // 推进缓动：悬停和聚焦的强调程度、镜头呼吸、飞行动画
  function step(now,dt){
    const rate=still()?1:Math.min(1,dt*8);
    mix.hover+=((hovered?1:0)-mix.hover)*rate;
    mix.focus+=((focus?1:0)-mix.focus)*rate;
    mix.sway+=(swayTarget()-mix.sway)*(still()?1:Math.min(1,dt*2.5));
    if(!still())swayClock+=dt*mix.sway;
    if(!flight)return;
    const progress=clamp((now-flight.start)/flight.duration), eased=easeOut(progress);
    camera={...camera,...Object.fromEntries(Object.keys(flight.to).map(key=>[key,flight.from[key]+(flight.to[key]-flight.from[key])*eased]))};
    if(progress===1)flight=null;
  }
  function draw(now) {
    frame=0;
    if(disposed||!data||chart.hidden||!visible||!canvas.clientWidth)return;
    // 只剩环境闪烁时降到约 30fps，交互和动画期间满帧
    if(!still()&&!settling(now)&&now-lastDraw<32){schedule();return;}
    const dt=clamp((now-lastDraw)/1000,0,.1);lastDraw=now;
    step(now,dt);
    const w=canvas.clientWidth,h=canvas.clientHeight,dpr=Math.min(devicePixelRatio||1,2);
    const width=Math.round(w*dpr),height=Math.round(h*dpr);
    if(canvas.width!==width||canvas.height!==height){canvas.width=width;canvas.height=height;}
    ctx.setTransform(dpr,0,0,dpr,0,0);
    pixel=1/dpr;
    const t=still()?0:now/1000, intro=still()?1e9:now-introAt, sway=still()?0:1;
    const view=projector(w,h,camera.yaw+Math.sin(swayClock*.26)*.08*sway,camera.pitch+Math.sin(swayClock*.19)*.035*sway);
    drawSky(view,w,h,t);
    drawDust(view.project,w,h,t);
    projectStars(view.project,intro);
    drawClusters(view,t,intro);
    drawLinks(view.project,now,intro);
    drawStars(t,intro);
    drawMarks(view,w,h,now,intro);
    if(!still()||flight)schedule();
  }
  function projector(w,h,yaw,pitch){
    const cosYaw=Math.cos(yaw),sinYaw=Math.sin(yaw),cosPitch=Math.cos(pitch),sinPitch=Math.sin(pitch);
    const unit=Math.min(w,h)*.37*camera.zoom,{x:ox,y:oy,z:oz}=camera,point={x:0,y:0,s:1,d:0};
    // 返回同一个 point 对象，调用方需要立即读取
    const project=(px,py,pz)=>{
      const x=px-ox,y=py-oy,z=pz-oz,depth=z*cosYaw-x*sinYaw,rotated=depth*cosPitch+y*sinPitch,s=4/Math.max(1,4-rotated);
      point.x=w/2+(x*cosYaw+z*sinYaw)*unit*s;point.y=h/2+(y*cosPitch-depth*sinPitch)*unit*s;point.s=s;point.d=rotated;
      return point;
    };
    return {project,unit,yaw,pitch};
  }
  function drawSky({yaw,pitch},w,h,t){
    const {far,nebula,margin}=backdrop(w,h), fx=Math.sin(yaw)*margin*.3, fy=Math.sin(pitch)*margin*.3;
    ctx.globalCompositeOperation='source-over';ctx.globalAlpha=1;
    ctx.drawImage(far,-margin-fx,-margin+fy,w+margin*2,h+margin*2);
    ctx.globalCompositeOperation='lighter';ctx.globalAlpha=.88+.12*Math.sin(t*.21);
    ctx.drawImage(nebula,-margin-fx*2+Math.sin(t*.05)*margin*.25,-margin+fy*2,w+margin*2,h+margin*2);
  }
  function drawDust(project,w,h,t){
    ctx.globalCompositeOperation='lighter';
    let tint=-1;
    for(const star of dust){
      const p=project(star.x,star.y,star.z);
      if(p.x<-6||p.x>w+6||p.y<-6||p.y>h+6)continue;
      ctx.globalAlpha=star.light*(.6+.4*Math.sin(t*star.speed+star.phase));
      const size=star.size*p.s;
      if(star.size>1.5){const g=size*6;ctx.drawImage(glint,p.x-g/2,p.y-g/2,g,g);continue;}
      if(star.tint!==tint){tint=star.tint;ctx.fillStyle=dustTints[tint];}
      ctx.fillRect(p.x-size/2,p.y-size/2,size,size);
    }
  }
  // 开场按时间线先后点亮星点，并从簇中心向外舒展
  const ignite=(m,intro)=>easeOut(clamp((intro-m.rank*900)/650));
  function projectStars(project,intro){
    for(let i=0;i<scene.length;i++){
      const m=scene[i], g=m.group, spread=.3+.7*ignite(m,intro);
      const p=project(g.x+(m.x-g.x)*spread,g.y+(m.y-g.y)*spread,g.z+(m.z-g.z)*spread);
      starX[i]=p.x;starY[i]=p.y;starScale[i]=p.s;starDepth[i]=p.d;
    }
    order.sort((a,b)=>starDepth[a]-starDepth[b]);
  }
  function drawClusters({project,unit},t,intro){
    const alpha=clamp(intro/700)*(1-.7*mix.focus);
    if(alpha<.01)return;
    ctx.globalCompositeOperation='lighter';
    for(const group of groups){
      const p=project(group.x,group.y,group.z), size=group.radius*unit*p.s*3.6;
      ctx.globalAlpha=alpha*(.85+.15*Math.sin(t*.4+group.radius*9));
      ctx.drawImage(clouds.get(colors[group.type]),p.x-size/2,p.y-size/2,size,size);
    }
    ctx.globalCompositeOperation='source-over';ctx.setLineDash([2,7]);ctx.lineWidth=pixel;
    for(const group of groups){
      ctx.strokeStyle=colors[group.type];ctx.globalAlpha=alpha*.3;ctx.beginPath();
      for(let k=0;k<group.ring.length;k+=3){
        const p=project(group.ring[k],group.ring[k+1],group.ring[k+2]);
        if(k)ctx.lineTo(p.x,p.y);else ctx.moveTo(p.x,p.y);
      }
      ctx.stroke();
    }
    ctx.setLineDash([]);
  }
  // 普通关联按样式和远近合并成少数几条路径一次描边；与悬停、聚焦星点相连的单独发光
  function drawLinks(project,now,intro){
    const reveal=clamp((intro-500)/800);
    if(!links.length||reveal<=0)return;
    const f=indexOf(focus), hv=indexOf(hovered), batches=new Map(), lit=[];
    for(const link of links){
      const level=link.a===f||link.b===f?2:link.a===hv||link.b===hv?1:0;
      if(level){lit.push([link,level]);continue;}
      const batch=batches.get(link.style)||{near:new Path2D(),far:new Path2D()};
      batches.set(link.style,batch);
      const path=starDepth[link.a]+starDepth[link.b]>0?batch.near:batch.far, c=project(link.cx,link.cy,link.cz);
      path.moveTo(starX[link.a],starY[link.a]);path.quadraticCurveTo(c.x,c.y,starX[link.b],starY[link.b]);
    }
    const base=.42*reveal*(1-.8*mix.focus)*(1-.5*mix.hover);
    ctx.globalCompositeOperation='source-over';ctx.lineCap='round';
    for(const [style,batch] of batches){
      ctx.strokeStyle=style.color;ctx.lineWidth=style.width>2?style.width:pixel;ctx.setLineDash(style.thin);
      ctx.globalAlpha=base;ctx.stroke(batch.near);
      ctx.globalAlpha=base*.5;ctx.stroke(batch.far);
    }
    for(const [link,level] of lit)drawLit(link,level,project,now,reveal,f);
    ctx.setLineDash([]);ctx.lineCap='butt';
  }
  function drawLit(link,level,project,now,reveal,f){
    const {style}=link, {x:cx,y:cy}=project(link.cx,link.cy,link.cz);
    const [a,b]=link.b===f?[link.b,link.a]:[link.a,link.b];
    const strength=(level===2?mix.focus:mix.hover)*reveal;
    ctx.globalCompositeOperation='lighter';ctx.strokeStyle=style.color;
    ctx.beginPath();ctx.moveTo(starX[a],starY[a]);ctx.quadraticCurveTo(cx,cy,starX[b],starY[b]);
    ctx.setLineDash([]);ctx.globalAlpha=.16*strength;ctx.lineWidth=style.width+6;ctx.stroke();
    ctx.setLineDash(style.dash);ctx.globalAlpha=.22+.7*strength;ctx.lineWidth=style.width+.4;ctx.stroke();
    if(level<2||still())return;
    // 光点沿曲线从聚焦星点流向相邻记忆
    const head=(now/2200+link.phase)%1;
    for(let k=0;k<8;k++){
      const u=head-k*.016;
      if(u<0)break;
      const v=1-u, x=v*v*starX[a]+2*v*u*cx+u*u*starX[b], y=v*v*starY[a]+2*v*u*cy+u*u*starY[b], size=k?9-k:15;
      ctx.globalAlpha=strength*(1-k/8);
      ctx.drawImage(spark,x-size/2,y-size/2,size,size);
    }
  }
  function drawStars(t,intro){
    const f=indexOf(focus), hv=indexOf(hovered), sel=indexOf(selected);
    const grow=camera.zoom**.3, density=clamp(1.35-scene.length/420,.55,1.25);
    ctx.globalCompositeOperation='lighter';
    for(const i of order){
      const m=scene[i], k=ignite(m,intro), light=.3+.7*accessibility(m,m.age);
      const focusDim=f>=0&&i!==f&&i!==hv&&!adjacency[f].has(i)?1-.72*mix.focus:1;
      const hoverDim=hv>=0&&i!==hv&&!adjacency[hv].has(i)?1-.35*mix.hover:1;
      starAlpha[i]=light*focusDim*hoverDim*k;
      starRadius[i]=3.4*starScale[i]*grow*(i===hv?1.4:i===f?1.3:1)*(.55+.45*k);
      if(starAlpha[i]<.01)continue;
      const flash=k<1?Math.sin(k*Math.PI)*.8:0, size=starRadius[i]*10*density*(.7+.3*light)*(1+flash);
      ctx.globalAlpha=Math.min(1,.78*starAlpha[i]*(.84+.16*Math.sin(t*m.speed+m.phase))*(1+flash));
      ctx.drawImage(halos.get(m.color),starX[i]-size/2,starY[i]-size/2,size,size);
      if(i===hv||i===f||i===sel){
        const spikes=starRadius[i]*(i===sel&&i!==hv&&i!==f?12:17);
        ctx.globalAlpha=Math.min(1,starAlpha[i]*.9);
        ctx.drawImage(flares.get(m.color),starX[i]-spikes/2,starY[i]-spikes/2,spikes,spikes);
      }
    }
    // 第二遍画星核：镜头对准的深度最清晰，越远离越接近失焦散景（景深）
    ctx.globalCompositeOperation='source-over';
    const depthOfField=(.9+1.1*mix.focus)*camera.zoom*.8;
    for(const i of order){
      if(starAlpha[i]<.01)continue;
      const m=scene[i], r=starRadius[i];
      const blur=clamp((Math.abs(starDepth[i])-.1)*depthOfField)*(i===hv||i===sel?.3:1);
      if(blur<.98){const s=r*2.7;ctx.globalAlpha=starAlpha[i]*(1-blur);ctx.drawImage((m.hollow?rings:cores).get(m.color),starX[i]-s/2,starY[i]-s/2,s,s);}
      if(blur>.02){const s=r*(2.8+3.4*blur);ctx.globalAlpha=starAlpha[i]*blur*.85;ctx.drawImage((m.hollow?blurRings:blurs).get(m.color),starX[i]-s/2,starY[i]-s/2,s,s);}
    }
  }
  function drawMarks({project,unit},w,h,now,intro){
    const sel=indexOf(selected), hv=indexOf(hovered), f=indexOf(focus);
    ctx.globalCompositeOperation='source-over';ctx.setLineDash([]);
    drawGroupLabels(project,unit,clamp(intro/900)*(1-.8*mix.focus)*(1-.6*mix.hover));
    if(sel>=0&&starAlpha[sel]>.05)drawReticle(sel,now);
    if(hv>=0&&hv!==sel){
      ctx.globalAlpha=.85*mix.hover;ctx.strokeStyle=scene[hv].color;ctx.lineWidth=1.2;
      ctx.beginPath();ctx.arc(starX[hv],starY[hv],starRadius[hv]+6,0,Math.PI*2);ctx.stroke();
    }
    if(f>=0&&f===sel)drawPlate(f,w,h,mix.focus);
    if(hv>=0&&hv!==f)drawPlate(hv,w,h,mix.hover);
  }
  function drawGroupLabels(project,unit,alpha){
    if(alpha<.02)return;
    ctx.textAlign='left';ctx.textBaseline='middle';ctx.lineJoin='round';ctx.lineWidth=3;ctx.strokeStyle='#040815';ctx.globalAlpha=alpha;
    for(const group of groups){
      const p=project(group.x,group.y,group.z), y=p.y-group.radius*unit*p.s*1.15-10, name=types[group.type], count=String(group.count);
      ctx.font=`600 12.5px ${font}`;
      const nameWidth=ctx.measureText(name).width;
      ctx.font=`12px ${font}`;
      const x=p.x-(nameWidth+7+ctx.measureText(count).width)/2;
      ctx.strokeText(count,x+nameWidth+7,y);ctx.fillStyle='#8ea4bb';ctx.fillText(count,x+nameWidth+7,y);
      ctx.font=`600 12.5px ${font}`;
      ctx.strokeText(name,x,y);ctx.fillStyle=colors[group.type];ctx.fillText(name,x,y);
    }
  }
  // 选中星点的观测准星：锁定时从外向内收拢，外圈三段弧缓慢旋转
  function drawReticle(i,now){
    const lock=still()?1:easeOut(clamp((now-lockAt)/520)), spin=still()?0:now/2800;
    const x=starX[i], y=starY[i], r=starRadius[i]+8+(1-lock)*24, alpha=lock*Math.min(1,starAlpha[i]*1.4);
    ctx.globalAlpha=.9*alpha;ctx.strokeStyle='#eef5ff';ctx.lineWidth=1.2;
    ctx.beginPath();ctx.arc(x,y,r,0,Math.PI*2);
    for(let k=0;k<4;k++){const cos=Math.cos(k*Math.PI/2),sin=Math.sin(k*Math.PI/2);ctx.moveTo(x+cos*(r+4),y+sin*(r+4));ctx.lineTo(x+cos*(r+10),y+sin*(r+10));}
    ctx.stroke();
    ctx.strokeStyle=scene[i].color;
    for(let k=0;k<3;k++){
      const a=spin+k*Math.PI*2/3;
      ctx.globalAlpha=(.8-k*.18)*alpha;ctx.beginPath();ctx.arc(x,y,r+16,a,a+1.2);ctx.stroke();
    }
  }
  function fit(text,max){
    const key=max+'\n'+text;
    if(!fitted.has(key)){
      ctx.font=`600 13px ${font}`;
      const chars=Array.from(text), whole=chars.length;
      while(chars.length>1&&ctx.measureText(chars.join('')+(chars.length<whole?'…':'')).width>max)chars.pop();
      fitted.set(key,chars.join('')+(chars.length<whole?'…':''));
    }
    return fitted.get(key);
  }
  // 悬停或聚焦星点的名牌：类型与状态、标题，自动避开画布边缘
  function drawPlate(i,w,h,strength){
    if(strength<.03)return;
    const m=scene[i], title=fit(m.title,Math.min(300,w-60)), meta=`${types[m.memory_type]} · ${statuses[m.status]}`;
    ctx.font=`600 13px ${font}`;
    const titleWidth=ctx.measureText(title).width;
    ctx.font=`11.5px ${font}`;
    const boxWidth=Math.max(titleWidth,ctx.measureText(meta).width)+26, boxHeight=48, gap=starRadius[i]+16;
    const below=starY[i]-gap-boxHeight<8;
    const x=clamp(starX[i]-boxWidth/2,8,Math.max(8,w-boxWidth-8)), y=below?Math.min(h-boxHeight-8,starY[i]+gap):starY[i]-gap-boxHeight;
    ctx.globalAlpha=strength;ctx.fillStyle='#070d1fe8';ctx.strokeStyle=m.color+'8c';ctx.lineWidth=1;
    ctx.beginPath();
    if(ctx.roundRect)ctx.roundRect(x,y,boxWidth,boxHeight,9);else ctx.rect(x,y,boxWidth,boxHeight);
    ctx.fill();ctx.stroke();
    ctx.beginPath();ctx.moveTo(starX[i],below?starY[i]+starRadius[i]+3:starY[i]-starRadius[i]-3);ctx.lineTo(clamp(starX[i],x+12,x+boxWidth-12),below?y:y+boxHeight);ctx.stroke();
    ctx.textAlign='left';ctx.textBaseline='alphabetic';
    ctx.fillStyle=m.color;ctx.fillText(meta,x+13,y+19);
    ctx.font=`600 13px ${font}`;ctx.fillStyle='#eef4ff';ctx.fillText(title,x+13,y+37);
  }
  function params(){return new URLSearchParams({...scope,query:$('obs-query').value,memory_type:$('obs-type').value,status:$('obs-status').value,include_sensitive:$('obs-sensitive').checked,limit:100,cursor,since:$('obs-since').value?$('obs-since').value+'T00:00:00Z':'',until:$('obs-until').value?$('obs-until').value+'T23:59:59.999999Z':''});}
  function cleanParams(){const p=params();for(const [k,v] of [...p])if(v==='')p.delete(k);return p;}
  async function load(){
    const serial=++requestId;const result=await api('/v1/observatory/snapshot?'+cleanParams());if(disposed||serial!==requestId)return;
    data=result;selected=data.memories.some(m=>m.id===selected)?selected:data.memories[0]?.id;hovered=undefined;
    layout();
    resetCamera(false);
    introAt=performance.now();
    $('obs-summary').textContent=`本页 ${data.count} 条真实记忆 · 待审核 ${data.memories.filter(m=>m.status==='pending_review').length} 条 · 敏感记忆${data.sensitive_hidden?'已隐藏':'已按管理员选择显示'} · 快照 ${data.as_of}`;
    $('obs-types').innerHTML=groups.map(g=>`<li data-type="${esc(g.type)}"><i class="obs-dot"></i>${esc(types[g.type])}<b>${g.count}</b></li>`).join('');
    $('obs-list').innerHTML=data.memories.length?data.memories.map(m=>`<button class="obs-memory" data-obs-id="${esc(m.id)}" data-type="${esc(m.memory_type)}"><span><i class="obs-dot"></i>${esc(types[m.memory_type])} · ${esc(statuses[m.status])} · ${esc(m.sensitivity)}</span><strong>${esc(m.title)}</strong><small>${esc(m.event_time_original||m.event_time||'事件时间未知')} · ${esc(m.id)}</small></button>`).join(''):'<p class="empty">没有符合当前授权与筛选的记忆。不会用演示数据替代。</p>';
    $('obs-list').querySelectorAll('[data-obs-id]').forEach(b=>{
      b.onclick=()=>select(b.dataset.obsId).catch(e=>notify(e.message,true));
      // 列表悬停或键盘聚焦时，星图同步点亮对应星点
      b.onmouseenter=b.onfocus=()=>setHover(b.dataset.obsId);
      b.onmouseleave=b.onblur=()=>setHover(undefined);
    });
    const timeline=[...data.memories].sort((a,b)=>a.timeline_at.localeCompare(b.timeline_at));
    $('obs-timeline').innerHTML=timeline.map(m=>`<article class="obs-event" data-type="${esc(m.memory_type)}"><time>${esc(m.event_time_original||m.timeline_at)}</time><p>${esc(m.title)}</p><small>${esc(m.time_basis==='event_time'?'来源事件时间':'按提炼时间排列；不是事件发生时间')} · ${esc(statuses[m.status])}</small></article>`).join('');
    for(const id of ['obs-types','obs-list','obs-timeline'])tint($(id));
    $('obs-prev').disabled=cursor===0;$('obs-next').disabled=data.next_cursor===null;
    schedule();if(selected)await select(selected,false);else $('obs-detail').textContent='当前页没有可显示的来源。';
  }
  async function select(id,fly=true){
    selected=id;lockAt=performance.now();
    $('obs-list').querySelectorAll('[data-obs-id]').forEach(b=>{if(b.dataset.obsId===id)b.setAttribute('aria-current','true');else b.removeAttribute('aria-current');});
    const target=scene[indexOf(id)];
    if(fly&&target){
      focus=id;
      flyTo({x:target.x,y:target.y,z:target.z,zoom:focusZoom()});
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
  function setHover(id,fromCanvas=false){
    if(fromCanvas&&overStar!==Boolean(id)){overStar=Boolean(id);canvas.classList.toggle('over-star',overStar);}
    if(id===hovered)return;
    hovered=id;schedule();
  }
  function hit(clientX,clientY,slack=16){
    const rect=canvas.getBoundingClientRect(), x=clientX-rect.left, y=clientY-rect.top;
    let best=-1, closest=slack;
    for(let i=0;i<scene.length;i++){
      if(starAlpha[i]<.05)continue;
      const distance=Math.hypot(starX[i]-x,starY[i]-y)-starRadius[i]*.5;
      if(distance<=closest){best=i;closest=distance;}
    }
    return best<0?undefined:scene[best].id;
  }
  function resetCamera(animated){
    focus=null;fitAt=fitZoom();
    const target={...overview,zoom:fitAt};
    if(animated)flyTo(target);
    else{flight=null;camera=target;schedule();}
  }
  function scale(f){flight=null;camera={...camera,zoom:clamp(camera.zoom*f,.55,4)};schedule();}
  function rotate(dx,dy){
    flight=null;
    camera={...camera,yaw:camera.yaw+dx,pitch:clamp(camera.pitch+dy,-1.5,1.5)};
    schedule();
  }
  const spread=()=>{const [[ax,ay],[bx,by]]=[...pointers.values()];return Math.hypot(ax-bx,ay-by);};
  function release(e){
    pointers.delete(e.pointerId);
    if(pointers.size<2)pinch=null;
    if(drag?.id===e.pointerId){drag=null;canvas.classList.remove('dragging');}
    schedule();
  }
  canvas.onwheel=e=>{e.preventDefault();scale(e.deltaY<0?1.1:1/1.1);};
  canvas.onpointerenter=e=>{if(e.pointerType!=='touch'){pointerInside=true;schedule();}};
  canvas.onpointerleave=()=>{pointerInside=false;setHover(undefined,true);schedule();};
  canvas.onpointerdown=e=>{
    if(e.button!==0)return;
    pointers.set(e.pointerId,[e.clientX,e.clientY]);
    // 捕获失败（指针已不活跃）只影响拖出画布后的跟踪，旋转和捏合照常进行
    try{canvas.setPointerCapture(e.pointerId);}catch(error){if(error.name!=='NotFoundError')throw error;}
    // 两指同时按下时切换为捏合缩放
    if(pointers.size===2){drag=null;pinch={distance:spread()||1,zoom:camera.zoom};}
    else if(pointers.size===1)drag={id:e.pointerId,x:e.clientX,y:e.clientY,moved:0};
    schedule();
  };
  canvas.onpointermove=e=>{
    if(pointers.has(e.pointerId))pointers.set(e.pointerId,[e.clientX,e.clientY]);
    if(pinch&&pointers.size===2){
      flight=null;camera={...camera,zoom:clamp(pinch.zoom*spread()/pinch.distance,.55,4)};schedule();
      return;
    }
    if(drag?.id===e.pointerId){
      const dx=e.clientX-drag.x,dy=e.clientY-drag.y;
      drag={...drag,moved:drag.moved+Math.abs(dx)+Math.abs(dy)};
      if(drag.moved>=5){
        canvas.classList.add('dragging');setHover(undefined,true);
        rotate(dx*.008,dy*.008);
        drag={...drag,x:e.clientX,y:e.clientY};
      }
      return;
    }
    if(e.pointerType!=='touch')setHover(hit(e.clientX,e.clientY),true);
  };
  canvas.onpointerup=e=>{
    if(drag?.id===e.pointerId&&drag.moved<5){
      const id=hit(e.clientX,e.clientY,e.pointerType==='touch'?24:16);
      if(id)select(id).catch(error=>notify(error.message,true));
    }
    release(e);
  };
  canvas.onpointercancel=canvas.onlostpointercapture=release;
  canvas.onkeydown=e=>{
    const directions={ArrowUp:[0,-.14],ArrowDown:[0,.14],ArrowLeft:[-.14,0],ArrowRight:[.14,0]};
    if(e.key==='Escape'){e.preventDefault();resetCamera(true);}
    else if(e.key==='+'||e.key==='-'||Object.hasOwn(directions,e.key)){
      e.preventDefault();
      if(e.key==='+')scale(1.2);
      else if(e.key==='-')scale(1/1.2);
      else rotate(...directions[e.key]);
    }
  };
  bind('obs-search','click',async()=>{cursor=0;await load();});bind('obs-refresh','click',load);
  bind('obs-prev','click',async()=>{cursor=Math.max(0,cursor-100);await load();});bind('obs-next','click',async()=>{cursor=data.next_cursor;await load();});
  bind('obs-days','input',()=>{simDays=Number($('obs-days').value)||0;updateProjection();});
  bind('obs-rehearse','click',()=>{if(selected){if(rehearsed.has(selected))rehearsed.delete(selected);else rehearsed.add(selected);updateProjection();}});
  bind('obs-reset','click',()=>{rehearsed.clear();$('obs-days').value=0;simDays=0;updateProjection();});
  bind('obs-center','click',()=>resetCamera(true));bind('obs-zoom-in','click',()=>scale(1.2));bind('obs-zoom-out','click',()=>scale(1/1.2));
  for(const view of ['star','time'])bind('obs-'+view,'click',()=>{
    chart.hidden=view!=='star';$('obs-timeline').hidden=view!=='time';
    $('obs-star').setAttribute('aria-pressed',String(view==='star'));$('obs-time').setAttribute('aria-pressed',String(view==='time'));
    schedule();
  });
  await load();
}
