"""页面模式的接口：页面清单、修改单、保存（文字写回 + 样式 / 意见进修改单）、反馈框。

接口和落盘格式和博客的旧编辑服务一致（见 docs/compat.md），回归脚本 tests/regression/run.py 逐条核对。
不同的地方只有两处：写回只替换被改的那个字符串（旧服务会把整份数据文件重排）；能在 HTML 正文里精确找到的原句也写回。
"""
import datetime
import json
import os
import re
import subprocess
import sys
import threading

from . import writeback as wb

LOCK = threading.Lock()
SHEET_NOTE = ('design-studio 保存的修改单。status：已写入 / 已写入，待 AI 跟进 / 待 AI / AI 已处理 / 不改 / 没写入。'
              'AI 处理后改 status 并写 reply。')
FB_NAME = re.compile(r'^[\w一-鿿-]{1,40}$')


def now():
    return datetime.datetime.now().strftime('%Y-%m-%d %H:%M')


def _dump(path, obj):
    """原子写 JSON：先写临时文件再替换，写到一半断电也不会留下半份文件。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _write_text(path, text):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8', newline='') as f:
        f.write(text)
    os.replace(tmp, path)


def _read_text(path):
    with open(path, encoding='utf-8', newline='') as f:
        return f.read()


def _load_json(path):
    with open(path, encoding='utf-8') as f:
        return json.load(f)


# ======================================================================
# 修改单
# ======================================================================
def load_sheet(cfg, page):
    if os.path.exists(page.sheet):
        return _load_json(page.sheet)
    return {'page': page.name, 'file': page.rel_html or page.url, 'data': page.rel_data, 'guide': page.rel_guide,
            'note': SHEET_NOTE, 'items': []}


# ======================================================================
# 保存后钩子（比如接一个敏感信息扫描脚本）：命令失败、跑不起来、超时都算不过，撤回这次写入
# ======================================================================
def run_hook(cfg, files):
    hook = cfg.after_save
    if not hook or not files:
        return []
    label = hook.get('label') or '保存后检查'
    ignore = tuple(hook.get('ignore_prefixes') or ())
    problems = []
    for f in files:
        cmd = [str(x).replace('{file}', f).replace('{python}', sys.executable).replace('{base}', cfg.base) for x in hook['cmd']]
        try:
            r = subprocess.run(cmd, cwd=cfg.base, capture_output=True, text=True, timeout=float(hook.get('timeout', 60)))
        except Exception as e:   # 跑不起来不能当通过
            problems.append(f'{label}没跑起来：{e}')
            continue
        if r.returncode != 0:
            lines = [l for l in (r.stdout + '\n' + r.stderr).splitlines() if l.strip() and not l.startswith(ignore)]
            problems.extend(lines or [f'{label}退出码 {r.returncode}'])
    return problems


# ======================================================================
# 保存
# ======================================================================
def save(cfg, name, items):
    page = cfg.pages[name]
    label = (cfg.after_save or {}).get('label') or '保存后检查'
    with LOCK:
        sheet = load_sheet(cfg, page)
        day = datetime.date.today().strftime('%Y%m%d')
        n = sum(1 for x in sheet['items'] if str(x.get('id', '')).startswith(day))
        originals = {}            # 本次写过的文件 → 写之前的原文（撤回用）
        current = {}              # 本次写过的文件 → 现在的原文
        data_obj = None
        if page.data and os.path.isfile(page.data):
            current[page.data] = _read_text(page.data)
            try:
                _, data_obj, _, _ = wb.split_data(current[page.data])
            except wb.WriteError:
                data_obj = None
        written, out, warnings = [], [], []
        for it in items if isinstance(items, list) else []:
            if not isinstance(it, dict):
                continue
            kind = it.get('type')
            if kind not in ('文字', '样式', '意见'):
                continue
            n += 1
            rec = {'id': f'{day}-{n:03d}', 'time': now(), 'type': kind, 'where': it.get('where') or {}}
            if kind == '文字':
                rec.update(old=(it.get('oldText') or '').strip(), new=(it.get('newText') or '').strip())
                done = _save_text(cfg, page, it, rec, data_obj, current, originals)
                if done:
                    written.append(rec)
            elif kind == '样式':
                rec.update(prop=it.get('prop'), label=it.get('label'), old=it.get('from'), new=it.get('to'),
                           token=it.get('token'), css=it.get('css'), status='待 AI')
            else:
                rec.update(text=(it.get('text') or '').strip(), status='待 AI')
            out.append(rec)
        if originals:
            for f in originals:
                _write_text(f, current[f])
            problems = run_hook(cfg, sorted(originals))
            if problems:
                for f, raw in originals.items():
                    _write_text(f, raw)
                for r in written:
                    for k in ('field', 'value', 'follow', 'file'):
                        r.pop(k, None)
                    r.update(status='没写入', note=f'{label}命中，已撤回：' + '；'.join(problems)[:300])
                warnings.append(f'{label}命中，文字改动已撤回：' + '；'.join(problems))
        sheet['items'].extend(out)
        _dump(page.sheet, sheet)
    return {'ok': True, 'saved': out, 'warnings': warnings, 'sheet': page.rel_sheet}


def _save_text(cfg, page, it, rec, data_obj, current, originals):
    """文字：先在数据文件里按字段找，找不到再试 HTML 正文。写进了返回 True；否则把原因记进 rec。"""
    reason = None
    if data_obj is not None:
        path, how, value = wb.locate(data_obj, it, cfg, page.skip)
        if path is not None:
            try:
                new_raw = wb.replace_in_data(current[page.data], path, value)
            except wb.WriteError as e:
                reason = f'精确写回没成功：{e}'
            else:
                originals.setdefault(page.data, current[page.data])
                current[page.data] = new_raw
                wb.set_path(data_obj, path, value)
                field = wb.dotted(path)
                follow = [msg for pat, msg in cfg.follow if pat.search(field)]
                rec.update(field=field, value=value, status='已写入，待 AI 跟进' if follow else '已写入')
                if how == 'part':
                    rec['note'] = '页面上只显示了这个字段的一部分，按原句替换'
                if follow:
                    rec['follow'] = '；'.join(follow)
                return True
        else:
            reason = how
    if page.html and os.path.isfile(page.html):
        raw = current.get(page.html)
        if raw is None:
            raw = current[page.html] = _read_text(page.html)
        try:
            new_raw = wb.replace_in_html(raw, it.get('oldText'), it.get('newText'))
        except wb.WriteError as e:
            reason = reason or str(e)
        else:
            originals.setdefault(page.html, raw)
            current[page.html] = new_raw
            rec.update(file=page.rel_html, status='已写入')
            return True
    rec.update(status='待 AI', note=reason or '没有登记数据文件或 HTML，写不回')
    new_html = wb.clean_html(it.get('newHtml', ''), cfg.allowed_tags)
    if new_html != rec['new']:
        rec['newHtml'] = new_html
    return False


# ======================================================================
# 反馈框（对比页、调整单里每条下面的选项和意见）：一张单子一个文件，整份覆盖
# ======================================================================
def fb_path(cfg, name):
    return os.path.join(cfg.feedback_dir, name + '.json')


def fb_load(cfg, name):
    path = fb_path(cfg, name)
    return _load_json(path) if os.path.exists(path) else {'name': name, 'items': {}}


def fb_save(cfg, name, page, items):
    if not isinstance(items, dict) or len(items) > 300:
        raise ValueError('items 格式不对')
    clean = {}
    for code, v in items.items():
        if not isinstance(v, dict) or not re.match(r'^[\w-]{1,12}$', str(code)):
            continue
        clean[str(code)] = {'title': str(v.get('title', ''))[:200], 'choice': str(v.get('choice', ''))[:20], 'text': str(v.get('text', ''))[:5000]}
    data = {'name': name, 'page': str(page or '')[:200], 'updated': now(), 'items': clean}
    path = fb_path(cfg, name)
    with LOCK:
        _dump(path, data)
    return {'ok': True, 'updated': data['updated'], 'count': sum(1 for v in clean.values() if v['choice'] or v['text'].strip()),
            'file': cfg.rel(path)}


# ======================================================================
# 路由（由 server.py 注册）
# ======================================================================
def register(router, cfg):
    def pages(h, q):
        return h.send_json(200, {name: p.public() for name, p in cfg.pages.items()})

    def config(h, q):
        return h.send_json(200, cfg.editor_public())

    def sheet(h, q):
        name = (q.get('page') or [next(iter(cfg.pages))])[0]
        if name not in cfg.pages:
            return h.send_json(404, {'error': 'page'})
        return h.send_json(200, load_sheet(cfg, cfg.pages[name]))

    def feedback_get(h, q):
        name = (q.get('name') or [''])[0]
        if not FB_NAME.match(name):
            return h.send_json(400, {'error': 'name'})
        return h.send_json(200, fb_load(cfg, name))

    def feedback_post(h, body):
        name = body.get('name', '')
        if not FB_NAME.match(str(name)):
            return h.send_json(400, {'error': 'name'})
        return h.send_json(200, fb_save(cfg, name, body.get('page'), body.get('items')))

    def save_post(h, body):
        name = body.get('page')
        if name not in cfg.pages:
            return h.send_json(404, {'error': f'没有登记的页面：{name}'})
        return h.send_json(200, save(cfg, name, body.get('items') or []))

    router.get('/api/pages', pages)
    router.get('/api/config', config)
    router.get('/api/sheet', sheet)
    router.get('/api/feedback', feedback_get)
    router.post('/api/feedback', feedback_post)
    router.post('/api/save', save_post)
