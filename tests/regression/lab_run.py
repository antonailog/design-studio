#!/usr/bin/env python3
"""design-studio 骨架模式回归：把用 forge-design-lab 的项目复制进临时沙盒，在沙盒里起工作台服务（旧 serve.py 或新工具），
逐条核对页面包的打开、保存（按 id 合并、三件同源、回执）、零件库、安全和命令行行为。

为什么要沙盒：用例会真的保存页面包。所有写操作只落在沙盒里；脚本开头和结尾各算一次真实项目
docs/assets/visuals/ 和 .forge/design-lab/ 的哈希，变了就报错（退出码 2）。

用法（design-studio 仓库根目录）：
  python3 tests/regression/lab_run.py --target old --project ../../项目/forge-demo-pomodoro
  python3 tests/regression/lab_run.py --target old --project ../../项目/info2action --copy docs --copy frontend-react/src
  python3 tests/regression/lab_run.py --target new ...（同上）
old 用 forge-design-lab skill 里的 scripts/serve.py（--skill 指定 skill 目录）；new 用本仓库的 design_studio.py lab。
退出码：0 全部通过（跳过不算失败）；1 有用例失败；2 环境问题或真实项目文件被改了。只用标准库；UI 冒烟要 python playwright。
"""
import argparse
import copy
import datetime
import hashlib
import http.client
import importlib.util
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path
from urllib.parse import quote

REPO = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
WORKSPACE = REPO.parent.parent
DEFAULT_SKILL = WORKSPACE / 'Skill' / 'forge-skills' / 'skills' / 'forge-design-lab'
VIS = 'docs/assets/visuals'
STATE = '.forge/design-lab'
SKIP_DIRS = {'.git', 'node_modules', '.worktrees', '__pycache__', '.venv', 'venv', 'dist'}
GUARDED = [VIS, STATE]


class SetupError(Exception):
    pass


def sha256(p):
    h = hashlib.sha256()
    with open(p, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def fingerprint(project):
    out = {}
    for rel in GUARDED:
        base = project / rel
        if base.is_dir():
            for f in sorted(base.rglob('*')):
                if f.is_file() and f.name != '.DS_Store':
                    out[f.relative_to(project).as_posix()] = sha256(f)
    return out


def build_sandbox(project, copies):
    root = Path(tempfile.mkdtemp(prefix='ds-lab-'))
    site = root / project.name
    if os.path.realpath(root).startswith(os.path.realpath(project) + os.sep):
        raise SetupError('临时目录落在真实项目里面，拒绝继续')
    n = 0
    for rel in copies or ['.']:
        src = project / rel
        for dirpath, dirnames, filenames in os.walk(src):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not os.path.islink(os.path.join(dirpath, d))]
            for fn in filenames:
                s = Path(dirpath) / fn
                if s.is_symlink() or fn == '.DS_Store':
                    continue
                d = site / s.relative_to(project)
                d.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(s, d)     # 只复制内容，不建硬链接
                if os.stat(s).st_ino == os.stat(d).st_ino:
                    raise SetupError(f'沙盒文件和原件是同一个 inode：{s}')
                n += 1
    if not (site / VIS).is_dir():
        raise SetupError(f'项目里没有 {VIS}')
    shutil.rmtree(site / STATE, ignore_errors=True)   # 回执从空开始，方便核对
    (site / '.env').write_text('SECRET=沙盒夹具\n', encoding='utf-8')   # 点开头文件不该被读到
    return root, site, n


def free_port():
    while True:
        with socket.socket() as s:
            s.bind(('127.0.0.1', 0))
            p = s.getsockname()[1]
        if not 4570 <= p <= 4610:
            return p


def port_open(port):
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(('127.0.0.1', port)) == 0


