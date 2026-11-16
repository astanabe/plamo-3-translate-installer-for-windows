#!/usr/bin/env python3
"""Checks of an installed PLaMo 3 Translate on Windows, run by tests\\windows-check.bat.

It needs an installation made by install-plamo-3-translate.bat (in the default directory,
or --destdir DIR), administrator privileges, and no PLaMo 3 Translate window open.  It
starts and stops the servers several times, runs the client, re-runs the installer, and
writes what it saw to a log file to send back to the developer.  Two optional parts are
asked about at the start: --expose-llama (Windows may show a firewall dialog) and
uninstalling.

It also starts the Start menu and "Send to" shortcuts themselves (console windows appear and a browser
tab opens; leave them alone) and closes their windows.

Not covered (needs a person): the questions of an interactive installation, the UAC
prompts, a look at the page in the browser, and the Ctrl-C key itself (Ctrl-Break and
closing the window stand in for it).

Standard library only.  This file is not part of the unit tests (they run on Linux).
"""

import argparse
import base64
import ctypes
import http.client
import json
import os
import platform
import re
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import traceback

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTALL_BAT = os.path.join(REPO, "install-plamo-3-translate.bat")
DEFAULT_DESTDIR = r"C:\LLM\PLaMo3Translate"
LOG_PATH = os.path.join(os.path.expanduser("~"), "plamo-3-translate-check.log")
JAPANESE = "これは自動確認のための短い文章です。今日は良い天気です。"
LONG_JAPANESE = ("機械翻訳は、ある言語で書かれた文章を別の言語の文章に自動的に変換する技術である。"
                 "近年は大規模な言語モデルの発展によって、その品質は大きく向上した。") * 40
NEW_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

results = []  # (name, "PASS" | "FAIL" | "SKIP", detail)
log_file = None


def log(text=""):
    print(text.encode("ascii", "backslashreplace").decode("ascii"), flush=True)
    if log_file:
        log_file.write(text + "\n")
        log_file.flush()


def detail_only(text):
    """Write to the log file only (long output)."""
    if log_file:
        log_file.write(text.rstrip("\n") + "\n")
        log_file.flush()


class Skip(Exception):
    pass


def check(name, function):
    log(f"--- {name}")
    try:
        detail = function() or ""
        status = "PASS"
    except Skip as e:
        status, detail = "SKIP", str(e)
    except Exception as e:
        status, detail = "FAIL", f"{type(e).__name__}: {e}"
        detail_only(traceback.format_exc())
    results.append((name, status, detail))
    log(f"    {status}" + (f": {detail}" if detail else ""))
    return status == "PASS"


def expect(condition, message):
    if not condition:
        raise AssertionError(message)


def decode(data):
    """Output of a program: UTF-8, or else the ANSI code page (system messages)."""
    data = data or b""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("mbcs" if sys.platform == "win32" else "utf-8", "replace")


def tail(text, lines=15):
    return "\n".join(text.splitlines()[-lines:])


# ---------------------------------------------------------------- helpers

class Installation:
    def __init__(self, destdir):
        self.destdir = destdir
        self.python = os.path.join(destdir, "python", "python.exe")
        self.launcher = os.path.join(destdir, "app", "plamo-3-translate-launcher.py")
        self.installer = os.path.join(destdir, "app", "plamo-3-translate-installer.py")
        self.server = os.path.join(destdir, "app", "plamo-3-translate-server.py")
        self.client_bat = os.path.join(destdir, "plamo-3-translate-client.bat")
        self.key_file = os.path.join(destdir, "llama-api-key.txt")
        self.reload()

    def reload(self):
        with open(os.path.join(self.destdir, "install.json"), encoding="utf-8") as f:
            self.cfg = json.load(f)
        self.web_port = int(self.cfg["web_port"])
        self.llama_port = int(self.cfg["llama_port"])


