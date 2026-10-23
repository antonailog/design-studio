"""文字写回：在源文件里找到原句，只替换那一段，文件其余字节一个不动。

两种源文件：
- 数据文件：JS 或 JSON，形如 `window.X = {...};` 或纯 `{...}`。在数据里按字段找原句（规则和旧编辑服务一致），
  定位到那个字符串字面量在原文里的起止位置，换成新值的 JSON 写法。写完重新解析，确认只有这一个字段变了。
- HTML：只看 <script>、<style>、注释和标签以外的文本，原句（按 HTML 转义后）在这些文本里正好出现一次才改。
写不回的（找不到、对上好几处、校验不过）交给调用方记进修改单。
"""
import copy
import html
import json
import re

WS = ' \t\r\n'
_NUM = re.compile(r'-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?')


class WriteError(Exception):
    """写回校验没过：撤回，改记修改单。"""


# ======================================================================
# 数据文件：定位字符串字面量
# ======================================================================
def split_data(raw):
    """把数据文件拆成 (头, 对象, 尾, 对象起点)。头尾是 `window.X = ` 和 `;` 这类包装，纯 JSON 时为空。"""
    try:
        i, j = raw.index('{'), raw.rindex('}') + 1
    except ValueError:
        raise WriteError('数据文件里找不到 {…} 对象')
    try:
        obj = json.loads(raw[i:j])
    except ValueError as e:
        raise WriteError(f'数据文件里的对象不是合法 JSON：{e}')
    return raw[:i], obj, raw[j:], i


class _Scanner:
    """极简 JSON 扫描器：不求值，只记下每个字符串值的路径和它在原文里的 [起, 止)。"""

    def __init__(self, s, pos):
        self.s, self.i, self.spans = s, pos, {}

    def ws(self):
        s, n = self.s, len(self.s)
        while self.i < n and s[self.i] in WS:
            self.i += 1

    def expect(self, ch):
        if self.s[self.i:self.i + 1] != ch:
            raise WriteError(f'数据文件第 {self.i} 个字符处应为 {ch!r}')
        self.i += 1

    def string(self):
        start = self.i
        self.expect('"')
        s = self.s
        while True:
            c = s[self.i]
            if c == '\\':
                self.i += 6 if s[self.i + 1] == 'u' else 2
            elif c == '"':
                self.i += 1
                return start, self.i
            else:
                self.i += 1

    def value(self, path):
        self.ws()
        c = self.s[self.i]
        if c == '{':
            self.i += 1
            self.ws()
            if self.s[self.i] == '}':
                self.i += 1
                return
            while True:
                self.ws()
                a, b = self.string()
                key = json.loads(self.s[a:b])
                self.ws()
                self.expect(':')
                self.value(path + (key,))
                self.ws()
                if self.s[self.i] == ',':
                    self.i += 1
                    continue
                self.expect('}')
                return
        if c == '[':
            self.i += 1
            self.ws()
            if self.s[self.i] == ']':
                self.i += 1
                return
            k = 0
            while True:
                self.value(path + (k,))
                k += 1
                self.ws()
                if self.s[self.i] == ',':
                    self.i += 1
                    continue
                self.expect(']')
                return
        if c == '"':
            self.spans[path] = self.string()
            return
        for lit in ('true', 'false', 'null'):
            if self.s.startswith(lit, self.i):
                self.i += len(lit)
                return
        m = _NUM.match(self.s, self.i)
        if not m:
            raise WriteError(f'数据文件第 {self.i} 个字符处不认识')
        self.i = m.end()


def string_spans(raw, start):
    sc = _Scanner(raw, start)
    try:
        sc.value(())
    except IndexError:
        raise WriteError('数据文件提前结束')
    return sc.spans


def set_path(o, path, value):
    for p in path[:-1]:
        o = o[p]
    o[path[-1]] = value


def get_path(o, path):
    for p in path:
        o = o[p]
    return o


def replace_in_data(raw, path, value):
    """把数据文件里 path 那个字符串换成 value，返回新原文。只动那一个字面量；校验不过抛 WriteError。"""
    head, obj, tail, start = split_data(raw)
    spans = string_spans(raw, start)
    if path not in spans:
        raise WriteError(f'数据文件里 {dotted(path)} 不是字符串，没法精确替换')
    a, b = spans[path]
    new_raw = raw[:a] + json.dumps(value, ensure_ascii=False) + raw[b:]
    want = copy.deepcopy(obj)
    set_path(want, path, value)
    h2, o2, t2, _ = split_data(new_raw)
    if (h2, t2) != (head, tail) or o2 != want:
        raise WriteError('写回后的数据和预期不一致，已撤回')
    return new_raw


# ======================================================================
# 数据文件：按原句找字段（规则和旧编辑服务一致，参数来自站点配置）
# ======================================================================
def dotted(path):
    return '.'.join(str(p) for p in path)


def parse_dotted(s):
    return tuple(int(x) if x.isdigit() else x for x in s.split('.'))


def clean_html(s, allowed):
    """只留允许的行内标签（默认 <b> <strong> <em>），换行标签和其余标签去掉。"""
    alt = '|'.join(re.escape(t) for t in allowed) or 'a^'
    s = re.sub(r'<br\s*/?>', '', s or '')
    s = re.sub(r'<(?!/?(?:%s)>)[^>]+>' % alt, '', s)
    return s.strip()


def has_allowed_tag(s, allowed):
    alt = '|'.join(re.escape(t) for t in allowed) or 'a^'
    return bool(re.search(r'</?(?:%s)>' % alt, s))


