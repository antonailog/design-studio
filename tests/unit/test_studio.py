"""design-studio 单元测试：写回、原句定位、HTML 写回、站点配置，外加一个真起服务的小集成测试。

跑法（仓库根目录）：python3 -m unittest discover -s tests/unit -v
只用标准库。
"""
import http.client
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from studio import writeback as wb  # noqa: E402
from studio.config import Config, ConfigError  # noqa: E402

DATA = '''window.SITE = {
 "_note": "说明，不参与查找",
 "person": {"name": "阿東", "about": ["第一段 <b>加粗</b> 文字", "第二段"]},
 "stamps": [
   { "k": "清华", "hl": ["清华硕士", "优秀硕士毕业论文"] },
   { "k": "研发", "hl": ["研发"] }
 ],
 "tools": [
  {"id": "a", "title": "工具甲", "line": "引号 \\"内\\" 和反斜杠 \\\\ 和 \\u4e2d", "url": "https://x/甲", "n": 3, "ok": true, "x": null},
  {"id": "b", "title": "工具乙", "line": "重复的一句", "meta": ["★ 1", "MIT"]}
 ],
 "projects": [{"title": "项目丙", "line": "重复的一句"}],
 "footer": "在时代列车上，享受自己的好奇心"
};
'''


class Cfg:
    """locate() 只用到这几个属性。"""
    allowed_tags = ['b', 'strong', 'em']
    skip_leaf = {'id', 'url'}
    section_keys = {'工具': ['tools'], '项目': ['projects']}


def read(path):
    with open(path, encoding='utf-8') as f:
        return f.read()


def write(path, text):
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text)


def text_item(old, new, section='页面', old_html=None, new_html=None, hint=None):
    return {'type': '文字', 'where': {'section': section}, 'oldText': old, 'newText': new,
            'oldHtml': old_html if old_html is not None else old, 'newHtml': new_html if new_html is not None else new, 'hint': hint}


class TestSpans(unittest.TestCase):
    def test_every_string_located_exactly(self):
        head, obj, tail, start = wb.split_data(DATA)
        spans = wb.string_spans(DATA, start)
        for path, (a, b) in spans.items():
            self.assertEqual(json.loads(DATA[a:b]), wb.get_path(obj, path), path)
        self.assertIn(('tools', 0, 'line'), spans)
        self.assertIn(('stamps', 0, 'hl', 1), spans)
        self.assertNotIn(('tools', 0, 'n'), spans)   # 数字不是字符串

    def test_bad_json(self):
        with self.assertRaises(wb.WriteError):
            wb.split_data('window.X = {"a": };')


class TestReplaceInData(unittest.TestCase):
    def check_only_changed(self, raw, path, value):
        new = wb.replace_in_data(raw, path, value)
        _, obj, _, start = wb.split_data(raw)
        a, b = wb.string_spans(raw, start)[path]
        self.assertEqual(new, raw[:a] + json.dumps(value, ensure_ascii=False) + raw[b:])
        _, obj2, _, _ = wb.split_data(new)
        wb.set_path(obj, path, value)
        self.assertEqual(obj2, obj)
        return new

    def test_simple(self):
        self.check_only_changed(DATA, ('footer',), '在时代列车上，享受好奇心')

    def test_compact_array_line_kept(self):
        new = self.check_only_changed(DATA, ('stamps', 0, 'hl', 0), '清华大学硕士')
        self.assertIn('{ "k": "清华", "hl": ["清华大学硕士", "优秀硕士毕业论文"] },', new)

    def test_escapes_in_old_and_new(self):
        self.check_only_changed(DATA, ('tools', 0, 'line'), '新的 "引号" \\ 和换行\n和 tab\t')

    def test_head_tail_kept(self):
        new = wb.replace_in_data(DATA, ('person', 'name'), 'Anton')
        self.assertTrue(new.startswith('window.SITE = {'))
        self.assertTrue(new.endswith('};\n'))

    def test_pure_json(self):
        raw = '{\n  "a": "一",\n  "b": [1, "二"]\n}\n'
        self.assertEqual(wb.replace_in_data(raw, ('b', 1), '三'), '{\n  "a": "一",\n  "b": [1, "三"]\n}\n')

    def test_non_string_rejected(self):
        with self.assertRaises(wb.WriteError):
            wb.replace_in_data(DATA, ('tools', 0, 'n'), '4')


