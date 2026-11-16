#!/usr/bin/env python3
"""A stand-in for llama-server, for the tests (standard library only).

It accepts the command line the launcher and the installer use, and answers /health,
/v1/models, /props and /v1/chat/completions.  Behaviour is set through the environment:

    STUB_LOAD_SECONDS   seconds during which /health answers 503 "Loading model" (default 0)
    STUB_EXIT_STATUS    exit with this status after STUB_LOAD_SECONDS instead of becoming ready
    STUB_N_CTX          context size reported by /props (default: -c, or 262144)
    STUB_TRANSLATION    the translation returned (default "Hello")
    STUB_BUSY_SECONDS   seconds after start-up during which /slots reports a busy slot
    STUB_DEVICES        output lines of --list-devices, separated by "|"
    STUB_ARGS_FILE      write the command line (JSON) to this file at start-up
    STUB_GPU_LINES      log lines printed only with -lv 4 (as llama-server b12366 does with
                        "using device ..."), separated by "|", followed by a "load_tensors:" line
    STUB_PROBE_ARGS_FILE  with -lv 4, write the command line here instead of STUB_ARGS_FILE
    LLAMA_API_KEY       when set, requests other than /health need "Authorization: Bearer <key>"
"""

import argparse
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def main():
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--version", action="store_true")
    parser.add_argument("--list-devices", action="store_true")
    parser.add_argument("--model")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("-c", type=int, dest="ctx")
    parser.add_argument("--device")
    parser.add_argument("--jinja", action="store_true")
    parser.add_argument("--no-prefill-assistant", action="store_true")
    parser.add_argument("--alias", default="stub")
    parser.add_argument("-lv", type=int, dest="verbosity", default=3)
    parser.add_argument("-ngl")
    parser.add_argument("-fit")
    parser.add_argument("--no-warmup", action="store_true")
    args = parser.parse_args()

    if args.version:
        print("version: 12366 (stub)", file=sys.stderr)
        return 0
    if args.list_devices:
        print("Available devices:")
        devices = [d for d in os.environ.get("STUB_DEVICES", "").split("|") if d]
        for line in devices or ["(none)"]:
            print("  " + line)
        return 0

    args_file = os.environ.get("STUB_ARGS_FILE")
    if args.verbosity >= 4:
        args_file = os.environ.get("STUB_PROBE_ARGS_FILE")
    if args_file:
        with open(args_file, "w", encoding="utf-8") as f:
            json.dump({"argv": sys.argv[1:], "api_key": os.environ.get("LLAMA_API_KEY", "")}, f)
    print(f"stub llama-server: model {args.model}", flush=True)
    if not args.model or not os.path.isfile(args.model):
        print("stub llama-server: model file not found", flush=True)
        return 1
    if args.verbosity >= 4:
        for line in filter(None, os.environ.get("STUB_GPU_LINES", "").split("|")):
            print("0.00.123.456 I llama_model_load_from_file_impl: " + line, flush=True)

    load_seconds = float(os.environ.get("STUB_LOAD_SECONDS", "0"))
    exit_status = os.environ.get("STUB_EXIT_STATUS")
    api_key = os.environ.get("LLAMA_API_KEY", "")
    n_ctx = int(os.environ.get("STUB_N_CTX") or args.ctx or 262144)
    translation = os.environ.get("STUB_TRANSLATION", "Hello")
    ready_at = time.monotonic() + load_seconds
    busy_until = time.monotonic() + float(os.environ.get("STUB_BUSY_SECONDS", "0"))

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *a):
            pass

        def reply(self, status, obj):
            body = json.dumps(obj).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def allowed(self):
            if time.monotonic() < ready_at:
                self.reply(503, {"error": {"code": 503, "message": "Loading model",
                                           "type": "unavailable_error"}})
                return False
            if api_key and self.headers.get("Authorization") != "Bearer " + api_key:
                self.reply(401, {"error": {"code": 401, "message": "Invalid API Key",
                                           "type": "authentication_error"}})
                return False
            return True

        def do_GET(self):
            if self.path == "/health":
                if time.monotonic() < ready_at:
                    self.reply(503, {"error": {"code": 503, "message": "Loading model",
                                               "type": "unavailable_error"}})
                else:
                    self.reply(200, {"status": "ok"})
            elif not self.allowed():
                pass
            elif self.path == "/v1/models":
                self.reply(200, {"object": "list", "data": [{"id": args.alias, "object": "model"}]})
            elif self.path == "/slots":
                self.reply(200, [{"id": 0, "is_processing": time.monotonic() < busy_until}])
            elif self.path == "/props":
                self.reply(200, {"model_path": args.model,
                                 "default_generation_settings": {"n_ctx": n_ctx}})
            else:
                self.reply(404, {"error": {"code": 404, "message": "not found"}})

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            request = json.loads(self.rfile.read(length) or b"{}")
            if not self.allowed():
                return
            if self.path != "/v1/chat/completions":
                self.reply(404, {"error": {"code": 404, "message": "not found"}})
                return
            if request.get("stream"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Connection", "close")
                self.end_headers()
                for piece in (translation[:2], translation[2:]):
                    chunk = {"choices": [{"delta": {"content": piece}, "finish_reason": None}]}
                    self.wfile.write(b"data: " + json.dumps(chunk).encode("utf-8") + b"\n\n")
                    self.wfile.flush()
                chunk = {"choices": [{"delta": {}, "finish_reason": "stop"}]}
                self.wfile.write(b"data: " + json.dumps(chunk).encode("utf-8") + b"\n\ndata: [DONE]\n\n")
                self.close_connection = True
            else:
                self.reply(200, {"choices": [{"message": {"role": "assistant", "content": translation},
                                              "finish_reason": "stop"}]})

    try:
        httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError as e:
        print(f"stub llama-server: cannot listen on {args.host}:{args.port}: {e}", flush=True)
        return 1
    httpd.daemon_threads = True
    if exit_status is not None:
        def die():
            print("stub llama-server: failing as requested", flush=True)
            os._exit(int(exit_status))
        threading.Timer(load_seconds, die).start()
    print(f"stub llama-server: listening on {args.host}:{args.port}", flush=True)
    if args.verbosity >= 4:
        print("load_tensors: loading model tensors, this can take a while...", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