def port_open(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def http_request(port, method, path, body=None, headers=None, timeout=10):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        return response.status, response.read()
    except (OSError, http.client.HTTPException):
        return None
    finally:
        conn.close()


def wait_until(condition, timeout, step=0.5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(step)
    return False


class Launcher:
    """A launcher started in the background, with its output in a file."""

    def __init__(self, inst, mode, workdir):
        self.inst = inst
        self.mode = mode
        self.out_path = os.path.join(workdir, f"launcher-{mode}-{int(time.time() * 1000)}.txt")
        self.out = open(self.out_path, "wb")
        self.proc = subprocess.Popen(
            [inst.python, inst.launcher, "--mode", mode, "--no-browser"],
            stdin=subprocess.DEVNULL, stdout=self.out, stderr=subprocess.STDOUT,
            cwd=inst.destdir, creationflags=NEW_GROUP)

    def output(self):
        with open(self.out_path, "rb") as f:
            return decode(f.read())

    def wait_ready(self, timeout=600):
        inst = self.inst

        def ready():
            if self.proc.poll() is not None:
                raise AssertionError(f"the launcher ended (status {self.proc.returncode}):\n{tail(self.output())}")
            health = http_request(inst.llama_port, "GET", "/health", timeout=5)
            if not (health and health[0] == 200):
                return False
            return self.mode == "llama" or port_open(inst.web_port)
        expect(wait_until(ready, timeout), f"not ready within {timeout} s:\n{tail(self.output())}")

    def stop(self):
        """Ctrl-Break, as a stand-in for Ctrl-C; returns the exit status."""
        if self.proc.poll() is None:
            self.proc.send_signal(signal.CTRL_BREAK_EVENT)
            try:
                self.proc.wait(timeout=60)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
                raise AssertionError("the launcher did not stop within 60 s after Ctrl-Break")
        self.out.close()
        return self.proc.returncode

    def kill(self):
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()
        self.out.close()


def run_launcher(inst, mode):
    """Run a launcher that is expected to end by itself; (status, output)."""
    result = subprocess.run([inst.python, inst.launcher, "--mode", mode, "--no-browser"],
                            stdin=subprocess.DEVNULL, capture_output=True, timeout=900, cwd=inst.destdir)
    return result.returncode, decode(result.stdout) + decode(result.stderr)


def run_client(inst, args, workdir, data=None, timeout=900):
    kwargs = {"input": data} if data is not None else {"stdin": subprocess.DEVNULL}
    result = subprocess.run(["cmd.exe", "/d", "/c", "call", inst.client_bat] + args,
                            capture_output=True, timeout=timeout, cwd=workdir, **kwargs)
    return result.returncode, result.stdout, decode(result.stderr)


def run_installer(args, timeout=3600):
    """Run install-plamo-3-translate.bat with options, without a terminal; (status, output)."""
    result = subprocess.run(["cmd.exe", "/d", "/c", "call", INSTALL_BAT] + args,
                            stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout, cwd=REPO)
    output = decode(result.stdout) + decode(result.stderr)
    detail_only(f"[install-plamo-3-translate.bat {' '.join(args)}] exit status {result.returncode}\n{output}")
    return result.returncode, output


def powershell(script):
    """Run a short inline PowerShell script; returns its output."""
    encoded = base64.b64encode(("$ProgressPreference = 'SilentlyContinue'\n" + script).encode("utf-16-le"))
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand",
                             encoded.decode("ascii")], stdin=subprocess.DEVNULL, capture_output=True, timeout=120)
    return decode(result.stdout)


def launcher_pids(mode):
    """Process ids of the launchers running in this mode (e.g. started from a shortcut)."""
    output = powershell("Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
                        "ForEach-Object { \"$($_.ProcessId)`t$($_.CommandLine)\" }")
    pids = []
    for line in output.splitlines():
        pid, _, command = line.partition("\t")
        if "plamo-3-translate-launcher.py" in command and f"--mode {mode}" in command and pid.strip().isdigit():
            pids.append(int(pid))
    return pids


def kill_launchers():
    for mode in ("webui", "llama", "translate"):
        for pid in launcher_pids(mode):
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)


def users_rights(path):
    """The icacls lines for BUILTIN\\Users, and whether they give read and execute only."""
    result = subprocess.run(["icacls", path], capture_output=True, timeout=60)
    text = result.stdout.decode("oem", "replace")
    detail_only(text)
    expect(result.returncode == 0, f"icacls failed: {text}")
    lines = [line for line in text.splitlines() if "BUILTIN\\Users" in line]
    read_only = bool(lines) and all(
        re.fullmatch(r"\s*(\((OI|CI|IO|I)\))*\(RX\)\s*", line.split("BUILTIN\\Users:", 1)[1]) for line in lines)
    others = "Authenticated Users" in text or "Everyone" in text
    return [line.strip() for line in lines], read_only and not others


def ports_closed(inst):
    return not port_open(inst.web_port) and not port_open(inst.llama_port)


# ----------------------------------------------------------------- checks

def checks_permissions(inst):
    def permissions():
        lines, read_only = users_rights(inst.destdir)
        expect(lines, "no entry for BUILTIN\\Users")
        expect(read_only, f"Users (or other groups) have more than read and execute: {lines}")
        return lines[0]
    check("install directory is read-only for Users", permissions)

    def parent_permissions():
        parent = os.path.dirname(inst.destdir)
        lines, read_only = users_rights(parent)
        if not read_only:
            # Only a parent that the installer created itself is protected.
            raise Skip(f"{parent} is writable for users; fine if it existed before the installation: {lines}")
        return lines[0]
    check("parent directory made by the installer is read-only for Users", parent_permissions)


