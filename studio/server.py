"""本机服务：静态文件 + 编辑器前端 + 各模式的接口。

安全（和旧编辑服务一致，见 docs/compat.md A 组）：只绑 127.0.0.1；Host 必须是 127.0.0.1 / localhost；
POST 只收 JSON、带 Origin 时必须是本机；静态文件只出静态根里的、不出以 . 开头的路径、不跟出静态根的符号链接。
路由按模式注册：页面模式一组（page_mode.register），以后骨架模式（/api/lab/*）再加一组。
"""
import json
import mimetypes
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlparse

from . import page_mode

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(os.path.dirname(HERE), 'web')
EDITOR_DIR = os.path.join(WEB, 'editor')

MIME = {'.html': 'text/html; charset=utf-8', '.htm': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
        '.mjs': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8', '.json': 'application/json; charset=utf-8',
        '.svg': 'image/svg+xml', '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.webp': 'image/webp',
        '.gif': 'image/gif', '.avif': 'image/avif', '.ico': 'image/x-icon', '.mp4': 'video/mp4', '.webm': 'video/webm',
        '.mov': 'video/quicktime', '.woff2': 'font/woff2', '.woff': 'font/woff', '.ttf': 'font/ttf', '.otf': 'font/otf',
        '.md': 'text/plain; charset=utf-8', '.txt': 'text/plain; charset=utf-8', '.xml': 'application/xml; charset=utf-8',
        '.pdf': 'application/pdf', '.mp3': 'audio/mpeg', '.wasm': 'application/wasm'}
LOCAL_HOSTS = ('127.0.0.1', 'localhost')


class Router:
    def __init__(self):
        self.gets, self.posts = {}, {}
        self.static_headers = []   # fn(真实路径, 文件字节) -> 额外响应头；骨架模式给零件库 json 加 X-Lab-Rev

    def get(self, path, fn):
        self.gets[path] = fn

    def post(self, path, fn):
        self.posts[path] = fn


def mime_of(path):
    ext = os.path.splitext(path)[1].lower()
    return MIME.get(ext) or mimetypes.guess_type(path)[0] or 'application/octet-stream'


def safe_join(root, rel):
    """rel 解码后的站内路径 → 静态根里的真实文件；越界、点开头、不存在都返回 None。"""
    rel = rel.lstrip('/')
    if rel.endswith('/') or rel == '':
        rel += 'index.html'
    parts = rel.replace('\\', '/').split('/')
    if any(p.startswith('.') for p in parts if p):
        return None
    root_real = os.path.realpath(root)
    full = os.path.realpath(os.path.join(root_real, rel))
    if not full.startswith(root_real + os.sep) or not os.path.isfile(full):
        return None
    return full


