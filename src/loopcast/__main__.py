"""Command-line entry point for LoopCast."""
import argparse
import asyncio
import json
from importlib.metadata import version
import os
from pathlib import Path
import socket
import sqlite3
import sys


async def _serve(args, settings):
    import uvicorn
    from .api import create_app
    from .diagnostics import configure_logging

    configure_logging()

    managed = stop_task = None
    desktop_session = None
    if args.managed_stdin:
        from .managed import ManagedInput
        managed = await ManagedInput().open()
        if args.desktop_session_stdin:
            from .desktop_session import DesktopSession
            from .platform_info import source_revision
            token = await managed.session()
        stop_task = asyncio.create_task(managed.stop())

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((args.host, args.port))
    sock.listen(128)
    sock.setblocking(False)
    port = sock.getsockname()[1]
    if args.desktop_session_stdin:
        desktop_session = DesktopSession(token, f'http://127.0.0.1:{port}', args.public_origin, source_revision())
    media_settings = json.loads(args.media_config.read_text()) if args.media_config else None
    social_settings = json.loads(args.social_config.read_text()) if args.social_config else None
    schedule_settings = json.loads(args.schedule_config.read_text()) if args.schedule_config else None
    from .schedule_config import ScheduleSettings
    schedule_settings = ScheduleSettings.model_validate(schedule_settings or {})
    inspection_settings=json.loads(args.inspection_config.read_text()) if args.inspection_config else None
    from .inspection_models import InspectionSettings
    inspection_settings=InspectionSettings.model_validate(inspection_settings or {})
    app = create_app(args.data_dir, settings, schedule_settings=schedule_settings, inspection_settings=inspection_settings, public_origin=args.public_origin, trusted_proxies=tuple(args.trusted_proxy), media_settings=media_settings, social_settings=social_settings, desktop_session=desktop_session, desktop_identity=args.desktop_guard_identity)
    config = uvicorn.Config(app, host=args.host, port=port, workers=1, reload=False, access_log=False, proxy_headers=False, lifespan="on", timeout_graceful_shutdown=5)
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        while not server.started:
            if task.done():
                await task
                if stop_task and stop_task.done() and getattr(app.state, 'shutdown_complete', False): break
                raise RuntimeError("ASGI server stopped before readiness")
            if stop_task and stop_task.done(): server.should_exit = True
            await asyncio.sleep(.02)
        if not (stop_task and stop_task.done()): print(f"READY http://127.0.0.1:{port}", flush=True)
        if stop_task:
            done, _ = await asyncio.wait([task, stop_task], return_when=asyncio.FIRST_COMPLETED)
            if stop_task in done:
                if hasattr(app.state, 'runtime'): app.state.runtime.stopping.set()
                server.should_exit = True
        await task
        if managed:
            if managed.invalid or not getattr(app.state, 'shutdown_complete', False): raise RuntimeError('Managed shutdown failed')
            print('STOPPED', flush=True)
    finally:
        if not task.done():
            server.should_exit = True
            await task
        sock.close()
        if stop_task:
            stop_task.cancel()
            await asyncio.gather(stop_task, return_exceptions=True)
        if managed: managed.close()


