/* Fixed regions, persistent density/width, one scroll owner per content region.
   This module renders existing task facts; it never submits or resumes a run. */
window.AssistantPanel = (() => {
  let chatKey='', activityKey='', attentionKey='', follow=true;
  const q=s=>document.querySelector(s), qa=s=>[...document.querySelectorAll(s)];
  const saved=(k,d)=>{try{return localStorage.getItem('assistant.'+k)||d;}catch{return d;}};
  const save=(k,v)=>{try{localStorage.setItem('assistant.'+k,String(v));}catch{}};
  let tab='chat',density=Number(saved('density',90)),width=Number(saved('width',364));
  const expanded=new Set();
  const agentName=t=>t?.mode==='dsh'?'DSH':'Hermes';
  function clampWidth(w){return Math.min(Math.max(Number(w)||364,300),Math.max(300,Math.min(620,innerWidth-570)));}
  function size(w){width=clampWidth(w);q('#ai-pane').style.setProperty('--ai-width',width+'px');q('#ai-resize').setAttribute('aria-valuenow',Math.round(width));save('width',width);}
  function setDensity(d){density=[85,90,100].includes(Number(d))?Number(d):90;q('#ai-pane').dataset.density=density;save('density',density);q('#ai-density').value=String(density);}
  function setTab(value){tab=value==='activity'?'activity':'chat';q('#chat-scroll').hidden=tab!=='chat';q('#ai-activity').hidden=tab!=='activity';qa('[data-ai-tab]').forEach(b=>{b.classList.toggle('active',b.dataset.aiTab===tab);b.setAttribute('aria-selected',String(b.dataset.aiTab===tab));});q('#chat-newer').hidden=tab!=='chat'||follow;}
  function snapshotOpen(){qa('#ai-pane details[data-fold]').forEach(d=>d.open?expanded.add(d.dataset.fold):expanded.delete(d.dataset.fold));}
  function restoreOpen(){qa('#ai-pane details[data-fold]').forEach(d=>d.open=expanded.has(d.dataset.fold));}
  function context(){
    const t=state.task,a=state.settings?.agent,configured=a?.mode==='dsh'?a.dsh_model_url:a?.mode==='webui'?a.webui_url:a?.base_url;
    const label=a?.mode==='dsh'?'本地 DSH SDK':a?.mode==='hermes'?'Hermes API':a?.mode==='legacy'?'旧 API · YOLO兼容':'Hermes WebUI';
    q('#ai-mode').innerHTML=`<span class="mode-dot ${configured?'live':''}"></span><b>${e(label)}</b><span>${configured?'已配置':'待连接'}</span>`;
    const items=[['业务',state.data?.scenario.name||''],['页面',pages.find(x=>x[0]===state.page)?.[1]||''],['选中',state.selected.size+' 项'],['数据时间',dt(state.data?.latest_run?.created_at)]];
    const sig=JSON.stringify(items);
    if(q('#ai-context').dataset.signature!==sig){q('#ai-context').dataset.signature=sig;const was=q('#ai-context details')?.open;
      q('#ai-context').innerHTML=`<details class="context-details"><summary><span>⌖</span><b>${e(items[0][1])}</b><small>${state.selected.size} 项 · 上下文</small></summary><div class="context-fields">${items.map(([k,v])=>`<div><span>${e(k)}</span><b>${e(v)}</b></div>`).join('')}</div></details>`;
      q('#ai-context details').open=!!was;
    }
    q('#session-mark').textContent=state.session?'会话 '+state.session.slice(-6):'新对话';
  }
  function messageHTML(m,index){const text=m.body.text||m.body.reply||'';return `<article class="chat-message ${m.role==='user'?'user':'assistant'}" data-message="${e(String(m.id??index))}"><div class="message-label"><span>${m.role==='user'?'你':'✳ '+agentName(state.task)}</span><time>${dt(m.created_at)}</time><button class="message-copy" data-ai-copy="${index}" title="复制消息原文" aria-label="复制消息">复制</button></div><div class="message-text ${m.role==='user'?'':'md-content'}">${m.role==='user'?e(text):ChatMarkdown.render(text)}</div>${(m.body.outputs||[]).map(miniOutput).join('')}</article>`;}
  function render(force=false){
    const t=state.task,scroll=q('#chat-scroll');if(!scroll)return;
    snapshotOpen();
    const signature=JSON.stringify([state.messages,t?.id,!ended(t)?t?.partial:'']);
    if(signature!==chatKey||force){
      chatKey=signature;const top=scroll.scrollTop,atBottom=scroll.scrollHeight-scroll.scrollTop-scroll.clientHeight<60;
      const anchor=qa('#chat .chat-message').find(el=>el.getBoundingClientRect().bottom>scroll.getBoundingClientRect().top+2);
      const anchorKey=anchor?.dataset.message,anchorOffset=anchor?anchor.getBoundingClientRect().top-scroll.getBoundingClientRect().top:0;
      q('#chat').innerHTML=state.messages.length?state.messages.map(messageHTML).join(''):`<div class="chat-welcome"><span class="ai-symbol">✳</span><h3>有什么需要处理？</h3><p>查数据、解释风险，或准备业务动作。<br>需要 Agent 时会使用真实连接。</p><div class="chat-suggestions">${['分析当前风险与数据缺项','只看高风险对象','检查数据公式是否有重复计算'].map(x=>btn(e(x),'chat-prompt',`data-text="${e(x)}"`,'small')).join('')}</div></div>`;
      q('#agent-progress').innerHTML=!ended(t)&&t?.partial?`<article class="chat-message assistant streaming"><div class="message-label">✳ ${agentName(t)} <span class="stream-dot">●</span></div><div class="message-text md-content">${ChatMarkdown.render(t.partial)}</div></article>`:'';
      if(force||atBottom){scroll.scrollTop=scroll.scrollHeight;follow=true;q('#chat-newer').hidden=true;}else{
        const next=qa('#chat .chat-message').find(el=>el.dataset.message===anchorKey);
        if(next)scroll.scrollTop+=next.getBoundingClientRect().top-scroll.getBoundingClientRect().top-anchorOffset;else scroll.scrollTop=top;
        follow=false;q('#chat-newer').hidden=tab!=='chat';
      }
    }
    const activities=t?.activity||[];q('#activity-count').textContent=activities.length||'';
    const actSig=JSON.stringify([t?.id,activities,t?.error,t?.connection_state]);
    if(actSig!==activityKey){activityKey=actSig;const pos=q('#ai-activity').scrollTop;
      q('#ai-activity').innerHTML=t?`<div class="activity-caption">${e(t.id)}<br>仅展示执行端上报的活动，不重复执行工具。</div>${t.error?`<p class="ai-error">${e(t.error)}</p>`:''}${activities.length?activities.map((x,i)=>`<details class="activity-item" data-fold="${e(t.id)}-${i}"><summary><i class="activity-dot ${e(x.status)}">${x.status==='completed'?'✓':x.status==='failed'?'×':'↳'}</i><b>${e(x.name||'工具')}</b><small>${e(statusNames[x.status]||x.status||'')}</small></summary><div class="activity-content"><div class="activity-time">${dt(x.at)}</div><div class="md-content">${ChatMarkdown.render(x.preview||'暂无详细输出。')}</div></div></details>`).join(''):'<p class="ai-muted">当前任务还没有工具活动。</p>'}`:'<div class="ai-empty">尚无执行记录<br><small>真实工具活动会显示在这里</small></div>';
      q('#ai-activity').scrollTop=pos;
    }
    renderAttention(t);renderStatus(t);restoreOpen();
    q('#chat-send').disabled=state.busy||!ended(t);
    q('#chat-input').placeholder=!ended(t)?'本会话正在执行，可先写下下一条消息…':'询问业务，或描述你要执行的操作…';
    q('#composer-state').textContent=!ended(t)?'当前会话执行中 · 可先编辑':'Enter 发送 · Shift+Enter 换行';
    state.chatSignature=signature;
  }
  function renderAttention(t){
    const c=t?.card,valid=c&&!ended(t),key=JSON.stringify([t?.id,valid?c:null]);
    if(attentionKey===key)return;
    const oldInput=q('#card-answer'),value=oldInput?.value||'',focused=document.activeElement===oldInput,selection=oldInput?.selectionStart,old=q('#pending-card')?.dataset.key;
    attentionKey=key;q('#ai-attention').hidden=!valid;
    if(!valid){q('#ai-attention').innerHTML='';return;}
    q('#ai-attention').innerHTML=`<section class="pending-card" id="pending-card" data-key="${e(c.key)}"><div class="attention-heading"><span class="attention-icon">${c.kind==='approval'?'!':'?'}</span><b>${c.kind==='approval'?'需要工具批准':'需要补充信息'}</b><button class="icon-btn" type="button" data-ai-full-card title="展开完整请求" aria-label="展开完整请求">↗</button></div><div class="attention-title">${e(c.title)}</div><details class="attention-detail" data-fold="request-${e(c.key)}"><summary>查看操作内容与请求编号</summary><div class="attention-detail-scroll"><div class="md-content">${ChatMarkdown.render(c.detail||'无补充说明')}</div><small>请求 ${e(c.request_id||'无有效编号')} · ${e(t.id)}</small></div></details>${c.kind==='input'?'<textarea id="card-answer" aria-label="补充信息回答" placeholder="在这里补充本次缺少的信息…" rows="2"></textarea>':''}<div class="attention-actions">${c.actionable?(c.kind==='approval'?btn('允许这一次','reply-card',`data-id="${e(t.id)}" data-key="${e(c.key)}" data-choice="once"`,'primary small')+btn('拒绝','reply-card',`data-id="${e(t.id)}" data-key="${e(c.key)}" data-choice="deny"`,'small'):btn('提交回答','reply-card',`data-id="${e(t.id)}" data-key="${e(c.key)}" data-choice="once"`,'primary small')):'<small>原请求缺少有效编号，请在 WebUI 核对。</small>'}</div></section>`;
    if(old===c.key&&q('#card-answer')){q('#card-answer').value=value;if(focused){q('#card-answer').focus();q('#card-answer').setSelectionRange(selection,selection);}}
  }
  function renderStatus(t){
    q('#ai-runbar').hidden=!t;if(!t){q('#ai-runbar').innerHTML='';return;}
    const disconnected=t.connection_state==='disconnected';
    const phase=t.card?.kind==='approval'?'等待你的决定':t.card?.kind==='input'?'等待补充信息':t.phase||'';
    q('#ai-runbar').innerHTML=`<div class="runbar-main"><span class="run-indicator ${ended(t)?'ended':''}"></span><b>${e(disconnected?'连接中断 · 待核对':statusNames[t.status]||t.status)}</b><span class="run-phase" title="${e(phase)}">${e(phase)}</span>${!ended(t)?btn(t.stop_requested?'核对停止':'停止','stop-task',`data-id="${e(t.id)}"`,'small danger'):['failed','interrupted','cancelled'].includes(t.status)?btn('手动继续','continue-task',`data-id="${e(t.id)}"`,'small'):''}${btn('详情','task-detail',`data-id="${e(t.id)}"`,'small')}</div>${(t.outputs||[]).some(o=>o.type==='action_draft')?`<div class="runbar-outputs">${(t.outputs||[]).filter(o=>o.type==='action_draft').map(miniOutput).join('')}</div>`:''}`;
  }
  function init(){
    if(!q('#ai-pane'))return;size(width);setDensity(density);setTab('chat');
    q('#ai-density').addEventListener('change',ev=>setDensity(ev.target.value));
    q('#chat-scroll').addEventListener('scroll',()=>{const s=q('#chat-scroll');follow=s.scrollHeight-s.scrollTop-s.clientHeight<60;if(follow)q('#chat-newer').hidden=true;},{passive:true});
    const splitter=q('#ai-resize');
    splitter.addEventListener('pointerdown',ev=>{if(innerWidth<=900)return;const start=ev.clientX,initial=q('#ai-pane').getBoundingClientRect().width;splitter.setPointerCapture(ev.pointerId);document.body.classList.add('assistant-resizing');const move=e=>size(initial+start-e.clientX);const stop=()=>{document.body.classList.remove('assistant-resizing');splitter.removeEventListener('pointermove',move);splitter.removeEventListener('pointerup',stop);splitter.removeEventListener('pointercancel',stop);};splitter.addEventListener('pointermove',move);splitter.addEventListener('pointerup',stop);splitter.addEventListener('pointercancel',stop);});
    splitter.addEventListener('keydown',ev=>{if(['ArrowLeft','ArrowRight','Home'].includes(ev.key)){ev.preventDefault();size(ev.key==='Home'?364:width+(ev.key==='ArrowLeft'?20:-20));}});
    window.addEventListener('resize',()=>size(width));
    document.addEventListener('click',async ev=>{
      const el=ev.target.closest('[data-ai-tab],[data-ai-copy],[data-ai-full-card],[data-ai-focus],[data-ai-bottom],[data-ai-context]');if(!el)return;
      if(el.dataset.aiTab)setTab(el.dataset.aiTab);
      if(el.hasAttribute('data-ai-bottom')){q('#chat-scroll').scrollTop=q('#chat-scroll').scrollHeight;follow=true;el.hidden=true;}
      if(el.hasAttribute('data-ai-focus')){q('#ai-pane').classList.toggle('focus-mode');el.setAttribute('aria-pressed',String(q('#ai-pane').classList.contains('focus-mode')));el.title=q('#ai-pane').classList.contains('focus-mode')?'恢复侧栏宽度':'展开阅读';}
      if(el.dataset.aiCopy!==undefined){try{const m=state.messages[Number(el.dataset.aiCopy)];await ChatMarkdown.copyText(m.body.text||m.body.reply||'');el.textContent='已复制';}catch{toast('复制失败，请选择文本复制',true);}}
      if(el.hasAttribute('data-ai-full-card')){
        const t=state.task,c=t?.card;if(!c||ended(t))return;
        modal(c.kind==='approval'?'工具批准 · 完整请求':'补充信息 · 完整请求',`<p>${e(c.title)}</p><div class="md-content">${ChatMarkdown.render(c.detail||'')}</div><p class="hint">请求 ${e(c.request_id||'')} · 任务 ${e(t.id)}</p><p class="hint">请关闭详情后在右侧当前请求卡上操作；旧请求不会误批新请求。</p>`,btn('返回当前对话','close-modal'),'REQUEST / 执行端原始请求');
      }
    });
  }
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',init);else queueMicrotask(init);
  return {render,context,setTab,setDensity,size};
})();
