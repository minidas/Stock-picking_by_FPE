"""Local read-only range cache for one public, immutable SEC mirror database.

Only /sec.duckdb is served. No arbitrary URLs or local files can be requested.
Downloaded bytes are retained with offsets and hashes for research audit.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import atexit
import hashlib
import json
from pathlib import Path
import re
import threading
import uuid
from urllib.request import Request, urlopen
from urllib.parse import urlsplit


def _check_source_identity(folder: Path, source_url: str, size: int):
    """Bind cached bytes to a content object, ignoring temporary URL signatures."""
    parsed = urlsplit(source_url)
    # HF's final CDN path is content-addressed; expiry signatures are not identity.
    if parsed.hostname != 'cas-bridge.xethub.hf.co' or not re.search(r'/[0-9a-f]{64}$', parsed.path):
        raise ValueError('Cached access requires the resolved content-addressed HF CDN URL')
    identity = {'schema_version': 1, 'source_identity': f'https://{parsed.hostname}{parsed.path}', 'source_size': size}
    identity_path = folder / 'source_identity.json'
    if identity_path.exists():
        if json.loads(identity_path.read_text(encoding='utf-8')) != identity:
            raise ValueError('Byte cache belongs to a different source object')
    elif any(folder.glob('*.bin')):
        raise ValueError('Existing byte cache lacks source identity; verify provenance before reuse')
    else:
        identity_path.write_text(json.dumps(identity, indent=2), encoding='utf-8')
    return identity


def _read_cached_slice(path: Path, start: int, stop: int):
    """Read only the requested bytes from an already validated-size block."""
    if not 0 <= start <= stop:
        raise ValueError('Invalid cached byte range')
    with path.open('rb') as cached:
        cached.seek(start)
        data = cached.read(stop - start)
    if len(data) != stop - start:
        raise ValueError('Cached block returned a short byte range')
    return data


def _download_block(dest: Path, source_url: str, offset: int, count: int, size: int):
    """Publish a block only after its status, byte range and size are validated."""
    if offset < 0 or count <= 0 or offset + count > size:
        raise ValueError('Invalid upstream byte range')
    request = Request(source_url, headers={'Range': f'bytes={offset}-{offset + count - 1}'})
    with urlopen(request, timeout=120) as response:
        data = response.read(count + 1)
        expected = f'bytes {offset}-{offset + count - 1}/{size}'
        if response.status != 206 or response.headers.get('Content-Range') != expected or len(data) != count:
            raise ValueError('Invalid upstream byte range response')
    tmp = dest.with_name(dest.name + '.' + uuid.uuid4().hex + '.tmp')
    tmp.write_bytes(data)
    tmp.replace(dest)
    return hashlib.sha256(data).hexdigest()


def start_cache(raw: Path, source_url: str, allow_network=True):
    size = 5276446720
    chunk_size = 4 * 1024 * 1024
    folder = raw / 'range_blocks'
    folder.mkdir(exist_ok=True)
    _check_source_identity(folder, source_url, size)
    locks = {}
    guard = threading.Lock()

    def block(offset):
        with guard:
            lock = locks.setdefault(offset, threading.Lock())
        with lock:
            count = min(chunk_size, size - offset)
            dest = folder / f'{offset:012d}_{count}.bin'
            if dest.exists() and dest.stat().st_size == count:
                return dest
            if not allow_network:
                raise FileNotFoundError('Required byte range is not cached')
            digest = _download_block(dest, source_url, offset, count, size)
            entry = {'path': str(dest.relative_to(raw)), 'offset': offset, 'length': count,
                     'sha256': digest}
            with guard:
                with (raw / 'range_blocks.jsonl').open('a', encoding='utf-8') as log:
                    log.write(json.dumps(entry) + '\n')
            return dest

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def handle(self):
            try:
                super().handle()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

        def log_message(self, *args):
            pass

        def do_HEAD(self):
            if self.path != '/sec.duckdb':
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header('Content-Length', str(size))
            self.send_header('Accept-Ranges', 'bytes')
            self.end_headers()

        def do_GET(self):
            with guard:
                (raw / 'last_range_request.json').write_text(json.dumps({'path': self.path, 'range': self.headers.get('Range')}), encoding='utf-8')
            if self.path != '/sec.duckdb':
                self.send_error(404)
                return
            match = re.fullmatch(r'bytes=(\d+)-(\d+)', self.headers.get('Range', ''))
            if not match:
                self.send_error(416)
                return
            start, end = map(int, match.groups())
            if not 0 <= start <= end < size or end - start >= 64 * 1024 * 1024:
                self.send_error(416)
                return
            try:
                parts = []
                for off in range(start // chunk_size * chunk_size, end + 1, chunk_size):
                    path = block(off)
                    lo = max(start - off, 0)
                    hi = min(end - off + 1, path.stat().st_size)
                    parts.append(_read_cached_slice(path, lo, hi))
                content = b''.join(parts)
                self.send_response(206)
                self.send_header('Content-Length', str(len(content)))
                self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
                self.send_header('Accept-Ranges', 'bytes')
                self.end_headers()
                self.wfile.write(content)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
            except Exception as exc:
                print('Range cache upstream failure: ' + type(exc).__name__, flush=True)
                self.send_error(502)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    def close():
        server.shutdown()
        server.server_close()
    atexit.register(close)
    return server, f'http://127.0.0.1:{server.server_port}/sec.duckdb'