class TestLocate(unittest.TestCase):
    def setUp(self):
        _, self.obj, _, _ = wb.split_data(DATA)

    def test_unique_whole(self):
        path, how, new = wb.locate(self.obj, text_item('工具乙', '工具乙二'), Cfg)
        self.assertEqual((path, how, new), (('tools', 1, 'title'), 'whole', '工具乙二'))

    def test_ambiguous_without_section(self):
        path, why, _ = wb.locate(self.obj, text_item('重复的一句', 'x'), Cfg)
        self.assertIsNone(path)
        self.assertIn('2 处', why)

    def test_ambiguous_resolved_by_section(self):
        path, _, _ = wb.locate(self.obj, text_item('重复的一句', 'x', section='项目'), Cfg)
        self.assertEqual(path, ('projects', 0, 'line'))

    def test_part_match(self):
        path, how, new = wb.locate(self.obj, text_item('享受自己的好奇心', '享受好奇心'), Cfg)
        self.assertEqual((path, how, new), (('footer',), 'part', '在时代列车上，享受好奇心'))

    def test_html_tags_kept(self):
        it = text_item('第一段 加粗 文字', '第一段 加粗 内容', old_html='第一段 <b>加粗</b> 文字', new_html='第一段 <b>加粗</b> 内容')
        path, how, new = wb.locate(self.obj, it, Cfg)
        self.assertEqual((path, new), (('person', 'about', 0), '第一段 <b>加粗</b> 内容'))

    def test_hint(self):
        path, _, _ = wb.locate(self.obj, text_item('第二段', '第二段改', hint='person.about.1'), Cfg)
        self.assertEqual(path, ('person', 'about', 1))

    def test_skips(self):
        self.assertIsNone(wb.locate(self.obj, text_item('说明，不参与查找', 'x'), Cfg)[0])        # _ 开头的字段
        self.assertIsNone(wb.locate(self.obj, text_item('https://x/甲', 'x'), Cfg)[0])           # skip_leaf
        self.assertIsNone(wb.locate(self.obj, text_item('工具乙', 'x'), Cfg, skip_top=('tools',))[0])

    def test_clean_html(self):
        self.assertEqual(wb.clean_html('<span class="a">一<b>二</b><br>三<i>四</i></span>', ['b', 'em']), '一<b>二</b>三四')


class TestHtml(unittest.TestCase):
    PAGE = ('<!doctype html><html><head><title>你好</title><style>a::after{content:"你好"}</style></head><body>\n'
            '<!-- 你好 --><h1 title="你好">你好</h1><p>A &amp; B</p><p>重复</p><p>重复</p>'
            '<script>var s = "你好";</script><textarea>你好</textarea></body></html>')

    def test_only_body_text(self):
        new = wb.replace_in_html(self.PAGE, '你好', '您好')
        self.assertEqual(new, self.PAGE.replace('<h1 title="你好">你好</h1>', '<h1 title="你好">您好</h1>'))

    def test_escaping(self):
        new = wb.replace_in_html(self.PAGE, 'A & B', 'A < B')
        self.assertIn('<p>A &lt; B</p>', new)

    def test_ambiguous(self):
        with self.assertRaises(wb.WriteError) as e:
            wb.replace_in_html(self.PAGE, '重复', 'x')
        self.assertIn('2 处', str(e.exception))

    def test_not_found(self):
        with self.assertRaises(wb.WriteError):
            wb.replace_in_html(self.PAGE, 'var s', 'x')   # 只在 script 里


