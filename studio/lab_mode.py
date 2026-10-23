"""骨架模式（原 forge-design-lab 的工作台服务）：在页面包上定比例和 token，保存直写仓库。

从 forge-design-lab 的 scripts/serve.py 搬来，接口和落盘行为不变（tests/regression/lab_run.py 对着旧服务逐条核对）：
  · 静态根 = 仓库根。页面 URL 形如 /docs/assets/visuals/<page>/<file>.html，页面里 ../../../../frontend-react/... 的相对路径原样可用。
  · GET  /api/lab/status      绑定的仓库、端口、页面包和各自的版本戳
  · GET  /api/lab/packs       页面包索引
  · GET  /api/lab/system-rev  零件库 json 的内容指纹
  · POST /api/lab/save        按 id 合并后直写三文件（json、js、css）；回执写 .forge/design-lab/last-save.json，并追加到 saves.jsonl
  · GET  / 或 /index.html     页面包索引页
保存为什么要合并而不是覆盖：工作台页面一加载 spec 就固定在内存里，Agent 在这期间改了 json，页面一存就会把新增节点抹掉。
所以按 id 合并：值、结局、反馈取页面的，结构取磁盘的，并把「页面没见过的节点」回给页面。
先校验再落盘；落盘之后的附加步骤出错只记 warnings，不把请求报成失败（已落盘却报失败，用户会以为工作没了）。
"""
import datetime
import hashlib
import html
import json
import os

from .lab import build_tokens as BT

STATE_REL = os.path.join('.forge', 'design-lab')
VIS = os.path.join('docs', 'assets', 'visuals')
SYSTEM_JSON = os.path.join(VIS, 'system', 'system.tokens.json')
SYSTEM_CSS = os.path.join(VIS, 'system', 'template.css')


def state_dir(repo):
    d = os.path.join(repo, STATE_REL)
    os.makedirs(d, exist_ok=True)
    return d


def pack_rev(repo, name):
    """页面包的版本戳：spec、生成物、骨架、引擎的 mtime+size 摘要。页面拿它和启动时记下的比，不同就知道文件被改过。"""
    d = os.path.join(repo, VIS, name)
    paths = [os.path.join(d, f'{name}.tokens.json'), os.path.join(d, f'{name}.tokens.js'),
             os.path.join(repo, VIS, '_workbench', 'workbench.js')]
    paths += sorted(os.path.join(d, f) for f in os.listdir(d) if f.endswith('.html')) if os.path.isdir(d) else []
    h = hashlib.sha1()
    for p in paths:
        try:
            st = os.stat(p)
            h.update(f'{os.path.basename(p)}:{int(st.st_mtime)}:{st.st_size};'.encode())
        except OSError:
            h.update(f'{os.path.basename(p)}:missing;'.encode())
    return h.hexdigest()[:12]


def find_packs(repo):
    base = os.path.join(repo, VIS)
    out = []
    if not os.path.isdir(base):
        return out
    for name in sorted(os.listdir(base)):
        d = os.path.join(base, name)
        if not os.path.isdir(d) or name.startswith('_'):
            continue
        spec = os.path.join(d, f'{name}.tokens.json')
        if not os.path.exists(spec):
            continue
        htmls = sorted(f for f in os.listdir(d) if f.endswith('.html'))
        try:
            with open(spec, encoding='utf-8') as fh:
                s = json.load(fh)
        except (OSError, ValueError):
            continue
        out.append({'page': name, 'title': s.get('title') or name, 'version': s.get('version'),
                    'nodes': len(s.get('nodes') or s.get('colors') or []), 'updated': s.get('updated'),
                    'exported': s.get('exported'),
                    'questions': sum(1 for n in s.get('nodes') or [] if n.get('q')),
                    'feedback': len(s.get('feedback') or []),
                    'html': [f'/docs/assets/visuals/{name}/{h}' for h in htmls],
                    'rev': pack_rev(repo, name)})
    return out


def write_receipt(repo, report):
    """回执会被下一次保存覆盖（连点两次保存时第二次报「无值改动」），所以另存一份追加流水 saves.jsonl。"""
    for name, mode in (('last-save.json', 'w'), ('saves.jsonl', 'a')):
        try:
            with open(os.path.join(state_dir(repo), name), mode, encoding='utf-8') as fh:
                if mode == 'a':
                    fh.write(json.dumps(report, ensure_ascii=False) + '\n')
                else:
                    json.dump(report, fh, ensure_ascii=False, indent=1)
        except OSError as exc:
            report.setdefault('warnings', []).append(f'回执写不进去（文件已保存）：{exc}')


