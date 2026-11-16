"""Helpers shared by the tests (they run on Linux; nothing here needs Windows)."""

import importlib.util
import json
import os
import shutil
import socket
import stat
import sys
import time

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(TESTS_DIR)
STUB = os.path.join(TESTS_DIR, "stub_llama_server.py")
LAUNCHER = os.path.join(REPO_DIR, "plamo-3-translate-launcher.py")
INSTALLER = os.path.join(REPO_DIR, "plamo-3-translate-installer.py")
SERVER = os.path.join(REPO_DIR, "plamo-3-translate-server.py")
CLIENT = os.path.join(REPO_DIR, "plamo-3-translate-client.py")
BAT = os.path.join(REPO_DIR, "install-plamo-3-translate.bat")


def load_module(name, path):
    """Import a script whose file name is not a module name."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def port_open(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def wait_until(condition, timeout=20, step=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(step)
    return False


def stub_exe_text():
    """An executable that stands for llama-server.exe: it runs the stub with this Python."""
    return f"#!/bin/sh\nexec '{sys.executable}' '{STUB}' \"$@\"\n"


def write_stub_exe(path):
    with open(path, "w", encoding="utf-8") as f:
        f.write(stub_exe_text())
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def make_install_tree(root, **settings):
    """A minimal install directory for the launcher, with the stub as llama-server.exe."""
    cfg = {"version": 1, "destdir": root, "model": "0.8b", "quant": "Q4_K_M", "backend": "cpu",
           "gpus": "", "ctx_size": "auto", "web_port": free_port(), "llama_port": free_port(),
           "listen": "no", "expose_llama": "no", "llama_tag": "b12366"}
    cfg.update(settings)
    os.makedirs(os.path.join(root, "app"))
    os.makedirs(os.path.join(root, "models"))
    bin_dir = os.path.join(root, "llama.cpp", f"{cfg['llama_tag']}-{cfg['backend']}")
    os.makedirs(bin_dir)
    shutil.copy(SERVER, os.path.join(root, "app"))
    shutil.copy(LAUNCHER, os.path.join(root, "app"))
    shutil.copy(CLIENT, os.path.join(root, "app"))
    write_stub_exe(os.path.join(bin_dir, "llama-server.exe"))
    with open(os.path.join(root, "models", f"plamo-3-translate-{cfg['model']}-{cfg['quant']}.gguf"), "wb") as f:
        f.write(b"GGUF stub")
    with open(os.path.join(root, "install.json"), "w", encoding="utf-8") as f:
        json.dump(cfg, f)
    if cfg["expose_llama"] == "yes":
        with open(os.path.join(root, "llama-api-key.txt"), "w", encoding="ascii") as f:
            f.write("0123456789abcdef" * 4 + "\n")
    return cfg


class FileServer:
    """A local HTTP server for the download tests: serves bytes from a dict, with Range
    support.  cut[path] = N closes the connection after N bytes of the first response
    for that path; ignore_range answers 200 with the whole file to a Range request."""

    def __init__(self, files):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        self.files = files
        self.requests = []  # (path, Range header or None)
        self.cut = {}
        self.ignore_range = False
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                pass

            def do_GET(self):
                rng = self.headers.get("Range")
                owner.requests.append((self.path, rng))
                data = owner.files.get(self.path)
                if data is None:
                    self.send_error(404)
                    return
                start = 0
                if rng and not owner.ignore_range:
                    start = int(rng.split("=")[1].split("-")[0])
                    if start >= len(data):
                        self.send_error(416)
                        return
                    self.send_response(206)
                    self.send_header("Content-Range", f"bytes {start}-{len(data) - 1}/{len(data)}")
                else:
                    self.send_response(200)
                body = data[start:]
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                cut = owner.cut.pop(self.path, None)
                if cut is not None:
                    self.wfile.write(body[:cut])
                    self.wfile.flush()
                    self.connection.shutdown(socket.SHUT_RDWR)
                    return
                self.wfile.write(body)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join()
