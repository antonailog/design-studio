#!/usr/bin/env python3
"""design-studio 命令行入口。

用法：
  python3 design_studio.py serve <站点配置.json> [--port N] [--proxy http://localhost:5173]
  python3 design_studio.py check <站点配置.json>

serve 起本机服务，打印编辑器地址，浏览器打开就能在页面上改。
端口：命令行给了 --port 或配置里写了 port，就只用这个端口，被占就报错退出（不悄悄换端口：
浏览器里存的草稿和偏好按「地址 + 端口」隔离，换了端口看起来就像全丢了）；两者都没给，才从 4580 往后找空闲端口。
只用 Python 标准库。
"""
import argparse
import datetime
import os
import socket
import sys

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
    a = ap.parse_args()
    sys.exit(a.fn(a) or 0)


if __name__ == '__main__':
    main()