def walk(o, skip_leaf, skip_top=(), path=()):
    """数据里所有显示得出来的字符串：(路径, 值)。跳过 _ 开头的字段、skip_leaf 里的字段、顶层 skip_top 里的字段。"""
    if isinstance(o, dict):
        for k, v in o.items():
            if str(k).startswith('_') or k in skip_leaf or (not path and k in skip_top):
                continue
            yield from walk(v, skip_leaf, skip_top, path + (k,))
    elif isinstance(o, list):
        for i, v in enumerate(o):
            yield from walk(v, skip_leaf, skip_top, path + (i,))
    elif isinstance(o, str):
        yield path, o


def locate(data, item, cfg, skip_top=()):
    """在数据里找这段文字。返回 (路径, 'whole' | 'part', 新值)，找不到返回 (None, 原因, None)。
    编辑器对已知元素会带字段提示 hint，原句对得上就直接用它。"""
    allowed = cfg.allowed_tags
    old_html, old_text = clean_html(item.get('oldHtml', ''), allowed), (item.get('oldText') or '').strip()
    new_html, new_text = clean_html(item.get('newHtml', ''), allowed), (item.get('newText') or '').strip()
    pairs = [(old_html, new_html)] if old_html and has_allowed_tag(old_html, allowed) else []
    pairs.append((old_text, new_text))
    hint = item.get('hint')
    if hint and isinstance(hint, str):
        path = parse_dotted(hint)
        try:
            cur = get_path(data, path)
        except (KeyError, IndexError, TypeError):
            cur = None
        if isinstance(cur, str):
            for old, new in pairs:
                if old and cur == old:
                    return path, 'whole', new
    strings = list(walk(data, cfg.skip_leaf, skip_top))
    prefer = cfg.section_keys.get((item.get('where') or {}).get('section'), [])
    for old, new in pairs:
        if not old:
            continue
        hits = [p for p, v in strings if v == old]
        if len(hits) > 1 and prefer:
            hits = [p for p in hits if p[0] in prefer] or hits
        if len(hits) == 1:
            return hits[0], 'whole', new
        if len(hits) > 1:
            return None, f'原句在数据里出现了 {len(hits)} 处（{", ".join(dotted(p) for p in hits[:4])}），分不清改哪处', None
    # 页面上只显示了字段的一部分（比如按逗号分行）：原句在某个字段里只出现一次也算
    if len(old_text) >= 4:
        hits = [(p, v) for p, v in strings if old_text in v]
        if len(hits) > 1 and prefer:
            hits = [h for h in hits if h[0][0] in prefer] or hits
        if len(hits) == 1 and hits[0][1].count(old_text) == 1:
            p, v = hits[0]
            return p, 'part', v.replace(old_text, new_text)
    return None, '数据里找不到原句，可能写死在页面模板里', None


# ======================================================================
# HTML：只改正文文本里唯一出现的原句
# ======================================================================
_SKIP_BLOCK = re.compile(r'<!--.*?-->|<script\b[^>]*>.*?</script\s*>|<style\b[^>]*>.*?</style\s*>|<textarea\b[^>]*>.*?</textarea\s*>'
                         r'|<title\b[^>]*>.*?</title\s*>'   # 标签页标题不在页面上，编辑器也选不到
                         r'|<!\[CDATA\[.*?\]\]>|<![^>]*>|<\?[^>]*>|<[^>]*>', re.S | re.I)


def text_segments(raw):
    """HTML 里标签之外、且不在 script / style / textarea / title / 注释里的文本段：[(起, 止), ...]。"""
    out, pos = [], 0
    for m in _SKIP_BLOCK.finditer(raw):
        if m.start() > pos:
            out.append((pos, m.start()))
        pos = m.end()
    if pos < len(raw):
        out.append((pos, len(raw)))
    return out


def _count_in_segments(raw, segs, needle):
    hits = []
    for a, b in segs:
        k = raw.find(needle, a, b)
        while k != -1 and k + len(needle) <= b:
            hits.append(k)
            k = raw.find(needle, k + 1, b)
    return hits


def replace_in_html(raw, old_text, new_text):
    """原句按 HTML 转义后，在正文文本里正好出现一次就换掉；否则抛 WriteError 说明原因。"""
    old_text, new_text = (old_text or '').strip(), (new_text or '').strip()
    if not old_text:
        raise WriteError('原句是空的')
    needle = html.escape(old_text, quote=False)
    segs = text_segments(raw)
    hits = _count_in_segments(raw, segs, needle)
    if not hits:
        raise WriteError('HTML 正文里找不到原句（可能是脚本生成的，或者跨了好几个标签）')
    if len(hits) > 1:
        raise WriteError(f'原句在 HTML 正文里出现了 {len(hits)} 处，分不清改哪处')
    k = hits[0]
    rep = html.escape(new_text, quote=False)
    new_raw = raw[:k] + rep + raw[k + len(needle):]
    # 校验：新句在正文里出现次数 = 原来出现次数 + 1（原句本来不含新句时），文件其余部分不变
    before_new = len(_count_in_segments(raw, segs, rep)) if rep else 0
    after_new = len(_count_in_segments(new_raw, text_segments(new_raw), rep)) if rep else 0
    if rep and after_new != before_new + (0 if rep in needle else 1):
        raise WriteError('写回后在 HTML 正文里没对上新句，已撤回')
    return new_raw
