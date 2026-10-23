#!/usr/bin/env python3
"""design-studio 回归脚本：把博客复制进临时沙盒，在沙盒里起编辑服务（旧服务或新工具），逐条跑兼容用例。

为什么要沙盒：用例会真的保存文字、写修改单、写反馈，所有写操作只能落在沙盒里。
脚本开头和结尾各算一次真实博客 site.js 与 0-记录/修改单/ 的哈希，变了就报错（退出码 2）。

用法（在 design-studio 仓库根目录）：
  python3 tests/regression/run.py --target old
  python3 tests/regression/run.py --target new --cmd "{python} -I {repo}/<入口>.py serve {site} --port {port}"

命令模板的占位：{site} 沙盒里的博客根目录（相当于 内容/个人博客），{port} 本次分到的空闲端口，
{python} 跑本脚本的 Python，{repo} design-studio 仓库根目录。模板先按空格拆词再替换，路径里有空格也不会拆坏。

退出码：0 全部通过（已知差异、跳过不算失败）；1 有用例失败；2 沙盒没建成、服务没起来、或真实博客文件被改了。
只用 Python 标准库；UI 冒烟（U 开头）要已安装的 python playwright，没有就跳过并在结果里标注。
"""
import argparse
import datetime
import difflib
import hashlib
import http.client
import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

REPO = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
DEFAULT_BLOG = REPO.parent.parent / '内容' / '个人博客'   # 工作区/工具/design-studio → 工作区/内容/个人博客

# ---------- 被测的博客结构（都相对博客根目录） ----------
PAGES_DIR = '4-页面'                       # 静态文件根
SHEET_DIR = '0-记录/修改单'                 # 修改单目录
FB_DIR = '0-记录/修改单/反馈'               # 调整单反馈
HOME = '首页'                              # 登记的页面名
SITE_JS = '4-页面/首页/首页-v3/data/site.js'
SHEET = '0-记录/修改单/首页.json'
HOME_HTML = '4-页面/首页/首页-v3/v3-7.html'
RANGE_VIDEO = '4-页面/头像跟随/avatar.mp4'  # 首屏 3D 头像靠它跳帧，必须支持 Range
OLD_SERVICE = '99-其他/脚本/编辑服务.py'
SCRIPTS = ['99-其他/脚本/编辑服务.py', '99-其他/脚本/脱敏扫描.py', '99-其他/脚本/脱敏扫描-白名单.txt']
OPTIONAL = ['design-studio.json']          # 新工具的站点配置（暂定名），博客里有就一起带进沙盒
GUARDED = [SITE_JS, SHEET_DIR]             # 真实博客里要守住的：运行前后哈希必须一致

# 复制 4-页面 时：截图目录不要；视频、动图、图片换成小的假文件（RANGE_VIDEO 保留原样），--full-media 时全部照抄
SKIP_DIRS = {'_shots', '__pycache__', 'node_modules'}
SKIP_FILES = {'.DS_Store'}
MEDIA = {'.mp4', '.webm', '.mov', '.m4v', '.gif', '.png', '.jpg', '.jpeg', '.webp', '.avif'}
PLACEHOLDER = b'design-studio regression placeholder\n'

# 测试数据：手机号和 IP 运行时拼出来，免得本文件和结果文件被脱敏扫描命中
FAKE_PHONE = '138' + '0013' + '8000'
FAKE_IP = '.'.join(['10', '23', '45', '67'])
FAKE_LAN = '.'.join(['192', '168', '1', '10'])
MASKS = {FAKE_PHONE: '<测试手机号>', FAKE_IP: '<测试IP>', FAKE_LAN: '<测试内网IP>'}


def mask(s):
    for k, v in MASKS.items():
        s = s.replace(k, v)
    return s

# 挑「数据里唯一的一句话」时跳过的字段（链接、路径、编号等不显示的东西；和旧服务 SKIP_LEAF 同口径）
NON_TEXT_KEYS = {'id', 'url', 'src', 'href', 'poster', 'cover', 'image', 'qr', 'page', 'page_base', 'avatar', 'avatar_note',
                 'domain', 'kind', 'type', 'repo', 'tag', 'alt'}
TEXT_SECTIONS = {'tools': '小工具', 'projects': '项目', 'articles': '文章', 'skills': 'Skill', 'now': '近况',
                 'milestones': '近况', 'stats': '作品'}   # 只在这些顶层字段里挑，避开会触发「待 AI 跟进」的 person / picks