class Server:
    def __init__(self, cmd, site, port, timeout=20):
        self.cmd, self.port, self.logs = cmd, port, []
        self.proc = subprocess.Popen(cmd, cwd=str(site), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                     encoding='utf-8', errors='replace', start_new_session=True)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None and not port_open(port):
                raise SetupError(f'服务退出了（{self.proc.returncode}）：{self.proc.stdout.read()[-600:]}')
            if port_open(port):
                return
            time.sleep(0.1)
        raise SetupError(f'{timeout} 秒内服务没在 {port} 起来')

    def stop(self):
        try:
            os.killpg(self.proc.pid, signal.SIGTERM)
            self.proc.wait(5)
        except Exception:
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except Exception:
                pass


class Resp:
    def __init__(self, status, headers, body):
        self.status, self.headers, self.body = status, {k.lower(): v for k, v in headers}, body

    def json(self):
        return json.loads(self.body.decode('utf-8'))


def call(port, method, path, body=None, headers=None, raw=None):
    h = {'Host': f'127.0.0.1:{port}'}
    data = raw
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode('utf-8')
    if data is not None:
        h['Content-Type'] = 'application/json'
        h['Origin'] = f'http://127.0.0.1:{port}'
    h.update(headers or {})
    h = {k: v for k, v in h.items() if v is not None}
    c = http.client.HTTPConnection('127.0.0.1', port, timeout=30)
    try:
        c.request(method, quote(path, safe="/?=&%#:;,+@!$'()*[]~"), body=data, headers=h)
        r = c.getresponse()
        return Resp(r.status, r.getheaders(), r.read())
    finally:
        c.close()


CASES = []


def case(cid, name):
    def deco(fn):
        CASES.append((cid, name, fn))
        return fn
    return deco


class Case:
    def __init__(self, cid, name):
        self.id, self.name, self.evidence, self.failed, self.skipped = cid, name, [], False, None

    def ok(self, cond, what, got=None):
        line = ('✓ ' if cond else '✗ ') + what
        if got is not None:
            g = got if isinstance(got, str) else json.dumps(got, ensure_ascii=False)
            line += f'（实际：{g[:240]}）'
        self.evidence.append(line)
        if not cond:
            self.failed = True
        return bool(cond)

    def note(self, t):
        self.evidence.append('· ' + t)

    @property
    def status(self):
        return 'skip' if self.skipped else ('fail' if self.failed else 'pass')


def load_bt(skill):
    """用 skill 里的 build_tokens 独立算出期望的 js/css（不经过被测服务）。"""
    spec = importlib.util.spec_from_file_location('bt_ref', str(skill / 'scripts' / 'build_tokens.py'))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class Ctx:
    def __init__(self, site, port, args, bt):
        self.site, self.port, self.args, self.bt = site, port, args, bt
        self.packs = sorted(p.name for p in (site / VIS).iterdir()
                            if p.is_dir() and not p.name.startswith('_') and (p / f'{p.name}.tokens.json').is_file())
        self.pack = next((p for p in self.packs if p != 'system'), None)

    def get(self, path, **kw):
        return call(self.port, 'GET', path, **kw)

    def post(self, path, body=None, **kw):
        return call(self.port, 'POST', path, body, **kw)

    def spec_path(self, page):
        return self.site / VIS / page / f'{page}.tokens.json'

    def spec(self, page):
        return json.loads(self.spec_path(page).read_text(encoding='utf-8'))

    def state(self, name):
        p = self.site / STATE / name
        return p.read_text(encoding='utf-8') if p.is_file() else None


def bump(node):
    """给节点一个和原值不同、类型合法的新值。"""
    v = node.get('value')
    if isinstance(v, bool):
        return not v
    if isinstance(v, (int, float)):
        return v + 1
    opts = node.get('options') or node.get('enum') or []
    for o in opts:
        ov = o.get('v') if isinstance(o, dict) else o
        if ov != v:
            return ov
    return None


