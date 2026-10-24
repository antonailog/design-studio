"""站点配置：被编辑的网站有哪些页面、数据文件在哪、修改单写到哪、编辑器面板给哪些选项。

配置是一个 JSON 文件（建议叫 design-studio.json），字段说明见 docs/config.md。
配置里的相对路径都以 base 为基准；base 本身相对配置文件所在目录，默认就是配置文件所在目录。
工具本体不含任何站点专属的东西，站点之间的差别全在这份配置里。
"""
import json
import math
import os
import re

DEFAULT_PORT = 4580
DEFAULT_MOUNT = '/_studio/'

# 文字写回时默认跳过的字段：链接、图片路径、编号这类不显示在页面上的东西
DEFAULT_SKIP_LEAF = ['id', 'url', 'src', 'href', 'poster', 'cover', 'image', 'qr', 'page', 'page_base', 'avatar',
                     'domain', 'kind', 'type', 'repo']
DEFAULT_ALLOWED_TAGS = ['b', 'strong', 'em']


class ConfigError(Exception):
    """配置写错了：缺字段、路径不存在、格式不对。消息直接给人看。"""


def _norm_mount(m):
    m = '/' + str(m or DEFAULT_MOUNT).strip('/') + '/'
    return '/' if m == '//' else m


class Page:
    """一个可编辑的页面。路径都是绝对路径；rel_* 是相对 base 的写法（回给前端、写进修改单）。"""

    def __init__(self, cfg, name, raw):
        if not isinstance(raw, dict):
            raise ConfigError(f'pages.{name} 应该是对象')
        self.name = name
        self.url = raw.get('url')
        if not isinstance(self.url, str) or not self.url.startswith('/'):
            raise ConfigError(f'pages.{name}.url 要写站内绝对路径（以 / 开头），现在是 {self.url!r}')
        self.rel_html = raw.get('html')
        self.rel_data = raw.get('data')
        self.rel_sheet = raw.get('sheet') or (cfg.rel_sheet_dir.rstrip('/') + f'/{name}.json')
        self.rel_guide = raw.get('guide')
        self.html = cfg.path(self.rel_html) if self.rel_html else None
        self.data = cfg.path(self.rel_data) if self.rel_data else None
        self.sheet = cfg.path(self.rel_sheet)
        self.guide = cfg.path(self.rel_guide) if self.rel_guide else None
        self.skip = list(raw.get('skip') or [])      # 数据里有、但页面上没用到的顶层字段：改了不生效，找原句时跳过

    def public(self):
        """/api/pages 回给编辑器的东西。"""
        out = {'url': self.url, 'sheet': self.rel_sheet, 'guide': self.rel_guide,
               'sheet_abs': self.sheet, 'guide_abs': self.guide}   # 绝对路径给「复制给 AI 的指令」用（服务只对本机开放）
        if self.rel_data:
            out['data'] = self.rel_data
        if self.rel_html:
            out['html'] = self.rel_html
        return out


def _num(v):   # 有限的数字：json 会把 Infinity / NaN 读进来，原样回给前端时浏览器解析不了
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


FEEDBACK_NAME = re.compile(r'^[\w一-鿿-]{1,40}$')   # 和 page_mode.FB_NAME 一致：反馈文件名


