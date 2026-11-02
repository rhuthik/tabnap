"""Local HTTP API for the panel, the CLI and `tabnap top`. 127.0.0.1 only, every request needs the token."""
import asyncio
import html
import json
import os
import secrets
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources

from tabnap.config import DAEMON_FILE, LOG_FILE, STATE_DIR, TOKEN_FILE
from tabnap.engine import ActionError, log


def panel_html(token, title):
    page = resources.files("tabnap").joinpath("panel.html").read_text("utf-8")
    return page.replace("__TOKEN__", token).replace("__TITLE__", html.escape(title))


def load_token():
    """Same token across restarts, so an open panel just reconnects."""
    try:
        with open(TOKEN_FILE) as fh:
            token = fh.read().strip()
    except OSError:
        token = ""
    if not token:
        os.makedirs(STATE_DIR, exist_ok=True)
        token = secrets.token_urlsafe(18)
        with open(os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as fh:
            fh.write(token)
    return token


def make_handler(engine, loop, token):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, code, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def run(self, coro):
            return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=60)

        def handle_any(self):
            url = urllib.parse.urlparse(self.path)
            q = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
            if not secrets.compare_digest(q.get("t", ""), token):
                return self.reply(403, {"error": "bad token"})
            key = q.get("id", "")
            try:
                if url.path == "/panel":
                    return self.reply(200, panel_html(token, engine.cfg.panel_title).encode(), "text/html; charset=utf-8")
                if url.path == "/api/state":
                    return self.reply(200, engine.snapshot())
                if self.command != "POST":
                    return self.reply(405, {"error": "POST only"})
                actions = {"/api/hibernate": engine.hibernate, "/api/wake": engine.wake,
                           "/api/refresh": engine.refresh, "/api/toggle": engine.toggle, "/api/focus": engine.focus}
                if url.path in actions:
                    self.run(actions[url.path](key))
                    self.run(engine.settle())
                elif url.path in ("/api/pin", "/api/unpin"):
                    engine.set_pin(key, url.path == "/api/pin")
                elif url.path == "/api/auto":
                    engine.set_auto(q.get("on") == "1")
                elif url.path == "/api/adopt":
                    length = int(self.headers.get("Content-Length") or 0)
                    if length > 1 << 20:
                        return self.reply(413, {"error": "too large"})
                    try:
                        records = json.loads(self.rfile.read(length) or b"{}")
                    except ValueError:
                        return self.reply(400, {"error": "body must be JSON"})
                    engine.adopt(records)
                elif url.path == "/api/revert":
                    self.run(engine.revert_all())
                else:
                    return self.reply(404, {"error": "unknown endpoint"})
                return self.reply(200, {"ok": True})
            except ActionError as e:
                return self.reply(409, {"error": str(e)})
            except Exception as e:  # pylint: disable=broad-except
                log(f"api {url.path} failed: {type(e).__name__}: {e}")
                return self.reply(500, {"error": f"{type(e).__name__}: {e}"})

        do_GET = handle_any
        do_POST = handle_any

    return Handler


def serve(engine, loop, port, token):
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(engine, loop, token))
    except OSError:
        log(f"port {port} busy; using a random one (reopen the panel)")
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(engine, loop, token))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    with open(os.open(DAEMON_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as fh:
        json.dump({"pid": os.getpid(), "port": port, "token": token}, fh)
    return port


def api(path, method="POST", **params):
    """Call the running daemon. Raises SystemExit with a readable message on failure."""
    try:
        with open(DAEMON_FILE) as fh:
            info = json.load(fh)
    except (OSError, ValueError):
        raise SystemExit("tabnap daemon isn't running. Start it: tabnap install (or tabnap daemon)")
    url = f"http://127.0.0.1:{info['port']}{path}?" + urllib.parse.urlencode({"t": info["token"], **params})
    req = urllib.request.Request(url, data=b"" if method == "POST" else None, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            msg = json.load(e).get("error", str(e))
        except ValueError:
            msg = str(e)
        raise SystemExit(msg)
    except urllib.error.URLError as e:
        raise SystemExit(f"tabnap daemon not reachable ({e.reason}); see {LOG_FILE}")