@case('L01', '根地址是页面包索引页，列出每个页面包')
def l_gallery(ctx, c):
    r = ctx.get('/')
    c.ok(r.status == 200 and 'text/html' in r.headers.get('content-type', ''), '/ → 200 HTML', r.status)
    body = r.body.decode('utf-8')
    for p in ctx.packs:
        c.ok(f'/{VIS}/{p}/' in body, f'列出页面包 {p}')
    r2 = ctx.get('/index.html')
    c.ok(r2.status == 200 and r2.body == r.body, '/index.html 同样是索引页')


@case('L02', '/api/lab/status 和 /api/lab/packs')
def l_status(ctx, c):
    s = ctx.get('/api/lab/status').json()
    c.ok(s.get('ok') is True and s.get('repoName') == ctx.site.name, 'ok、repoName = 项目目录名', {k: s.get(k) for k in ('ok', 'repoName')})
    c.ok(s.get('port') == ctx.port, 'port = 服务端口', s.get('port'))
    c.ok(sorted((s.get('revs') or {}).keys()) == ctx.packs and s.get('packs') == len(ctx.packs), f'revs 覆盖全部页面包 {ctx.packs}', s.get('revs'))
    p = ctx.get('/api/lab/packs').json()
    got = sorted(x['page'] for x in p.get('packs', []))
    c.ok(got == ctx.packs, 'packs 列出全部页面包', got)
    for x in p.get('packs', []):
        c.ok(isinstance(x.get('rev'), str) and len(x['rev']) == 12, f'{x["page"]} 有 12 位 rev', x.get('rev'))
        c.ok(x.get('rev') == s['revs'].get(x['page']), f'{x["page"]} 的 rev 和 status 里的一致')


@case('L03', '页面包能打开：骨架 HTML、引擎、tokens.js、跨目录引用的前端 css 都 200')
def l_open(ctx, c):
    p = ctx.get('/api/lab/packs').json()
    for x in p.get('packs', []):
        for h in x.get('html') or []:
            r = ctx.get(h)
            c.ok(r.status == 200, f'{h} → 200', r.status)
            base = h.rsplit('/', 1)[0]
            for ref in re.findall(r'(?:src|href)="([^"#?:]+\.(?:js|css))"', r.body.decode('utf-8', 'replace')):
                url = os.path.normpath(base + '/' + ref).replace(os.sep, '/')
                r2 = ctx.get(url)
                c.ok(r2.status == 200, f'  引用 {ref} → 200', r2.status)