def system_rev(repo):
    """零件库 json 的内容指纹。页面加载时拿到它，保存时带回来；不一致说明页面打开期间磁盘被改过。"""
    try:
        with open(os.path.join(repo, SYSTEM_JSON), 'rb') as fh:
            return hashlib.sha1(fh.read()).hexdigest()[:12]
    except OSError:
        return None


def _validate_system(inc):
    errs = []
    t = inc.get('template')
    if t is not None and not (isinstance(t, dict) and isinstance(t.get('colors'), list) and t['colors']):
        errs.append('template 必须是带 colors 的对象')
    for k, typ in (('custom', list), ('feedback', list), ('edits', dict), ('decisions', dict)):
        v = inc.get(k)
        if v is not None and not isinstance(v, typ):
            errs.append(f'{k} 类型不对')
    for k in ('custom', 'feedback'):
        if isinstance(inc.get(k), list) and not all(isinstance(x, dict) for x in inc[k]):
            errs.append(f'{k} 的每一项必须是对象')
    if isinstance(inc.get('decisions'), dict) and not all(isinstance(v, dict) for v in inc['decisions'].values()):
        errs.append('decisions 的值必须是对象')
    if inc.get('template_css') is not None and not isinstance(inc.get('template_css'), str):
        errs.append('template_css 必须是字符串')
    return errs


class StaleSave(ValueError):
    pass


def merge_system(repo, incoming):
    """零件库（kind=system）。路径固定，不信请求里的 files。
    抽取字段以磁盘为准；用户产出（template、custom、feedback，以及旧版的 edits、decisions）只在请求里带了才替换。
    页面带 rev 来，磁盘在页面打开期间被改过就拒绝（409），不静默覆盖。"""
    spath = os.path.join(repo, SYSTEM_JSON)
    if not os.path.exists(spath):
        raise ValueError(f'磁盘上没有 {SYSTEM_JSON}')
    errs = _validate_system(incoming)
    if errs:
        raise ValueError('；'.join(errs))
    cur = system_rev(repo)
    if incoming.get('rev') and incoming['rev'] != cur:
        raise StaleSave('页面打开期间零件库文件被改过（可能是 Agent 刚更新了模板或抽取）。刷新页面后再改；你这次的改动已另存为下载文件。')
    with open(spath, encoding='utf-8') as fh:
        disk = json.load(fh)
    report = {'page': 'system', 'kind': 'system', 'files': [SYSTEM_JSON], 'template_changes': [], 'custom_changes': [],
              'new_feedback': [], 'removed_feedback': 0, 'warnings': []}
    if 'feedback' in incoming:
        old_fb = disk.get('feedback') or []
        seen = {(f.get('at'), f.get('t')) for f in old_fb}
        report['new_feedback'] = [f for f in incoming['feedback'] if (f.get('at'), f.get('t')) not in seen]
        report['removed_feedback'] = max(0, len(old_fb) + len(report['new_feedback']) - len(incoming['feedback']))
        disk['feedback'] = incoming['feedback']
    for k in ('edits', 'decisions'):
        if k in incoming:
            disk[k] = incoming[k]
    tpl_ok = False
    if incoming.get('template') is not None:
        old_t, new_t = disk.get('template') or {}, incoming['template']
        for k in sorted(set(old_t) | set(new_t)):
            a, b = old_t.get(k), new_t.get(k)
            if isinstance(a, list) and isinstance(b, list) and len(a) == len(b) and all(isinstance(x, dict) for x in a + b):
                for x, y in zip(a, b):
                    if x != y:
                        report['template_changes'].append({'part': k, 'item': y.get('name') or y.get('id'),
                                                           'fields': {f: [x.get(f), y.get(f)] for f in set(x) | set(y) if x.get(f) != y.get(f)}})
            elif a != b:
                report['template_changes'].append({'part': k, 'from': a, 'to': b})
        disk['template'], tpl_ok = new_t, True
    if incoming.get('custom') is not None:
        old_c = {c.get('id'): c for c in disk.get('custom') or []}
        for c in incoming['custom']:
            o = old_c.get(c.get('id')) or {}
            if o.get('status') != c.get('status') or (o.get('note') or '') != (c.get('note') or ''):
                report['custom_changes'].append({'id': c.get('id'), 'name': c.get('name'), 'status': [o.get('status'), c.get('status')], 'note': c.get('note') or ''})
        disk['custom'] = incoming['custom']
    disk['exported'] = incoming.get('exported') or datetime.datetime.now().isoformat(timespec='seconds')
    with open(spath, 'w', encoding='utf-8') as fh:
        json.dump(disk, fh, ensure_ascii=False, indent=1)
    # ↑ 提交点。之后任何失败只记 warnings，不能把已落盘的保存报成失败
    try:
        if tpl_ok and incoming.get('template_css'):
            with open(os.path.join(repo, SYSTEM_CSS), 'w', encoding='utf-8') as fh:
                fh.write(incoming['template_css'])
            report['files'].append(SYSTEM_CSS)
    except OSError as exc:
        report['warnings'].append(f'template.css 没写进去（json 已保存，跑 build_template.js 补）：{exc}')
    report.update({'at': disk['exported'], 'repo': repo, 'rev': system_rev(repo)})
    try:
        write_receipt(repo, report)
    except Exception as exc:  # noqa: BLE001
        report['warnings'].append(f'回执没写进去：{exc}')
    return report