def checks_llama_mode(inst, workdir):
    launcher = None

    def start():
        nonlocal launcher
        launcher = Launcher(inst, "llama", workdir)
        launcher.wait_ready()
        expect(not port_open(inst.web_port), "the web server port is open in llama mode")
    if not check("llama-server shortcut mode starts", start):
        if launcher:
            launcher.kill()
        return

    def translate_file():
        path = os.path.join(workdir, "check-ja.txt")
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(JAPANESE + "\n")
        status, out, err = run_client(inst, ["check-ja.txt"], workdir)
        detail_only(err)
        expect(status == 0, f"exit status {status}: {tail(err)}")
        target = os.path.join(workdir, "check-ja.en.txt")
        expect(os.path.isfile(target), f"check-ja.en.txt was not written: {os.listdir(workdir)}")
        with open(target, encoding="utf-8") as f:
            text = f.read().strip()
        expect(text, "the translation is empty")
        expect(not [n for n in os.listdir(workdir) if n.endswith(".tmp")], "a temporary file was left")
        return text
    check("client: file -> check-ja.en.txt", translate_file)

    def translate_stdin():
        status, out, err = run_client(inst, ["-q", "-"], workdir, data=(JAPANESE + "\n").encode("utf-8"))
        expect(status == 0, f"exit status {status}: {tail(err)}")
        text = out.decode("utf-8").strip()  # must be valid UTF-8
        expect(text, "nothing on standard output")
        return text
    check("client: UTF-8 standard input -> standard output", translate_stdin)

    def crlf_and_bom():
        path = os.path.join(workdir, "check-crlf.txt")
        with open(path, "wb") as f:
            f.write(b"\xef\xbb\xbf" + (JAPANESE + "\r\n\r\n" + JAPANESE + "\r\n").encode("utf-8"))
        status, out, err = run_client(inst, ["-q", "-o", "check-crlf-out.txt", "check-crlf.txt"], workdir)
        expect(status == 0, f"exit status {status}: {tail(err)}")
        with open(os.path.join(workdir, "check-crlf-out.txt"), "rb") as f:
            data = f.read()
        expect(data.startswith(b"\xef\xbb\xbf"), "the BOM was not kept")
        expect(b"\r\n" in data and b"\n" not in data.replace(b"\r\n", b""), "CRLF line ends were not kept")
    check("client: BOM and CRLF are kept", crlf_and_bom)

    def read_only_destination():
        target = os.path.join(workdir, "check-readonly.txt")
        with open(target, "w", encoding="utf-8") as f:
            f.write("keep\n")
        os.chmod(target, stat.S_IREAD)
        try:
            start_time = time.monotonic()
            status, out, err = run_client(inst, ["-q", "-f", "-o", "check-readonly.txt", "check-ja.txt"], workdir)
            elapsed = time.monotonic() - start_time
            expect(status == 2, f"exit status {status} (expected 2): {tail(err)}")
            expect("cannot be overwritten" in err, f"unexpected message: {tail(err)}")
            with open(target, encoding="utf-8") as f:
                expect(f.read() == "keep\n", "the read-only file was changed")
            return f"{err.strip().splitlines()[-1]} ({elapsed:.1f} s)"
        finally:
            os.chmod(target, stat.S_IWRITE | stat.S_IREAD)
    check("client: -f on a read-only destination fails before translating", read_only_destination)

    def locked_destination():
        target = os.path.join(workdir, "check-locked.txt")
        with open(target, "w", encoding="utf-8") as f:
            f.write("keep\n")
        # Hold the file open without sharing, as some editors do (CreateFileW, share mode 0).
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.restype = ctypes.c_void_p
        kernel32.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
                                         ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        held = kernel32.CreateFileW(target, 0x80000000 | 0x40000000, 0, None, 3, 0x80, None)  # no sharing
        expect(held and held != ctypes.c_void_p(-1).value, f"could not open the file exclusively ({ctypes.get_last_error()})")
        try:
            status, out, err = run_client(inst, ["-q", "-f", "-o", "check-locked.txt", "check-ja.txt"], workdir)
        finally:
            kernel32.CloseHandle(held)
        expect(status == 2, f"exit status {status} (expected 2): {tail(err)}")
        expect("cannot be overwritten" in err, f"unexpected message: {tail(err)}")
        return err.strip().splitlines()[-1]
    check("client: -f on a destination open in another program fails before translating", locked_destination)

    def reader_goes_away():
        # As "plamo-3-translate-client ... | more" ended with q: the reader of standard
        # output goes away while the client still translates.
        path = os.path.join(workdir, "check-long.txt")
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write((JAPANESE + "\n\n") * 30)
        proc = subprocess.Popen(["cmd.exe", "/d", "/c", "call", inst.client_bat, "-q", "--chunk-chars", "100",
                                 "-o", "-", "check-long.txt"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, cwd=workdir)
        try:
            first = proc.stdout.read(1)
            expect(first, "no output")
            proc.stdout.close()
            err = decode(proc.stderr.read())
            status = proc.wait(timeout=300)
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.stderr.close()
        detail_only(err)
        expect("Traceback" not in err, f"traceback:\n{tail(err, 30)}")
        expect(status == 1, f"exit status {status} (expected 1): {tail(err)}")
        return f"exit status 1, no traceback ({err.strip() or 'no message'})"
    check("client: the reader of standard output goes away (| more, q)", reader_goes_away)

    def webui_conflict():
        status, output = run_launcher(inst, "webui")
        expect(status == 1, f"exit status {status}: {tail(output)}")
        expect("llama-server is already running on TCP port" in output, tail(output))
    check("Web UI while llama-server runs: error", webui_conflict)

    def second_llama():
        status, output = run_launcher(inst, "llama")
        expect(status == 1, f"exit status {status}: {tail(output)}")
        expect("already running" in output, tail(output))
    check("second llama-server: error", second_llama)

    def stop():
        status = launcher.stop()
        output = launcher.output()
        detail_only(output)
        expect(status == 0, f"exit status {status}:\n{tail(output)}")
        expect("Traceback" not in output, f"traceback:\n{tail(output, 30)}")
        expect(wait_until(lambda: ports_closed(inst), 20), "a port is still open after the launcher ended")
    if not check("llama-server mode stops on Ctrl-Break (status 0, ports closed)", stop):
        launcher.kill()


def checks_webui_mode(inst, workdir):
    launcher = None

    def start():
        nonlocal launcher
        launcher = Launcher(inst, "webui", workdir)  # right after the stop above: the ports must be free at once
        launcher.wait_ready()
        page = http_request(inst.web_port, "GET", "/")
        expect(page and page[0] == 200 and b"<title>PLaMo 3 Translate</title>" in page[1], "unexpected page")
    if not check("Web UI mode starts right after a stop", start):
        if launcher:
            launcher.kill()
        return

    def api():
        body = json.dumps({"text": JAPANESE, "target_lang": "en"}).encode("utf-8")
        result = http_request(inst.web_port, "POST", "/api/translate", body=body,
                              headers={"Content-Type": "application/json"}, timeout=600)
        expect(result and result[0] == 200, f"unexpected answer: {result}")
        text = json.loads(result[1])["translation"]
        expect(text.strip(), "empty translation")
        return text
    check("web server: JSON API translates", api)

    def second_webui():
        status, output = run_launcher(inst, "webui")
        expect(status == 0, f"exit status {status}: {tail(output)}")
        expect("already running" in output, tail(output))
    check("second Web UI: only reports that it is running (status 0)", second_webui)

    def llama_conflict():
        status, output = run_launcher(inst, "llama")
        expect(status == 1, f"exit status {status}: {tail(output)}")
        expect("can be used as it is" in output, tail(output))
    check("llama-server while the Web UI runs: error with a hint", llama_conflict)

    def client_with_webui():
        status, out, err = run_client(inst, ["-q", "-"], workdir, data=(JAPANESE + "\n").encode("utf-8"))
        expect(status == 0 and out.strip(), f"exit status {status}: {tail(err)}")
    check("client works while the Web UI runs", client_with_webui)

    def second_server(host):
        def run():
            env = dict(os.environ, PLAMO3_LLAMA_URL=f"http://127.0.0.1:{inst.llama_port}")
            result = subprocess.run([inst.python, inst.server, "--host", host, "--port", str(inst.web_port)],
                                    stdin=subprocess.DEVNULL, capture_output=True, timeout=30, env=env)
            err = decode(result.stderr)
            expect(result.returncode == 1, f"exit status {result.returncode}: {tail(err)}")
            expect("cannot listen on" in err and "Traceback" not in err, tail(err))
            return err.strip().splitlines()[-1]
        return run
    check("web server: a second instance on 127.0.0.1 fails with one line (status 1)", second_server("127.0.0.1"))
    check("web server: a second instance on 0.0.0.0 fails with one line (status 1)", second_server("0.0.0.0"))

    def abandoned_stream():
        llama_log = os.path.join(os.environ.get("LOCALAPPDATA", ""), "PLaMo3Translate", "logs", "llama-server.log")
        before = os.path.getsize(llama_log) if os.path.isfile(llama_log) else 0
        body = json.dumps({"text": LONG_JAPANESE, "target_lang": "en", "stream": True}).encode("utf-8")
        sock = socket.create_connection(("127.0.0.1", inst.web_port), timeout=300)
        try:
            sock.sendall(b"POST /api/translate HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\n"
                         + f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body)
            received = b""
            while b'"delta"' not in received:
                block = sock.recv(65536)
                expect(block, f"the stream ended early: {received[-300:]!r}")
                received += block
        finally:
            sock.close()  # the browser tab is closed in the middle of the translation
        stopped = None
        start_time = time.monotonic()
        while time.monotonic() - start_time < 30 and stopped is None:
            time.sleep(1)
            if os.path.isfile(llama_log):
                with open(llama_log, "rb") as f:
                    f.seek(before)
                    new = decode(f.read())
                lines = [line for line in new.splitlines() if "stop processing" in line or "cancel" in line.lower()]
                if lines:
                    stopped = time.monotonic() - start_time
                    detail_only("\n".join(lines[-3:]))
        page = http_request(inst.web_port, "GET", "/")
        expect(page and page[0] == 200, "the web server does not answer after the client went away")
        expect("Traceback" not in launcher.output(), f"traceback in the console:\n{tail(launcher.output(), 30)}")
        if stopped is None:
            return "no traceback; llama-server did NOT log the end of the generation within 30 s (see the log)"
        return f"no traceback; llama-server stopped generating after {stopped:.0f} s"
    check("web server: client goes away during a streamed translation", abandoned_stream)

    def stop():
        status = launcher.stop()
        output = launcher.output()
        detail_only(output)
        expect(status == 0, f"exit status {status}:\n{tail(output)}")
        expect("Traceback" not in output, f"traceback:\n{tail(output, 30)}")
        expect(wait_until(lambda: ports_closed(inst), 20), "a port is still open after the launcher ended")
    if not check("Web UI mode stops on Ctrl-Break (status 0, no traceback, ports closed)", stop):
        launcher.kill()

    def killed_launcher():
        victim = Launcher(inst, "webui", workdir)
        try:
            victim.wait_ready()
            victim.proc.kill()  # as from the Task Manager
            victim.proc.wait()
            expect(wait_until(lambda: ports_closed(inst), 30),
                   "llama-server or the web server survived the launcher (job object)")
        finally:
            victim.kill()
            if not ports_closed(inst):
                subprocess.run(["taskkill", "/F", "/IM", "llama-server.exe"], capture_output=True)
    check("killing the launcher stops its children (job object)", killed_launcher)


def checks_shortcuts(inst):
    """The Start menu shortcuts themselves: their windows are real console windows."""
    menu = os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), "Microsoft", "Windows", "Start Menu",
                        "Programs", "PLaMo 3 Translate")
    webui_lnk = os.path.join(menu, "PLaMo 3 Translate Web UI.lnk")
    llama_lnk = os.path.join(menu, "PLaMo 3 Translate llama-server.lnk")

    def properties():
        script = "$shell = New-Object -ComObject WScript.Shell\n"
        for name in sorted(os.listdir(menu)):
            if name.endswith(".lnk"):
                script += ("$s = $shell.CreateShortcut('" + os.path.join(menu, name).replace("'", "''") + "'); "
                           "\"$($s.TargetPath)`t$($s.Arguments)`t$($s.WorkingDirectory)\"\n")
        lines = [line for line in powershell(script).splitlines() if line.strip()]
        detail_only("\n".join(lines))
        expect(len(lines) == 3, f"expected three shortcuts: {lines}")
        for line in lines:
            target, arguments, workdir = (line.split("\t") + ["", ""])[:3]
            expect(os.path.isfile(target), f"the target does not exist: {target}")
            expect(os.path.isdir(workdir), f"the working directory does not exist: {workdir}")
            for quoted in re.findall(r'"([^"]+\.(?:py|bat))"', arguments):
                expect(os.path.isfile(quoted), f"a file in the arguments does not exist: {quoted}")
    check("Start menu: three shortcuts with existing targets", properties)

    def close_window(mode):
        """Close the console window of the launcher, as the X button does (taskkill
        without /F sends the window a close message).  True if the servers stopped."""
        pids = launcher_pids(mode)
        expect(len(pids) == 1, f"expected one {mode} launcher, found {pids}")
        result = subprocess.run(["taskkill", "/PID", str(pids[0])], capture_output=True)
        detail_only(result.stdout.decode("oem", "replace") + result.stderr.decode("oem", "replace"))
        return wait_until(lambda: ports_closed(inst) and not launcher_pids(mode), 30)

    try:
        def start_llama():
            os.startfile(llama_lnk)
            expect(wait_until(lambda: (http_request(inst.llama_port, "GET", "/health", timeout=5) or (0,))[0] == 200,
                              600), "llama-server did not become ready")
            expect(len(launcher_pids("llama")) == 1, "no launcher process found")
        if check("llama-server shortcut starts llama-server", start_llama):
            def error_screen():
                os.startfile(webui_lnk)
                time.sleep(12)
                pids = launcher_pids("webui")
                expect(len(pids) == 1, "the window with the error closed by itself (no launcher is waiting)")
                expect(not port_open(inst.web_port), "a web server was started")
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(pids[0])], capture_output=True)
                return "the window stays open, waiting for a key"
            check("Web UI shortcut while llama-server runs: the error stays until a key is pressed", error_screen)

            def close_llama():
                if not close_window("llama"):
                    kill_launchers()
                    raise Skip("the window could not be closed from a program (taskkill without /F); "
                               "closing it by hand was checked before")
            check("closing the llama-server window stops llama-server", close_llama)

        def start_webui():
            expect(wait_until(lambda: ports_closed(inst), 30), "a port is still in use")
            os.startfile(webui_lnk)  # this opens the web browser, too
            expect(wait_until(lambda: (http_request(inst.llama_port, "GET", "/health", timeout=5) or (0,))[0] == 200
                              and port_open(inst.web_port), 600), "the Web UI did not become ready")
            page = http_request(inst.web_port, "GET", "/")
            expect(page and page[0] == 200 and b"<title>PLaMo 3 Translate</title>" in page[1], "unexpected page")
            return "started; a browser tab should have opened"
        if check("Web UI shortcut starts both servers", start_webui):
            def close_webui():
                if not close_window("webui"):
                    kill_launchers()
                    raise Skip("the window could not be closed from a program (taskkill without /F); "
                               "closing it by hand was checked before")
            check("closing the Web UI window stops both servers", close_webui)
    finally:
        kill_launchers()
        if not wait_until(lambda: ports_closed(inst), 15):
            subprocess.run(["taskkill", "/F", "/IM", "llama-server.exe"], capture_output=True)
            wait_until(lambda: ports_closed(inst), 15)