class TestConfig(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.dir, 'site'))
        write(os.path.join(self.dir, 'site', 'index.html'), '<p>x</p>')

    def tearDown(self):
        shutil.rmtree(self.dir)

    def write(self, obj, name='ds.json'):
        p = os.path.join(self.dir, name)
        write(p, json.dumps(obj, ensure_ascii=False))
        return p

    def test_defaults(self):
        cfg = Config(self.write({'static_root': 'site', 'pages': {'首页': {'url': '/index.html', 'html': 'site/index.html'}}}))
        self.assertIsNone(cfg.port)
        self.assertEqual(cfg.mount, '/_studio/')
        self.assertEqual(cfg.root_redirect, '/index.html')
        self.assertEqual(cfg.pages['首页'].rel_sheet, '.design-studio/修改单/首页.json')
        self.assertEqual(cfg.rel(cfg.feedback_dir), '.design-studio/修改单/反馈')

    def test_base_and_mount(self):
        os.makedirs(os.path.join(self.dir, 'conf'))
        cfg = Config(self.write({'base': '..', 'static_root': 'site', 'editor_mount': '编辑器', 'port': 4580,
                                 'pages': {'p': {'url': '/index.html'}}}, 'conf/ds.json'))
        self.assertEqual(cfg.base, os.path.normpath(self.dir))
        self.assertEqual(cfg.mount, '/编辑器/')
        self.assertEqual(cfg.port, 4580)

    def test_scale_defaults(self):
        cfg = Config(self.write({'static_root': 'site', 'pages': {'首页': {'url': '/index.html'}}}))
        sc = cfg.editor_public()['scale']
        self.assertEqual(sc['pages'], [['首页', '/index.html']])
        self.assertEqual((sc['keep'], sc['keep_from'], sc['lock'], sc['presets'], sc['feedback']), ('', 34, [], [], '字号收敛'))

    def test_scale_full(self):
        cfg = Config(self.write({'static_root': 'site', 'pages': {'首页': {'url': '/index.html'}}, 'scale': {
            'pages': {'文章': '/posts/a/', '首页': '/重复的不加'}, 'keep': ['.h1', '.h2 *'], 'keep_from': 40,
            'lock': [{'selector': '.prose p', 'min': 16, 'max': 17.5, 'why': '正文'}],
            'presets': [{'name': 'A', 'recommend': True, 'steps': [[12, '小字'], 14, [16]], 'upto': [[14.5, 14], [12.5, 12]]}],
            'feedback': '字号'}}))
        sc = cfg.scale
        self.assertEqual(sc['pages'], [['首页', '/index.html'], ['文章', '/posts/a/']])
        self.assertEqual(sc['keep'], '.h1, .h2 *')
        self.assertEqual(sc['presets'][0]['steps'], [[12, '小字'], [14, ''], [16, '']])
        self.assertEqual(sc['presets'][0]['upto'], [[12.5, 12], [14.5, 14]])   # 按上限排好序
        self.assertTrue(sc['presets'][0]['recommend'])
        ok, lines = cfg.check()
        self.assertTrue(any('另审 1 个页面' in l for l in lines))

    def test_scale_errors(self):
        base = {'static_root': 'site', 'pages': {'p': {'url': '/index.html'}}}
        for bad in ([], {'pages': {'x': 'no-slash'}}, {'keep': 3}, {'keep_from': 0}, {'lock': [{'min': 1}]},
                    {'lock': [{'selector': '.a', 'min': 5, 'max': 1}]}, {'presets': [{'name': '', 'steps': [12]}]},
                    {'presets': [{'name': 'A', 'steps': []}]}, {'presets': [{'name': 'A', 'steps': [12, 12]}]},
                    {'presets': [{'name': 'A', 'steps': ['12px']}]}, {'presets': [{'name': 'A', 'steps': [12], 'upto': [[13, 14]]}]},
                    {'feedback': '../x'}):
            with self.assertRaises(ConfigError, msg=str(bad)):
                Config(self.write({**base, 'scale': bad}))

    def test_errors(self):
        for bad in ({}, {'pages': {}}, {'pages': {'p': {'url': 'index.html'}}}, {'port': 'x', 'pages': {'p': {'url': '/a'}}},
                    {'editor_mount': '/', 'pages': {'p': {'url': '/a'}}}, {'static_root': '没有', 'pages': {'p': {'url': '/a'}}}):
            with self.assertRaises(ConfigError, msg=str(bad)):
                Config(self.write(bad))


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


