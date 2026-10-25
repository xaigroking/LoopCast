"""Command-line entry point for LoopCast."""
import argparse
import asyncio
import json
from importlib.metadata import version
import os
from pathlib import Path
import socket


async def _serve(args, settings):
    import uvicorn
    from .api import create_app

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((args.host, args.port))
    sock.listen(128)
    sock.setblocking(False)
    port = sock.getsockname()[1]
    app = create_app(args.data_dir, settings, public_origin=args.public_origin, trusted_proxies=tuple(args.trusted_proxy))
    config = uvicorn.Config(app, host=args.host, port=port, workers=1, reload=False, access_log=False, proxy_headers=False, lifespan="on")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        while not server.started:
            if task.done():
                await task
                raise RuntimeError("ASGI server stopped before readiness")
            await asyncio.sleep(.02)
        print(f"READY http://127.0.0.1:{port}", flush=True)
        await task
    finally:
        if not task.done():
            server.should_exit = True
            await task


def main():
    parser = argparse.ArgumentParser(prog='loopcast', description='LoopCast local fixture task console. Optional isolated video generation; real platform sending is unavailable.')
    parser.add_argument('--version', action='version', version=f"%(prog)s {version('loopcast')}")
    commands = parser.add_subparsers(dest='command')
    serve = commands.add_parser('serve', help='Start the local single-user fixture console')
    serve.add_argument('--host', choices=['127.0.0.1', 'localhost'], default='127.0.0.1')
    serve.add_argument('--port', type=int, default=8080)
    serve.add_argument('--data-dir', type=Path, default=Path.home() / '.local/share/loopcast')
    serve.add_argument('--generator-config', type=Path, help='Trusted local generator settings JSON')
    serve.add_argument('--public-origin', default=os.environ.get('LOOPCAST_PUBLIC_ORIGIN'), help='Exact public origin served by the configured reverse proxy')
    serve.add_argument('--trusted-proxy', action='append', default=[item for item in os.environ.get('LOOPCAST_TRUSTED_PROXIES', '').split(',') if item], help='Trusted reverse-proxy address or CIDR; may be repeated')
    args = parser.parse_args()
    if args.command != 'serve':
        parser.print_help()
        return
    if not 0 <= args.port <= 65535:
        parser.error('port must be between 0 and 65535')
    try:
        settings = json.loads(args.generator_config.read_text()) if args.generator_config else None
        asyncio.run(_serve(args, settings))
    except (OSError, RuntimeError, ValueError):
        parser.exit(1, 'Unable to start: check the port and data directory availability.\n')


if __name__ == '__main__':
    main()
