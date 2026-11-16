"""Bundled macOS service; no development server or fixture data."""
import argparse
import json
import os
from pathlib import Path
import socket
import sys


def _normalize_code_object(code, module_name: str):
    """Strip build/install path prefixes recursively so bytecode hashes are path-independent."""
    import types

    consts = tuple(
        _normalize_code_object(item, module_name) if isinstance(item, types.CodeType) else item
        for item in code.co_consts
    )
    return code.replace(co_filename=module_name, co_consts=consts)


def _digest_backend_tree(root: Path) -> str:
    """Hash bundled backend resource/source files deterministically, excluding __pycache__."""
    import hashlib

    digest = hashlib.sha256()
    files = sorted(
        (
            path for path in root.rglob('*')
            if not path.is_symlink() and path.is_file()
            and path.name != '.DS_Store' and '__pycache__' not in path.parts
        ),
        key=lambda path: path.relative_to(root).as_posix(),
    )
    for path in files:
        digest.update(path.relative_to(root).as_posix().encode('utf-8'))
        digest.update(b'\0')
        digest.update(path.read_bytes())
        digest.update(b'\0')
    return digest.hexdigest()


def compute_backend_runtime_sha256(root: Path) -> str:
    """Compute a path-independent digest of bundled backend files and loaded app bytecode."""
    import hashlib
    import importlib.util
    import marshal
    import pkgutil
    import app

    digest = hashlib.sha256()
    app_dir = root / 'app'
    tree_digest = _digest_backend_tree(app_dir) if app_dir.is_dir() else ''
    digest.update(b'tree\0')
    digest.update(tree_digest.encode('ascii'))
    digest.update(b'\0')

    module_names = sorted(
        name for name in (
            {'app'} | {info.name for info in pkgutil.walk_packages(app.__path__, 'app.')}
        )
        if all(part.isidentifier() for part in name.split('.'))
    )
    for name in module_names:
        spec = importlib.util.find_spec(name)
        loader = spec.loader if spec is not None else None
        code = loader.get_code(name) if loader is not None and hasattr(loader, 'get_code') else None
        if code is None:
            continue
        normalized = _normalize_code_object(code, name)
        digest.update(name.encode('utf-8'))
        digest.update(b'\0')
        digest.update(marshal.dumps(normalized))
        digest.update(b'\0')
    return digest.hexdigest()


def validate_bundle(root: Path) -> dict:
    """Check the candidate before any running service is stopped or DB opened."""
    import re
    from app.frontend_release import _digest_tree

    manifest = json.loads((root / 'desktop-release.json').read_text())
    if (manifest.get('api_contract') != 2 or not isinstance(manifest.get('release_id'), str)
            or not manifest['release_id']):
        raise ValueError('Bundled release identity is invalid')
    backend_sha = manifest.get('backend_sha256')
    if not isinstance(backend_sha, str) or not re.fullmatch(r'[0-9a-f]{64}', backend_sha):
        raise ValueError('Bundled backend release digest is invalid')
    if _digest_tree(root / 'frontend') != manifest.get('frontend_sha256'):
        raise ValueError('Bundled frontend integrity check failed')
    if compute_backend_runtime_sha256(root) != manifest.get('backend_runtime_sha256'):
        raise ValueError('Bundled backend integrity check failed')
    if root.parent.name == 'service':
        resources_manifest = root.parent.parent / 'desktop-release.json'
        if resources_manifest.is_file() and json.loads(resources_manifest.read_text()) != manifest:
            raise ValueError('Bundled desktop release manifest mismatch')
    return manifest


def main():
    arguments = sys.argv[1:]
    if arguments[:1] == ['--hook']:
        from app.integrations.hook_entry import run_hook
        return run_hook(arguments[1:])
    if arguments[:2] == ['-m', 'app.cli']:
        if arguments[2:] == ['check-install']:
            validate_bundle(Path(sys._MEIPASS))
            return 0
        if arguments[2:] == ['print-backend-runtime-sha256']:
            print(compute_backend_runtime_sha256(Path(sys._MEIPASS)))
            return 0
        arguments = arguments[2:]
        if arguments and arguments[0] != 'serve':
            from app.cli import main as cli_main
            return cli_main(arguments)
        arguments = arguments[1:]
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-dir', required=True)
    parser.add_argument('--port', type=int, default=8420)
    parser.add_argument('--host', default='127.0.0.1', choices=['127.0.0.1'])
    args = parser.parse_args(arguments)
    selected_data = Path(args.data_dir).expanduser()
    from app.private_storage import check_directory_path, private_directory
    check_directory_path(selected_data)
    data = selected_data.resolve()
    os.environ['CONTEXTCORE_DATA_DIR'] = str(data)
    os.environ['CONTEXTCORE_DB'] = str(data / 'contextcore.db')
    os.environ['CONTEXTCORE_LISTEN_URL'] = f'http://127.0.0.1:{args.port}'
    root = Path(sys._MEIPASS)
    frontend = root / 'frontend'
    manifest = validate_bundle(root)
    from app.frontend_release import PinnedStaticFiles
    from app import runtime
    running, _ = runtime.is_service_running(data)
    if running:
        raise RuntimeError('An existing service already owns this data directory')
    # Reserve the port before opening the database or starting application tasks.
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(('127.0.0.1', args.port))
    listener.listen(128)
    from app.main import app
    app.state.release = manifest
    app.mount('/', PinnedStaticFiles(directory=frontend, html=True), name='desktop_frontend')
    import uvicorn
    private_directory(data)
    runtime.write_pid_file(os.getpid(), port=args.port, host='127.0.0.1', data_dir=data)
    runtime.clear_shutdown_receipt(data)
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=args.port,
            log_level='warning', timeout_graceful_shutdown=3))
    import signal
    def request_shutdown(signum, frame):
        server.should_exit = True
    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)
    try:
        server.run(sockets=[listener])
    finally:
        listener.close()
        record = runtime.read_pid_file(data)
        if record and record.get('pid') == os.getpid():
            runtime.remove_pid_file(data)


if __name__ == '__main__':
    import multiprocessing
    multiprocessing.freeze_support()
    sys.exit(main())