@case('L04', '保存：改一个节点的值 → 三件直写且同源，回执 last-save.json 和 saves.jsonl')
def l_save(ctx, c):
    if not ctx.pack:
        return setattr(c, 'skipped', '没有普通页面包')
    spec = ctx.spec(ctx.pack)
    node = next((n for n in spec['nodes'] if bump(n) is not None), None)
    if not node:
        return setattr(c, 'skipped', '找不到能改值的节点')
    inc = copy.deepcopy(spec)
    new = bump(node)
    next(n for n in inc['nodes'] if n['id'] == node['id'])['value'] = new
    r = ctx.post('/api/lab/save', inc)
    c.ok(r.status == 200, '200', r.body[:200].decode('utf-8', 'replace'))
    j = r.json()
    c.ok(j.get('ok') is True and j.get('stale') is False, 'ok，没过期', {k: j.get(k) for k in ('ok', 'stale')})
    c.ok([x['id'] for x in j.get('changes', [])] == [node['id']] and j['changes'][0]['to'] == new, f'changes 只有 {node["id"]}', j.get('changes'))
    c.ok(j.get('files') == [spec['files'][k] for k in ('json', 'js', 'css')], 'files 是 json / js / css 三件', j.get('files'))
    disk = ctx.spec(ctx.pack)
    c.ok(next(n for n in disk['nodes'] if n['id'] == node['id'])['value'] == new, 'json 里该节点 = 新值')
    js = (ctx.site / spec['files']['js']).read_text(encoding='utf-8')
    css = (ctx.site / spec['files']['css']).read_text(encoding='utf-8')
    c.ok(js == ctx.bt.build_js(disk) and css == ctx.bt.build_css(disk), 'js / css 和 build_tokens 从 json 生成的逐字节一致（三件同源）')
    c.ok(disk.get('baseline', {}).get('changed') == 1, 'baseline.changed = 1', disk.get('baseline'))
    last = json.loads(ctx.state('last-save.json') or '{}')
    c.ok(last.get('changes') == j.get('changes') and last.get('page') == ctx.pack, 'last-save.json 和回执一致')
    lines = [l for l in (ctx.state('saves.jsonl') or '').splitlines() if l.strip()]
    c.ok(len(lines) >= 1 and json.loads(lines[-1]).get('changes') == j.get('changes'), 'saves.jsonl 追加了一行', len(lines))
    s = ctx.get('/api/lab/status').json()
    c.ok(j.get('rev') == s['revs'].get(ctx.pack), '回执 rev = 保存后的版本戳（页面靠它不自我重载）', [j.get('rev'), s['revs'].get(ctx.pack)])
    r = ctx.post('/api/lab/save', inc)
    c.ok(r.status == 200 and r.json().get('changes') == [] and r.json().get('ok') is True, '同样内容再存一次：ok、无值改动')
    lines2 = [l for l in (ctx.state('saves.jsonl') or '').splitlines() if l.strip()]
    c.ok(len(lines2) == len(lines) + 1, 'saves.jsonl 又追加一行（流水不丢）', len(lines2))


@case('L05', '按 id 合并：页面没见过的节点保留、页面多出的节点丢弃，回执里点名')
def l_merge(ctx, c):
    if not ctx.pack:
        return setattr(c, 'skipped', '没有普通页面包')
    spec = ctx.spec(ctx.pack)
    if len(spec['nodes']) < 2:
        return setattr(c, 'skipped', '节点太少')
    inc = copy.deepcopy(spec)
    hidden = inc['nodes'].pop(0)
    ghost = copy.deepcopy(inc['nodes'][0])
    ghost['id'] = 'ds.ghost-node'
    inc['nodes'].append(ghost)
    inc['decisions'] = {inc['nodes'][0]['id']: {'r': 'unsure'}, 'ds.ghost-node': {'r': 'ok'}}
    old_fb = spec.get('feedback') or []
    inc['feedback'] = old_fb + [{'at': '2026-10-23T03:00:00', 't': '回归：一条新反馈'}]
    j = ctx.post('/api/lab/save', inc).json()
    c.ok(j.get('ok') is True and j.get('stale') is True, 'ok，stale = true', {k: j.get(k) for k in ('ok', 'stale')})
    c.ok(j.get('nodes_user_not_seen') == [hidden['id']], f'nodes_user_not_seen = [{hidden["id"]}]', j.get('nodes_user_not_seen'))
    c.ok(j.get('nodes_removed') == ['ds.ghost-node'], 'nodes_removed = [ds.ghost-node]', j.get('nodes_removed'))
    disk = ctx.spec(ctx.pack)
    ids = [n['id'] for n in disk['nodes']]
    c.ok(hidden['id'] in ids and 'ds.ghost-node' not in ids, '磁盘上：没见过的节点还在，多出的节点没写进去')
    c.ok(set(disk.get('decisions', {})) == {inc['nodes'][0]['id']}, 'decisions 只留磁盘上有的节点', disk.get('decisions'))
    c.ok(len(disk.get('feedback') or []) == len(old_fb) + 1 and j.get('new_feedback') == [inc['feedback'][-1]], '反馈追加 1 条，回执 new_feedback 对得上')