def _scale(raw, pages):
    """字号收敛页（<挂载路径>scale.html）的配置：审哪些页面、哪些字不动、哪些锁定、有哪几套方案。不写也能用。"""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError('scale 应该是对象，见 docs/config.md「scale：字号收敛」')
    out = {'pages': [[name, p.url] for name, p in pages.items()]}
    extra = raw.get('pages') or {}
    if not isinstance(extra, dict) or not all(isinstance(u, str) and u.startswith('/') for u in extra.values()):
        raise ConfigError('scale.pages 要写成 {"页面名": "/站内地址"}，地址以 / 开头（可以带 #）')
    out['pages'] += [[name, url] for name, url in extra.items() if name not in pages]
    keep = raw.get('keep') or ''
    if isinstance(keep, list):
        if not all(isinstance(k, str) for k in keep):
            raise ConfigError('scale.keep 写成数组时每一项都要是 CSS 选择器字符串')
        keep = ', '.join(keep)
    if not isinstance(keep, str):
        raise ConfigError('scale.keep 要写 CSS 选择器（字符串或字符串数组）')
    out['keep'] = keep
    keep_from = raw.get('keep_from', 34)
    if not _num(keep_from) or keep_from <= 0:
        raise ConfigError(f'scale.keep_from 要写正数（像素），现在是 {keep_from!r}')
    out['keep_from'] = keep_from
    locks = raw.get('lock') or []
    if not isinstance(locks, list):
        raise ConfigError('scale.lock 要写成 [{"selector": "...", "min": 16, "max": 17.5, "why": "..."}]')
    out['lock'] = []
    for i, l in enumerate(locks):
        if not isinstance(l, dict) or not isinstance(l.get('selector'), str) or not l['selector'].strip():
            raise ConfigError(f'scale.lock[{i}] 缺 selector')
        lo, hi = l.get('min', 0), l.get('max', 9999)
        if not (_num(lo) and _num(hi) and lo <= hi):
            raise ConfigError(f'scale.lock[{i}] 的 min / max 要写数字，且 min ≤ max')
        out['lock'].append({'selector': l['selector'], 'min': lo, 'max': hi, 'why': str(l.get('why') or '')})
    presets = raw.get('presets') or []
    if not isinstance(presets, list):
        raise ConfigError('scale.presets 要写成数组')
    out['presets'] = []
    for i, p in enumerate(presets):
        where = f'scale.presets[{i}]'
        if not isinstance(p, dict) or not isinstance(p.get('name'), str) or not 0 < len(p['name']) <= 12:
            raise ConfigError(f'{where}.name 要写 1—12 个字的方案名')
        steps = p.get('steps')
        if not isinstance(steps, list) or not steps:
            raise ConfigError(f'{where}.steps 至少要一档：[[12, "说明"], 14, ...]')
        norm = []
        for s in steps:
            px, role = (s[0], s[1] if len(s) > 1 else '') if isinstance(s, list) and s else (s, '')
            if not _num(px) or not 6 <= px <= 200:
                raise ConfigError(f'{where}.steps 里的 {s!r} 不对：每档写像素数（6—200），可带说明 [12, "标签"]')
            norm.append([px, '' if role is None else str(role)])
        if len({px for px, _ in norm}) != len(norm):
            raise ConfigError(f'{where}.steps 有重复的档位')
        pxs = {px for px, _ in norm}
        upto = p.get('upto') or []
        if not isinstance(upto, list) or not all(isinstance(u, list) and len(u) == 2 and _num(u[0]) and u[1] in pxs for u in upto):
            raise ConfigError(f'{where}.upto 要写成 [[原字号上限, 并到哪一档], ...]，并到的档位必须在 steps 里')
        out['presets'].append({'name': p['name'], 'note': str(p.get('note') or ''), 'recommend': bool(p.get('recommend')),
                               'steps': norm, 'upto': sorted(upto)})
    fb = raw.get('feedback', '字号收敛')
    if not isinstance(fb, str) or not FEEDBACK_NAME.match(fb):
        raise ConfigError('scale.feedback 是保存用的名字：1—40 个汉字、字母、数字、下划线或横线')
    out['feedback'] = fb
    return out