def _inside(repo, p):
    return os.path.realpath(p).startswith(os.path.realpath(repo) + os.sep)


def merge_and_write(repo, incoming):
    """按 id 合并页面提交的 spec 到磁盘 spec，写三文件与回执。返回报告。"""
    if incoming.get('kind') == 'system':
        return merge_system(repo, incoming)
    rel = (incoming.get('files') or {}).get('json')
    if not rel:
        raise ValueError('spec 里没有 files.json')
    spath = os.path.join(repo, rel)
    if not _inside(repo, spath):
        raise ValueError('越界路径')
    if not os.path.exists(spath):
        raise ValueError(f'磁盘上没有 {rel}')
    with open(spath, encoding='utf-8') as fh:
        disk = json.load(fh)

    dk = {n['id']: n for n in disk['nodes']}
    ik = {n['id']: n for n in incoming.get('nodes') or []}
    only_disk = sorted(set(dk) - set(ik))      # 页面没见过的新节点
    only_page = sorted(set(ik) - set(dk))      # 磁盘已删的节点
    stale = bool(only_disk or only_page)

    changes = []
    for nid, inode in ik.items():
        dnode = dk.get(nid)
        if dnode is None:
            continue
        if inode.get('value') != dnode.get('value'):
            changes.append({'id': nid, 'label': dnode.get('label'), 'from': dnode.get('value'),
                            'to': inode.get('value'), 'mod': dnode.get('mod'), 'g': dnode.get('g')})
            dnode['value'] = inode.get('value')

    dec_in = incoming.get('decisions') or {}
    disk['decisions'] = {k: v for k, v in dec_in.items() if k in dk}
    old_fb = disk.get('feedback') or []
    seen = {(f.get('at'), f.get('t')) for f in old_fb}
    new_fb = [f for f in (incoming.get('feedback') or []) if (f.get('at'), f.get('t')) not in seen]
    disk['feedback'] = old_fb + new_fb
    disk['exported'] = incoming.get('exported') or datetime.datetime.now().isoformat(timespec='seconds')
    counts = {}
    for v in disk['decisions'].values():
        counts[v.get('r') or '（无）'] = counts.get(v.get('r') or '（无）', 0) + 1
    disk['baseline'] = {'from': f"工作台服务保存 {disk['exported']}" + ('（页面结构过期→按 id 合并）' if stale else ''),
                        'changed': len(changes),
                        'changed_after': ' '.join(f"{c['id']}={c['to']}" for c in changes) or '无值改动',
                        'unsure': sorted(k for k, v in disk['decisions'].items() if v.get('r') == 'unsure'),
                        'stale_nodes_user_not_seen': only_disk}

    errs, warns = BT.validate(disk)
    if errs:
        raise ValueError('合并后 json 校验失败：' + '；'.join(errs[:5]))
    for k, text in (('json', json.dumps(disk, ensure_ascii=False, indent=1)),
                    ('js', BT.build_js(disk)), ('css', BT.build_css(disk))):
        p = os.path.join(repo, disk['files'][k])
        if not _inside(repo, p):
            raise ValueError('越界路径：' + disk['files'][k])
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, 'w', encoding='utf-8') as fh:
            fh.write(text)

    # 到这里三个文件已经落盘，这就是提交点。之后的任何失败都不准把请求报成失败
    report = {'at': disk['exported'], 'repo': repo, 'page': disk.get('page'), 'version': disk.get('version'),
              'stale': stale, 'nodes_user_not_seen': only_disk, 'nodes_removed': only_page,
              'changes': changes, 'decisions': counts, 'new_feedback': new_fb,
              'files': [disk['files'][k] for k in ('json', 'js', 'css')], 'warnings': warns}
    try:
        report['rev'] = pack_rev(repo, disk.get('page') or '')
    except Exception as exc:                        # noqa: BLE001 — 附加信息，坏了不能影响保存
        report['warnings'] = warns + [f'版本戳算不出来：{exc}']
    write_receipt(repo, report)
    return report