def sendto_lnk():
    return os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows", "SendTo", "PLaMo 3 Translate.lnk")


def checks_sendto(inst, workdir):
    """The "Send to" shortcut: --mode translate."""
    def source(name):
        path = os.path.join(workdir, name)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(JAPANESE + "\n")
        return path

    def translate(*files):
        result = subprocess.run([inst.python, inst.launcher, "--mode", "translate", *files],
                                stdin=subprocess.DEVNULL, capture_output=True, timeout=1800, cwd=inst.destdir)
        output = decode(result.stdout) + decode(result.stderr)
        detail_only(output)
        return result.returncode, output

    def properties():
        script = ("$s = (New-Object -ComObject WScript.Shell).CreateShortcut('" + sendto_lnk().replace("'", "''")
                  + "'); \"$($s.TargetPath)`t$($s.Arguments)\"\n")
        expect(os.path.isfile(sendto_lnk()), f"{sendto_lnk()} does not exist")
        target, _, arguments = powershell(script).strip().partition("\t")
        expect(os.path.isfile(target), f"the target does not exist: {target}")
        expect(arguments.endswith("--mode translate") and inst.launcher.lower() in arguments.lower(),
               f"unexpected arguments: {arguments}")
        return sendto_lnk()
    check("Send to: the shortcut exists", properties)

    def alone():
        expect(ports_closed(inst), "a port is in use before the check")
        status, output = translate(source("sendto-a.txt"), source("sendto-b.md"))
        expect(status == 0, f"exit status {status}:\n{tail(output)}")
        expect("Starting llama-server" in output, tail(output))
        for name in ("sendto-a.en.txt", "sendto-b.en.md"):
            with open(os.path.join(workdir, name), encoding="utf-8") as f:
                expect(f.read().strip(), f"{name} is empty")
        expect(wait_until(lambda: ports_closed(inst), 20), "llama-server was not stopped afterwards")
    check("Send to: starts llama-server, translates two files, stops llama-server", alone)

    def existing_output():
        status, output = translate(os.path.join(workdir, "sendto-a.txt"))
        expect(status == 1, f"exit status {status}:\n{tail(output)}")
        expect("could not be translated" in output, tail(output))
        expect(wait_until(lambda: ports_closed(inst), 20), "llama-server was not stopped afterwards")
        return tail(output, 12).replace("\n", " | ")
    check("Send to: an existing translation is not overwritten (error, status 1)", existing_output)

    def with_running_server():
        launcher = Launcher(inst, "llama", workdir)
        try:
            launcher.wait_ready()
            status, output = translate(source("sendto-c.txt"))
            expect(status == 0, f"exit status {status}:\n{tail(output)}")
            expect("already running" in output and "Starting llama-server" not in output, tail(output))
            expect(os.path.isfile(os.path.join(workdir, "sendto-c.en.txt")), "no output file")
            time.sleep(4)
            health = http_request(inst.llama_port, "GET", "/health", timeout=5)
            expect(health and health[0] == 200, "the llama-server that was running before was stopped")
        finally:
            launcher.stop()
        expect(wait_until(lambda: ports_closed(inst), 20), "a port is still open")
    check("Send to: uses a running llama-server and leaves it running", with_running_server)

    def through_the_shortcut():
        # As Explorer does it: the shortcut with the file as its parameter.  A console
        # window opens and closes by itself.
        path = source("sendto-d.txt")
        target = os.path.join(workdir, "sendto-d.en.txt")
        result = ctypes.windll.shell32.ShellExecuteW(None, None, sendto_lnk(), f'"{path}"', None, 1)
        if result <= 32:
            raise Skip(f"the shortcut could not be started from a program (ShellExecute {result}); try it by hand")
        if not wait_until(lambda: os.path.isfile(target), 600, step=1):
            kill_launchers()
            raise Skip("no translation appeared; this way of starting a shortcut with a file may not work: "
                       "try \"Send to\" by hand")
        expect(wait_until(lambda: ports_closed(inst) and not launcher_pids("translate"), 60),
               "the window or llama-server did not go away after the translation")
    try:
        check("Send to: started through the shortcut file itself", through_the_shortcut)
    finally:
        kill_launchers()