class Config:
    def __init__(self, path):
        self.file = os.path.abspath(path)
        if not os.path.isfile(self.file):
            raise ConfigError(f'找不到站点配置：{path}')
        try:
            with open(self.file, encoding='utf-8') as f:
                raw = json.load(f)
        except ValueError as e:
            raise ConfigError(f'站点配置不是合法 JSON：{e}')
        if not isinstance(raw, dict):
            raise ConfigError('站点配置的最外层应该是对象')
        self.raw = raw
        self.base = os.path.normpath(os.path.join(os.path.dirname(self.file), raw.get('base') or '.'))
        if not os.path.isdir(self.base):
            raise ConfigError(f'base 指向的目录不存在：{self.base}')

        self.rel_static_root = raw.get('static_root') or '.'
        self.static_root = self.path(self.rel_static_root)
        if not os.path.isdir(self.static_root):
            raise ConfigError(f'static_root 不存在：{self.rel_static_root}')
        port = raw.get('port')
        if port is not None and not (isinstance(port, int) and 0 < port < 65536):
            raise ConfigError(f'port 要是 1—65535 的整数，现在是 {port!r}')
        self.port = port
        self.mount = _norm_mount(raw.get('editor_mount'))
        if self.mount == '/':
            raise ConfigError('editor_mount 不能是根目录 /，会把整个网站盖住')
        self.rel_sheet_dir = raw.get('sheet_dir') or '.design-studio/修改单'
        self.rel_feedback_dir = raw.get('feedback_dir') or (self.rel_sheet_dir.rstrip('/') + '/反馈')
        self.feedback_dir = self.path(self.rel_feedback_dir)

        pages = raw.get('pages')
        if not isinstance(pages, dict) or not pages:
            raise ConfigError('pages 至少要登记一个页面：{"页面名": {"url": "/index.html", ...}}')
        self.pages = {name: Page(self, name, p) for name, p in pages.items()}
        rr = raw.get('root_redirect', next(iter(self.pages)))
        if rr in (None, False, ''):
            self.root_redirect = None
        elif rr in self.pages:
            self.root_redirect = self.pages[rr].url
        elif isinstance(rr, str) and rr.startswith('/'):
            self.root_redirect = rr
        else:
            raise ConfigError(f'root_redirect 要写页面名或以 / 开头的地址，现在是 {rr!r}')

        text = raw.get('text') or {}
        self.skip_leaf = set(text.get('skip_leaf') or DEFAULT_SKIP_LEAF)
        self.section_keys = dict(text.get('section_keys') or {})
        try:
            self.follow = [(re.compile(p), str(m)) for p, m in (text.get('follow') or [])]
        except (re.error, ValueError, TypeError) as e:
            raise ConfigError(f'text.follow 写错了（应为 [[正则, 提示], ...]）：{e}')
        tags = [t.lower() for t in (text.get('allowed_tags') or DEFAULT_ALLOWED_TAGS) if re.fullmatch(r'[a-z][a-z0-9]*', str(t).lower())]
        self.allowed_tags = tags

        hook = raw.get('after_save')
        if hook is not None:
            if not isinstance(hook, dict) or not isinstance(hook.get('cmd'), list) or not hook['cmd']:
                raise ConfigError('after_save 要写成 {"cmd": ["命令", "参数", "{file}"], "label": "…"}')
        self.after_save = hook

        self.editor = raw.get('editor') or {}
        if not isinstance(self.editor, dict):
            raise ConfigError('editor 应该是对象')
        self.scale = _scale(raw.get('scale'), self.pages)

    # ---------- 路径 ----------
    def path(self, rel):
        """相对 base 的路径 → 绝对路径。"""
        return os.path.normpath(os.path.join(self.base, rel))

    def rel(self, abs_path):
        """绝对路径 → 相对 base 的写法（正斜杠）。"""
        return os.path.relpath(abs_path, self.base).replace(os.sep, '/')

    def editor_public(self):
        """/api/config 回给编辑器前端的东西：面板选项、元素名称、主题切换方式等。"""
        e = dict(self.editor)
        e.setdefault('title', 'design-studio')
        e['mount'] = self.mount
        e['allowed_tags'] = self.allowed_tags
        e['scale'] = self.scale
        return e

    def check(self):
        """--check / design_studio.py check 用：逐项列出配置里的文件在不在。返回 (全部正常, 行列表)。"""
        lines, ok = [], True
        lines.append(f'站点根目录（base）：{self.base}')
        lines.append(f'静态文件根：{self.rel_static_root}')
        lines.append(f'编辑器地址：{self.mount}　端口：{self.port or "未固定（默认从 %d 往后找空闲的）" % DEFAULT_PORT}')
        for name, p in self.pages.items():
            for label, rel, absp in (('html', p.rel_html, p.html), ('数据文件', p.rel_data, p.data), ('说明文档', p.rel_guide, p.guide)):
                if rel:
                    there = os.path.isfile(absp)
                    ok &= there or label == '说明文档'
                    lines.append(f'{name} {label}：{"✓" if there else "✗"} {rel}')
            lines.append(f'{name} 修改单：{p.rel_sheet}{"（已有）" if os.path.isfile(p.sheet) else "（第一次保存时建）"}')
        extra = self.scale['pages'][len(self.pages):]
        if extra:
            lines.append(f'字号收敛页：{self.mount}scale.html，另审 {len(extra)} 个页面，{len(self.scale["presets"])} 套方案')
            for name, url in extra:
                rel = url.split('#')[0].split('?')[0]
                there = os.path.isfile(os.path.join(self.static_root, rel.lstrip('/') + ('index.html' if rel.endswith('/') else '')))
                lines.append(f'  {name}：{"✓" if there else "?"} {url}')   # 代理站、动态路由的地址在本地找不到文件，只提示不算错
        return ok, lines
