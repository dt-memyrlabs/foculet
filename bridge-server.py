#!/usr/bin/env python3
"""Foculet Chrome bridge server.

Runs on the local PC. Binds 127.0.0.1 ONLY (never exposed to the network).
The Chrome extension long-polls /poll for commands; foculet.py enqueues
commands via POST /cmd and reads results via GET /result/<id>.
No auth needed: localhost-only by design.
"""
import json
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = 18721

cmd_queue = []   # [{id, action, args}]
results = {}     # id -> {ok, data, error}
lock = threading.Condition()


class Handler(BaseHTTPRequestHandler):
    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/poll":
            # Long-poll up to 25s for the next command (keeps the
            # extension's service worker responsive).
            deadline = time.time() + 25
            with lock:
                while True:
                    if cmd_queue:
                        return self._json(cmd_queue.pop(0))
                    remaining = deadline - time.time()
                    if remaining <= 0:
                        return self._json({"wait": True})
                    lock.wait(timeout=min(remaining, 5))
        elif self.path.startswith("/result/"):
            cid = self.path[len("/result/"):]
            with lock:
                if cid in results:
                    return self._json(results.pop(cid))
            return self._json({"pending": True})
        elif self.path == "/health":
            with lock:
                return self._json({"ok": True, "queued": len(cmd_queue)})
        else:
            self.send_error(404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            body = {}
        if self.path == "/cmd":
            cid = uuid.uuid4().hex[:8]
            with lock:
                cmd_queue.append({
                    "id": cid,
                    "action": body.get("action"),
                    "args": body.get("args") or {},
                })
                lock.notify_all()
            return self._json({"id": cid})
        elif self.path == "/result":
            cid = body.get("id")
            if cid:
                with lock:
                    results[cid] = {
                        "ok": body.get("ok", True),
                        "data": body.get("data"),
                        "error": body.get("error"),
                    }
                    lock.notify_all()
            return self._json({"stored": True})
        else:
            self.send_error(404)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
