"""代理方式：被编辑的页面来自开发服务器（Vite、Next 等）时，经本服务转发，和编辑器同源。

编辑器要直接读 iframe 里的页面（选中、改字、读样式），浏览器只允许同源这么做。
开 `--proxy http://localhost:5173` 后，编辑器挂载路径和 /api/* 以外的请求都原样转给开发服务器。
这类站的文字一般写在组件代码里，写不回，文字、样式、意见全部进修改单交给 AI。
限制：只转普通 HTTP 请求，不转 WebSocket，开发服务器的热更新不会生效，改完手动刷新页面。
"""
import http.client
from urllib.parse import urlparse

HOP = {'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization', 'te', 'trailers', 'transfer-encoding',
       'upgrade', 'content-length', 'host', 'accept-encoding'}
DROP_RESPONSE = HOP | {'x-frame-options', 'content-encoding'}


class Proxy:
    def __init__(self, upstream):
        u = urlparse(upstream if '://' in upstream else 'http://' + upstream)
        if u.scheme not in ('http', 'https') or not u.hostname:
            raise SystemExit(f'--proxy 要写成 http://主机:端口，现在是 {upstream!r}')
        self.scheme, self.host, self.port = u.scheme, u.hostname, u.port
        self.netloc = u.netloc
        self.prefix = u.path.rstrip('/')
        self.origin = f'{u.scheme}://{u.netloc}'

    def forward(self, h):
        body = None
        n = int(h.headers.get('Content-Length') or 0)
        if n:
            body = h.rfile.read(n)
        headers = {k: v for k, v in h.headers.items() if k.lower() not in HOP}
        headers['Host'] = self.netloc
        if 'Origin' in headers:
            headers['Origin'] = self.origin
        if 'Referer' in headers:
            headers.pop('Referer')
        Conn = http.client.HTTPSConnection if self.scheme == 'https' else http.client.HTTPConnection
        conn = Conn(self.host, self.port, timeout=60)
        try:
            conn.request(h.command, self.prefix + h.path, body=body, headers=headers)
            r = conn.getresponse()
            data = r.read()
        except Exception as e:
            msg = f'代理转发到 {self.origin} 失败：{e}（开发服务器开着吗？）'.encode('utf-8')
            h.send_response(502)
            h.send_header('Content-Type', 'text/plain; charset=utf-8')
            h.send_header('Content-Length', str(len(msg)))
            h.end_headers()
            h.wfile.write(msg)
            return
        finally:
            conn.close()
        h.send_response(r.status)
        for k, v in r.getheaders():
            kl = k.lower()
            if kl in DROP_RESPONSE:
                continue
            if kl == 'location' and v.startswith(self.origin):
                v = v[len(self.origin):] or '/'
            h.send_header(k, v)
        h.send_header('Content-Length', str(len(data)))
        h.end_headers()
        if h.command != 'HEAD':
            h.wfile.write(data)