@case('L06', '保存的错误：缺 files.json、路径越界、文件不存在 → 400 ok:false，不落盘')
def l_bad(ctx, c):
    before = fingerprint(ctx.site)
    for label, body in (('缺 files.json', {'nodes': []}), ('越界', {'files': {'json': '../escape.tokens.json'}, 'nodes': []}),
                        ('不存在', {'files': {'json': f'{VIS}/没有/没有.tokens.json'}, 'nodes': []})):
        r = ctx.post('/api/lab/save', body)
        c.ok(r.status == 400 and r.json().get('ok') is False and r.json().get('error'), f'{label} → 400 ok:false', r.body[:120].decode('utf-8', 'replace'))
    # 注：合并时结构取磁盘、只取页面的值，build_tokens.validate 又只查结构，所以「合并后校验不过」从接口上触发不了，不测
    c.ok(fingerprint(ctx.site) == before, '页面包和回执都没被改动')


@case('L07', '安全：Host、Origin（含端口不同）、非 JSON、点开头和越界路径')
def l_security(ctx, c):
    spec = ctx.spec(ctx.pack) if ctx.pack else {'files': {'json': 'x'}, 'nodes': []}
    r = ctx.get('/api/lab/status', headers={'Host': 'evil.example'})
    c.ok(r.status == 403, 'Host=evil.example → 403', r.status)
    for origin in ('http://evil.example', f'http://127.0.0.1:{ctx.port + 1}', 'null'):
        r = ctx.post('/api/lab/save', spec, headers={'Origin': origin})
        c.ok(r.status == 403, f'POST Origin={origin} → 403', r.status)
    r = call(ctx.port, 'POST', '/api/lab/save', raw=json.dumps(spec).encode(), headers={'Content-Type': 'text/plain'})
    c.ok(r.status == 415, 'POST text/plain → 415', r.status)
    for p in ('/.env', f'/{STATE}/last-save.json', '/.git/config', '/../outside.txt', '/%2e%2e/%2e%2e/etc/hosts'):
        r = ctx.get(p)
        c.ok(r.status == 404 and b'SECRET' not in r.body, f'GET {p} → 404', r.status)
    r = ctx.post('/api/没有这个', {'a': 1})
    c.ok(r.status == 404, 'POST 未知接口 → 404', r.status)


@case('L08', '零件库：读 system.tokens.json 带 X-Lab-Rev；页面过期保存 → 409；改模板 → 200 并列出改动')
def l_system(ctx, c):
    sj = ctx.site / VIS / 'system' / 'system.tokens.json'
    if not sj.is_file():
        return setattr(c, 'skipped', '这个项目没有零件库')
    r = ctx.get(f'/{VIS}/system/system.tokens.json')
    rev = ctx.get('/api/lab/system-rev').json().get('rev')
    c.ok(r.status == 200 and r.headers.get('x-lab-rev') == rev and len(rev or '') == 12, 'GET 零件库 json → X-Lab-Rev = /api/lab/system-rev', [r.headers.get('x-lab-rev'), rev])
    disk = json.loads(sj.read_text(encoding='utf-8'))
    r = ctx.post('/api/lab/save', {'kind': 'system', 'rev': 'aaaaaaaaaaaa', 'feedback': []})
    c.ok(r.status == 409 and r.json().get('stale') is True, 'rev 对不上 → 409 stale', r.status)
    tpl = copy.deepcopy(disk.get('template') or {})
    if not tpl.get('colors'):
        return c.note('零件库没有 template.colors，模板改动跳过')
    first = tpl['colors'][0]
    key = next((k for k in ('value', 'hex', 'v') if k in first), None)
    first[key or 'note'] = '#123456' if key else '回归'
    r = ctx.post('/api/lab/save', {'kind': 'system', 'rev': rev, 'template': tpl, 'template_css': ':root{--ds-regress:1}\n'})
    j = r.json()
    c.ok(r.status == 200 and j.get('ok') is True and j.get('template_changes'), '改模板 → 200，template_changes 非空', r.body[:200].decode('utf-8', 'replace'))
    c.ok(json.loads(sj.read_text(encoding='utf-8')).get('template') == tpl, 'json 里的 template 换成新值')
    tcss = ctx.site / VIS / 'system' / 'template.css'
    c.ok(tcss.read_text(encoding='utf-8') == ':root{--ds-regress:1}\n', 'template.css 写进去了')
    c.ok(j.get('rev') == ctx.get('/api/lab/system-rev').json().get('rev'), '回执带新的 rev')


