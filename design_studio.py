#!/usr/bin/env python3
"""design-studio 命令行入口。

用法：
  python3 design_studio.py serve <站点配置.json> [--port N] [--proxy http://localhost:5173]    页面模式：改真实页面
  python3 design_studio.py check <站点配置.json>
  python3 design_studio.py lab <仓库目录> [--port N] [--status | --stop]                        骨架模式：页面包工作台

serve 起本机服务，打印编辑器地址，浏览器打开就能在页面上改。
端口：命令行给了 --port 或配置里写了 port，就只用这个端口，被占就报错退出（不悄悄换端口：
浏览器里存的草稿和偏好按「地址 + 端口」隔离，换了端口看起来就像全丢了）；两者都没给，才从 4580 往后找空闲端口。
只用 Python 标准库。
"""
import argparse
import datetime
import json
import os
import signal
import socket
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from studio import __version__  # noqa: E402
from studio.config import DEFAULT_PORT, Config, ConfigError  # noqa: E402


def port_busy(port):
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(('127.0.0.1', port)) == 0


def pick_port(cfg, cli_port):
    pinned = cli_port or cfg.port
    if pinned:
        if port_busy(pinned):
            src = '命令行 --port' if cli_port else f'站点配置 {os.path.basename(cfg.file)} 里的 port'
            sys.exit(f'端口 {pinned} 已经被占用（{src}）。\n'
                     f'多半是已经开着一个 design-studio 或别的服务：先关掉它，或者用 --port 换一个端口。\n'
                     f'查是谁占的：lsof -nP -iTCP:{pinned} -sTCP:LISTEN')
        return pinned
    port = DEFAULT_PORT
    while port_busy(port):
        port += 1
    return port


def cmd_serve(a):
    try:
        cfg = Config(a.config)
    except ConfigError as e:
        sys.exit(f'站点配置有问题：{e}')
    proxy = None
    if a.proxy:
        from studio.proxy import Proxy
        proxy = Proxy(a.proxy)
    port = pick_port(cfg, a.port)
    from studio.server import build
    log = lambda msg: sys.stderr.write(f'{datetime.datetime.now():%H:%M:%S}  {msg}\n')
    try:
        srv = build(cfg, port, proxy, log)
    except OSError as e:
        sys.exit(f'端口 {port} 起不来：{e}')
    base = f'http://127.0.0.1:{port}'
    print(f'编辑器：{base}{cfg.mount}', flush=True)
    print(f'字号收敛：{base}{cfg.mount}scale.html', flush=True)
    if proxy:
        print(f'代理：{a.proxy} → {base}/（被编辑的页面经这里转发，和编辑器同源）', flush=True)
    elif cfg.root_redirect:
        print(f'页面预览：{base}/', flush=True)
    print(f'站点配置：{cfg.file}　按 Ctrl+C 停止', flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print('\n已停止', flush=True)
    finally:
        srv.server_close()


def cmd_lab(a):
    """骨架模式（原 forge-design-lab 的 serve.py）：静态根 = 仓库根，一个仓库一个常驻服务，状态记在 .forge/design-lab/server.json。
    和旧 serve.py 一样：已经在跑就不重复启动；端口默认 4577，被占就往后找（到 4599 为止）。"""
    from studio.lab_mode import find_packs, state_dir
    from studio.server import build_lab
    repo = os.path.abspath(a.repo)
    if not os.path.isdir(repo):
        print('✗ 仓库路径不存在:', repo)
        return 1
    sf = os.path.join(state_dir(repo), 'server.json')
    try:
        with open(sf, encoding='utf-8') as fh:
            cur = json.load(fh)
    except (OSError, ValueError):
        cur = None
    running = bool(cur and port_busy(cur.get('port', 0)))
    if a.status:
        if running:
            print(f"✓ 在跑 · http://127.0.0.1:{cur['port']}/ · pid {cur['pid']} · 绑定 {cur['repo']}")
            for p in find_packs(repo):
                print(f"    {p['page']:<14} {p.get('version') or '':<8} {p['nodes']} 节点  http://127.0.0.1:{cur['port']}{p['html'][0] if p['html'] else ''}")
        else:
            print('· 没在跑')
        return 0
    if a.stop:
        if running:
            try:
                os.kill(cur['pid'], signal.SIGTERM)
                print(f"✓ 已停止 pid {cur['pid']}")
            except OSError as e:
                print('✗ 停止失败:', e)
        else:
            print('· 没在跑')
        try:
            os.remove(sf)
        except OSError:
            pass
        return 0
    if running:
        print(f"= 已经在跑，不重复启动 · http://127.0.0.1:{cur['port']}/ · pid {cur['pid']}")
        return 0
    port = a.port or 4577
    while port < 4600 and port_busy(port):
        port += 1
    log = lambda msg: sys.stderr.write(f'[save] {msg}\n') if '/api/lab/save' in msg else None
    srv = build_lab(repo, port, log)
    with open(sf, 'w', encoding='utf-8') as fh:
        json.dump({'pid': os.getpid(), 'port': port, 'repo': repo,
                   'started': datetime.datetime.now().isoformat(timespec='seconds')}, fh, ensure_ascii=False)
    print(f'✓ 工作台服务 http://127.0.0.1:{port}/   绑定 {repo}')
    for p in find_packs(repo):
        print(f"    {p['page']:<14} http://127.0.0.1:{port}{p['html'][0] if p['html'] else ''}")
    sys.stdout.flush()
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()

    def bye(*_):
        try:
            os.remove(sf)
        except OSError:
            pass
        threading.Thread(target=srv.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, bye)
    signal.signal(signal.SIGINT, bye)
    while t.is_alive():
        t.join(0.5)
    srv.server_close()
    return 0


def cmd_check(a):
    try:
        cfg = Config(a.config)
    except ConfigError as e:
        print(f'✗ 站点配置有问题：{e}')
        return 1
    ok, lines = cfg.check()
    print('\n'.join(lines))
    print('✓ 配置正常' if ok else '✗ 有文件找不到，看上面的 ✗')
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(prog='design_studio.py', description='design-studio：在页面上做调整的本机工具')
    ap.add_argument('--version', action='version', version=f'design-studio {__version__}')
    sub = ap.add_subparsers(dest='cmd', required=True)
    s = sub.add_parser('serve', help='起本机服务，打开编辑器')
    s.add_argument('config', help='站点配置文件（design-studio.json）')
    s.add_argument('--port', type=int, help='端口；给了就只用它，被占就报错')
    s.add_argument('--proxy', help='被编辑的页面来自开发服务器（如 http://localhost:5173）时，经本服务转发，变成同源')
    s.set_defaults(fn=cmd_serve)
    c = sub.add_parser('check', help='只检查站点配置和文件，不起服务')
    c.add_argument('config')
    c.set_defaults(fn=cmd_check)
    lb = sub.add_parser('lab', help='骨架模式：页面包工作台（原 forge-design-lab 的 serve.py）')
    lb.add_argument('repo', help='仓库根目录（页面包在 docs/assets/visuals/ 下）')
    lb.add_argument('--port', type=int, default=0, help='默认 4577，被占用则自动递增')
    lb.add_argument('--status', action='store_true', help='查状态')
    lb.add_argument('--stop', action='store_true', help='停止')
    lb.set_defaults(fn=cmd_lab)
    a = ap.parse_args()
    sys.exit(a.fn(a) or 0)


if __name__ == '__main__':
    main()
