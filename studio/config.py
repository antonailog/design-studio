"""站点配置：被编辑的网站有哪些页面、数据文件在哪、修改单写到哪、编辑器面板给哪些选项。

配置是一个 JSON 文件（建议叫 design-studio.json），字段说明见 docs/config.md。
配置里的相对路径都以 base 为基准；base 本身相对配置文件所在目录，默认就是配置文件所在目录。
工具本体不含任何站点专属的东西，站点之间的差别全在这份配置里。
"""
import json
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
        return ok, lines
