"""Command-line entry point for LoopCast."""
import argparse
import json
from importlib.metadata import version
from pathlib import Path
import signal
import threading


def main():
    parser = argparse.ArgumentParser(prog='loopcast', description='LoopCast local fixture task console. Optional isolated video generation; real platform sending is unavailable.')
    parser.add_argument('--version', action='version', version=f"%(prog)s {version('loopcast')}")
    commands = parser.add_subparsers(dest='command')
    serve = commands.add_parser('serve', help='Start the local single-user fixture console')
    serve.add_argument('--host', choices=['127.0.0.1', 'localhost'], default='127.0.0.1')
    serve.add_argument('--port', type=int, default=8080)
    serve.add_argument('--data-dir', type=Path, default=Path.home() / '.local/share/loopcast')
    serve.add_argument('--generator-config', type=Path, help='Trusted local generator settings JSON')
    args = parser.parse_args()
    if args.command != 'serve':
        parser.print_help()
        return
    if not 0 <= args.port <= 65535:
        parser.error('port must be between 0 and 65535')
    from .api import Service
    try:
        settings = json.loads(args.generator_config.read_text()) if args.generator_config else None
        server = Service(args.host, args.port, args.data_dir, settings)
    except (OSError, RuntimeError, ValueError):
        parser.exit(1, 'Unable to start: check the port and data directory availability.\n')
    def stop(_signum, _frame):
        server.stopping.set()
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    print(f'READY http://127.0.0.1:{server.server_address[1]}', flush=True)
    try:
        server.serve_forever(poll_interval=.1)
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