def checks_installer(inst, workdir):
    def rerun():
        status, output = run_installer(["--accept-license", "--destdir", inst.destdir])
        expect(status == 0, f"exit status {status}:\n{tail(output, 25)}")
        for text in ("is already installed", "Reusing plamo-3-translate-", "Translation test passed",
                     "PLaMo 3 Translate is installed."):
            expect(text in output, f"missing in the output: {text!r}\n{tail(output, 25)}")
        expect("Downloading" not in output, "something was downloaded again")
        before = dict(inst.cfg)
        inst.reload()
        expect(inst.cfg == before, f"the settings changed: {before} -> {inst.cfg}")
        expect(wait_until(lambda: ports_closed(inst), 20), "a port is still open after the installer")
    check("installer: non-interactive re-run with options reuses everything", rerun)

    def refuses_while_running():
        launcher = Launcher(inst, "llama", workdir)
        try:
            launcher.wait_ready()
            status, output = run_installer(["--accept-license", "--destdir", inst.destdir])
            expect(status == 1, f"exit status {status}:\n{tail(output)}")
            expect("is running" in output, tail(output))
        finally:
            launcher.stop()
        expect(wait_until(lambda: ports_closed(inst), 20), "a port is still open")
    check("installer: stops while PLaMo 3 Translate is running", refuses_while_running)

    def license_gate():
        status, output = run_installer(["--yes", "--destdir", inst.destdir])
        expect(status == 1, f"exit status {status}:\n{tail(output)}")
        expect("--accept-license" in output, tail(output))
    check("installer: non-interactive run without --accept-license is refused", license_gate)

    def help_text():
        status, output = run_installer(["--help"])
        expect(status == 0 and "Usage: install-plamo-3-translate.bat" in output, tail(output))
    check("installer: --help", help_text)


