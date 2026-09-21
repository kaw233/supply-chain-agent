/* Shared offline Markdown renderer. Marked 4.0.19 (MIT), HTML disabled,
   reconstructed allow-list DOM. No raw model HTML or data-* actions survive. */
window.ChatMarkdown = (() => {
  const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const cache = new Map();
  const tags = new Set('P BR STRONG EM DEL S BLOCKQUOTE UL OL LI H1 H2 H3 H4 H5 H6 HR PRE CODE TABLE THEAD TBODY TR TH TD A INPUT'.split(' '));
  const url = value => {
    const raw=String(value||'').replace(/[\u0000-\u0020\u007f]/g,'');
    try { const u=new URL(raw, location.href);return ['http:','https:','mailto:'].includes(u.protocol)?raw:null; } catch { return null; }
  };
  function render(value) {
    const text=String(value??'');
    if(cache.has(text))return cache.get(text);
    if(!window.marked || text.length>150000)return '<pre class="md-fallback">'+escape(text)+'</pre>';
    const renderer=new marked.Renderer();
    renderer.html=raw=>escape(raw);
    renderer.image=(href,title,alt)=>'<p>[图片：'+escape(alt||title||'附件')+']</p>';
    let raw;
    try { raw=marked.parse(text,{gfm:true,breaks:true,headerIds:false,mangle:false,renderer}); }
    catch { return '<pre class="md-fallback">'+escape(text)+'</pre>'; }
    const source=document.createElement('template');source.innerHTML=raw;
    const output=document.createElement('div');
    function copy(node,parent) {
      if(node.nodeType===3){parent.append(document.createTextNode(node.nodeValue));return;}
      if(node.nodeType!==1)return;
      if(!tags.has(node.tagName)){for(const child of node.childNodes)copy(child,parent);return;}
      const el=document.createElement(node.tagName.toLowerCase());
      if(node.tagName==='A'){
        const href=url(node.getAttribute('href'));
        if(href){el.setAttribute('href',href);el.setAttribute('target','_blank');el.setAttribute('rel','noopener noreferrer');}
      }
      if(node.tagName==='INPUT'){
        el.setAttribute('type','checkbox');el.setAttribute('disabled','');if(node.hasAttribute('checked'))el.setAttribute('checked','');
      }
      if(node.tagName==='OL' && /^\d{1,6}$/.test(node.getAttribute('start')||''))el.setAttribute('start',node.getAttribute('start'));
      if(node.tagName==='CODE' && /^language-[\w-]{1,40}$/.test(node.className))el.className=node.className;
      if(['TH','TD'].includes(node.tagName)&&['left','center','right'].includes(node.getAttribute('align')))el.setAttribute('align',node.getAttribute('align'));
      for(const child of node.childNodes)copy(child,el);
      parent.append(el);
    }
    for(const child of source.content.childNodes)copy(child,output);
    output.querySelectorAll('table').forEach(table=>{const wrap=document.createElement('div');wrap.className='md-table-wrap';wrap.tabIndex=0;wrap.setAttribute('aria-label','表格，可横向滚动');table.replaceWith(wrap);wrap.append(table);});
    output.querySelectorAll('pre').forEach(pre=>{
      const wrap=document.createElement('div');wrap.className='md-code';
      const code=pre.querySelector('code'),lang=code?.className.replace('language-','')||'text';
      const head=document.createElement('div');head.className='md-code-head';
      const label=document.createElement('span');label.textContent=lang;
      const button=document.createElement('button');button.type='button';button.className='md-copy';button.textContent='复制';button.setAttribute('aria-label','复制代码');
      head.append(label,button);pre.replaceWith(wrap);wrap.append(head,pre);
    });
    const result=output.innerHTML;
    if(text.length<60000){if(cache.size>=50)cache.delete(cache.keys().next().value);cache.set(text,result);}
    return result;
  }
  async function copyText(text) {
    if(navigator.clipboard?.writeText){try{await navigator.clipboard.writeText(text);return;}catch{}}
    const a=document.createElement('textarea');a.value=text;a.style.cssText='position:fixed;left:-9999px';document.body.append(a);a.select();const ok=document.execCommand('copy');a.remove();if(!ok)throw Error('请手动选择复制');
  }
  document.addEventListener('click',async e=>{
    const b=e.target.closest('.md-copy');if(!b)return;
    try{await copyText(b.closest('.md-code').querySelector('code,pre').textContent);b.textContent='已复制';}
    catch{b.textContent='请选择复制';}
    setTimeout(()=>{if(b.isConnected)b.textContent='复制';},1500);
  });
  return {render,copyText};
})();