def main():
    if sys.argv[1:2] == ['desktop-diagnostics']:
        from .desktop_diagnostics import cli
        raise SystemExit(cli(sys.argv[2:]))
    parser = argparse.ArgumentParser(prog='loopcast', description='LoopCast creation workspace and controlled publishing. Live sending defaults off.')
    parser.add_argument('--version', action='version', version=f"%(prog)s {version('loopcast')}")
    commands = parser.add_subparsers(dest='command')
    commands.add_parser('desktop-diagnostics', help='Collect bounded offline desktop evidence without starting an owner')
    commands.add_parser('platform-info', help='Read installed source and platform capabilities without opening application data')
    serve = commands.add_parser('serve', help='Start the local single-user fixture console')
    serve.add_argument('--host', choices=['127.0.0.1', 'localhost'], default='127.0.0.1')
    serve.add_argument('--port', type=int, default=8080)
    serve.add_argument('--managed-stdin', action='store_true', help='Stop through a private bounded STOP/EOF input pipe')
    serve.add_argument('--desktop-session-stdin', action='store_true', help='Require a private desktop session first frame')
    serve.add_argument('--desktop-guard-identity', help=argparse.SUPPRESS)
    serve.add_argument('--data-dir', type=Path, default=Path.home() / '.local/share/loopcast')
    serve.add_argument('--generator-config', type=Path, help='Trusted local generator settings JSON')
    serve.add_argument('--media-config', type=Path, help='Pinned local media tools JSON')
    serve.add_argument('--inspection-config',type=Path,help='Trusted read-only inspection policy JSON; periodic and live checks default off')
    serve.add_argument('--schedule-config', type=Path, help='Trusted finite local-publication policy JSON; automatic sending defaults off')
    serve.add_argument('--social-config', type=Path, help='Isolated social adapter settings JSON; live sending defaults off')
    serve.add_argument('--public-origin', default=os.environ.get('LOOPCAST_PUBLIC_ORIGIN'), help='Exact public origin served by the configured reverse proxy')
    serve.add_argument('--trusted-proxy', action='append', default=[item for item in os.environ.get('LOOPCAST_TRUSTED_PROXIES', '').split(',') if item], help='Trusted reverse-proxy address or CIDR; may be repeated')
    diagnose = commands.add_parser('diagnose', help='Inspect generation configuration without generating or making provider calls')
    diagnose.add_argument('--generator-config', type=Path)
    diagnose.add_argument('--media-config', type=Path)
    setup = commands.add_parser('install-generator', help='Build the pinned upstream and install its frozen dependencies into a fresh directory')
    for name in ['source','destination','python','uv','ffmpeg','ffprobe']: setup.add_argument('--'+name, type=Path, required=True)
    setup.add_argument('--uv-sha256', required=True)
    setup.add_argument('--network-config', type=Path)
    setup.add_argument('--plan', action='store_true', help='Validate inputs and print steps without installing or downloading')
    demo = commands.add_parser('demo-feedback', help='Run an explicit fixture-only two-round feedback workflow in a fresh directory')
    demo.add_argument('--data-dir', type=Path, required=True)
    demo.add_argument('--media-config', type=Path, required=True)
    backup = commands.add_parser('backup', help='Create or verify a private offline consistency backup')
    backup_actions = backup.add_subparsers(dest='backup_action', required=True)
    backup_create = backup_actions.add_parser('create', help='Require a stopped owner and create a new private backup directory')
    backup_create.add_argument('--data-dir', type=Path, required=True)
    backup_create.add_argument('--output', type=Path, required=True)
    backup_verify = backup_actions.add_parser('verify', help='Verify a private backup against an independently recorded manifest digest')
    backup_verify.add_argument('--backup', type=Path, required=True)
    backup_verify.add_argument('--expected-manifest-sha256', required=True)
    restore = commands.add_parser('restore', help='Restore a verified package to a new directory with uploads durably blocked')
    restore.add_argument('--backup', type=Path, required=True)
    restore.add_argument('--expected-manifest-sha256', required=True)
    restore.add_argument('--data-dir', type=Path, required=True)
    reconcile = commands.add_parser('reconcile', help='Read the controlled-restore publication boundary without calling a platform')
    reconcile.add_argument('--data-dir', type=Path, required=True)
    snapshot_parser = commands.add_parser('snapshot',help='Read a redacted runtime and schema compatibility report without starting services')
    snapshot_parser.add_argument('--data-dir',type=Path,required=True)
    snapshot_parser.add_argument('--format',choices=['json','markdown'],default='json')
    snapshot_parser.add_argument('--output',type=Path)
    snapshot_parser.add_argument('--media-config',type=Path)
    snapshot_parser.add_argument('--generator-config',type=Path)
    args = parser.parse_args()
    if args.command == 'platform-info':
        from .platform_info import platform_info
        print(json.dumps(platform_info(), sort_keys=True)); return
    if args.command == 'snapshot':
        from .operations_snapshot import snapshot,render_snapshot,write_report
        try:
            def optional_settings(path):
                if path is None:return None
                try:return json.loads(path.read_text())
                except (OSError,ValueError):return {}
            media = optional_settings(args.media_config)
            generator = optional_settings(args.generator_config)
            report = snapshot(args.data_dir,media,generator)
            output = render_snapshot(report,args.format)
            if args.output:
                protected = [args.data_dir,*[path for path in (args.media_config,args.generator_config) if path]]
                if isinstance(media,dict):protected.extend(path for key,path in media.items() if key in {'ffmpeg','ffprobe'} and isinstance(path,str))
                if isinstance(generator,dict):protected.extend(path for key,path in generator.items() if key in {'wheel','python','config_ref','ffmpeg','ffprobe'} and isinstance(path,str))
                write_report(args.output,output,protected)
            else:print(output,end='')
            return
        except (OSError,ValueError,RuntimeError,KeyError,TypeError,AttributeError,sqlite3.Error):
            parser.exit(1,'Snapshot unavailable. Check the existing state, compatible schema and trusted optional tool configuration.\n')
    if args.command in {'restore', 'reconcile'}:
        from .restore import restore_backup, reconciliation_report
        try:
            report = restore_backup(args.backup, args.expected_manifest_sha256, args.data_dir) if args.command == 'restore' else reconciliation_report(args.data_dir)
            print(json.dumps(report, sort_keys=True)); return
        except (OSError, ValueError, RuntimeError, KeyError, TypeError, AttributeError, sqlite3.Error):
            parser.exit(1, 'Recovery operation failed. Check the pinned package, compatible schema, fresh private destination and operator permissions.\n')
    if args.command == 'backup':
        from .backup import create_backup, verify_backup
        try:
            if args.backup_action == 'create':
                result = create_backup(args.data_dir, args.output)
            else:
                manifest = verify_backup(args.backup, args.expected_manifest_sha256)
                result = {'state': 'VERIFIED_PRIVATE_BACKUP', 'backup_id': manifest['backup_id'], 'schema_version': manifest['schema_version'], 'files': len(manifest['files'])}
            print(json.dumps(result, sort_keys=True)); return
        except (OSError, ValueError, RuntimeError, KeyError, TypeError, AttributeError, sqlite3.Error):
            parser.exit(1, 'Backup operation failed. Stop the data owner and check the private package, paths, permissions, space and recorded digest.\n')
    if args.command == 'demo-feedback':
        from .feedback_demo import demonstrate
        try:
            print(json.dumps(demonstrate(args.data_dir,json.loads(args.media_config.read_text())),indent=2));return
        except (OSError,ValueError,RuntimeError): parser.exit(1,'Fixture demonstration failed. Use a fresh directory and verified media tools. No real model or platform was called.\n')
    if args.command == 'diagnose':
        from .generator_diagnostics import inspect_generator
        try:
            settings=json.loads(args.generator_config.read_text()) if args.generator_config else None
            media=json.loads(args.media_config.read_text()) if args.media_config else None
            report=inspect_generator(settings,media)
        except (OSError,ValueError): report=inspect_generator({})
        print(json.dumps(report));raise SystemExit(0 if report['callable'] else 1)
    if args.command == 'install-generator':
        from .mpt_install import plan, install
        try:
            value=plan(args.source,args.destination,args.python,args.uv,args.uv_sha256,args.ffmpeg,args.ffprobe)
            result=value if args.plan else install(value,json.loads(args.network_config.read_text()) if args.network_config else None)
            print(json.dumps(result,indent=2));return
        except (OSError,ValueError,RuntimeError): parser.exit(1,'Generator installation failed. Check source revision/cleanliness, tool digests, platform and the private installation record. No provider was called.\n')
    if args.command != 'serve':
        parser.print_help()
        return
    if not 0 <= args.port <= 65535:
        parser.error('port must be between 0 and 65535')
    if args.desktop_session_stdin and (not args.managed_stdin or not args.public_origin or not args.desktop_guard_identity):
        parser.error('desktop mode requires its managed process owner and public origin')
    if args.desktop_guard_identity and not args.desktop_session_stdin:
        parser.error('desktop ownership requires a managed desktop session')
    try:
        settings = json.loads(args.generator_config.read_text()) if args.generator_config else None
        asyncio.run(_serve(args, settings))
    except (OSError, RuntimeError, ValueError):
        parser.exit(1, 'Unable to start: check the port and data directory availability.\n')


if __name__ == '__main__':
    main()