def checks_expose(inst, workdir):
    def expose():
        status, output = run_installer(["--accept-license", "--expose-llama", "--destdir", inst.destdir])
        expect(status == 0, f"exit status {status}:\n{tail(output, 25)}")
        expect("rejects requests without the API key" in output, tail(output, 25))
        with open(inst.key_file, encoding="ascii") as f:
            expect(re.fullmatch(r"[0-9a-f]{64}\n", f.read()), "unexpected API key file")
        inst.reload()
    if not check("installer: --expose-llama writes an API key and tests it", expose):
        return

    def client_with_key():
        launcher = Launcher(inst, "llama", workdir)
        try:
            launcher.wait_ready()
            result = http_request(inst.llama_port, "GET", "/v1/models")
            expect(result and result[0] == 401, f"a request without the key was answered: {result}")
            status, out, err = run_client(inst, ["-q", "-"], workdir, data=(JAPANESE + "\n").encode("utf-8"))
            expect(status == 0 and out.strip(), f"exit status {status}: {tail(err)}")
            env = dict(os.environ, PLAMO3_LLAMA_API_KEY="wrong")
            result = subprocess.run(["cmd.exe", "/d", "/c", "call", inst.client_bat, "-q", "-"], env=env,
                                    input=b"test\n", capture_output=True, timeout=300, cwd=workdir)
            expect(result.returncode == 1, f"a wrong key was accepted (exit status {result.returncode})")
        finally:
            launcher.stop()
    check("client: the wrapper passes the API key; a wrong key is refused", client_with_key)

    def unexpose():
        status, output = run_installer(["--accept-license", "--no-expose-llama", "--destdir", inst.destdir])
        expect(status == 0, f"exit status {status}:\n{tail(output, 25)}")
        expect(not os.path.exists(inst.key_file), "the API key file was not removed")
        inst.reload()
    check("installer: --no-expose-llama removes the API key", unexpose)