@case('L09', '命令行：已在跑时不重复启动；--status；--stop 停掉并清掉 server.json')
def l_cli(ctx, c):
    cmd = ctx.args.cli
    out = subprocess.run(cmd + [str(ctx.site)], capture_output=True, text=True, timeout=20)
    c.ok('已经在跑' in out.stdout, '再启动一次：提示「已经在跑」，不重复起', out.stdout.strip()[:160])
    out = subprocess.run(cmd + [str(ctx.site), '--status'], capture_output=True, text=True, timeout=20)
    c.ok('在跑' in out.stdout and str(ctx.port) in out.stdout, '--status：在跑，带端口', out.stdout.strip()[:160])
    sf = ctx.site / STATE / 'server.json'
    c.ok(sf.is_file() and json.loads(sf.read_text()).get('port') == ctx.port, 'server.json 记着端口')
    out = subprocess.run(cmd + [str(ctx.site), '--stop'], capture_output=True, text=True, timeout=20)
    time.sleep(0.8)
    c.ok('已停止' in out.stdout and not port_open(ctx.port), '--stop：已停止，端口关了', out.stdout.strip()[:160])
    c.ok(not sf.exists(), 'server.json 清掉了')


def u_smoke(ctx, c):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return setattr(c, 'skipped', '没装 python playwright')
    if not ctx.pack:
        return setattr(c, 'skipped', '没有普通页面包')
    html = ctx.get('/api/lab/packs').json()['packs']
    html = next(x['html'][0] for x in html if x['page'] == ctx.pack)
    with sync_playwright() as p:
        b = p.chromium.launch()
        cx = b.new_context(viewport={'width': 1440, 'height': 900})
        cx.route(re.compile(r'^(?!https?://(127\.0\.0\.1|localhost)[:/])'), lambda r: r.abort())
        pg = cx.new_page()
        errs = []
        pg.on('pageerror', lambda e: errs.append(str(e)[:160]))
        pg.goto(f'http://127.0.0.1:{ctx.port}{quote(html)}', wait_until='domcontentloaded')
        pg.wait_for_function('window.WB && window.WB.spec', timeout=10000)
        c.ok(pg.evaluate('WB.spec.page') == ctx.pack, f'引擎载入了页面包 {ctx.pack}')
        n0 = len([l for l in (ctx.state('saves.jsonl') or '').splitlines() if l.strip()])
        pg.click('#wb-save')
        pg.wait_for_function("document.querySelector('#wb-savestat').textContent.includes('服务直写')", timeout=10000)
        c.ok(True, '点「保存」→ 状态「已保存 … 服务直写」', pg.text_content('#wb-savestat'))
        n1 = len([l for l in (ctx.state('saves.jsonl') or '').splitlines() if l.strip()])
        c.ok(n1 == n0 + 1, 'saves.jsonl 多了一行', [n0, n1])
        c.ok(not errs, '页面脚本零报错', errs)
        b.close()