def make_handler(cfg, router, proxy=None):
    class Handler(BaseHTTPRequestHandler):
        server_version = 'design-studio'   # HTTP/1.0：一次请求一个连接，和旧编辑服务一样，出错时不会有残留的请求体

        def log_message(self, fmt, *a):
            first = str(a[0]) if a else ''
            if '/api/' in first or (a and len(a) > 1 and str(a[1]).startswith(('4', '5')) and not str(a[1]).startswith('404')):
                self.server.log(fmt % a)

        # ---------- 回应 ----------
        def send_json(self, code, obj):
            body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def redirect(self, code, location):
            self.send_response(code)
            self.send_header('Location', quote(location, safe="/?=&#%:@!$'()*+,;~"))   # 响应头只能是 ASCII
            self.send_header('Content-Length', '0')
            self.end_headers()

        def send_file(self, full):
            size = os.path.getsize(full)
            rng = self.headers.get('Range')
            start, end, status = 0, size - 1, 200
            if rng:
                m = re.fullmatch(r'\s*bytes=(\d*)-(\d*)\s*', rng)
                if m and (m.group(1) or m.group(2)):
                    if m.group(1):
                        start = int(m.group(1))
                        end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
                    else:                                  # bytes=-N：最后 N 字节
                        n = int(m.group(2))
                        start, end = max(size - n, 0), size - 1
                    if start >= size or start > end:
                        self.send_response(416)
                        self.send_header('Content-Range', f'bytes */{size}')
                        self.send_header('Content-Length', '0')
                        self.end_headers()
                        return
                    status = 206
            extra = {}
            with open(full, 'rb') as fh:
                if router.static_headers:      # 骨架模式：额外头要按整份文件算（零件库 json 的指纹）
                    whole = fh.read()
                    body = whole[start:end + 1]
                    for fn in router.static_headers:
                        extra.update(fn(full, whole) or {})
                else:                          # 页面模式：只读请求的那一段（视频分段请求不用整份读）
                    fh.seek(start)
                    body = fh.read(end - start + 1) if size else b''
            self.send_response(status)
            for k, v in extra.items():
                self.send_header(k, v)
            if status == 206:
                self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
            self.send_header('Content-Type', mime_of(full))
            self.send_header('Accept-Ranges', 'bytes')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            if self.command != 'HEAD':
                self.wfile.write(body)

        # ---------- 校验 ----------
        def host_ok(self):
            host = (self.headers.get('Host') or '').split(':')[0]
            return host in LOCAL_HOSTS

        def origin_ok(self):
            origin = self.headers.get('Origin')
            if not origin:
                return True
            u = urlparse(origin)
            try:
                port = u.port
            except ValueError:
                return False
            # 页面都从本服务打开，Origin 必须就是本服务：http、本机、同端口（不带端口的 http://localhost 是 80 端口，也拒）
            return u.scheme == 'http' and u.hostname in LOCAL_HOSTS and port == self.server.server_address[1]

        # ---------- GET ----------
        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            if not self.host_ok():
                return self.send_json(403, {'error': 'host'})
            u = urlparse(self.path)
            path = unquote(u.path)
            if path in router.gets:
                return router.gets[path](self, parse_qs(u.query))
            if path in ('', '/') and cfg.root_redirect and not proxy:
                return self.redirect(302, cfg.root_redirect)
            mount = cfg.mount
            if path == mount.rstrip('/'):
                return self.redirect(301, mount + (('?' + u.query) if u.query else ''))
            if path.startswith(mount):
                full = safe_join(EDITOR_DIR, path[len(mount):])
                if not full:
                    return self.send_json(404, {'error': 'not found'})
                return self.send_file(full)
            if proxy:
                return proxy.forward(self)
            full = safe_join(cfg.static_root, path)
            if not full:
                return self.send_json(404, {'error': 'not found'})
            return self.send_file(full)

        # ---------- POST ----------
        def do_POST(self):
            if not self.host_ok():
                return self.send_json(403, {'error': 'host'})
            if not self.origin_ok():
                return self.send_json(403, {'error': 'origin'})
            route = unquote(urlparse(self.path).path)
            if route not in router.posts:
                if proxy and not route.startswith(cfg.mount):
                    return proxy.forward(self)
                self.close_connection = True
                return self.send_json(404, {'error': 'not found'})
            if not (self.headers.get('Content-Type') or '').startswith('application/json'):
                self.close_connection = True
                return self.send_json(415, {'error': 'json only'})
            try:
                n = int(self.headers.get('Content-Length') or 0)
                if n > 32 * 1024 * 1024:
                    self.close_connection = True
                    return self.send_json(400, {'ok': False, 'error': '请求体太大'})
                body = json.loads(self.rfile.read(n) or b'{}')
                if not isinstance(body, dict):
                    raise ValueError('请求体要是 JSON 对象')
            except ValueError as e:                 # 坏 JSON、不是对象、Content-Length 不是数字：请求的错
                return self.send_json(400, {'ok': False, 'error': f'请求体不对：{e}'})
            try:
                return router.posts[route](self, body)
            except Exception as e:
                return self.send_json(500, {'ok': False, 'error': str(e)})

    return Handler


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, addr, handler, logger=None):
        self._logger = logger
        super().__init__(addr, handler)

    def log(self, msg):
        if self._logger:
            self._logger(msg)


class LabSite:
    """骨架模式单独起服务时的站点：没有站点配置，静态根就是仓库根。"""
    def __init__(self, repo):
        self.base = self.static_root = os.path.abspath(repo)
        self.mount = '/_studio/'
        self.root_redirect = None


def build_lab(repo, port, logger=None):
    from . import lab_mode
    router = Router()
    lab_mode.register(router, repo)
    return Server(('127.0.0.1', port), make_handler(LabSite(repo), router), logger)


def build(cfg, port, proxy=None, logger=None):
    router = Router()
    page_mode.register(router, cfg)
    if cfg.raw.get('lab'):   # 站点配置里开了骨架模式：同一个服务也提供工作台接口，索引页在 <挂载路径>lab/
        if proxy or os.path.realpath(cfg.static_root) != os.path.realpath(cfg.base):
            raise SystemExit('站点配置里 lab: true 要求 static_root 就是 base（页面包按仓库根取文件），且不能和 --proxy 一起用')
        from . import lab_mode
        lab_mode.register(router, cfg.base, gallery_paths=(cfg.mount + 'lab/',))
    # 接口在编辑器挂载路径下再挂一份（/_studio/api/...），编辑器自己只用这一份。
    # 根路径的 /api/* 留给站点里直接调接口的页面（如反馈框）；代理模式下它属于被编辑的应用，原样转发，避免同名接口被截走
    mount_api = cfg.mount + 'api/'
    for table in (router.gets, router.posts):
        for path, fn in list(table.items()):
            if path.startswith('/api/'):
                table[mount_api + path[len('/api/'):]] = fn
                if proxy:
                    del table[path]
    handler = make_handler(cfg, router, proxy)
    return Server(('127.0.0.1', port), handler, logger)