def checks_uninstall(inst):
    menu = os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), "Microsoft", "Windows", "Start Menu",
                        "Programs", "PLaMo 3 Translate")

    def shortcuts_present():
        names = sorted(os.listdir(menu))
        expect(len([n for n in names if n.endswith(".lnk")]) == 3, f"unexpected shortcuts: {names}")
        expect(os.path.isfile(sendto_lnk()), "the Send to shortcut is missing")
        return ", ".join(names)
    check("Start menu: three shortcuts, and Send to", shortcuts_present)

    def uninstall():
        # As the Uninstall shortcut does it: with the Python inside the install directory,
        # which cannot delete itself, so the directory is removed after it has ended.
        result = subprocess.run([inst.python, inst.installer, "--uninstall", "--yes", "--destdir", inst.destdir],
                                stdin=subprocess.DEVNULL, capture_output=True, timeout=300, cwd=REPO)
        output = decode(result.stdout) + decode(result.stderr)
        detail_only(output)
        expect(result.returncode == 0, f"exit status {result.returncode}:\n{tail(output)}")
        expect(not os.path.exists(menu), "the Start menu folder is still there")
        expect(not os.path.exists(sendto_lnk()), "the Send to shortcut is still there")
        expect(wait_until(lambda: not os.path.exists(inst.destdir), 90, step=1),
               f"{inst.destdir} is still there after 90 s: {os.listdir(inst.destdir) if os.path.isdir(inst.destdir) else ''}")
        expect(os.path.isdir(os.path.dirname(inst.destdir)), "the parent directory was removed too")
    if not check("uninstall from the installed copy removes shortcuts and the install directory", uninstall):
        return

    def reinstall():
        # A whole non-interactive installation with the settings from before, downloads
        # included; it also puts PLaMo 3 Translate back.
        time.sleep(5)
        cfg = inst.cfg
        args = ["--accept-license", "--destdir", inst.destdir, "--model", cfg["model"], "--quant", cfg["quant"],
                "--backend", cfg["backend"], "--ctx-size", str(cfg["ctx_size"]),
                "--web-port", str(cfg["web_port"]), "--llama-port", str(cfg["llama_port"]),
                "--listen" if cfg.get("listen") == "yes" else "--no-listen",
                "--expose-llama" if cfg.get("expose_llama") == "yes" else "--no-expose-llama"]
        if cfg.get("gpus"):
            args += ["--gpus", cfg["gpus"]]
        status, output = run_installer(args)
        expect(status == 0, f"exit status {status}:\n{tail(output, 25)}")
        for text in ("Downloading llama-", "Downloading plamo-3-translate-", "Translation test passed",
                     "PLaMo 3 Translate is installed."):
            expect(text in output, f"missing in the output: {text!r}\n{tail(output, 25)}")
        time.sleep(40)  # a leftover of the uninstaller must not remove the new installation
        expect(os.path.isfile(inst.python) and os.path.isfile(inst.launcher), "the new installation is incomplete")
        expect(len([n for n in os.listdir(menu) if n.endswith(".lnk")]) == 3, "the shortcuts are missing")
        expect(os.path.isfile(sendto_lnk()), "the Send to shortcut is missing")
        lines, read_only = users_rights(inst.destdir)
        expect(read_only, f"Users have more than read and execute: {lines}")
    check("fresh non-interactive installation (downloads everything again)", reinstall)