class TestServer(unittest.TestCase):
    """真起一个服务：HTML 写回、保存后钩子失败撤回、编辑器静态文件、安全校验。"""

    def setUp(self):
        from studio.server import build
        self.dir = tempfile.mkdtemp()
        self.html = os.path.join(self.dir, 'index.html')
        write(self.html, '<h1>原来的标题</h1><p>别的</p>')
        self.cfgpath = os.path.join(self.dir, 'ds.json')
        write(self.cfgpath, json.dumps({'pages': {'首页': {'url': '/index.html', 'html': 'index.html'}},
                   'after_save': {'label': '检查', 'cmd': ['{python}', '-c', 'import sys; t=open(sys.argv[1],encoding="utf-8").read(); print("命中：坏词") if "坏词" in t else None; sys.exit(1 if "坏词" in t else 0)', '{file}']}},
                                  ensure_ascii=False))
        self.port = free_port()
        self.srv = build(Config(self.cfgpath), self.port)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        shutil.rmtree(self.dir)

    def call(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        h = {'Host': f'127.0.0.1:{self.port}'}
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode()
            h['Content-Type'] = 'application/json'
        h.update(headers or {})
        from urllib.parse import quote
        c.request(method, quote(path, safe='/?=&'), data, h)
        r = c.getresponse()
        return r.status, r.read()

    def save(self, old, new):
        st, body = self.call('POST', '/api/save', {'page': '首页', 'items': [text_item(old, new)]})
        self.assertEqual(st, 200)
        return json.loads(body)

    def test_html_writeback(self):
        j = self.save('原来的标题', '新的标题')
        self.assertEqual(j['saved'][0]['status'], '已写入')
        self.assertEqual(j['saved'][0]['file'], 'index.html')
        self.assertEqual(read(self.html), '<h1>新的标题</h1><p>别的</p>')
        sheet = json.loads(read(os.path.join(self.dir, '.design-studio', '修改单', '首页.json')))
        self.assertEqual(sheet['items'][-1]['id'], j['saved'][0]['id'])

    def test_hook_rollback(self):
        before = read(self.html)
        j = self.save('原来的标题', '坏词标题')
        rec = j['saved'][0]
        self.assertEqual(rec['status'], '没写入')
        self.assertIn('检查命中', rec['note'])
        self.assertTrue(j['warnings'])
        self.assertEqual(read(self.html), before)

    def test_editor_and_config(self):
        st, body = self.call('GET', '/_studio/')
        self.assertEqual(st, 200)
        self.assertIn(b'editor.js', body)
        st, _ = self.call('GET', '/_studio/editor.js')
        self.assertEqual(st, 200)
        st, body = self.call('GET', '/api/config')
        self.assertEqual(json.loads(body)['mount'], '/_studio/')
        self.assertEqual(json.loads(body)['scale']['pages'], [['首页', '/index.html']])
        st, body = self.call('GET', '/_studio/scale.html')
        self.assertEqual(st, 200)
        self.assertIn(b'scale.js', body)
        self.assertEqual(self.call('GET', '/_studio/scale.js')[0], 200)
        st, body = self.call('GET', '/_studio/')
        self.assertIn(b'href="scale.html"', body)          # 编辑器顶栏有入口

    def test_security(self):
        self.assertEqual(self.call('GET', '/api/pages', headers={'Host': 'evil.example'})[0], 403)
        self.assertEqual(self.call('POST', '/api/save', {'page': '首页', 'items': []}, headers={'Origin': 'http://evil.example'})[0], 403)
        self.assertEqual(self.call('GET', '/../ds.json')[0], 404)
        self.assertEqual(self.call('GET', '/.design-studio/修改单/首页.json')[0], 404)


class TestProxyRoutes(unittest.TestCase):
    """代理模式：编辑器的接口在 /_studio/api/ 下；根路径的 /api/* 属于被编辑的应用，原样转发。"""

    def setUp(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from studio.proxy import Proxy
        from studio.server import build

        class Up(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                body = json.dumps({'from': 'upstream', 'path': self.path}).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('X-Frame-Options', 'DENY')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.up = ThreadingHTTPServer(('127.0.0.1', 0), Up)
        threading.Thread(target=self.up.serve_forever, daemon=True).start()
        self.dir = tempfile.mkdtemp()
        cfgpath = os.path.join(self.dir, 'ds.json')
        write(cfgpath, json.dumps({'pages': {'首页': {'url': '/'}}, 'root_redirect': None}))
        self.port = free_port()
        self.srv = build(Config(cfgpath), self.port, Proxy(f'http://127.0.0.1:{self.up.server_address[1]}'))
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        for s in (self.srv, self.up):
            s.shutdown()
            s.server_close()
        shutil.rmtree(self.dir)

    def get(self, path):
        c = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        c.request('GET', path, headers={'Host': f'127.0.0.1:{self.port}'})
        r = c.getresponse()
        return r.status, dict(r.getheaders()), r.read()

    def test_routes(self):
        st, h, body = self.get('/api/config')            # 应用自己的同名接口：转发，不被截走
        self.assertEqual(json.loads(body)['from'], 'upstream')
        self.assertNotIn('X-Frame-Options', h)            # 去掉，免得 iframe 被拦
        st, _, body = self.get('/_studio/api/config')    # 编辑器用的
        self.assertEqual(json.loads(body)['mount'], '/_studio/')
        st, _, body = self.get('/')
        self.assertEqual(json.loads(body)['path'], '/')


class TestLabInConfig(unittest.TestCase):
    """站点配置里 lab: true：同一个服务既是页面模式，又提供骨架模式的接口，索引页在 <挂载路径>lab/。"""

    def setUp(self):
        from studio.server import build
        self.dir = tempfile.mkdtemp()
        vis = os.path.join(self.dir, 'docs', 'assets', 'visuals', 'demo')
        os.makedirs(vis)
        write(os.path.join(vis, 'demo.tokens.json'), json.dumps({'title': '示例页面包', 'nodes': []}))
        write(os.path.join(vis, '2026-10-23-skeleton-v1.html'), '<p>骨架</p>')
        write(os.path.join(self.dir, 'index.html'), '<p>首页</p>')
        cfgpath = os.path.join(self.dir, 'ds.json')
        write(cfgpath, json.dumps({'lab': True, 'pages': {'首页': {'url': '/index.html', 'html': 'index.html'}}}))
        self.port = free_port()
        self.srv = build(Config(cfgpath), self.port)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        shutil.rmtree(self.dir)

    def get(self, path):
        c = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        c.request('GET', path, headers={'Host': f'127.0.0.1:{self.port}'})
        r = c.getresponse()
        return r.status, r.read()

    def test_both_modes(self):
        st, body = self.get('/api/lab/status')
        self.assertEqual(json.loads(body)['revs'].keys(), {'demo'})
        st, body = self.get('/_studio/lab/')
        self.assertIn('示例页面包', body.decode())
        st, body = self.get('/api/pages')
        self.assertIn('首页', json.loads(body))
        self.assertEqual(self.get('/')[0], 302)          # 根地址照旧跳页面模式的首页


if __name__ == '__main__':
    unittest.main()