ID_RE = re.compile(r'^\d{8}-\d{3}$')
TIME_RE = re.compile(r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$')


class SetupError(Exception):
    """沙盒、服务、自检这类环境问题：直接退出码 2。"""


# ======================================================================
# 沙盒
# ======================================================================
def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def fingerprint(blog):
    """真实博客里要守住的文件 → 哈希。目录按递归列出的每个文件算。"""
    out = {}
    for rel in GUARDED:
        p = blog / rel
        if p.is_file():
            out[rel] = sha256(p)
        elif p.is_dir():
            for f in sorted(p.rglob('*')):
                if f.is_file() and f.name not in SKIP_FILES:
                    out[f.relative_to(blog).as_posix()] = sha256(f)
        else:
            out[rel] = None
    return out


def build_sandbox(blog, full_media=False, extra=()):
    """把博客复制进临时目录，返回 (临时根, 沙盒博客根, 统计)。只复制文件内容，绝不建硬链接或符号链接。"""
    root = Path(tempfile.mkdtemp(prefix='ds-regress-'))
    site = root / '个人博客'
    real_blog = os.path.realpath(blog)
    if os.path.realpath(root).startswith(real_blog + os.sep):
        raise SetupError('临时目录落在真实博客里面，拒绝继续')
    stats = {'files': 0, 'bytes': 0, 'placeholders': 0, 'skipped_links': 0, 'skipped_dirs': 0}

    def copy_file(src, rel):
        dst = site / rel
        if src.is_symlink():          # 博客里的符号链接不带进来：照抄会指回真实文件
            stats['skipped_links'] += 1
            return
        dst.parent.mkdir(parents=True, exist_ok=True)
        if not full_media and src.suffix.lower() in MEDIA and rel != RANGE_VIDEO:
            dst.write_bytes(PLACEHOLDER)
            stats['placeholders'] += 1
        else:
            shutil.copyfile(src, dst)   # 复制内容；不是硬链接，写沙盒不会碰到原件
            s, d = src.stat(), dst.stat()
            if (s.st_dev, s.st_ino) == (d.st_dev, d.st_ino):
                raise SetupError(f'沙盒文件和原件是同一个 inode：{rel}')
            stats['bytes'] += d.st_size
        stats['files'] += 1

    def copy_tree(rel_dir):
        base = blog / rel_dir
        if not base.is_dir():
            raise SetupError(f'博客里没有 {rel_dir}')
        for dirpath, dirnames, filenames in os.walk(base):   # 默认不跟随符号链接
            keep = []
            for d in dirnames:
                if d in SKIP_DIRS or os.path.islink(os.path.join(dirpath, d)):
                    stats['skipped_dirs'] += 1
                else:
                    keep.append(d)
            dirnames[:] = keep
            for fn in filenames:
                if fn in SKIP_FILES or fn.endswith('.tmp'):
                    continue
                src = Path(dirpath) / fn
                copy_file(src, src.relative_to(blog).as_posix())

    copy_tree(PAGES_DIR)
    copy_tree(SHEET_DIR)
    for rel in SCRIPTS:
        if not (blog / rel).is_file():
            raise SetupError(f'博客里没有 {rel}')
        copy_file(blog / rel, rel)
    for rel in list(OPTIONAL) + list(extra):
        p = blog / rel
        if p.is_file():
            copy_file(p, rel)
        elif p.is_dir():
            copy_tree(rel)
    if not (site / RANGE_VIDEO).is_file() or (site / RANGE_VIDEO).stat().st_size < 4096:
        raise SetupError(f'沙盒里缺少能测 Range 的视频：{RANGE_VIDEO}')

    # 沙盒专用的安全测试夹具（真实博客里没有）：点开头的文件和目录、指向静态根外面的符号链接
    (site / PAGES_DIR / '.ds-hidden.txt').write_text('沙盒夹具：点开头的文件\n', encoding='utf-8')
    (site / PAGES_DIR / '首页' / '.ds-secret').mkdir(parents=True, exist_ok=True)
    (site / PAGES_DIR / '首页' / '.ds-secret' / 'x.txt').write_text('沙盒夹具：点开头的目录\n', encoding='utf-8')
    os.symlink('../0-记录', site / PAGES_DIR / '_ds-link-out')
    return root, site, stats


# ======================================================================
# 服务
# ======================================================================
def free_port():
    while True:
        with socket.socket() as s:
            s.bind(('127.0.0.1', 0))
            port = s.getsockname()[1]
        if port != 4580:   # 4580 留给用户真开着的编辑服务
            return port


def port_open(port):
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(('127.0.0.1', port)) == 0


class Server:
    def __init__(self, target, cmd_template, site, port, timeout):
        if target == 'old':
            self.cmd = [sys.executable, '-I', str(site / OLD_SERVICE), '--port', str(port)]
        else:
            fill = lambda t: (t.replace('{site}', str(site)).replace('{port}', str(port))
                              .replace('{python}', sys.executable).replace('{repo}', str(REPO)))
            self.cmd = [fill(t) for t in shlex.split(cmd_template)]
        self.target, self.port, self.logs = target, port, []
        self.proc = subprocess.Popen(self.cmd, cwd=str(site), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     text=True, encoding='utf-8', errors='replace', start_new_session=True)
        printed = threading.Event()
        found = {}

        def pump(stream, tag):
            for line in stream:
                self.logs.append(f'{tag}: {line.rstrip()}')
                m = re.search(r'http://127\.0\.0\.1:(\d+)/', line)
                if m and 'port' not in found:
                    found['port'] = int(m.group(1))
                    printed.set()
        for stream, tag in ((self.proc.stdout, 'out'), (self.proc.stderr, 'err')):
            threading.Thread(target=pump, args=(stream, tag), daemon=True).start()

        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                time.sleep(0.2)
                raise SetupError(f'服务启动后退出了（退出码 {self.proc.returncode}）：' + ' | '.join(self.logs[-8:]))
            # 旧服务端口被占会自己 +1，以它打印的地址为准
            if target == 'old' and printed.is_set():
                self.port = found['port']
            if (target != 'old' or printed.is_set()) and port_open(self.port):
                return
            time.sleep(0.1)
        self.stop()
        raise SetupError(f'{timeout} 秒内服务没在 {self.port} 端口起来：' + ' | '.join(self.logs[-8:]))

    def stop(self):
        if self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
                self.proc.wait(5)
            except Exception:
                try:
                    os.killpg(self.proc.pid, signal.SIGKILL)
                except Exception:
                    pass


# ======================================================================
# HTTP
# ======================================================================
class Resp:
    def __init__(self, status, headers, body):
        self.status, self.headers, self.body = status, headers, body

    def header(self, name):
        return self.headers.get(name.lower())

    def json(self):
        return json.loads(self.body.decode('utf-8'))

    def brief(self):
        t = self.body[:160].decode('utf-8', 'replace').replace('\n', ' ')
        return f'{self.status} {t}'


def enc(path):
    """只把非 ASCII 字符编码，/ . % ? 等原样保留（穿越用例要发原样的 ../ 和 %2e%2e）。"""
    return quote(path, safe="/?=&%#:;,+@!$'()*[]~")


def call(port, method, path, body=None, headers=None, timeout=30):
    h = {'Host': f'127.0.0.1:{port}'}
    data = None
    if body is not None:
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode('utf-8')
        h['Content-Type'] = 'application/json'
        h['Origin'] = f'http://127.0.0.1:{port}'   # 浏览器 fetch POST 都会带 Origin
    h.update(headers or {})
    h = {k: v for k, v in h.items() if v is not None}   # 值为 None 表示这个头不发
    conn = http.client.HTTPConnection('127.0.0.1', port, timeout=timeout)
    try:
        conn.request(method, enc(path), body=data, headers=h)
        r = conn.getresponse()
        return Resp(r.status, {k.lower(): v for k, v in r.getheaders()}, r.read())
    finally:
        conn.close()


# ======================================================================
# 用例框架
# ======================================================================
CASES = []


def case(cid, name):
    def deco(fn):
        CASES.append((cid, name, fn))
        return fn
    return deco


class Case:
    def __init__(self, cid, name):
        self.id, self.name = cid, name
        self.evidence, self.failed, self.known, self.skipped = [], False, None, None

    def ok(self, cond, what, got=None):
        line = ('✓ ' if cond else '✗ ') + what
        if got is not None:
            g = mask(got if isinstance(got, str) else json.dumps(got, ensure_ascii=False))   # 先打码再截断
            line += f'（实际：{g[:300]}{"…" if len(g) > 300 else ""}）'
        self.evidence.append(line)
        if not cond:
            self.failed = True
        return bool(cond)

    def note(self, text):
        self.evidence.append('· ' + text)

    def skip(self, reason):
        self.skipped = reason

    @property
    def status(self):
        if self.skipped:
            return 'skip'
        if self.failed:
            return 'known' if self.known else 'fail'
        return 'pass'


class Ctx:
    """用例之间共享的东西：沙盒路径、端口、写过几条修改单、挑好的原句。"""
    def __init__(self, site, port, target):
        self.site, self.port, self.target = site, port, target
        self.initial_sheet = json.loads((site / SHEET).read_text(encoding='utf-8'))
        self.saved_ids = []          # 本次运行写进修改单的条目 id（按顺序）
        self.candidates = pick_candidates(site)
        self.used = set()            # 已经被用例改过的字段
        self.write = None            # R06 的写回前后原文，给 R07 用

    def get(self, path, **kw):
        return call(self.port, 'GET', path, **kw)

    def post(self, path, body, **kw):
        return call(self.port, 'POST', path, body=body, **kw)

    def read(self, rel):
        return (self.site / rel).read_text(encoding='utf-8')

    def sheet(self):
        return json.loads(self.read(SHEET))

    def take(self):
        """取一句还没被改过的唯一原句：(字段路径, 原文, 板块名)。"""
        for path, text, sec in self.candidates:
            if path not in self.used:
                self.used.add(path)
                return path, text, sec
        raise AssertionError('site.js 里能用的唯一原句用完了')


# ---------- site.js 读写辅助 ----------
def split_js(raw):
    i, j = raw.index('{'), raw.rindex('}') + 1
    return raw[:i], json.loads(raw[i:j]), raw[j:]


def leaves(o, path=()):
    if isinstance(o, dict):
        for k, v in o.items():
            yield from leaves(v, path + (k,))
    elif isinstance(o, list):
        for i, v in enumerate(o):
            yield from leaves(v, path + (i,))
    elif isinstance(o, str):
        yield path, o


def dotted(path):
    return '.'.join(str(p) for p in path)


def get_in(o, path):
    for p in path:
        o = o[p]
    return o


def set_in(o, path, value):
    for p in path[:-1]:
        o = o[p]
    o[path[-1]] = value


def pick_candidates(site):
    """挑 site.js 里「只出现一次、也不是别的字段的一部分」的普通文字，按出现顺序返回。
    同时要求它不出现在首页 HTML 里（新工具会尝试 HTML 写回，避免两边都对得上）。"""
    raw = (site / SITE_JS).read_text(encoding='utf-8')
    html = (site / HOME_HTML).read_text(encoding='utf-8')
    _, data, _ = split_js(raw)
    all_strings = [v for _, v in leaves(data)]
    out = []
    for path, v in leaves(data):
        top, key = path[0], next((p for p in reversed(path) if isinstance(p, str)), '')
        if top not in TEXT_SECTIONS or key in NON_TEXT_KEYS or str(key).startswith('_'):
            continue
        if any(str(p).startswith('_') for p in path):
            continue
        if not (4 <= len(v) <= 40) or re.search(r'[<>&\n/\\]|://|\d{3,}', v) or v != v.strip():
            continue
        if all_strings.count(v) != 1 or sum(1 for s in all_strings if v in s) != 1:
            continue
        if raw.count(json.dumps(v, ensure_ascii=False)) != 1 or v in html:
            continue
        out.append((path, v, TEXT_SECTIONS[top]))
    return out


def where(section, label, selector='#ds-regression-only', tag='p'):
    return {'section': section, 'label': f'{section} · {label}', 'num': None, 'selector': selector, 'classes': '', 'tag': tag}


def text_item(old, new, section, label='回归测试'):
    esc = lambda s: s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')   # 和 editor.js cleanHtml 一样转义
    return {'type': '文字', 'key': 'text|#ds-regression-only', 'where': where(section, label), 'hint': None,
            'oldText': old, 'oldHtml': esc(old), 'newText': new, 'newHtml': esc(new)}


def check_saved_record(c, ctx, resp_rec, expect_type, expect_status):
    """保存接口回的条目和修改单文件里最后几条要一致，id、时间格式对。"""
    c.ok(resp_rec.get('type') == expect_type, f'条目类型 = {expect_type}', resp_rec.get('type'))
    c.ok(resp_rec.get('status') == expect_status, f'状态 = {expect_status}', resp_rec.get('status'))
    c.ok(bool(ID_RE.match(str(resp_rec.get('id', '')))), 'id 形如 YYYYMMDD-NNN', resp_rec.get('id'))
    c.ok(bool(TIME_RE.match(str(resp_rec.get('time', '')))), 'time 形如 YYYY-MM-DD HH:MM', resp_rec.get('time'))
    on_disk = next((x for x in ctx.sheet()['items'] if x.get('id') == resp_rec.get('id')), None)
    c.ok(on_disk == resp_rec, '修改单文件里有同一条，内容和接口回的一致', on_disk)
    ctx.saved_ids.append(resp_rec.get('id'))


def save(ctx, items, page=HOME):
    return ctx.post('/api/save', {'page': page, 'items': items})


# ======================================================================
# 用例：地址与静态文件
# ======================================================================
@case('R01', '根地址 302 跳首页，Location 编码正确')
def t_root(ctx, c):
    r = ctx.get('/')
    c.ok(r.status == 302, '状态 302', r.status)
    loc = r.header('Location') or ''
    c.ok(bool(loc) and all(ord(ch) < 128 for ch in loc), 'Location 只含 ASCII（中文按 UTF-8 百分号编码）', loc)
    path = unquote(urlparse(loc).path)
    pages = ctx.get('/api/pages').json()
    want = (pages.get(HOME) or {}).get('url')
    c.ok(path == want, f'解码后等于 /api/pages 里首页的 url（{want}）', path)
    r2 = ctx.get(path)
    c.ok(r2.status == 200 and 'text/html' in (r2.header('Content-Type') or ''), '跟过去是 200 的 HTML', f'{r2.status} {r2.header("Content-Type")}')


@case('R02', '/编辑器/ 返回编辑器页面')
def t_editor(ctx, c):
    r = ctx.get('/编辑器/')
    c.ok(r.status == 200, '/编辑器/ → 200', r.status)
    c.ok('text/html' in (r.header('Content-Type') or ''), 'Content-Type 是 HTML', r.header('Content-Type'))
    body = r.body.decode('utf-8', 'replace')
    c.ok('editor.js' in body and 'id="frame"' in body, '是编辑器页面（含 editor.js 和 #frame）')
    if ctx.target == 'old':
        subs = ['/编辑器/editor.js', '/编辑器/editor.css', '/设计规范/tokens.css']
    else:   # 新编辑器自带界面样式，不再引用被编辑站点的 tokens.css：改成检查它实际引用的每个本地资源
        refs = [x for x in re.findall(r'(?:src|href)="([^"#?:]+)"', body) if not x.startswith('data')]
        subs = [x if x.startswith('/') else '/编辑器/' + x for x in refs]
        c.ok(any(x.endswith('editor.js') for x in subs) and any(x.endswith('editor.css') for x in subs), '引用了 editor.js 和 editor.css', subs)
        c.ok(not any('设计规范' in x for x in subs), '不再引用被编辑站点的样式（编辑器界面自带）', subs)
    for sub in subs:
        r2 = ctx.get(sub)
        c.ok(r2.status == 200, f'{sub} → 200（编辑器页里的相对引用要能取到）', r2.status)


@case('R03', '静态文件根是 4-页面/，目录地址补 index.html，响应不缓存')
def t_static(ctx, c):
    for rel, mime in ((HOME_HTML, 'text/html'), ('4-页面/首页/调整单/feedback.js', 'javascript'),
                      ('4-页面/posts/_样张/文章页调整台.html', 'text/html'), ('4-页面/首页/首页-v3/data/site.js', 'javascript')):
        url = '/' + rel[len(PAGES_DIR) + 1:]
        r = ctx.get(url)
        same = r.status == 200 and r.body == (ctx.site / rel).read_bytes()
        c.ok(same, f'{url} → 200 且内容和沙盒文件一致', r.status)
        c.ok(mime in (r.header('Content-Type') or ''), f'{url} 的 Content-Type 含 {mime}', r.header('Content-Type'))
    r = ctx.get('/首页/首页-v3/data/site.js')
    c.ok('no-store' in (r.header('Cache-Control') or '') or 'no-cache' in (r.header('Cache-Control') or ''),
         'site.js 带 Cache-Control: no-store / no-cache（保存后刷新 iframe 要拿到新值）', r.header('Cache-Control'))
    posts = sorted(p.parent.name for p in (ctx.site / PAGES_DIR / 'posts').glob('20*/index.html'))
    if posts:
        r = ctx.get(f'/posts/{posts[0]}/')
        c.ok(r.status == 200 and b'<html' in r.body[:2000].lower(), f'/posts/{posts[0]}/ → 目录补 index.html，200', r.status)
    r = ctx.get('/不存在的目录/不存在.html')
    c.ok(r.status == 404, '不存在的文件 → 404', r.status)


# ======================================================================
# 用例：页面登记与修改单读取
# ======================================================================
@case('R04', '/api/pages 列出首页及其 url')
def t_pages(ctx, c):
    r = ctx.get('/api/pages')
    c.ok(r.status == 200, '200', r.status)
    j = r.json()
    c.ok(isinstance(j, dict) and HOME in j, f'返回对象里有「{HOME}」', list(j)[:5] if isinstance(j, dict) else j)
    cfg = j.get(HOME) or {}
    url = cfg.get('url', '')
    c.ok(isinstance(url, str) and url.startswith('/'), '首页 url 是站内绝对路径', url)
    c.ok(url == '/' + HOME_HTML[len(PAGES_DIR) + 1:], f'首页 url 指向现行首页 {HOME_HTML}', url)
    r2 = ctx.get(url)
    c.ok(r2.status == 200, 'url 能打开（200）', r2.status)
    c.note(f'旧服务还回了：{json.dumps({k: v for k, v in cfg.items() if k != "url"}, ensure_ascii=False)}')


@case('R05', '/api/sheet?page=首页 返回修改单 items')
def t_sheet(ctx, c):
    r = ctx.get(f'/api/sheet?page={HOME}')
    c.ok(r.status == 200, '200', r.status)
    j = r.json()
    items = j.get('items')
    c.ok(isinstance(items, list), 'items 是数组', type(items).__name__)
    disk = ctx.sheet()
    c.ok(items == disk['items'], f'items 和修改单文件一致（{len(disk["items"])} 条）', len(items or []))
    for k in ('page', 'file', 'data', 'guide', 'note'):
        c.ok(j.get(k) == disk.get(k), f'外层字段 {k} 原样返回（editor.js 用 file 显示修改单位置）', j.get(k))
    r2 = ctx.get('/api/sheet')
    c.note(f'不带 page 参数：{r2.status}（旧服务默认按「首页」返回，editor.js 总会带 page）')


# ======================================================================
# 用例：保存
# ======================================================================
@case('R06', '保存「文字」：唯一原句 → 已写入，site.js 对应字段变新值，其他字段语义不变')
def t_save_text(ctx, c):
    path, old, sec = ctx.take()
    new = old + '（回归改）'
    c.note(f'挑中的原句：{dotted(path)} = 「{old}」（site.js 里能用的唯一原句共 {len(ctx.candidates)} 句）')
    before_raw = ctx.read(SITE_JS)
    r = save(ctx, [text_item(old, new, sec)])
    c.ok(r.status == 200, '200', r.brief())
    j = r.json()
    c.ok(j.get('ok') is True, 'ok = true', j.get('ok'))
    c.ok(j.get('sheet') == SHEET, f'回执 sheet = {SHEET}', j.get('sheet'))
    c.ok(j.get('warnings') == [], 'warnings 为空', j.get('warnings'))
    rec = (j.get('saved') or [{}])[0]
    check_saved_record(c, ctx, rec, '文字', '已写入')
    c.ok(rec.get('field') == dotted(path), f'field = {dotted(path)}', rec.get('field'))
    c.ok(rec.get('value') == new and rec.get('old') == old and rec.get('new') == new, 'value / old / new 记对了',
         {k: rec.get(k) for k in ('old', 'new', 'value')})
    after_raw = ctx.read(SITE_JS)
    h0, d0, t0 = split_js(before_raw)
    h1, d1, t1 = split_js(after_raw)
    c.ok(get_in(d1, path) == new, 'site.js 解析后该字段 = 新值', get_in(d1, path))
    set_in(d1, path, old)
    c.ok(d1 == d0, '把该字段换回原值后，整份数据和写入前完全相同（其他字段语义不变）')
    c.ok(h1.strip() == h0.strip() and t1.strip() == t0.strip(), '文件头 window.SITE = 和结尾 ; 保留', [h1.strip(), t1.strip()])
    ctx.write = {'before': before_raw, 'after': after_raw, 'old': old, 'new': new, 'path': path}


@case('R07', '格式检查：写回后 site.js 除被改字段外逐字节不变')
def t_exact(ctx, c):
    w = ctx.write
    if not w:
        return c.skip('R06 没写成，没有可比的前后原文')
    old_tok, new_tok = json.dumps(w['old'], ensure_ascii=False), json.dumps(w['new'], ensure_ascii=False)
    expect = w['before'].replace(old_tok, new_tok, 1)
    exact = w['after'] == expect
    diff = [l for l in difflib.unified_diff(w['before'].splitlines(), w['after'].splitlines(), lineterm='', n=0)
            if l[:1] in '+-' and l[:3] not in ('+++', '---')]
    c.note(f'写入前 {len(w["before"].encode())} 字节 / 写入后 {len(w["after"].encode())} 字节；按行比较有 {len(diff)} 行增删'
           f'（精确写回应该只有 2 行：被改字段那一行的一删一增）')
    h, d, t = split_js(w['after'])
    if w['after'] == h + json.dumps(d, ensure_ascii=False, indent=1) + t:
        c.note('写入后的文件正好是 json.dumps(indent=1) 的输出：整份文件被重排了')
    if ctx.target == 'old':
        c.known = '已知差异：旧服务用 json.dumps 重排整份 site.js（HEAD 版本来就不能原样写回）；新工具要做到精确写回'
    c.ok(exact, '只有被改字段的那段字符变了，其余逐字节相同', f'{len(diff)} 行增删')


@case('R08', '保存「文字」：找不到原句 → 待 AI 进修改单，site.js 不动')
def t_save_text_missing(ctx, c):
    before = ctx.read(SITE_JS)
    r = save(ctx, [text_item('回归测试：数据里不存在的一句话', '改成别的', '首屏')])
    c.ok(r.status == 200 and r.json().get('ok') is True, '200 且 ok = true', r.brief())
    rec = (r.json().get('saved') or [{}])[0]
    check_saved_record(c, ctx, rec, '文字', '待 AI')
    c.ok(bool(rec.get('note')), 'note 写了原因', rec.get('note'))
    c.ok('field' not in rec, '没有 field（没写进数据）', rec.get('field'))
    c.ok(rec.get('old') == '回归测试：数据里不存在的一句话' and rec.get('new') == '改成别的', 'old / new 记对了')
    c.ok(ctx.read(SITE_JS) == before, 'site.js 一个字节没变')


@case('R09', '保存「样式」→ 待 AI，字段原样记录，site.js 不动')
def t_save_style(ctx, c):
    before = ctx.read(SITE_JS)
    css = {'selector': '#ds-regression-only', 'prop': 'font-size', 'value': 'var(--fs-18)'}
    it = {'type': '样式', 'key': 'style|#ds-regression-only|font-size', 'where': where('首屏', '回归测试 · 样式'),
          'prop': 'font-size', 'label': '字号', 'from': '16px', 'to': '--fs-18（18px）', 'token': '--fs-18', 'css': css}
    r = save(ctx, [it])
    c.ok(r.status == 200 and r.json().get('ok') is True, '200 且 ok = true', r.brief())
    rec = (r.json().get('saved') or [{}])[0]
    check_saved_record(c, ctx, rec, '样式', '待 AI')
    want = {'prop': 'font-size', 'label': '字号', 'old': '16px', 'new': '--fs-18（18px）', 'token': '--fs-18', 'css': css,
            'where': it['where']}
    got = {k: rec.get(k) for k in want}
    c.ok(got == want, 'prop / label / old(=from) / new(=to) / token / css / where 都按请求记下', got)
    c.ok(ctx.read(SITE_JS) == before, 'site.js 一个字节没变')


@case('R10', '保存「意见」→ 待 AI，site.js 不动')
def t_save_opinion(ctx, c):
    before = ctx.read(SITE_JS)
    it = {'type': '意见', 'key': 'note|1', 'where': where('作品', '回归测试 · 意见', '#works', 'section'),
          'text': '  回归测试：这里想和上面拉开一点  '}
    r = save(ctx, [it])
    c.ok(r.status == 200 and r.json().get('ok') is True, '200 且 ok = true', r.brief())
    rec = (r.json().get('saved') or [{}])[0]
    check_saved_record(c, ctx, rec, '意见', '待 AI')
    c.ok(rec.get('text') == '回归测试：这里想和上面拉开一点', 'text 去掉首尾空白后记下', rec.get('text'))
    c.ok(rec.get('where') == it['where'], 'where 原样记下', rec.get('where'))
    c.ok(ctx.read(SITE_JS) == before, 'site.js 一个字节没变')


def desensitize_case(ctx, c, label, secret):
    path, old, sec = ctx.take()
    new = f'{secret} {old}'
    c.note(f'挑中的原句：{dotted(path)} = 「{old}」，新文字带{label}')
    before = ctx.read(SITE_JS)
    r = save(ctx, [text_item(old, new, sec)])
    c.ok(r.status == 200 and r.json().get('ok') is True, '200 且 ok = true（撤回不算接口失败）', r.brief())
    j = r.json()
    rec = (j.get('saved') or [{}])[0]
    check_saved_record(c, ctx, rec, '文字', '没写入')
    c.ok('脱敏' in str(rec.get('note', '')), 'note 说明是脱敏扫描命中、已撤回', rec.get('note'))
    c.ok(bool(j.get('warnings')), 'warnings 非空（编辑器会弹出来）', j.get('warnings'))
    c.ok(ctx.read(SITE_JS) == before, 'site.js 回滚到保存前，一个字节不差')


@case('R11', '脱敏：新文字带手机号 → 没写入，site.js 回滚')
def t_scan_phone(ctx, c):
    desensitize_case(ctx, c, '手机号', FAKE_PHONE)


@case('R12', '脱敏：新文字带 IP → 没写入，site.js 回滚')
def t_scan_ip(ctx, c):
    desensitize_case(ctx, c, ' IP', FAKE_IP)


@case('R13', '一次保存多条（编辑器的真实用法）：文字写入 + 样式 + 意见，顺序和 id 连号')
def t_save_batch(ctx, c):
    path, old, sec = ctx.take()
    new = old + '（批量）'
    items = [text_item(old, new, sec),
             {'type': '样式', 'key': 'style|#ds-b|gap', 'where': where('项目', '回归 · 批量样式'), 'prop': 'gap', 'label': '子元素间距',
              'from': '24px', 'to': '--sp-32（32px）', 'token': '--sp-32', 'css': {'selector': '#ds-b', 'prop': 'gap', 'value': 'var(--sp-32)'}},
             {'type': '意见', 'key': 'note|2', 'where': where('项目', '回归 · 批量意见'), 'text': '回归测试：批量里的意见'}]
    r = save(ctx, items)
    c.ok(r.status == 200 and r.json().get('ok') is True, '200 且 ok = true', r.brief())
    saved = r.json().get('saved') or []
    c.ok([x.get('type') for x in saved] == ['文字', '样式', '意见'], '回执顺序和请求一致', [x.get('type') for x in saved])
    c.ok([x.get('status') for x in saved] == ['已写入', '待 AI', '待 AI'], '状态：已写入 / 待 AI / 待 AI', [x.get('status') for x in saved])
    nums = [int(x['id'][-3:]) for x in saved if ID_RE.match(str(x.get('id', '')))]
    c.ok(len(nums) == 3 and nums == list(range(nums[0], nums[0] + 3)), '三条 id 连号', [x.get('id') for x in saved])
    ctx.saved_ids.extend(x.get('id') for x in saved)
    _, d, _ = split_js(ctx.read(SITE_JS))
    c.ok(get_in(d, path) == new, f'site.js 的 {dotted(path)} 写成新值', get_in(d, path))


@case('R14', '修改单只追加：原有条目和外层字段一个不改，本次新增的 id 按天连号')
def t_sheet_append(ctx, c):
    now = ctx.sheet()
    init = ctx.initial_sheet
    n0 = len(init['items'])
    c.ok(now['items'][:n0] == init['items'], f'原有 {n0} 条（含 AI 写的 reply / reply_time）原样保留')
    for k in ('page', 'file', 'data', 'guide', 'note'):
        c.ok(now.get(k) == init.get(k), f'外层字段 {k} 没被改', now.get(k))
    added = [x.get('id') for x in now['items'][n0:]]
    c.ok(added == ctx.saved_ids, f'新增 {len(added)} 条，和各次保存回执的 id 一一对应', added)
    day = datetime.date.today().strftime('%Y%m%d')
    already = sum(1 for x in init['items'] if str(x.get('id', '')).startswith(day))
    want = [f'{day}-{k:03d}' for k in range(already + 1, already + 1 + len(added))]
    c.ok(added == want, f'id 从今天已有的 {already} 条往后连号', added)


@case('R15', '未登记页面 → 404，不建修改单')
def t_unknown_page(ctx, c):
    r = save(ctx, [{'type': '意见', 'where': where('首屏', 'x'), 'text': 'x'}], page='不存在的页面')
    c.ok(r.status == 404, 'POST /api/save page=不存在的页面 → 404', r.brief())
    r = ctx.get('/api/sheet?page=不存在的页面')
    c.ok(r.status == 404, 'GET /api/sheet?page=不存在的页面 → 404', r.brief())
    c.ok(not (ctx.site / SHEET_DIR / '不存在的页面.json').exists(), '没有建出 不存在的页面.json')
    r = ctx.post('/api/没有这个接口', {'a': 1})
    c.ok(r.status == 404, 'POST 未知接口 → 404', r.brief())


# ======================================================================
# 用例：调整单反馈
# ======================================================================
@case('R16', '/api/feedback 保存后 GET 能读回，文件落在 0-记录/修改单/反馈/')
def t_feedback(ctx, c):
    items = {'A1': {'title': '标题一', 'choice': '按建议', 'text': '文字一'},
             'B2': {'title': '标题二', 'choice': '', 'text': '多行\n文字'}}
    r = ctx.post('/api/feedback', {'name': '回归测试', 'page': '/首页/调整单/回归测试.html', 'items': items})
    c.ok(r.status == 200, '200', r.brief())
    j = r.json()
    c.ok(j.get('ok') is True, 'ok = true', j.get('ok'))
    c.ok(bool(TIME_RE.match(str(j.get('updated', '')))), 'updated 形如 YYYY-MM-DD HH:MM（feedback.js 取 slice(11) 显示时间）', j.get('updated'))
    c.ok(j.get('count') == 2, 'count = 2（有选项或有文字的条数）', j.get('count'))
    rel = f'{FB_DIR}/回归测试.json'
    c.ok(j.get('file') == rel, f'file = {rel}（相对博客根目录，feedback.js 显示它）', j.get('file'))
    disk = ctx.site / rel
    c.ok(disk.is_file(), '文件确实落在沙盒的 0-记录/修改单/反馈/ 下')
    if disk.is_file():
        d = json.loads(disk.read_text(encoding='utf-8'))
        c.ok(d.get('name') == '回归测试' and d.get('page') == '/首页/调整单/回归测试.html', 'name / page 写进文件')
        c.ok(d.get('items') == items, 'items 原样写进文件', d.get('items'))
        c.ok(d.get('updated') == j.get('updated'), 'updated 和回执一致', d.get('updated'))
    g = ctx.get('/api/feedback?name=回归测试')
    c.ok(g.status == 200, 'GET 200', g.brief())
    gj = g.json()
    c.ok(gj.get('items') == items and gj.get('updated') == j.get('updated'), 'GET 读回的 items、updated 和保存的一致', gj.get('items'))


@case('R17', '/api/feedback 三类调用方的真实请求（调整单 feedback.js、文章页调整台、背景格子调色）')
def t_feedback_callers(ctx, c):
    other = ctx.site / FB_DIR / '项目卡片参考.json'
    other_hash = sha256(other) if other.is_file() else None
    calls = [
        ('文案审查', '/首页/调整单/文案审查.html',
         {'X1': {'title': '《我的2025》要不要留在网站上', 'choice': '按建议', 'text': ''}, 'S1': {'title': '标题', 'choice': '', 'text': '回归测试意见'}}),
        ('文章页调整台', '/posts/_样张/文章页调整台.html',
         {'set': {'title': '文章页按钮和正文排版', 'choice': '自己调的', 'text': '按钮 B「图标小方块」｜正文 霞鹜文楷，字号 17px，行高 1.85'}}),
        ('背景格子调色', '/首页/首页-v3/背景格子调色.html',
         {'grid': {'title': '背景格子数值', 'choice': '自己调的', 'text': '自己调的：格子底色 246.5、格线 237｜--g-cell: 1.46; --g-line: 5.42;'}}),
    ]
    for name, page, items in calls:
        r = ctx.post('/api/feedback', {'name': name, 'page': page, 'items': items})
        ok = r.status == 200 and r.json().get('ok') is True
        c.ok(ok, f'POST {name} → 200 ok', r.brief())
        g = ctx.get(f'/api/feedback?name={name}').json()
        c.ok(g.get('items') == items and g.get('page') == page, f'GET {name} 读回一致', g.get('items'))
    if other_hash:
        c.ok(sha256(other) == other_hash, '没被请求到的 项目卡片参考.json 一个字节没变')


@case('R18', '/api/feedback 名字校验、条目清洗、整份覆盖')
def t_feedback_rules(ctx, c):
    for bad in ('../逃逸', '', 'a' * 41, 'a b', '名字/斜杠', '.隐藏'):
        r = ctx.post('/api/feedback', {'name': bad, 'page': '/x', 'items': {}})
        c.ok(r.status == 400, f'POST name={bad!r} → 400', r.brief())
    r = ctx.get('/api/feedback?name=../逃逸')
    c.ok(r.status == 400, 'GET name=../逃逸 → 400', r.brief())
    c.ok(not list((ctx.site / '0-记录').glob('逃逸*')), '没有写出 逃逸.json')
    g = ctx.get('/api/feedback?name=回归测试-没有这份')
    c.ok(g.status == 200 and g.json().get('items') == {} and g.json().get('name') == '回归测试-没有这份',
         'GET 没存过的名字 → 200，items 为空对象', g.brief())
    items = {'A1': {'title': 'T' * 250, 'choice': 'C' * 30, 'text': '保留'}, 'x' * 13: {'title': '编号太长', 'choice': '', 'text': 'x'},
             'B2': '不是对象'}
    r = ctx.post('/api/feedback', {'name': '回归测试', 'page': '/x', 'items': items})
    c.ok(r.status == 200, '200', r.brief())
    d = ctx.get('/api/feedback?name=回归测试').json()
    got = d.get('items', {})
    c.ok(list(got) == ['A1'], '整份覆盖：上次的 A1/B2 只剩这次合法的 A1；编号超 12 位、值不是对象的被丢掉', list(got))
    a1 = got.get('A1', {})
    c.ok(len(a1.get('title', '')) == 200 and len(a1.get('choice', '')) == 20, 'title 截到 200 字、choice 截到 20 字',
         [len(a1.get('title', '')), len(a1.get('choice', ''))])
    r = ctx.post('/api/feedback', {'name': '回归测试', 'page': '/x', 'items': {f'k{i}': {} for i in range(301)}})
    c.ok(r.status >= 400, '超过 300 条 → 拒绝（4xx/5xx）', r.brief())
    r = ctx.post('/api/feedback', {'name': '回归测试', 'page': '/x', 'items': ['不是对象']})
    c.ok(r.status >= 400, 'items 不是对象 → 拒绝', r.brief())


# ======================================================================
# 用例：安全
# ======================================================================
@case('R19', '安全：Host 不是本机 → 403')
def t_host(ctx, c):
    for host in ('evil.example', '127.0.0.1.evil.example', f'localhost.evil.example:{ctx.port}', FAKE_LAN):
        r = ctx.get('/api/pages', headers={'Host': host})
        c.ok(r.status == 403, f'GET /api/pages Host={mask(host)} → 403', r.status)
    r = ctx.get('/编辑器/', headers={'Host': 'evil.example'})
    c.ok(r.status == 403, 'GET /编辑器/ Host=evil.example → 403（静态文件也拦）', r.status)
    r = ctx.get('/', headers={'Host': 'evil.example'})
    c.ok(r.status == 403, 'GET / Host=evil.example → 403', r.status)
    before = ctx.read(SITE_JS)
    r = ctx.post('/api/feedback', {'name': '回归-host', 'page': '/x', 'items': {'A': {'title': 't', 'choice': '', 'text': 'x'}}},
                 headers={'Host': 'evil.example'})
    c.ok(r.status == 403, 'POST /api/feedback Host=evil.example → 403', r.status)
    c.ok(not (ctx.site / FB_DIR / '回归-host.json').exists() and ctx.read(SITE_JS) == before, '没有落盘')
    for host in (f'localhost:{ctx.port}', f'127.0.0.1:{ctx.port}'):
        r = ctx.get('/api/pages', headers={'Host': host})
        c.ok(r.status == 200, f'对照：Host={host} → 200', r.status)


@case('R20', '安全：Origin 不是本机 → 403')
def t_origin(ctx, c):
    body = {'name': '回归-origin', 'page': '/x', 'items': {'A': {'title': 't', 'choice': '', 'text': 'x'}}}
    for origin in ('http://evil.example', 'http://127.0.0.1.evil.example', 'null', 'https://localhost.evil.example'):
        r = ctx.post('/api/feedback', body, headers={'Origin': origin})
        c.ok(r.status == 403, f'POST /api/feedback Origin={origin} → 403', r.status)
    sheet_before = ctx.read(SHEET)
    r = ctx.post('/api/save', {'page': HOME, 'items': [{'type': '意见', 'where': where('首屏', 'x'), 'text': '跨站'}]},
                 headers={'Origin': 'http://evil.example'})
    c.ok(r.status == 403, 'POST /api/save Origin=http://evil.example → 403', r.status)
    c.ok(not (ctx.site / FB_DIR / '回归-origin.json').exists() and ctx.read(SHEET) == sheet_before, '反馈和修改单都没落盘')
    r = ctx.post('/api/feedback', {**body, 'name': '回归-origin-ok'}, headers={'Origin': f'http://localhost:{ctx.port}'})
    c.ok(r.status == 200, '对照：Origin=http://localhost:端口 → 200', r.status)
    r = ctx.post('/api/feedback', {**body, 'name': '回归-origin-none'}, headers={'Origin': None})
    c.note(f'不带 Origin（命令行工具直接 POST）：{r.status}（旧服务放行；浏览器的 fetch POST 一定带 Origin）')


@case('R21', '安全：POST 不是 JSON → 415')
def t_json_only(ctx, c):
    sheet_before = ctx.read(SHEET)
    for route, payload in (('/api/save', {'page': HOME, 'items': [{'type': '意见', 'where': {}, 'text': 'x'}]}),
                           ('/api/feedback', {'name': '回归-415', 'page': '/x', 'items': {}})):
        data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        for ctype in ('text/plain', 'application/x-www-form-urlencoded', None):
            r = ctx.post(route, data, headers={'Content-Type': ctype})
            c.ok(r.status == 415, f'POST {route} Content-Type={ctype} → 415', r.status)
    c.ok(ctx.read(SHEET) == sheet_before and not (ctx.site / FB_DIR / '回归-415.json').exists(), '都没落盘')


@case('R22', '安全：路径穿越（含编码、符号链接）→ 404，不泄露内容')
def t_traversal(ctx, c):
    secret = (ctx.site / SHEET).read_bytes()[:40]
    for p in ('/../0-记录/修改单/首页.json', '/%2e%2e/0-记录/修改单/首页.json', '/%2E%2E%2F0-记录%2F修改单%2F首页.json',
              '/首页/../../0-记录/修改单/首页.json', '/首页/..%2f..%2f0-记录/修改单/首页.json',
              '/..%5c0-记录%5c修改单%5c首页.json', '/_ds-link-out/修改单/首页.json',
              '/../99-其他/脚本/编辑服务.py', '/%2e%2e/%2e%2e/%2e%2e/etc/hosts'):
        r = ctx.get(p)
        c.ok(r.status == 404 and secret not in r.body, f'GET {p} → 404', r.status)


@case('R23', '安全：点开头的文件和目录 → 404')
def t_dotfiles(ctx, c):
    for p in ('/.ds-hidden.txt', '/首页/.ds-secret/x.txt', '/首页/%2eds-secret/x.txt', '/%2Eds-hidden.txt'):
        r = ctx.get(p)
        c.ok(r.status == 404 and '沙盒夹具' not in r.body.decode('utf-8', 'replace'), f'GET {p} → 404', r.status)


# ======================================================================
# 用例：视频分段
# ======================================================================
@case('R24', 'mp4 Range → 206 + Content-Range（首屏头像跳帧靠它）')
def t_range(ctx, c):
    url = '/' + RANGE_VIDEO[len(PAGES_DIR) + 1:]
    blob = (ctx.site / RANGE_VIDEO).read_bytes()
    size = len(blob)
    r = ctx.get(url)
    c.ok(r.status == 200 and r.body == blob, f'不带 Range → 200，整份 {size} 字节', r.status)
    c.ok('video/mp4' in (r.header('Content-Type') or ''), 'Content-Type: video/mp4', r.header('Content-Type'))
    c.ok((r.header('Accept-Ranges') or '') == 'bytes', 'Accept-Ranges: bytes', r.header('Accept-Ranges'))
    r = ctx.get(url, headers={'Range': 'bytes=0-99'})
    c.ok(r.status == 206, 'Range: bytes=0-99 → 206', r.status)
    c.ok(r.header('Content-Range') == f'bytes 0-99/{size}', f'Content-Range: bytes 0-99/{size}', r.header('Content-Range'))
    c.ok(r.body == blob[:100] and r.header('Content-Length') == '100', '正好 100 字节，内容对得上', len(r.body))
    start = size // 2
    r = ctx.get(url, headers={'Range': f'bytes={start}-'})
    c.ok(r.status == 206 and r.header('Content-Range') == f'bytes {start}-{size - 1}/{size}' and r.body == blob[start:],
         f'Range: bytes={start}- → 206，到文件尾', f'{r.status} {r.header("Content-Range")}')
    # 下面两种不常见的 Range 只记录、不判对错：给新工具对照用
    def probe(rng):
        try:
            return ctx.get(url, headers={'Range': rng})
        except (http.client.HTTPException, OSError) as e:
            return e
    r = probe('bytes=-100')
    c.note('Range: bytes=-100（规范：取最后 100 字节）：' + (f'连接被断开，没有响应（{type(r).__name__}）' if isinstance(r, Exception) else
           f'{r.status} {r.header("Content-Range")}，{"内容是文件尾" if r.body == blob[-100:] else "内容不是文件尾"}'))
    r = probe(f'bytes={size + 10}-')
    c.note(f'Range 起点超出文件大小（规范：416）：' + (f'连接被断开，没有响应（{type(r).__name__}）' if isinstance(r, Exception) else
           f'{r.status} {r.header("Content-Range")}'))


@case('R25', '默认端口 4580（文章页调整台按 location.port 判断能不能自动保存）')
def t_port(ctx, c):
    c.note('posts/_样张/文章页调整台.html:127 写死 location.port === "4580"')
    c.skip('回归不占 4580（用户可能正开着真编辑服务）；切换时手动验：从 4580 打开文章页调整台，改一个值，看 反馈/文章页调整台.json 更新')


# ======================================================================
# UI 冒烟（python playwright，可选）
# ======================================================================
class UI:
    def __init__(self):
        self.error = None
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.error = '没装 python playwright（pip install playwright && playwright install chromium），UI 冒烟跳过'
            return
        try:
            self.pw = sync_playwright().start()
            self.browser = self.pw.chromium.launch()
            self.context = self.browser.new_context(viewport={'width': 1440, 'height': 900})

            # 外网请求（Google Fonts、CDN）一律掐掉，不让网络状况影响结果。用正则在浏览器那头匹配，
            # 本机请求不经过 Python 回调（sync API 只在调用 playwright 时才处理回调，经过回调会卡住页面的 fetch）
            self.context.route(re.compile(r'^(?!https?://(127\.0\.0\.1|localhost)[:/])'), lambda route: route.abort())
            self.page = self.context.new_page()
            self.page_errors = []
            self.page.on('pageerror', lambda e: self.page_errors.append(str(e)[:200]))
        except Exception as e:
            self.error = f'playwright 起不来浏览器：{e}'

    def close(self):
        if not self.error:
            try:
                self.browser.close()
                self.pw.stop()
            except Exception:
                pass


def wait_for(fn, timeout=10.0, page=None):
    """轮询 fn 直到返回真值。给了 page 就用 page.wait_for_timeout 等，让 playwright 在等的时候继续处理浏览器事件。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            v = fn()
            if v:
                return v
        except Exception:
            pass
        page.wait_for_timeout(200) if page else time.sleep(0.2)
    return None


def editor_frame(page, url):
    target = enc(url)
    return next((f for f in page.frames if f != page.main_frame and f.url.endswith(target)), None)


@case('U01', 'UI：编辑器连上服务，载入首页，修改单页签条数对得上')
def u_editor(ctx, c):
    ui = ctx.ui
    if ui.error:
        return c.skip(ui.error)
    page = ui.page
    page.goto(f'http://127.0.0.1:{ctx.port}{enc("/编辑器/")}', wait_until='domcontentloaded')
    st = wait_for(lambda: '已连上' in page.text_content('#status'), 10, page)
    c.ok(bool(st), '顶栏状态变成「已连上本机服务」', page.text_content('#status'))
    opts = page.eval_on_selector_all('#page option', 'els => els.map(e => e.textContent)')
    c.ok(HOME in opts, f'页面下拉里有「{HOME}」', opts)
    url = ctx.get('/api/pages').json()[HOME]['url']
    fr = wait_for(lambda: (lambda f: f if f and f.evaluate('document.readyState') == 'complete' and f.evaluate('document.body.children.length') > 0 else None)(editor_frame(page, url)), 15, page)
    c.ok(fr is not None, f'iframe 载入了 {url}')
    n = len(ctx.sheet()['items'])
    got = wait_for(lambda: page.text_content('#sheetN') == str(n) and page.text_content('#sheetN'), 5, page)
    c.ok(bool(got), f'「修改单」页签条数 = 修改单文件条数 {n}', page.text_content('#sheetN'))
    if ui.page_errors:
        c.note('页面脚本报错（外网请求被掐，仅记录）：' + ' | '.join(ui.page_errors[:5]))


@case('U02', 'UI：编辑器里双击改字 → 保存 → site.js 写回、修改单记「已写入」')
def u_edit_text(ctx, c):
    ui = ctx.ui
    if ui.error:
        return c.skip(ui.error)
    page = ui.page
    url = ctx.get('/api/pages').json()[HOME]['url']
    fr = editor_frame(page, url)
    if fr is None:
        return c.ok(False, '先要 U01 把编辑器打开')
    left = [(p, t, s) for p, t, s in ctx.candidates if p not in ctx.used]
    idx = fr.evaluate('''(cands) => {
      const norm = el => (el.innerText || '').replace(/\\u00a0/g, ' ').replace(/\\s*\\n\\s*/g, '').trim();
      const INLINE = new Set(['B','STRONG','EM','I','SPAN','BR','MARK','SMALL','SUB','SUP']);
      const all = [...document.body.querySelectorAll('*')];
      for (let i = 0; i < cands.length; i++) {
        const hits = all.filter(el => norm(el) === cands[i] && [...el.querySelectorAll('*')].every(x => INLINE.has(x.tagName)));
        const outer = hits.filter(el => !hits.some(o => o !== el && o.contains(el)));
        if (outer.length !== 1) continue;
        const r = outer[0].getBoundingClientRect();
        if (r.width > 0 && r.height > 0) { outer[0].setAttribute('data-ds-target', '1'); return i; }
      }
      return -1;
    }''', [t for _, t, _ in left])
    if idx < 0:
        return c.skip('首页上找不到一个「整段文字 = site.js 里唯一原句」的元素，没法在界面上改字')
    path, old, _ = left[idx]
    ctx.used.add(path)
    new = old + '（界面改）'
    c.note(f'在页面上改的元素：{dotted(path)} = 「{old}」')
    fr.locator('[data-ds-target]').dblclick(force=True)
    ed = wait_for(lambda: fr.evaluate('!!document.querySelector(\'[contenteditable="true"]\')'), 5, page)
    c.ok(bool(ed), '双击后元素进入可编辑状态')
    if not ed:
        return
    fr.evaluate('''() => { const el = document.querySelector('[contenteditable="true"]'); const r = document.createRange();
      r.selectNodeContents(el); const s = getSelection(); s.removeAllRanges(); s.addRange(r); }''')
    page.keyboard.insert_text(new)
    page.keyboard.press('Enter')
    c.ok(wait_for(lambda: page.text_content('#count') == '1', 3, page) is not None, '顶栏「保存」计数 = 1', page.text_content('#count'))
    n0 = len(ctx.sheet()['items'])
    page.click('#save')
    tok = json.dumps(new, ensure_ascii=False)
    done = wait_for(lambda: tok in ctx.read(SITE_JS), 10, page)
    c.ok(bool(done), 'site.js 里出现了新文字')
    items = ctx.sheet()['items']
    last = items[-1] if len(items) > n0 else {}
    c.ok(last.get('status') == '已写入' and last.get('field') == dotted(path), f'修改单新增一条「已写入」，field = {dotted(path)}',
         {k: last.get(k) for k in ('status', 'field', 'old', 'new')})
    if last.get('id'):
        ctx.saved_ids.append(last['id'])
    toast = wait_for(lambda: page.text_content('#toast'), 3, page)
    c.note(f'编辑器提示：{toast}')


@case('U03', 'UI：调整单反馈框边写边存到 0-记录/修改单/反馈/<名字>.json')
def u_feedback(ctx, c):
    ui = ctx.ui
    if ui.error:
        return c.skip(ui.error)
    page = ui.context.new_page()
    try:
        page.goto(f'http://127.0.0.1:{ctx.port}{enc("/首页/调整单/文案审查.html")}', wait_until='domcontentloaded')
        page.wait_for_selector('.fb textarea', timeout=10000)
        token = f'界面冒烟 {int(time.time())}'
        page.fill('.fb textarea >> nth=0', token)
        path = ctx.site / FB_DIR / '文案审查.json'
        done = wait_for(lambda: token in path.read_text(encoding='utf-8'), 8, page)
        c.ok(bool(done), '输入后自动保存，文案审查.json 里出现了新输入')
        # 只有 POST 成功后才会显示文件路径（页面刚打开时读回旧反馈，显示的是「已保存 HH:MM · 边写边自动保存」）
        st = wait_for(lambda: f'{FB_DIR}/文案审查.json' in page.text_content('#fbSt') and page.text_content('#fbSt'), 5, page)
        c.ok(bool(st), '底栏显示「已保存 HH:MM · 0-记录/修改单/反馈/文案审查.json」（用的是回执里的 updated 和 file）', page.text_content('#fbSt'))
    finally:
        page.close()


# ======================================================================
# 主流程
# ======================================================================
def sanitize(s, ctx_paths):
    """结果里不留本机绝对路径、不留测试用的手机号和 IP（结果文件要进仓库）。"""
    for real, alias in ctx_paths:
        s = s.replace(real, alias)
    return mask(s)


ICON = {'pass': '✓', 'fail': '✗', 'known': '~', 'skip': '-'}
WORD = {'pass': '通过', 'fail': '失败', 'known': '已知差异', 'skip': '跳过'}


def main():
    ap = argparse.ArgumentParser(description='design-studio 回归：在博客沙盒里起编辑服务，逐条跑兼容用例')
    ap.add_argument('--target', choices=['old', 'new'], default='old', help='old = 博客里的旧编辑服务；new = 新工具（要给 --cmd）')
    ap.add_argument('--cmd', help='--target new 的启动命令模板，可用 {site} {port} {python} {repo}')
    ap.add_argument('--blog', default=os.environ.get('DS_BLOG', str(DEFAULT_BLOG)), help=f'真实博客目录（只读），默认 {DEFAULT_BLOG}')
    ap.add_argument('--only', help='只跑这些用例，逗号分隔，如 R01,R06,U01（R07 依赖 R06、R14 依赖前面的保存用例）')
    ap.add_argument('--no-ui', action='store_true', help='跳过 UI 冒烟')
    ap.add_argument('--full-media', action='store_true', help='沙盒里视频、图片也照抄（默认换成小的假文件）')
    ap.add_argument('--extra', action='append', default=[], help='额外带进沙盒的路径（相对博客根目录），可重复')
    ap.add_argument('--inject', action='append', default=[], metavar='源=目标',
                    help='把沙盒外的文件放进沙盒：源是本机路径（相对当前目录），目标相对沙盒博客根目录，可重复。'
                         '例：--inject tests/regression/fixtures/blog.design-studio.json=99-其他/design-studio.json')
    ap.add_argument('--keep', action='store_true', help='跑完保留沙盒，方便排查')
    ap.add_argument('--startup-timeout', type=float, default=20)
    ap.add_argument('--out', default=str(HERE / 'results'), help='结果 JSON 目录')
    ap.add_argument('-v', '--verbose', action='store_true', help='通过的用例也打印证据')
    a = ap.parse_args()
    try:
        sys.stdout.reconfigure(line_buffering=True)   # 输出接到管道或日志时也逐行出来
    except Exception:
        pass
    if a.target == 'new' and not a.cmd:
        ap.error('--target new 要给 --cmd "<启动命令模板>"')

    blog = Path(a.blog).resolve()
    started = datetime.datetime.now()
    print(f'design-studio 回归 · 目标 {a.target} · {started:%Y-%m-%d %H:%M:%S}')
    if not (blog / SITE_JS).is_file():
        print(f'✗ 找不到真实博客：{blog}（用 --blog 或环境变量 DS_BLOG 指定）')
        return 2

    before = fingerprint(blog)
    root = site = server = ui = None
    results, setup_error, stats = [], None, {}
    try:
        root, site, stats = build_sandbox(blog, a.full_media, a.extra)
        for spec in a.inject:
            src, sep, dst = spec.partition('=')
            src_p, dst_p = Path(src).resolve(), (site / dst).resolve()
            if not sep or not src_p.is_file():
                raise SetupError(f'--inject 要写成 源文件=沙盒里的位置，且源文件要存在：{spec}')
            if not str(dst_p).startswith(str(site.resolve()) + os.sep):
                raise SetupError(f'--inject 的目标跑出沙盒了：{dst}')
            dst_p.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src_p, dst_p)
            print(f'注入：{src} → <沙盒博客>/{dst}')
        print(f'沙盒：{site}（复制 {stats["files"]} 个文件 {stats["bytes"] / 1e6:.1f} MB，其中 {stats["placeholders"]} 个媒体换成假文件）')
        server = Server(a.target, a.cmd, site, free_port(), a.startup_timeout)
        print(f'服务：{" ".join(shlex.quote(x) for x in server.cmd)}')
        print(f'端口：{server.port}\n')
        ctx = Ctx(site, server.port, a.target)
        only = set(x.strip() for x in a.only.split(',')) if a.only else None
        need_ui = not a.no_ui and any(cid.startswith('U') and (not only or cid in only) for cid, _, _ in CASES)
        ctx.ui = UI() if need_ui else type('NoUI', (), {'error': '用了 --no-ui，UI 冒烟跳过'})()
        if need_ui and ctx.ui.error:
            print(f'· {ctx.ui.error}')
        ui = ctx.ui if need_ui else None
        for cid, name, fn in CASES:
            if only and cid not in only:
                continue
            c = Case(cid, name)
            try:
                fn(ctx, c)
            except Exception:
                c.failed, c.known = True, None
                c.evidence.append('✗ 用例抛异常：' + traceback.format_exc(limit=4).strip().replace('\n', ' | ')[-600:])
            results.append(c)
            print(f'{ICON[c.status]} {c.id} {c.name}  [{WORD[c.status]}]')
            if c.status != 'pass' or a.verbose:
                for line in c.evidence:
                    print('    ' + line)
                if c.known:
                    print('    ~ ' + c.known)
                if c.skipped:
                    print('    - ' + c.skipped)
    except SetupError as e:
        setup_error = str(e)
        print(f'✗ 环境问题：{e}')
    finally:
        if ui:
            ui.close()
        if server:
            server.stop()

    after = fingerprint(blog)
    changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))

    paths = [(str(site), '<沙盒>/个人博客')] if site else []
    if root:
        paths.append((str(root), '<沙盒>'))
        paths.append((os.path.realpath(root), '<沙盒>'))
    paths += [(str(blog), '<博客>'), (str(Path.home()), '~')]
    count = {k: sum(1 for c in results if c.status == k) for k in ICON}
    finished = datetime.datetime.now()
    report = {
        'target': a.target,
        'command': sanitize(' '.join(server.cmd), paths) if server else None,
        'started': started.isoformat(timespec='seconds'),
        'finished': finished.isoformat(timespec='seconds'),
        'seconds': round((finished - started).total_seconds(), 1),
        'python': sys.version.split()[0],
        'blog': sanitize(str(blog), [(str(Path.home()), '~')]),
        'sandbox': {**stats, 'kept': bool(a.keep and root)},
        'ui': (ui.error or 'playwright chromium') if ui else '没跑（--no-ui 或 --only 没选 U 用例）',
        'summary': {'cases': len(results), **count},
        'integrity': {'guarded': GUARDED, 'files': len(before), 'unchanged': not changed, 'changed': changed},
        'setup_error': sanitize(setup_error, paths) if setup_error else None,
        'cases': [{'id': c.id, 'name': c.name, 'status': c.status,
                   'evidence': [sanitize(x, paths) for x in c.evidence],
                   **({'known': c.known} if c.known else {}), **({'skip': c.skipped} if c.skipped else {})} for c in results],
        'server_log_tail': [sanitize(x, paths) for x in (server.logs[-20:] if server else [])],
    }
    out_dir = Path(a.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f'{a.target}-{started:%Y%m%d-%H%M%S}.json'
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1) + '\n', encoding='utf-8')

    if root and not a.keep:
        shutil.rmtree(root, ignore_errors=True)
    elif root:
        print(f'\n沙盒保留在：{root}')

    print(f'\n共 {len(results)} 条：通过 {count["pass"]}，失败 {count["fail"]}，已知差异 {count["known"]}，跳过 {count["skip"]}')
    if changed:
        print('✗ 真实博客文件变了（可能是回归写穿了，也可能是你同时在用真编辑服务）：')
        for k in changed:
            print('    ' + k)
    else:
        print(f'✓ 真实博客自检：{len(before)} 个受保护文件（site.js + 修改单目录）运行前后哈希一致')
    print(f'结果：{out}')
    if setup_error or changed:
        return 2
    return 1 if count['fail'] else 0


if __name__ == '__main__':
    sys.exit(main())
