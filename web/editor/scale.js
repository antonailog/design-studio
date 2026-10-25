/* design-studio 字号收敛页：量出页面上每段文字的计算字号，按方案把它们并到几档，在 iframe 里当场预览。
   只往 iframe 里带文字的元素加行内 font-size（!important），按住「看原样」或切到「现在」时全部去掉；页面原文件不改。
   站点专属的东西来自站点配置的 scale 字段：审哪些页面（pages）、哪些字不动（keep / keep_from）、哪些锁定（lock）、方案（presets）、存到哪（feedback）。 */
(function () {
  'use strict';
  const $ = id => document.getElementById(id);
  const API = new URL('api/', location.href).pathname;
  const frame = $('frame'), frameBox = $('frameBox'), stage = $('stage'), tip = $('tip');
  const DEFAULT_VIEWPORTS = [{ w: 1440, h: 900, label: '桌面 1440' }, { w: 1280, h: 800, label: '笔记本 1280' }, { w: 390, h: 844, label: '手机 390' }];
  const KEY = 'ds-scale:' + location.pathname;
  const half = v => Math.round(v * 2) / 2;
  const esc = s => String(s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[c]);

  let cfg = {}, SC = { pages: [], keep: '', keep_from: 34, lock: [], presets: [], feedback: '字号收敛' };
  let presets = [];            // 配置里的方案；没配时载入页面后按现有刻度自动生成一套
  let nowScale = [];           // 页面现有的字号刻度（从配置的 sizes 或页面 :root 变量读出来的像素值）
  let recs = new Map(), holding = false, mo = null, saveTimer = 0;
  let st = { page: 0, vp: 'fit', preset: -1, steps: [], over: {}, edited: false, note: '' };   // preset：-1 = 现在，其余是 presets 下标

  const doc = () => { try { return frame.contentDocument; } catch (e) { return null; } };
  const win = () => { try { return frame.contentWindow; } catch (e) { return null; } };
  const P = () => presets[st.preset] || null;

  /* ---------- 页面现有的字号刻度 ---------- */
  function sizeNames(d) {
    const sizes = (cfg.styles || {}).sizes;
    if (sizes && sizes.length) return sizes.map(e => String(Array.isArray(e) ? e[0] : e));
    const names = new Set();
    const visit = rules => {
      for (const r of rules) {
        try {
          if (r.cssRules && !r.selectorText) visit(r.cssRules);
          if (r.style && /(^|,)\s*(:root|html)(\s*,|\s*$|\[)/.test(r.selectorText || '')) {
            for (let i = 0; i < r.style.length; i++) if (/^--(fs|font-size|text)-/.test(r.style[i])) names.add(r.style[i]);
          }
        } catch (e) { /* 跨域样式表读不了 */ }
      }
    };
    for (const sh of d.styleSheets) { try { visit(sh.cssRules); } catch (e) { /* 跨域 */ } }
    const all = [...names];
    return all.filter(n => !all.some(m => m !== n && n.startsWith(m + '--')));   // Tailwind 的 --text-xs--line-height 这类附属变量不算
  }
  function readScale() {
    const d = doc(), w = win(); if (!d || !d.body) return [];
    const probe = d.createElement('span'); probe.__dsProbe = true;
    probe.style.cssText = 'position:absolute;visibility:hidden;pointer-events:none';
    d.body.appendChild(probe);
    const out = new Set();
    for (const n of sizeNames(d)) {
      probe.style.fontSize = '';
      probe.style.fontSize = n.startsWith('--') ? `var(${n})` : (/^[\d.]+$/.test(n) ? n + 'px' : n);
      const v = parseFloat(w.getComputedStyle(probe).fontSize);
      if (v && v < SC.keep_from) out.add(half(v));
    }
    probe.remove();
    return [...out].sort((a, b) => a - b);
  }

  /* ---------- 映射：原字号 → 第几档 ---------- */
  function nearest(v) {
    let best = -1, gap = Infinity;
    st.steps.forEach((px, i) => { const g = Math.abs(px - v); if (g < gap || (g === gap && px > st.steps[best])) { best = i; gap = g; } });
    return best;
  }
  function ruleIdx(v) {
    const p = P(); if (!p) return -1;
    for (const [max, px] of p.upto) if (v <= max) { const i = p.steps.findIndex(s => s[0] === px); if (i >= 0) return i; }
    return nearest(v);
  }
  const idxOf = v => (Object.prototype.hasOwnProperty.call(st.over, v) ? st.over[v] : ruleIdx(v));
  function target(r) {                                  // 这个元素在当前方案下显示多大；null = 原样
    if (!P() || r.keep || r.lock) return null;
    const i = idxOf(half(r.o));
    return i < 0 ? null : st.steps[i];
  }

  /* ---------- 量字号、套方案 ---------- */
  function texty(el) {
    for (const n of el.childNodes) if (n.nodeType === 3 && n.textContent.trim()) return true;
    return false;
  }
  // 撤掉预览：还原页面自己写的行内字号（没有就删掉），不能一律 removeProperty
  function unpin(el, r) {
    if (r && r.inl) el.style.setProperty('font-size', r.inl, r.pri); else el.style.removeProperty('font-size');
  }
  function unpinAll() { for (const [el, r] of recs) unpin(el, r); }
  function measure() {
    const d = doc(), w = win(); if (!d || !d.body) return;
    for (const el of [...recs.keys()]) if (!el.isConnected) recs.delete(el);
    unpinAll();                                        // 量的都是没套方案时的原字号
    const keepSel = SC.keep.trim();
    for (const el of d.body.querySelectorAll('*')) {
      if (recs.has(el) || /^(SCRIPT|STYLE|NOSCRIPT|TEMPLATE)$/.test(el.tagName) || el.closest('svg') || !texty(el)) continue;
      const o = parseFloat(w.getComputedStyle(el).fontSize);
      let keep = o >= SC.keep_from;
      if (!keep && keepSel) { try { keep = el.matches(keepSel); } catch (e) { /* 选择器写错了，当没写 */ } }
      const lk = SC.lock.find(l => { try { return el.matches(l.selector) && o >= l.min && o <= l.max; } catch (e) { return false; } });
      recs.set(el, { o, keep, lock: !!lk, inl: el.style.getPropertyValue('font-size'), pri: el.style.getPropertyPriority('font-size') });
    }
    apply();
  }
  function measureFresh() {
    unpinAll(); recs = new Map();
    nowScale = readScale();
    if (!SC.presets.length) autoPreset();
    measure();
    renderPresets(); renderSteps();
  }
  function apply() {
    const scale = new Set(P() ? st.steps : nowScale);
    const mark = $('mark').checked, off = holding || !P();
    for (const [el, r] of recs) {
      const t = target(r);
      if (off) unpin(el, r);
      else el.style.setProperty('font-size', (t == null ? r.o : t) + 'px', 'important');   // 不动的也钉住原值，免得 em 继承跟着父元素变
      const shown = off || t == null ? r.o : t;
      el.toggleAttribute('data-ds-off', mark && !holding && !r.keep && !r.lock && !scale.has(half(shown)));
    }
    stat(); table();
  }

  /* ---------- 面板 ---------- */
  function visible() {   // 读屏专用的 1px 裁切文字、没展开的不算
    return [...recs].filter(([el, r]) => { if (r.keep) return false; const b = el.getBoundingClientRect(); return b.width > 2 && b.height > 2; });
  }
  function stat() {
    const vis = visible();
    const before = new Set(vis.map(([, r]) => half(r.o)));
    const offBefore = vis.filter(([, r]) => !r.lock && !nowScale.includes(half(r.o))).length;
    let html = `这页能看到的普通文字 <b>${vis.length}</b> 处：<b>${before.size}</b> 种字号，<b>${offBefore}</b> 处不在现有刻度里`;
    if (!nowScale.length) html += '（没找到字号变量，配置 editor.styles.sizes 或在 :root 上写 --fs-*）';
    if (P()) {
      const shown = r => { const t = target(r); return half(t == null ? r.o : t); };
      const after = new Set(vis.map(([, r]) => shown(r)));
      const off = vis.filter(([, r]) => !r.lock && !st.steps.includes(shown(r))).length;
      html += `<br>${esc(P().name)}${st.edited ? '（改过）' : ''}：<b>${after.size}</b> 种字号，刻度外 <b>${off}</b> 处`;
    }
    $('stat').innerHTML = html;
  }
  function table() {
    const groups = new Map();
    for (const [el, r] of visible()) {
      const v = half(r.o), g = groups.get(v) || { n: 0, s: '', lock: 0 };
      g.n++; if (r.lock) g.lock++;
      if (!g.s) g.s = (el.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 16);
      groups.set(v, g);
    }
    const p = P();
    const opts = st.steps.map((px, i) => `<option value="${i}">→ ${px}px</option>`).join('') + '<option value="-1">不动</option>';
    $('map').innerHTML = [...groups].sort((a, b) => a[0] - b[0]).map(([v, g]) => {
      const i = p ? idxOf(v) : null, chg = p && i >= 0 && st.steps[i] !== v;
      const lock = g.lock === g.n ? '<option>锁定</option>' : '';
      return `<tr class="${chg ? 'chg' : ''}"><td class="v">${v}</td><td class="n">${g.n}</td><td class="s" title="${esc(g.s)}">${esc(g.s)}</td>
        <td class="m"><select data-v="${v}" ${p && !lock ? '' : 'disabled'}>${lock || opts}</select></td></tr>`;
    }).join('');
    if (p) $('map').querySelectorAll('select:not(:disabled)').forEach(s => { s.value = String(idxOf(+s.dataset.v)); });
  }
  function renderSteps() {
    const p = P() || presets[0];
    const pxs = P() ? st.steps : p ? p.steps.map(s => s[0]) : nowScale;
    $('steps').innerHTML = pxs.map((px, i) =>
      `<label class="step"><input type="number" min="6" max="200" step="1" value="${px}" data-i="${i}" ${P() ? '' : 'disabled'} aria-label="第 ${i + 1} 档"><span>${esc(p && p.steps[i] ? p.steps[i][1] : '')}</span></label>`).join('');
  }
  function renderPresets() {
    const btn = (i, name, extra) => `<button type="button" data-i="${i}" aria-pressed="${i === st.preset}">${esc(name)}${extra || ''}</button>`;
    $('pre').innerHTML = btn(-1, '现在') + presets.map((p, i) => btn(i, p.name, (p.recommend ? '<em>推荐</em>' : '') + (i === st.preset && st.edited ? '<em>改过</em>' : ''))).join('');
    const p = P();
    const locks = SC.lock.map(l => l.why).filter(Boolean);
    $('pnote').textContent = p ? (p.note || `${p.steps.length} 档：${p.steps.map(s => s[0]).join(' / ')}px。`) + (locks.length ? ` 锁定不动：${locks.join('；')}。` : '')
      : `页面原样。红虚线 = 不在现有刻度（${nowScale.join('、') || '没找到'}）里的字号。`;
    $('keepNote').textContent = `只列普通文字。${SC.keep_from}px 及以上的字${SC.keep ? '和配置里列为展示字的元素' : ''}不动，不在表里。`;
  }
  function autoPreset() {
    if (!nowScale.length) { presets = []; return; }
    presets = [{ name: '并到现有刻度', note: '站点没配方案：按现有字号变量当刻度，每个字号并到最近的一档。', recommend: false, steps: nowScale.map(px => [px, '']), upto: [] }];
    if (st.preset >= presets.length) st.preset = -1;
    if (P() && (!st.steps.length || st.steps.length !== P().steps.length)) st.steps = P().steps.map(s => s[0]);
  }
  function render(save = true) {
    renderPresets(); renderSteps(); apply();
    st.presetName = P() ? P().name : '';
    try { localStorage.setItem(KEY, JSON.stringify(st)); } catch (e) { /* 存不了就算了 */ }
    if (save) { clearTimeout(saveTimer); saveTimer = setTimeout(push, 900); }
  }

  /* ---------- 保存、复制 ---------- */
  function summary() {
    const p = P();
    if (!p) return '方案：现在（不改）' + (st.note ? `｜备注：${st.note}` : '');
    const ov = Object.entries(st.over).map(([v, i]) => `${v}→${i < 0 ? '不动' : st.steps[i] + 'px'}`);
    return `方案 ${p.name}${st.edited ? '（改过）' : ''}｜刻度 ${st.steps.join(' / ')}px` + (ov.length ? `｜单独改的映射：${ov.join('，')}` : '') + (st.note ? `｜备注：${st.note}` : '');
  }
  function forAI() {
    const p = P(); if (!p) return '字号收敛：先不改。' + (st.note ? '备注：' + st.note : '');
    const rule = [];
    let lo = 0;
    for (const [max, px] of p.upto) { const i = p.steps.findIndex(s => s[0] === px); if (i >= 0) rule.push(`原字号 ${lo ? '>' + lo + ' 且 ' : ''}≤${max}px → ${st.steps[i]}px`); lo = max; }
    if (!rule.length || lo < SC.keep_from) rule.push('其余并到最近的一档（一样近取大的）');
    const ov = Object.entries(st.over).map(([v, i]) => `${v}px → ${i < 0 ? '不动' : st.steps[i] + 'px'}`);
    return [`字号收敛：全站文字字号并到这几档：${st.steps.join(' / ')}px（方案「${p.name}」${st.edited ? '，改过' : ''}）。`,
      `并法：${rule.join('；')}。` + (ov.length ? `例外：${ov.join('，')}。` : ''),
      `不动：${SC.keep_from}px 及以上的字` + (SC.keep ? `，以及展示字（${SC.keep}）` : '') + (SC.lock.length ? `；锁定：${SC.lock.map(l => `${l.selector} 里 ${l.min}—${l.max}px 的字${l.why ? '（' + l.why + '）' : ''}`).join('；')}` : '') + '。',
      '改法：字号写成设计变量（没有对应变量的先在 tokens 里加），不要手写像素；改完在 design-studio 的字号收敛页切到「现在」，刻度外应为 0。',
      st.note ? `备注：${st.note}` : ''].filter(Boolean).join('\n');
  }
  async function push() {
    try {
      const r = await fetch(API + 'feedback', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: SC.feedback, page: '字号收敛', items: { set: { title: '字号刻度', choice: P() ? P().name : '现在', text: summary() } } }) });
      const j = await r.json();
      $('st').textContent = j.ok ? `已存到 ${j.file}` : '保存失败，用「复制给 AI」';
    } catch (e) { $('st').textContent = '没连上本机服务，用「复制给 AI」'; }
  }

  /* ---------- iframe ---------- */
  function hook() {
    const d = doc(); if (!d || !d.body) return;
    recs = new Map();
    if (!d.getElementById('ds-scale-style')) {
      const s = d.createElement('style'); s.id = 'ds-scale-style'; s.__dsProbe = true;
      s.textContent = '[data-ds-off]{outline:1.5px dashed #e5484d!important;outline-offset:1px!important}';
      d.head.appendChild(s);
    }
    if (mo) mo.disconnect();
    let t = 0, last = 0, runs = 0;
    mo = new MutationObserver(list => {   // 页面后来插进来的内容（懒加载、切视图）再量；自己加的探针不算
      if (list.every(m => [...m.addedNodes, ...m.removedNodes].every(n => n.__dsProbe))) return;
      // 节流：至少隔 1 秒、每次载入最多重量 30 次（有的页面按尺寸重建 DOM，预览改了字号又触发重建，会量个不停）
      if (runs >= 30) return;
      clearTimeout(t); t = setTimeout(() => { last = performance.now(); runs++; measure(); }, Math.max(250, 1000 - (performance.now() - last)));
    });
    mo.observe(d.body, { childList: true, subtree: true });
    d.addEventListener('mousemove', e => {
      let el = e.target; while (el && !recs.has(el)) el = el.parentElement;
      if (!el) { tip.hidden = true; return; }
      const r = recs.get(el), t2 = target(r), fr = frame.getBoundingClientRect();
      const cls = el.classList.length ? '.' + [...el.classList].slice(0, 2).join('.') : '';
      tip.innerHTML = `${esc(el.tagName.toLowerCase() + cls)}　原 ${+r.o.toFixed(2)}px` + (r.keep ? '　展示字，不动' : r.lock ? '　锁定' : t2 != null ? `　→ <b>${t2}px</b>` : '');
      tip.hidden = false;
      tip.style.left = Math.max(8, Math.min(fr.left + e.clientX + 14, innerWidth - tip.offsetWidth - 8)) + 'px';
      tip.style.top = (fr.top + e.clientY + 18) + 'px';
    });
    d.addEventListener('mouseleave', () => { tip.hidden = true; });
    applyTheme();
    (d.fonts ? d.fonts.ready : Promise.resolve()).then(() => setTimeout(measureFresh, 300));
  }
  frame.addEventListener('load', hook);
  function go() {
    const pg = SC.pages[st.page] || SC.pages[0]; if (!pg) return;
    unpinAll(); recs = new Map();   // 先撤掉预览，免得后面把钉着的预览字号当成原字号
    const cur = (() => { try { return frame.contentWindow.location.href; } catch (e) { return ''; } })();
    const next = new URL(pg[1], location.href).href;
    const sameDoc = cur && cur.split('#')[0] === next.split('#')[0];
    frame.src = pg[1];
    if (sameDoc) setTimeout(measureFresh, 500);   // 只差 #：浏览器不重新载入、不触发 load，页面自己切视图后整页重量
  }

  /* ---------- 视口：按 100% 实际像素显示，不缩放 ---------- */
  const VPS = () => [{ w: 0, h: 0, label: '适应窗口' }].concat(cfg.viewports || DEFAULT_VIEWPORTS);
  function layout() {
    const v = VPS().find(x => String(x.w) === String(st.vp)) || VPS()[0];
    const w = v.w ? +v.w : stage.clientWidth - 32, h = v.h ? +v.h : stage.clientHeight - 32;
    frameBox.style.width = w + 'px'; frameBox.style.height = h + 'px';
    frame.style.width = w + 'px'; frame.style.height = h + 'px';
  }
  function renderViewports() {
    $('vp').innerHTML = VPS().map(v => `<button type="button" data-w="${v.w ? +v.w : 'fit'}" aria-pressed="${String(v.w ? +v.w : 'fit') === String(st.vp)}">${esc(v.label || v.w)}</button>`).join('');
  }
  let rt = 0;
  window.addEventListener('resize', () => { if (st.vp !== 'fit') return; clearTimeout(rt); rt = setTimeout(() => { layout(); setTimeout(measureFresh, 300); }, 200); });

  /* ---------- 主题（同编辑器：按站点配置的 localStorage 键和 html 属性切换） ---------- */
  const TH = () => cfg.theme;
  function themeNow() { const th = TH(); let t = th.light || 'light'; try { t = localStorage.getItem(th.storage_key) || t; } catch (e) { /* */ } return t; }
  function applyTheme() {
    const th = TH(); if (!th) return;
    const t = themeNow(), d = doc();
    if (d) d.documentElement.setAttribute(th.attr || 'data-theme', t);
    $('theme').classList.toggle('dark', t === (th.dark || 'dark'));
  }
  $('theme').onclick = () => {
    const th = TH(); if (!th) return;
    const t = themeNow() === (th.dark || 'dark') ? (th.light || 'light') : (th.dark || 'dark');
    try { localStorage.setItem(th.storage_key, t); } catch (e) { /* */ }
    applyTheme();
  };

  /* ---------- 事件 ---------- */
  $('page').addEventListener('change', e => { st.page = +e.target.value; go(); render(false); });
  $('vp').addEventListener('click', e => {
    const b = e.target.closest('button'); if (!b) return;
    st.vp = b.dataset.w; renderViewports(); layout(); render(false);
    setTimeout(measureFresh, 400);   // 换视口后原字号会变（窄屏规则），整页重量
  });
  $('pre').addEventListener('click', e => {
    const b = e.target.closest('button'); if (!b) return;
    st.preset = +b.dataset.i; st.over = {}; st.edited = false;
    st.steps = P() ? P().steps.map(s => s[0]) : [];
    render();
  });
  $('steps').addEventListener('change', e => {
    const i = +e.target.dataset.i, v = Math.round(+e.target.value);
    if (!P() || !(v >= 6 && v <= 200)) return;
    st.steps[i] = v; st.edited = true; render();
  });
  $('map').addEventListener('change', e => {
    const v = +e.target.dataset.v, i = +e.target.value;
    if (i === ruleIdx(v)) delete st.over[v]; else st.over[v] = i;
    st.edited = Object.keys(st.over).length > 0 || st.steps.some((px, k) => px !== P().steps[k][0]);
    render();
  });
  $('note').addEventListener('input', e => { st.note = e.target.value; try { localStorage.setItem(KEY, JSON.stringify(st)); } catch (er) { /* */ } clearTimeout(saveTimer); saveTimer = setTimeout(push, 900); });
  $('mark').addEventListener('change', apply);
  const hold = on => { holding = on; apply(); };
  $('hold').addEventListener('pointerdown', () => hold(true));
  ['pointerup', 'pointerleave', 'pointercancel'].forEach(ev => $('hold').addEventListener(ev, () => { if (holding) hold(false); }));
  $('reset').addEventListener('click', () => { st.over = {}; st.edited = false; st.steps = P() ? P().steps.map(s => s[0]) : []; render(); });
  $('copy').addEventListener('click', async () => {
    const t = forAI();
    try { await navigator.clipboard.writeText(t); $('st').textContent = '已复制，发给 Claude Code 或 Codex'; } catch (e) { prompt('复制下面的内容：', t); }
  });
  $('min').addEventListener('click', () => { const m = $('panel').classList.toggle('min'); $('min').textContent = m ? '展开' : '收起'; });

  /* ---------- 启动 ---------- */
  (async () => {
    try {
      cfg = await fetch(API + 'config', { cache: 'no-store' }).then(r => r.json()) || {};
      SC = Object.assign(SC, cfg.scale || {});
    } catch (e) {
      $('status').textContent = '没连上本机服务：先运行 python3 design_studio.py serve <站点配置.json>';
      $('status').classList.add('bad');
      return;
    }
    document.title = '字号收敛 · ' + (cfg.title || 'design-studio');
    $('brand').textContent = cfg.title || 'design-studio';
    $('theme').hidden = !cfg.theme;
    $('status').textContent = '已连上本机服务';
    presets = SC.presets || [];
    let saved = null;
    try { saved = JSON.parse(localStorage.getItem(KEY)); } catch (e) { /* 浏览器不让读就用默认 */ }
    const okNum = v => typeof v === 'number' && isFinite(v);
    const valid = v => v && typeof v === 'object' && okNum(v.preset) && Array.isArray(v.steps) && v.steps.every(okNum)
      && v.over && typeof v.over === 'object' && !Array.isArray(v.over) && Object.values(v.over).every(Number.isInteger);
    if (valid(saved)) {   // 坏数据（手改过、旧版本存的）直接丢掉，用默认
      st = Object.assign(st, saved);
      const byName = presets.findIndex(p => p.name === saved.presetName);   // 方案按名字找：配置里调了顺序也不会选错
      if (saved.presetName && byName >= 0) st.preset = byName;
    } else { const r = presets.findIndex(p => p.recommend); if (r >= 0) st.preset = r; }   // 第一次打开：选推荐的那套
    if (st.preset >= presets.length && SC.presets.length) st.preset = -1;
    // 配置里的档位变了（档数或像素），又没手动改过：回到方案默认，免得沿用旧档位还不显示「改过」
    if (P() && (st.steps.length !== P().steps.length || (!st.edited && st.steps.some((px, i) => px !== P().steps[i][0])))) {
      st.steps = P().steps.map(s => s[0]); st.over = {}; st.edited = false;
    }
    if (st.page >= SC.pages.length) st.page = 0;
    $('page').innerHTML = SC.pages.map(([n], i) => `<option value="${i}">${esc(n)}</option>`).join('');
    $('page').value = String(st.page);
    $('note').value = st.note || '';
    renderViewports(); layout(); render(false); go();
  })();
})();
