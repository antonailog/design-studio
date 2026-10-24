/* design-studio 页面模式的编辑器：在 iframe 里原样加载页面，点选元素改文字、调样式、写意见，保存时交给本机服务。
   被编辑的页面和编辑器同源（都由本机服务提供，或经 --proxy 转发），所以能直接操作 iframe 里的 DOM；页面原文件一个字不改。
   站点专属的东西都来自站点配置（GET /api/config）：样式面板的选项、元素名称和编号、板块名、主题切换方式、视口预设。 */
(function () {
  'use strict';
  const $ = id => document.getElementById(id);
  const API = new URL('api/', location.href).pathname;   // 接口挂在编辑器路径下（/_studio/api/），代理模式时不和被编辑应用自己的 /api/* 撞名
  const frame = $('frame'), frameBox = $('frameBox'), stage = $('stage');
  const S = { cfg: {}, pages: {}, page: '', w: 1440, h: 900, mode: 'select', doc: null, win: null,
              sel: null, hover: null, editing: null, items: new Map(), sheet: null, scrollY: 0 };

  /* ---------- 配置里的名单（没配就用默认或从页面里自动找） ---------- */
  const DEFAULT_VIEWPORTS = [{ w: 1440, h: 900, label: '桌面 1440' }, { w: 1280, h: 800, label: '笔记本 1280' }, { w: 390, h: 844, label: '手机 390' }];
  const DEFAULT_WEIGHTS = [['300', '细 300'], ['400', '常规 400'], ['500', '中 500'], ['600', '粗 600'], ['700', '粗 700']];
  const C = () => S.cfg || {};
  const ST = () => C().styles || {};

  // 设计 token 的值从被编辑页面自己读（它引用了站点的 tokens），页面还没载入时退回编辑器自己的
  const tok = n => {
    const d = (S.win && S.doc && S.doc.documentElement) ? S.win.getComputedStyle(S.doc.documentElement) : getComputedStyle(document.documentElement);
    return d.getPropertyValue(n).trim();
  };

  // 站点没在配置里列名单时，从页面样式表里找 :root 上的自定义属性，按名字前缀归组
  let discovered = null;
  function discover() {
    if (discovered || !S.doc) return discovered || {};
    const names = new Set();
    const visit = rules => {
      for (const r of rules) {
        try {
          if (r.cssRules && !r.selectorText) visit(r.cssRules);
          if (r.style && /(^|,)\s*(:root|html)(\s*,|\s*$|\[)/.test(r.selectorText || '')) {
            for (let i = 0; i < r.style.length; i++) if (r.style[i].startsWith('--')) names.add(r.style[i]);
          }
        } catch (e) { /* 跨域样式表读不了，跳过 */ }
      }
    };
    for (const sh of S.doc.styleSheets) { try { visit(sh.cssRules); } catch (e) { /* 跨域 */ } }
    const all = [...names];
    const pick = re => all.filter(n => re.test(n) && !n.slice(2).includes('--'));   // Tailwind 的 --text-xs--line-height 这类附属变量不算
    const probe = S.doc.createElement('i'); probe.style.display = 'none'; S.doc.body.appendChild(probe);
    const isColor = n => { probe.style.color = ''; probe.style.color = `var(${n})`; const v = tok(n); return !!v && /^(#|rgb|hsl|oklch|oklab|lab|lch|color\()/i.test(v); };
    discovered = {
      sizes: pick(/^--(fs|font-size|text)-/), spaces: pick(/^--(sp|space|spacing|gap)-/), radii: pick(/^--(r|radius|rounded)(-|$)/),
      line_heights: pick(/^--(lh|leading|line-height)-/), fonts: pick(/^--font-(?!size|weight)/).map(n => [n, n.replace('--font-', '字体 ')]),
      colors: all.filter(isColor),
    };
    probe.remove();
    return discovered;
  }
  const list = key => ST()[key] || discover()[key] || [];
  const colorNames = () => ST().color_names || {};

  // 每项是变量名（"--fs-16"）或直接写的值（"28px"、"1.85"，给规范只写在文档里、CSS 没有变量的站点用），也可以写成 [变量名或值, 名字]
  const scaleOpts = (names, zero) => (zero ? [{ v: '0', t: '0', label: '0' }] : []).concat(names.map(e => {
    const [n, name] = Array.isArray(e) ? e : [e, ''];
    const v = n.startsWith('--') ? tok(n) : n;
    const shown = v.replace(/^clamp\((.*)\)$/, '随屏宽变化');
    return { v, t: n, label: name ? `${name} · ${shown}` : n.startsWith('--') ? `${n.replace('--', '')} · ${shown}` : shown };
  }));
  const CONTROLS = () => [
    { prop: 'font-size', label: '字号', opts: scaleOpts(list('sizes')) },
    { prop: 'font-weight', label: '字重', opts: (ST().font_weights || DEFAULT_WEIGHTS).map(([v, l]) => ({ v, t: v, label: l })) },
    { prop: 'line-height', label: '行高', opts: scaleOpts(list('line_heights')) },
    { prop: 'font-family', label: '字体', opts: list('fonts').map(([n, l]) => ({ v: `var(${n})`, t: n, label: l })) },
    { prop: 'color', label: '文字颜色', colors: ST().text_colors || list('colors') },
    { prop: 'background-color', label: '底色', colors: ST().bg_colors || ['none'].concat(list('colors')) },
    { prop: 'text-align', label: '对齐', opts: [['left', '靠左'], ['center', '居中'], ['right', '靠右']].map(([v, l]) => ({ v, t: v, label: l })) },
    { prop: 'margin-top', label: '上方留白', opts: scaleOpts(list('spaces'), true) },
    { prop: 'margin-bottom', label: '下方留白', opts: scaleOpts(list('spaces'), true) },
    { prop: 'padding', label: '内边距', opts: scaleOpts(list('spaces'), true) },
    { prop: 'gap', label: '子元素间距', opts: scaleOpts(list('spaces'), true), when: cs => /flex|grid/.test(cs.display) },
    { prop: 'border-radius', label: '圆角', opts: scaleOpts(list('radii'), true) },
  ].filter(c => c.colors ? c.colors.length > 0 : c.opts.some(o => o.t !== '0'));   // 站点没有这类 token 的控件不显示

  /* ---------- 元素叫什么、在哪 ---------- */
  const INLINE = new Set(['B', 'STRONG', 'EM', 'I', 'SPAN', 'BR', 'MARK', 'SMALL', 'SUB', 'SUP']);
  let ignoreRe = /^(is-|active$)/;

  function sectionOf(el) {
    for (const x of C().section_extra || []) if (el.closest(x.sel)) return x.name;
    const sec = el.closest('section[id]');
    return sec ? ((C().sections || {})[sec.id] || sec.id) : '页面';
  }
  function knownOf(el) {
    for (const k of C().known || []) { const hit = el.closest(k.sel); if (hit) return { ...k, el: hit }; }
    return null;
  }
  function hintOf(el) {
    const k = knownOf(el);
    if (k && k.hint && k.el === el) return k.hint;
    for (const r of C().hint_rules || []) {
      const p = el.closest(r.selector);
      if (p && p === el) return r.hint.replace('{i}', [...p.parentNode.querySelectorAll(r.siblings)].indexOf(p));
    }
    return null;
  }
  function classOf(el) { return (el.getAttribute('class') || '').split(/\s+/).filter(c => c && !ignoreRe.test(c)); }
  function cssPath(el) {
    const parts = [];
    while (el && el.nodeType === 1 && el !== S.doc.body && el !== S.doc.documentElement) {
      if (el.id) { parts.unshift('#' + CSS.escape(el.id)); break; }
      let seg = el.tagName.toLowerCase();
      const c = classOf(el)[0];
      if (c) seg += '.' + CSS.escape(c);
      const parent = el.parentElement;
      if (parent && [...parent.children].filter(x => x.matches(seg)).length > 1) seg += `:nth-child(${[...parent.children].indexOf(el) + 1})`;
      parts.unshift(seg);
      el = parent;
    }
    return parts.join(' > ');
  }
  function snippet(el) { const t = (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim(); return t.length > 28 ? t.slice(0, 28) + '…' : t; }
  function describe(el) {
    const k = knownOf(el), sec = sectionOf(el);
    let label = sec;
    if (k) label += ` · ${k.num != null ? k.num + ' ' : ''}${k.name}${k.el === el ? '' : ' 里的一部分'}`;
    else { const t = snippet(el); if (t) label += ` · 「${t}」`; }
    const tag = el.tagName.toLowerCase(), cls = classOf(el);
    return { section: sec, label, num: k && k.num != null ? k.num : null, selector: cssPath(el), classes: cls.join(' '), tag, desc: tag + (cls.length ? '.' + cls.join('.') : '') };
  }
  function textual(el) {
    if (!(el.innerText || '').trim()) return false;
    return [...el.querySelectorAll('*')].every(x => INLINE.has(x.tagName));
  }
  function pickTarget(t) {
    if (!t || t.nodeType !== 1) return null;
    const svg = t.closest('svg');
    if (svg) t = svg.id ? svg : (svg.parentElement || svg);   // 图标归到外面的按钮；有 id 的整张图单独选
    // 只往上并「格式」标签：加粗、斜体、换行、没有 class 的 span，以及配置里列的格式类（如逐字动画、荧光笔）；有自己 class 的 span 单独选
    const fmtCls = C().inline_format_classes || [];
    const fmt = x => /^(B|STRONG|EM|I|BR|MARK|SMALL)$/.test(x.tagName) ||
      (x.tagName === 'SPAN' && (!x.getAttribute('class') || fmtCls.some(c => x.classList.contains(c))));
    while (t && fmt(t) && t.parentElement && t.parentElement !== S.doc.body && textual(t.parentElement)) t = t.parentElement;
    return t;
  }

  // 鼠标那一点上真正带文字的元素：卡片常用透明的链接层盖住整张卡，只看 e.target 会一直选到链接
  function pickAt(e) {
    let found = [];
    try { found = S.doc.elementsFromPoint(e.clientX, e.clientY); } catch (x) { /* */ }
    const hit = found.find(x => !x.closest('.__ed-box') && [...x.childNodes].some(n => n.nodeType === 3 && n.nodeValue.trim()));
    return pickTarget(hit || e.target);
  }

  /* ---------- 文字的原文和改后的样子 ---------- */
  const normText = el => (el.innerText || '').replace(/ /g, ' ').replace(/\s*\n\s*/g, '').trim();
  function cleanHtml(el) {
    const allowed = new Set((C().allowed_tags || ['b', 'strong', 'em']).map(t => t.toUpperCase()));
    let out = '';
    const walk = n => {
      for (const c of n.childNodes) {
        if (c.nodeType === 3) out += c.nodeValue.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
        else if (c.nodeType === 1) {
          const t = c.tagName;
          if ((t === 'B' || t === 'STRONG') && (allowed.has('B') || allowed.has('STRONG'))) { out += '<b>'; walk(c); out += '</b>'; }
          else if ((t === 'EM' || t === 'I') && allowed.has('EM')) { out += '<em>'; walk(c); out += '</em>'; }
          else if (t !== 'BR') walk(c);
        }
      }
    };
    walk(el);
    return out.replace(/ /g, ' ').replace(/\s*\n\s*/g, '').trim();
  }

  /* ---------- iframe 里的高亮框 ---------- */
  let boxH, boxS, raf = 0;
  function mountOverlay() {
    const d = S.doc;
    const mk = (cls) => { const b = d.createElement('div'); b.className = cls; d.documentElement.appendChild(b); return b; };
    const st = d.createElement('style');
    st.textContent = `.__ed-box{position:fixed;pointer-events:none;z-index:2147483646;border-radius:4px;display:none}
      .__ed-hover{outline:1.5px dashed #d62839;outline-offset:2px}
      .__ed-sel{outline:2px solid #d62839;outline-offset:2px;box-shadow:0 0 0 9999px rgba(0,0,0,.04)}
      .__ed-tag{position:absolute;left:-2px;top:-24px;font:500 11px/18px ui-monospace,Menlo,monospace;color:#fff;background:#d62839;padding:0 6px;border-radius:3px;white-space:nowrap}
      [contenteditable="true"]{outline:2px solid #d62839!important;outline-offset:3px;cursor:text!important;caret-color:#d62839}
      html.__ed-select a, html.__ed-select button{cursor:default!important}`;
    d.head.appendChild(st);
    boxH = mk('__ed-box __ed-hover');
    boxS = mk('__ed-box __ed-sel');
    const tag = d.createElement('span'); tag.className = '__ed-tag'; boxS.appendChild(tag);
    cancelAnimationFrame(raf);
    const place = (box, el) => {
      if (!el || !el.isConnected || S.mode !== 'select') { box.style.display = 'none'; return; }
      const r = el.getBoundingClientRect();
      Object.assign(box.style, { display: 'block', left: r.left + 'px', top: r.top + 'px', width: r.width + 'px', height: r.height + 'px' });
    };
    const loop = () => { place(boxH, S.hover !== S.sel ? S.hover : null); place(boxS, S.editing ? null : S.sel); raf = requestAnimationFrame(loop); };
    loop();
  }

  /* ---------- iframe 加载 ---------- */
  function bindFrame() {
    S.win = frame.contentWindow;
    try { S.doc = frame.contentDocument; } catch (e) { S.doc = null; }
    if (!S.doc || !S.doc.body) { toast('读不到页面内容：页面要和编辑器同源（同一个本机服务，或用 --proxy 转发）。', 6000); return; }
    discovered = null;
    S.doc.documentElement.classList.toggle('__ed-select', S.mode === 'select');
    mountOverlay();
    const block = e => {
      if (S.mode !== 'select') return false;
      if (S.editing && S.editing.contains(e.target)) return false;
      e.preventDefault(); e.stopImmediatePropagation();
      return true;
    };
    ['pointerdown', 'mousedown', 'mouseup', 'pointerup', 'auxclick', 'submit'].forEach(t => S.win.addEventListener(t, block, true));
    S.win.addEventListener('click', e => { if (block(e)) { if (S.editing) endEdit(); select(pickAt(e)); } }, true);
    S.win.addEventListener('dblclick', e => {
      if (!block(e)) return;
      const el = pickAt(e);
      select(el);
      if (el && textual(el)) {
        startEdit(el);
        const r = S.doc.caretRangeFromPoint && S.doc.caretRangeFromPoint(e.clientX, e.clientY);   // 光标放在双击的位置
        if (r && el.contains(r.startContainer)) { const sel = S.win.getSelection(); sel.removeAllRanges(); sel.addRange(r); }
      }
    }, true);
    let mm = 0;
    S.win.addEventListener('mousemove', e => {
      if (S.mode !== 'select' || mm) return;
      mm = requestAnimationFrame(() => { mm = 0; S.hover = pickAt(e); });
    }, true);
    S.win.addEventListener('mouseout', e => { if (!e.relatedTarget) S.hover = null; }, true);
    // 编辑文字时不让页面自己的快捷键收到按键
    S.win.addEventListener('keydown', e => {
      if (!S.editing || !S.editing.contains(e.target)) return;
      e.stopImmediatePropagation();
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); endEdit(); }
      if (e.key === 'Escape') { e.preventDefault(); endEdit(true); }
    }, true);
    S.win.addEventListener('paste', e => {
      if (!S.editing) return;
      e.preventDefault();
      S.doc.execCommand('insertText', false, (e.clipboardData || S.win.clipboardData).getData('text/plain').replace(/\s*\n\s*/g, ''));
    }, true);
    applyTheme();
    // 还没落地的样式改动（本轮 + 修改单里待 AI 的）在预览里重新套上
    setTimeout(() => {
      for (const it of S.items.values()) if (it.type === '样式') applyCss(it.css.selector, it.css.prop, it.css.value);
      for (const it of (S.sheet && S.sheet.items) || []) if (it.type === '样式' && it.status === '待 AI' && it.css) applyCss(it.css.selector, it.css.prop, it.css.value);
      if (S.scrollY) S.win.scrollTo(0, S.scrollY);
    }, 400);
  }
  function applyCss(selector, prop, value) {
    let el = null;
    try { el = S.doc.querySelector(selector); } catch (e) { /* 选择器失效 */ }
    if (el) value == null ? el.style.removeProperty(prop) : el.style.setProperty(prop, value, 'important');
  }
  function loadPage() {
    const p = S.pages[S.page];
    if (!p) return;
    S.sel = S.hover = S.editing = null;
    renderCur();
    frame.src = p.url;
  }
  frame.addEventListener('load', bindFrame);

  /* ---------- 视口 ---------- */
  function layout() {
    const availW = stage.clientWidth - 32, availH = stage.clientHeight - 32;
    const s = Math.min(1, availW / S.w);   // 页面按真实宽度渲染，放不下就整体缩小显示
    Object.assign(frame.style, { width: S.w + 'px', height: availH / s + 'px', transform: `scale(${s})` });
    frameBox.style.width = S.w * s + 'px';
    frameBox.style.height = availH + 'px';
  }
  new ResizeObserver(layout).observe(stage);

  /* ---------- 选中 ---------- */
  function select(el) {
    if (!el) return;
    S.sel = el;
    if (boxS) boxS.querySelector('.__ed-tag').textContent = describe(el).label;
    renderCur();
    showTab('cur');
  }
  function renderCur() {
    const el = S.sel;
    $('hint').hidden = !!el; $('sel').hidden = !el;
    if (!el) return;
    const d = describe(el);
    $('selLabel').textContent = d.label;
    $('selDesc').textContent = d.desc;
    const tx = textual(el);
    $('textGroup').hidden = false;
    $('editText').disabled = !tx;
    const key = 'text|' + d.selector;
    $('textNote').textContent = S.items.has(key) ? '这段文字本轮改过了，保存后生效。'
      : tx ? '双击页面上的这段文字，或点下面的按钮，直接在页面上改；回车结束，Esc 放弃。'
      : '这里面不只是文字。选更里面的一段文字再改，或者写成意见。';
    renderControls(el, d);
  }
  function renderControls(el, d) {
    const box = $('controls'); box.innerHTML = '';
    const cs = S.win.getComputedStyle(el);
    for (const c of CONTROLS()) {
      if (c.when && !c.when(cs)) continue;
      const key = `style|${d.selector}|${c.prop}`;
      const pending = S.items.get(key);
      const row = document.createElement('div');
      row.className = 'ctl' + (pending ? ' changed' : '');
      row.innerHTML = `<span>${c.label}</span>`;
      const cur = pending ? pending.from : currentValue(el, c.prop);
      if (c.colors) {
        const sws = document.createElement('div'); sws.className = 'swatches';
        for (const n of c.colors) {
          const b = document.createElement('button');
          b.type = 'button'; b.className = 'sw' + (n === 'none' ? ' none' : '');
          if (n !== 'none') b.style.background = tok(n);
          b.title = n === 'none' ? '透明' : `${colorNames()[n] || ''} ${n}`.trim();
          b.setAttribute('aria-pressed', String(pending ? pending.token === n : matchColor(el, c.prop, n)));
          b.onclick = () => setStyle(el, d, c, n === 'none' ? 'transparent' : `var(${n})`, n, cur);
          sws.appendChild(b);
        }
        row.appendChild(sws);
      } else {
        const s = document.createElement('select');
        const hit = pending ? null : c.opts.find(o => o.v === cur || (c.prop === 'font-family' && cs.fontFamily === fontOf(o.t)));
        s.innerHTML = `<option value="">${esc(pending ? '原来：' + pending.from : '现在：' + (hit ? hit.label : cur + '（规范外）'))}</option>` +
          c.opts.map(o => `<option value="${esc(o.t)}" ${pending && pending.token === o.t ? 'selected' : ''}>${esc(o.label)}</option>`).join('');
        s.onchange = () => { const o = c.opts.find(x => x.t === s.value); if (o) setStyle(el, d, c, o.v, o.t, cur); else resetStyle(el, d, c); };
        row.appendChild(s);
      }
      const r = document.createElement('button');
      r.type = 'button'; r.className = 'reset'; r.textContent = '恢复';
      r.onclick = () => resetStyle(el, d, c);
      row.appendChild(r);
      box.appendChild(row);
    }
  }
  function currentValue(el, prop) {
    const v = rawValue(el, prop);
    return v.split(' ').every(x => x === '0px') ? '0' : v;
  }
  function rawValue(el, prop) {
    const cs = S.win.getComputedStyle(el);
    if (prop === 'padding') return [cs.paddingTop, cs.paddingRight, cs.paddingBottom, cs.paddingLeft].every(v => v === cs.paddingTop) ? cs.paddingTop : `${cs.paddingTop} ${cs.paddingRight} ${cs.paddingBottom} ${cs.paddingLeft}`;
    if (prop === 'border-radius') return cs.borderTopLeftRadius;
    if (prop === 'gap') return cs.rowGap === cs.columnGap ? cs.rowGap : `${cs.rowGap} ${cs.columnGap}`;
    if (prop === 'line-height') { const lh = parseFloat(cs.lineHeight), fs = parseFloat(cs.fontSize); return isNaN(lh) ? cs.lineHeight : String(Math.round(lh / fs * 100) / 100); }
    const v = cs.getPropertyValue(prop);
    if (prop === 'text-align') return { start: 'left', end: 'right', '-webkit-left': 'left', '-webkit-center': 'center' }[v] || v;
    return v;
  }
  function fontOf(n) {   // 浏览器会改写字体名（BlinkMacSystemFont → system-ui），按算出来的值比，不比变量原文
    const probe = S.doc.createElement('i'); probe.style.cssText = `display:none;font-family:var(${n})`;
    S.doc.body.appendChild(probe);
    const v = S.win.getComputedStyle(probe).fontFamily; probe.remove();
    return v;
  }
  function matchColor(el, prop, n) {
    if (n === 'none') return S.win.getComputedStyle(el).getPropertyValue(prop) === 'rgba(0, 0, 0, 0)';
    const probe = S.doc.createElement('i'); probe.style.cssText = `display:none;color:var(${n})`;
    S.doc.body.appendChild(probe);
    const v = S.win.getComputedStyle(probe).color; probe.remove();
    return S.win.getComputedStyle(el).getPropertyValue(prop) === v;
  }
  function setStyle(el, d, c, value, token, from) {
    const key = `style|${d.selector}|${c.prop}`;
    const prev = S.items.get(key);
    el.style.setProperty(c.prop, value, 'important');
    S.items.set(key, { type: '样式', key, where: pick(d), prop: c.prop, label: c.label, from: prev ? prev.from : from, to: token.startsWith('--') ? `${token}（${tok(token)}）` : value, token, css: { selector: d.selector, prop: c.prop, value } });
    changed(); renderControls(el, d);
  }
  function resetStyle(el, d, c) {
    const key = `style|${d.selector}|${c.prop}`;
    el.style.removeProperty(c.prop);
    S.items.delete(key);
    changed(); renderControls(el, d);
  }
  const pick = d => ({ section: d.section, label: d.label, num: d.num, selector: d.selector, classes: d.classes, tag: d.tag });

  /* ---------- 改文字 ---------- */
  function startEdit(el) {
    if (S.editing) endEdit();
    const d = describe(el), key = 'text|' + d.selector;
    if (!el.__edOrig) el.__edOrig = S.items.has(key) ? { text: S.items.get(key).oldText, html: S.items.get(key).oldHtml } : { text: normText(el), html: cleanHtml(el) };
    el.__edBefore = el.innerHTML;
    el.__edDesc = el.__edDesc || d;   // 位置和名字按改之前的文字记
    el.setAttribute('contenteditable', 'true');
    el.focus();
    S.editing = el;
    $('textNote').textContent = '正在页面上编辑：回车结束，Esc 放弃这次修改。';
  }
  function endEdit(cancel) {
    const el = S.editing;
    if (!el) return;
    S.editing = null;
    el.removeAttribute('contenteditable');
    if (cancel) el.innerHTML = el.__edBefore;
    const d = el.__edDesc || describe(el), key = 'text|' + d.selector;
    const o = el.__edOrig, newText = normText(el), newHtml = cleanHtml(el);
    if (newText === o.text && newHtml === o.html) S.items.delete(key);
    else S.items.set(key, { type: '文字', key, where: pick(d), hint: hintOf(el), oldText: o.text, oldHtml: o.html, newText, newHtml });
    changed(); renderCur();
  }
  $('editText').onclick = () => { if (S.sel && textual(S.sel)) startEdit(S.sel); };

  /* ---------- 意见 ---------- */
  $('addOpinion').onclick = () => {
    const t = $('opinion').value.trim();
    if (!t || !S.sel) return;
    const d = describe(S.sel), key = 'note|' + Date.now();
    S.items.set(key, { type: '意见', key, where: pick(d), text: t });
    $('opinion').value = '';
    changed(); toast('记下了，保存后进修改单。');
  };

  /* ---------- 本轮清单、修改单 ---------- */
  function changed() {
    const n = S.items.size;
    $('count').textContent = n; $('todoN').textContent = n;
    $('save').disabled = !n;
    renderTodo();
  }
  const esc = s => String(s == null ? '' : s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  function itemBody(it) {
    if (it.type === '文字') return `<div class="diff"><del>${esc(it.old != null ? it.old : it.oldText)}</del><ins>${esc(it.new != null ? it.new : it.newText)}</ins></div>`;
    if (it.type === '样式') return `<div class="diff"><span>${esc(it.label || it.prop)}：</span><del>${esc(it.from || it.old)}</del><ins>${esc(it.to || it.new)}</ins></div>`;
    return `<div class="diff"><ins>${esc(it.text)}</ins></div>`;
  }
  function renderTodo() {
    const box = $('todo');
    if (!S.items.size) { box.innerHTML = '<p class="empty">这一轮还没有改动。在「当前元素」里改文字、调样式、写意见，都会出现在这里。</p>'; return; }
    box.innerHTML = '<p class="sum">还没保存。点顶栏的「保存」写进文件夹。</p><div class="list">' + [...S.items.values()].map(it => `
      <div class="item" data-key="${esc(it.key)}">
        <div class="item-top"><span class="kind">${it.type}</span><span class="where">${esc(it.where.label)}</span></div>
        ${itemBody(it)}
        <div class="item-act"><button type="button" class="btn sm" data-act="go">定位</button><button type="button" class="btn sm" data-act="del">撤销</button></div>
      </div>`).join('') + '</div>';
  }
  $('todo').addEventListener('click', e => {
    const b = e.target.closest('button[data-act]'); if (!b) return;
    const key = b.closest('.item').dataset.key, it = S.items.get(key); if (!it) return;
    let el = null; try { el = S.doc.querySelector(it.where.selector); } catch (x) { /* */ }
    if (b.dataset.act === 'go') { if (el) { el.scrollIntoView({ block: 'center' }); select(el); } return; }
    if (it.type === '样式') applyCss(it.css.selector, it.css.prop, null);
    if (it.type === '文字' && el) { toast('文字撤销后刷新页面就恢复原样。'); }
    S.items.delete(key); changed(); if (S.sel) renderCur();
  });
  const CHIP = { '已写入': 'done', 'AI 已处理': 'done', '待 AI': 'wait', '已写入，待 AI 跟进': 'wait', '待你确认': 'wait', '没写入': 'bad', '不改': '' };
  const waitingOf = items => items.filter(x => /待 AI/.test(x.status || ''));
  const baseName = p => String(p || '').split('/').pop();
  function renderSheet() {
    const box = $('sheet'), items = ((S.sheet && S.sheet.items) || []).slice().reverse();
    const pg = S.pages[S.page] || {};
    $('sheetN').textContent = items.length;
    const waiting = waitingOf(items).length;
    const aiRow = `<div class="ai-row"><button type="button" class="btn sm" id="copyAi" ${waiting ? '' : 'disabled'}>复制给 AI 的指令</button>
      <span class="meta">${waiting ? `把这句话发给 Claude Code 或 Codex，它会读修改单、逐条落地、写回复。` : '没有等 AI 处理的条目。'}</span></div>`;
    if (!items.length) { box.innerHTML = aiRow + '<p class="empty">修改单还是空的。保存过的改动和意见都记在这里，AI 处理完会写上回复。</p>'; return; }
    box.innerHTML = aiRow + `<p class="sum">文件：${esc(pg.sheet || '')}。等 AI 处理的 ${waiting} 条。</p><div class="list">` + items.map(it => `
      <div class="item">
        <div class="item-top"><span class="kind">${it.type}</span><span class="where">${esc(it.where && it.where.label)}</span><span class="chip ${CHIP[it.status] || ''}">${esc(it.status)}</span></div>
        ${itemBody(it)}
        ${it.field ? `<p class="meta">写进了 ${esc(baseName(pg.data) || '数据文件')} 的 ${esc(it.field)}</p>` : ''}
        ${it.file && !it.field ? `<p class="meta">写进了 ${esc(it.file)}</p>` : ''}
        ${it.follow ? `<p class="meta">还要跟进：${esc(it.follow)}</p>` : ''}
        ${it.note ? `<p class="meta">${esc(it.note)}</p>` : ''}
        ${it.reply ? `<p class="reply">${esc(it.reply)}</p>` : ''}
        <p class="meta">${esc(it.id)} · ${esc(it.time)}</p>
      </div>`).join('') + '</div>';
  }
  const AI_PROMPT = '请处理 design-studio 的修改单：{sheet_abs}。\n只处理 status 是「待 AI」或「已写入，待 AI 跟进」的条目（共 {count} 条）：按 where 里的板块、元素名和 selector 在页面 {url} 上找到元素；文字改到它的源文件，样式改 CSS 并且只用设计规范里的 token，意见按描述改，「已写入，待 AI 跟进」按 follow 补做连带的修改。\n每条改完把 status 改成「AI 已处理」（决定不改就写「不改」并说明原因），在 reply 里写一句做了什么。{guide_line}';
  function aiPrompt() {
    const pg = S.pages[S.page] || {};
    const waiting = waitingOf((S.sheet && S.sheet.items) || []).length;
    const tpl = C().ai_prompt || AI_PROMPT;
    return tpl.replace(/\{sheet_abs\}/g, pg.sheet_abs || pg.sheet || '').replace(/\{sheet\}/g, pg.sheet || '')
      .replace(/\{count\}/g, waiting).replace(/\{url\}/g, pg.url || '').replace(/\{page\}/g, S.page)
      .replace(/\{guide\}/g, pg.guide_abs || pg.guide || '')
      .replace(/\{guide_line\}/g, pg.guide ? `\n动手前先读这个页面的修改说明：${pg.guide_abs || pg.guide}。` : '');
  }
  async function copyText(t) {
    try { await navigator.clipboard.writeText(t); return true; } catch (e) { /* 退回老办法 */ }
    const ta = document.createElement('textarea'); ta.value = t; ta.style.position = 'fixed'; ta.style.opacity = '0';
    document.body.appendChild(ta); ta.select();
    let ok = false; try { ok = document.execCommand('copy'); } catch (e) { /* */ }
    ta.remove(); return ok;
  }
  $('sheet').addEventListener('click', async e => {
    if (!e.target.closest('#copyAi')) return;
    const ok = await copyText(aiPrompt());
    toast(ok ? '已复制。发给 Claude Code 或 Codex，它处理完回到这里刷新就能看到回复。' : '复制失败，浏览器不让写剪贴板。', 5000);
  });
  async function loadSheet() {
    try { S.sheet = await (await fetch(API + 'sheet?page=' + encodeURIComponent(S.page), { cache: 'no-store' })).json(); }
    catch (e) { S.sheet = null; }
    renderSheet();
  }

  /* ---------- 保存 ---------- */
  $('save').onclick = async () => {
    if (S.editing) endEdit();
    if (!S.items.size) return;
    $('save').disabled = true;
    try {
      const r = await fetch(API + 'save', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ page: S.page, items: [...S.items.values()] }) });
      const j = await r.json();
      if (!r.ok || !j.ok) throw new Error(j.error || r.status);
      const c = s => j.saved.filter(x => x.status === s).length;
      const done = c('已写入') + c('已写入，待 AI 跟进'), wait = j.saved.filter(x => x.status === '待 AI').length, bad = c('没写入');
      toast(`已保存。文字直接写回 ${done} 条；${wait} 条进了修改单等 AI${bad ? `；${bad} 条没写入（看修改单里的原因）` : ''}。${j.warnings.length ? ' ' + j.warnings.join(' ') : ''}`, 6000);
      // 文字已写回的，刷新后就是新值；样式待 AI 的，刷新后从修改单里重新套上
      S.items.clear(); changed();
      S.scrollY = S.win ? S.win.scrollY : 0;
      await loadSheet(); showTab('sheet');
      frame.contentWindow.location.reload();
    } catch (e) {
      toast('保存失败：' + e.message + '。改动还在，修好再点保存。', 6000);
      $('save').disabled = false;
    }
  };
  window.addEventListener('beforeunload', e => { if (S.items.size) { e.preventDefault(); e.returnValue = ''; } });

  /* ---------- 顶栏控件 ---------- */
  function renderViewports() {
    const vps = C().viewports || DEFAULT_VIEWPORTS;
    $('vp').innerHTML = vps.map((v, i) => `<button type="button" data-w="${+v.w}" data-h="${+v.h}" aria-pressed="${i === 0}">${esc(v.label || v.w)}</button>`).join('');
    S.w = +vps[0].w; S.h = +vps[0].h;
  }
  $('vp').addEventListener('click', e => {
    const b = e.target.closest('button'); if (!b) return;
    $('vp').querySelectorAll('button').forEach(x => x.setAttribute('aria-pressed', String(x === b)));
    S.w = +b.dataset.w; S.h = +b.dataset.h; layout();
  });
  $('mode').addEventListener('click', e => {
    const b = e.target.closest('button'); if (!b) return;
    if (S.editing) endEdit();
    $('mode').querySelectorAll('button').forEach(x => x.setAttribute('aria-pressed', String(x === b)));
    S.mode = b.dataset.mode;
    if (S.doc) S.doc.documentElement.classList.toggle('__ed-select', S.mode === 'select');
  });
  // 主题：按站点配置的 localStorage 键和 html 属性切换（编辑器和页面同源，共用同一份存储）
  const TH = () => C().theme;
  function themeNow() { const th = TH(); let t = th.light || 'light'; try { t = localStorage.getItem(th.storage_key) || t; } catch (e) { /* */ } return t; }
  function applyTheme() {
    const th = TH(); if (!th) return;
    const t = themeNow();
    if (S.doc) S.doc.documentElement.setAttribute(th.attr || 'data-theme', t);
    $('theme').classList.toggle('dark', t === (th.dark || 'dark'));
  }
  $('theme').onclick = () => {
    const th = TH(); if (!th) return;
    const t = themeNow() === (th.dark || 'dark') ? (th.light || 'light') : (th.dark || 'dark');
    try { localStorage.setItem(th.storage_key, t); } catch (e) { /* */ }
    applyTheme();
    if (S.sel) renderCur();
  };
  $('up').onclick = () => { if (S.sel && S.sel.parentElement && S.sel.parentElement !== S.doc.body) select(S.sel.parentElement); };
  $('locate').onclick = () => { if (S.sel) S.sel.scrollIntoView({ block: 'center' }); };
  function showTab(name) {
    document.querySelectorAll('.tabs button').forEach(b => b.setAttribute('aria-selected', String(b.dataset.tab === name)));
    document.querySelectorAll('.tab-body').forEach(s => { s.hidden = s.dataset.body !== name; });
  }
  document.querySelector('.tabs').addEventListener('click', e => { const b = e.target.closest('button'); if (b) showTab(b.dataset.tab); });
  $('page').onchange = () => {
    if (S.items.size && !confirm('这一轮还有没保存的改动，换页面会丢掉。继续？')) { $('page').value = S.page; return; }
    S.items.clear(); changed(); S.page = $('page').value; loadSheet(); loadPage();
  };
  let tt = 0;
  function toast(msg, ms = 2600) { const t = $('toast'); t.textContent = msg; t.classList.add('on'); clearTimeout(tt); tt = setTimeout(() => t.classList.remove('on'), ms); }

  /* ---------- 启动 ---------- */
  (async () => {
    try {
      const [cfg, pages] = await Promise.all([
        fetch(API + 'config', { cache: 'no-store' }).then(r => r.json()),
        fetch(API + 'pages', { cache: 'no-store' }).then(r => r.json()),
      ]);
      S.cfg = cfg || {}; S.pages = pages || {};
      if (S.cfg.ignore_classes) { try { ignoreRe = new RegExp(S.cfg.ignore_classes); } catch (e) { /* 配置写错了，用默认 */ } }
      document.title = S.cfg.title || 'design-studio';
      $('brand').textContent = S.cfg.title || 'design-studio';
      $('theme').hidden = !S.cfg.theme;
      const names = Object.keys(S.pages);
      S.page = names[0] || '';
      $('page').innerHTML = names.map(n => `<option>${esc(n)}</option>`).join('');
      $('status').textContent = '已连上本机服务';
    } catch (e) {
      $('status').textContent = '没连上本机服务：先运行 python3 design_studio.py serve <站点配置.json>';
      $('status').classList.add('bad');
      return;
    }
    renderViewports(); changed(); layout(); await loadSheet(); loadPage();
  })();
})();