def main():
    ap = argparse.ArgumentParser(description='design-studio 骨架模式回归')
    ap.add_argument('--target', choices=['old', 'new'], required=True)
    ap.add_argument('--project', required=True, help='用 design-lab 的真实项目目录（只读）')
    ap.add_argument('--copy', action='append', default=[], help='只复制这些相对路径进沙盒（默认整个项目，跳过 .git、node_modules 等）')
    ap.add_argument('--skill', default=str(DEFAULT_SKILL), help='forge-design-lab skill 目录（old 用它的 serve.py；期望值用它的 build_tokens）')
    ap.add_argument('--no-ui', action='store_true')
    ap.add_argument('--keep', action='store_true')
    a = ap.parse_args()
    project, skill = Path(a.project).resolve(), Path(a.skill).resolve()
    if a.target == 'old':
        a.cli = [sys.executable, str(skill / 'scripts' / 'serve.py')]
    else:
        a.cli = [sys.executable, '-I', str(REPO / 'design_studio.py'), 'lab']
    started = datetime.datetime.now()
    print(f'骨架模式回归 · 目标 {a.target} · 项目 {project.name} · {started:%Y-%m-%d %H:%M:%S}')
    before = fingerprint(project)
    root = server = None
    results, setup_error = [], None
    try:
        root, site, n = build_sandbox(project, a.copy)
        print(f'沙盒：{site}（{n} 个文件）')
        port = free_port()
        server = Server(a.cli + [str(site), '--port', str(port)], site, port)
        print(f'服务：{" ".join(a.cli)} <沙盒> --port {port}\n')
        ctx = Ctx(site, port, a, load_bt(skill))
        cases = list(CASES)
        if not a.no_ui:
            cases.insert(-1, ('U01', 'UI：页面包载入引擎，点「保存」走服务直写', u_smoke))
        for cid, name, fn in cases:
            c = Case(cid, name)
            try:
                fn(ctx, c)
            except Exception:
                c.failed = True
                c.evidence.append('✗ 用例抛异常：' + traceback.format_exc(limit=3).strip().replace('\n', ' | ')[-500:])
            results.append(c)
            print(f'{ {"pass": "✓", "fail": "✗", "skip": "-"}[c.status] } {c.id} {c.name}  [{ {"pass": "通过", "fail": "失败", "skip": "跳过"}[c.status] }]')
            if c.status != 'pass':
                for l in c.evidence:
                    print('    ' + l)
                if c.skipped:
                    print('    - ' + c.skipped)
    except SetupError as e:
        setup_error = str(e)
        print(f'✗ 环境问题：{e}')
    finally:
        if server:
            server.stop()
    after = fingerprint(project)
    changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
    cnt = {k: sum(1 for c in results if c.status == k) for k in ('pass', 'fail', 'skip')}
    out = HERE / 'results' / f'lab-{a.target}-{project.name}-{started:%Y%m%d-%H%M%S}.json'
    out.parent.mkdir(exist_ok=True)
    home = str(Path.home())
    clean = lambda s: s.replace(str(root), '<沙盒>') .replace(str(project), '<项目>').replace(home, '~') if root else s.replace(home, '~')
    out.write_text(json.dumps({'target': a.target, 'project': project.name, 'started': started.isoformat(timespec='seconds'),
                               'summary': {'cases': len(results), **cnt}, 'integrity': {'unchanged': not changed, 'changed': changed},
                               'setup_error': setup_error,
                               'cases': [{'id': c.id, 'name': c.name, 'status': c.status, 'evidence': [clean(x) for x in c.evidence],
                                          **({'skip': c.skipped} if c.skipped else {})} for c in results]},
                              ensure_ascii=False, indent=1) + '\n', encoding='utf-8')
    if root and not a.keep:
        shutil.rmtree(root, ignore_errors=True)
    print(f'\n共 {len(results)} 条：通过 {cnt["pass"]}，失败 {cnt["fail"]}，跳过 {cnt["skip"]}')
    print('✗ 真实项目文件变了：' + '、'.join(changed) if changed else f'✓ 真实项目自检：{len(before)} 个受保护文件运行前后哈希一致')
    print(f'结果：{out}')
    if setup_error or changed:
        return 2
    return 1 if cnt['fail'] else 0


if __name__ == '__main__':
    sys.exit(main())