def ask(question):
    try:
        return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def main():
    global log_file
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--destdir", default=DEFAULT_DESTDIR)
    parser.add_argument("--expose", action="store_true", help="also test --expose-llama (no question)")
    parser.add_argument("--uninstall", action="store_true", help="also test uninstalling (no question)")
    args = parser.parse_args()
    if sys.platform != "win32":
        sys.exit("This check runs on Windows only.")
    if not ctypes.windll.shell32.IsUserAnAdmin():
        sys.exit("Run tests\\windows-check.bat as Administrator.")
    if not os.path.isfile(os.path.join(args.destdir, "install.json")):
        sys.exit(f"No installation found in {args.destdir}; run install-plamo-3-translate.bat first.")
    inst = Installation(args.destdir)
    if not ports_closed(inst):
        sys.exit("PLaMo 3 Translate seems to be running (a port is in use); close its windows and run this again.")

    print("This checks the installed PLaMo 3 Translate: it starts and stops the servers several")
    print("times and re-runs the installer.  Console windows appear and close, and a browser tab")
    print("opens: leave them alone, and do not start PLaMo 3 Translate meanwhile.  It takes a few")
    print("minutes.  Two optional parts:")
    expose = args.expose or ask("Also test --expose-llama?  (Windows may show a firewall dialog: choose Cancel)")
    uninstall = args.uninstall or ask("Also test uninstalling and installing again at the end?  "
                                      "(the model is downloaded again)")
    print()

    log_file = open(LOG_PATH, "w", encoding="utf-8", newline="\n")
    log(f"PLaMo 3 Translate check, {time.strftime('%Y-%m-%d %H:%M:%S')}")
    log(f"Windows: {platform.platform()}  Python: {platform.python_version()} ({sys.executable})")
    try:
        log(f"Console code page: {ctypes.windll.kernel32.GetConsoleOutputCP()}, "
            f"ANSI code page: {ctypes.windll.kernel32.GetACP()}")
    except Exception:
        pass
    log(f"Settings: {json.dumps(inst.cfg)}")
    log()
    with tempfile.TemporaryDirectory(prefix="plamo-3-translate-check-") as workdir:
        try:
            checks_permissions(inst)
            checks_llama_mode(inst, workdir)
            checks_webui_mode(inst, workdir)
            checks_shortcuts(inst)
            checks_sendto(inst, workdir)
            checks_installer(inst, workdir)
            if expose:
                checks_expose(inst, workdir)
            else:
                results.append(("--expose-llama", "SKIP", "not selected"))
            if uninstall:
                checks_uninstall(inst)
            else:
                results.append(("uninstall", "SKIP", "not selected"))
        except KeyboardInterrupt:
            log("Interrupted.")
        except Exception:
            log("The check itself failed:")
            log(traceback.format_exc())
    log()
    log("=== Summary")
    for name, status, detail in results:
        log(f"{status}  {name}" + (f"  [{detail}]" if detail and status != "PASS" else ""))
    counts = {s: sum(1 for r in results if r[1] == s) for s in ("PASS", "FAIL", "SKIP")}
    log(f"\n{counts['PASS']} passed, {counts['FAIL']} failed, {counts['SKIP']} skipped")
    log_file.close()
    print(f"\nThe log is in {LOG_PATH}")
    return 1 if counts["FAIL"] else 0


if __name__ == "__main__":
    sys.exit(main())