def gallery(repo):
    packs = find_packs(repo)
    rows = ''.join(
        f'<li><a href="{html.escape(p["html"][0]) if p["html"] else "#"}"><b>{html.escape(str(p["title"]))}</b>'
        f'<span>{html.escape(p["page"])} · {html.escape(str(p.get("version") or ""))} · {p["nodes"]} 节点 · {p["questions"]} 判断题'
        f' · {p["feedback"]} 反馈</span></a></li>' for p in packs)
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>设计工作台 · {html.escape(os.path.basename(repo))}</title><style>
body{{font:14px/1.6 system-ui,-apple-system,'PingFang SC',sans-serif;background:#F4F3EF;color:#1A1917;margin:0;padding:40px}}
.w{{max-width:720px;margin:0 auto}} h1{{font-size:20px;margin:0 0 4px}}
.sub{{font:12px ui-monospace,Menlo,monospace;color:#6B6A65;margin-bottom:24px}}
ul{{list-style:none;padding:0}} li{{margin:0 0 10px}}
a{{display:block;padding:14px 16px;background:#fff;border:1px solid #E4E2DC;border-radius:6px;text-decoration:none;color:inherit}}
a:hover{{border-color:#C65A1E}} b{{display:block;font-size:15px}}
span{{font:11.5px ui-monospace,Menlo,monospace;color:#6B6A65}}
.e{{color:#6B6A65}}</style></head><body><div class="w">
<h1>设计工作台</h1><div class="sub">{html.escape(repo)}</div>
{'<ul>' + rows + '</ul>' if packs else '<p class="e">这个仓库还没有页面包。用 forge-design-lab 的 adapt_tokens.py + scaffold_pack.py 建一个。</p>'}
</div></body></html>"""


# ======================================================================
# 路由（由 server.py 注册）
# ======================================================================
def register(router, repo, gallery_paths=('/', '/index.html')):
    repo = os.path.abspath(repo)

    def status(h, q):
        packs = find_packs(repo)
        return h.send_json(200, {'ok': True, 'repo': repo, 'repoName': os.path.basename(repo),
                                 'port': h.server.server_address[1], 'packs': len(packs),
                                 'revs': {p['page']: p['rev'] for p in packs}})

    def packs(h, q):
        return h.send_json(200, {'repo': repo, 'packs': find_packs(repo)})

    def srev(h, q):
        return h.send_json(200, {'rev': system_rev(repo)})

    def index(h, q):
        body = gallery(repo).encode('utf-8')
        h.send_response(200)
        h.send_header('Content-Type', 'text/html; charset=utf-8')
        h.send_header('Cache-Control', 'no-store')
        h.send_header('Content-Length', str(len(body)))
        h.end_headers()
        h.wfile.write(body)

    def save(h, body):
        try:
            report = merge_and_write(repo, body)
            return h.send_json(200, {'ok': True, **report})
        except StaleSave as e:
            return h.send_json(409, {'ok': False, 'stale': True, 'error': str(e)})
        except Exception as e:  # noqa: BLE001 — 回给页面显示
            return h.send_json(400, {'ok': False, 'error': str(e)})

    router.get('/api/lab/status', status)
    router.get('/api/lab/packs', packs)
    router.get('/api/lab/system-rev', srev)
    for p in gallery_paths:
        router.get(p, index)
    router.post('/api/lab/save', save)

    system_json = os.path.realpath(os.path.join(repo, SYSTEM_JSON))

    def static_headers(full, data):
        # 版本指纹与这次读到的字节同源：页面拿数据和拿指纹是同一次请求，中间不会被插入改动
        return {'X-Lab-Rev': hashlib.sha1(data).hexdigest()[:12]} if full == system_json else {}

    router.static_headers.append(static_headers)
