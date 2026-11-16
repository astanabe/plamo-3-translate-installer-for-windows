#!/usr/bin/env python3
"""PLaMo 3 Translate launcher (Windows): starts llama-server and the web server on demand.

    python.exe plamo-3-translate-launcher.py --mode webui|llama|check [options]

It is run by the Start menu shortcuts that install-plamo-3-translate.bat creates, with the
Python that the installer put into the install directory (standard library only), and
reads its settings from install.json in the install directory.

  webui   start llama-server and plamo-3-translate-server.py in this console window, wait
          until the model is loaded and open the translation page in the web browser.
          Closing the window (or Ctrl-C) stops both.  If the Web UI is already running,
          only the browser is opened.
  llama   start llama-server only, for plamo-3-translate-client.py and other clients.
  check   used by the installer: start both, translate a test sentence through the web
          server, write the result to --result-file and stop.

Options:
  --root DIR          install directory (default: the parent of this file's directory)
  --no-browser        webui: do not open the web browser
  --result-file FILE  check: write a JSON object (n_ctx, translation, gpu_lines) to FILE

Exit status: 0 success (also "already running" and a stop by Ctrl-C), 1 error.  In the
webui and llama modes an error is shown until a key is pressed, so that the message can
be read before the window closes.
"""

import argparse
import http.client
import json
import os
import signal
import socket
import subprocess
import sys
import time
import webbrowser

WINDOWS = sys.platform == "win32"
APP_TITLE = "PLaMo 3 Translate"
MODEL_ALIAS = "plamo-3-translate"
CONFIG_NAME = "install.json"
API_KEY_NAME = "llama-api-key.txt"
SERVER_PY = "plamo-3-translate-server.py"
LLAMA_SERVER_EXE = "llama-server.exe"
LOG_NAME = "llama-server.log"
WEB_TITLE = b"<title>PLaMo 3 Translate</title>"  # how the page of the web server is recognized
# Seconds to wait for llama-server to load the model.
HEALTH_TIMEOUT = {"0.8b": 180, "2b": 300, "8b": 900, "31b": 1800}
WEB_START_TIMEOUT = 30
TEST_TEXT = "こんにちは"
TEST_TIMEOUT = 600
STATUS_DLL_NOT_FOUND = 0xC0000135
VC_REDIST_URL = "https://aka.ms/vs/17/release/vc_redist.x64.exe"


class LauncherError(Exception):
    """An error to show to the user (no traceback)."""


def info(message):
    print(message, flush=True)


# ------------------------------------------------------------------ settings

def default_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_config(root):
    path = os.path.join(root, CONFIG_NAME)
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError) as e:
        raise LauncherError(f"cannot read {path}: {e}\nRun the installer again.") from None
    try:
        cfg = {
            "model": str(cfg["model"]),
            "quant": str(cfg["quant"]),
            "backend": str(cfg["backend"]),
            "gpus": str(cfg.get("gpus", "")),
            "ctx_size": str(cfg.get("ctx_size", "auto")),
            "web_port": int(cfg["web_port"]),
            "llama_port": int(cfg["llama_port"]),
            "listen": str(cfg.get("listen", "no")) == "yes",
            "expose_llama": str(cfg.get("expose_llama", "no")) == "yes",
            "llama_tag": str(cfg["llama_tag"]),
        }
    except (KeyError, TypeError, ValueError) as e:
        raise LauncherError(f"{path} is incomplete ({e!r}).\nRun the installer again.") from None
    return cfg


def read_api_key(root):
    """The llama-server API key (used only when llama-server is exposed)."""
    path = os.path.join(root, API_KEY_NAME)
    try:
        with open(path, encoding="ascii") as f:
            key = f.read().strip()
    except (OSError, ValueError) as e:
        raise LauncherError(f"cannot read {path}: {e}\nRun the installer again.") from None
    if not key:
        raise LauncherError(f"{path} is empty.\nRun the installer again.")
    return key


def log_dir():
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "PLaMo3Translate", "logs")


def llama_command(root, cfg):
    exe = os.path.join(root, "llama.cpp", f"{cfg['llama_tag']}-{cfg['backend']}", LLAMA_SERVER_EXE)
    model = os.path.join(root, "models", f"plamo-3-translate-{cfg['model']}-{cfg['quant']}.gguf")
    for path in (exe, model):
        if not os.path.isfile(path):
            raise LauncherError(f"{path} not found.\nRun the installer again.")
    cmd = [exe, "--model", model,
           "--host", "0.0.0.0" if cfg["expose_llama"] else "127.0.0.1",
           "--port", str(cfg["llama_port"])]
    # Without -c, llama-server uses the model maximum and reduces it to fit the memory.
    if cfg["ctx_size"] != "auto":
        cmd += ["-c", cfg["ctx_size"]]
    # GPU selection by llama.cpp device name (see --list-devices); untested on real GPUs.
    if cfg["backend"] != "cpu" and cfg["gpus"] not in ("", "all"):
        cmd += ["--device", cfg["gpus"]]
    cmd += ["--jinja", "--no-prefill-assistant", "--alias", MODEL_ALIAS]
    return cmd


# ---------------------------------------------------------------------- HTTP

def port_open(port):
    """True if something accepts connections on 127.0.0.1:port."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def http_request(port, method, path, body=None, headers=None, timeout=5):
    """(status, body) of a request to 127.0.0.1:port, or None if it cannot be made."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        return response.status, response.read()
    except (OSError, http.client.HTTPException):
        return None
    finally:
        conn.close()


def auth_headers(api_key):
    return {"Authorization": "Bearer " + api_key} if api_key else {}


def is_our_web(port):
    result = http_request(port, "GET", "/")
    return bool(result and result[0] == 200 and WEB_TITLE in result[1])


def llama_ready(port):
    result = http_request(port, "GET", "/health")
    return bool(result and result[0] == 200)


def llama_state(port, api_key=""):
    """What listens on the llama-server port: "free", "ours" (a llama-server with our
    model alias), "loading" (a llama-server that is still loading its model; the alias
    cannot be asked yet) or "other"."""
    if not port_open(port):
        return "free"
    result = http_request(port, "GET", "/health")
    if not result:
        return "other"
    status, body = result
    if status == 503 and b"Loading model" in body:
        return "loading"
    if status != 200:
        return "other"
    result = http_request(port, "GET", "/v1/models", headers=auth_headers(api_key))
    if not result or result[0] != 200:
        return "other"
    try:
        ids = [m.get("id") for m in json.loads(result[1]).get("data", [])]
    except (ValueError, AttributeError, TypeError):
        return "other"
    return "ours" if MODEL_ALIAS in ids else "other"


# ----------------------------------------------------------------- processes

class Children:
    """The child processes; all of them are stopped together."""

    def __init__(self):
        self.procs = []  # (name, Popen)
        self.job = None
        if WINDOWS:
            self.job = _create_job()

    def start(self, name, cmd, env, stdout=None):
        try:
            proc = subprocess.Popen(cmd, env=env, stdin=subprocess.DEVNULL, stdout=stdout,
                                    stderr=subprocess.STDOUT if stdout is not None else None)
        except OSError as e:
            raise LauncherError(f"cannot start {name}: {e}") from None
        self.procs.append((name, proc))
        if self.job is not None:
            _assign_to_job(self.job, proc)
        return proc

    def exited(self):
        """(name, exit status) of the first child that is not running any more, or None."""
        for name, proc in self.procs:
            status = proc.poll()
            if status is not None:
                return name, status
        return None

    def stop(self, interrupted=False):
        procs = [proc for _, proc in self.procs if proc.poll() is None]
        if WINDOWS and interrupted:
            # Ctrl-C in a console goes to the children too: give them time to finish.
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and any(p.poll() is None for p in procs):
                time.sleep(0.1)
        for proc in procs:
            if proc.poll() is None:
                try:
                    proc.terminate()
                except OSError:
                    pass
        for proc in procs:
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        self.procs = []


def _create_job():
    """A job object that kills the children when the launcher goes away, however it ends
    (e.g. from the Task Manager).  Returns None if it cannot be made: the launcher then
    works without it."""
    try:
        import ctypes
        from ctypes import wintypes

        class BasicLimits(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                        ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD),
                        ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t),
                        ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t),
                        ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BasicLimits),
                        ("IoInfo", IoCounters),
                        ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                     ctypes.c_void_p, wintypes.DWORD]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None
        limits = ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        # 9 = JobObjectExtendedLimitInformation
        if not kernel32.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            return None
        return job
    except Exception:
        return None


def _assign_to_job(job, proc):
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject(job, int(proc._handle))
    except Exception:
        pass


def quiet_error_dialogs():
    """Do not let Windows show a dialog box (e.g. for a missing DLL) when a child cannot
    start: the exit status is reported in the console instead."""
    if not WINDOWS:
        return
    try:
        import ctypes
        ctypes.WinDLL("kernel32").SetErrorMode(0x8003)  # FAILCRITICALERRORS | NOGPFAULTERRORBOX | NOOPENFILEERRORBOX
    except Exception:
        pass


def set_title(title):
    if not WINDOWS:
        return
    try:
        import ctypes
        ctypes.WinDLL("kernel32").SetConsoleTitleW(title)
    except Exception:
        pass


def wait_for_key():
    """Keep the console window open until a key is pressed."""
    if not WINDOWS:
        return
    try:
        if not (sys.stdin and sys.stdin.isatty()):
            return
        import msvcrt
        print("\nPress any key to close this window.", flush=True)
        msvcrt.getch()
    except Exception:
        pass


# --------------------------------------------------------------- llama-server

def open_log():
    """Open the llama-server log (the previous one is kept as .1); returns (path, file)."""
    directory = log_dir()
    path = os.path.join(directory, LOG_NAME)
    try:
        os.makedirs(directory, exist_ok=True)
        if os.path.exists(path):
            os.replace(path, path + ".1")
        return path, open(path, "wb")
    except OSError as e:
        raise LauncherError(f"cannot write the log file {path}: {e}") from None


def log_tail(path, lines=30):
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 16384))
            text = f.read().decode("utf-8", "replace")
    except OSError:
        return ""
    return "\n".join(text.splitlines()[-lines:])


def exit_text(name, status, log_path):
    text = f"{name} stopped unexpectedly (exit status {status})."
    if status in (STATUS_DLL_NOT_FOUND, STATUS_DLL_NOT_FOUND - (1 << 32)):
        text += ("\nA DLL that it needs was not found.  Install the Microsoft Visual C++ "
                 f"Redistributable (x64) and try again:\n  {VC_REDIST_URL}")
    if name == "llama-server":
        tail = log_tail(log_path)
        if tail:
            text += f"\n\nLast lines of {log_path}:\n{tail}"
    return text


def wait_health(cfg, children, log_path):
    """Wait until llama-server has loaded the model; returns the seconds it took."""
    limit = HEALTH_TIMEOUT.get(cfg["model"], 1800)
    start = time.monotonic()
    tty = bool(sys.stdout and sys.stdout.isatty())
    shown = -1
    info(f"Loading the model (up to {limit} s) ...")
    while True:
        if llama_ready(cfg["llama_port"]):
            break
        exited = children.exited()
        if exited:
            if tty:
                print()
            raise LauncherError(exit_text(exited[0], exited[1], log_path))
        elapsed = int(time.monotonic() - start)
        if elapsed > limit:
            if tty:
                print()
            raise LauncherError(f"llama-server did not become ready within {limit} s.\n\n"
                                f"Last lines of {log_path}:\n{log_tail(log_path)}")
        if tty and elapsed != shown:
            print(f"\r  {elapsed} s", end="", flush=True)
            shown = elapsed
        time.sleep(0.5)
    if tty:
        print("\r", end="")
    elapsed = int(time.monotonic() - start)
    info(f"llama-server is ready ({elapsed} s).")
    return elapsed


def wait_web(cfg, children, proc):
    """Wait until the web server accepts connections."""
    deadline = time.monotonic() + WEB_START_TIMEOUT
    while True:
        status = proc.poll()
        if status is not None:
            # Status 1 with "cannot listen on" on standard error (shown above) is a bind failure.
            raise LauncherError(f"the web server could not be started (exit status {status}); "
                                "see the message above.")
        if port_open(cfg["web_port"]):
            return
        if time.monotonic() > deadline:
            raise LauncherError(f"the web server did not start listening on port {cfg['web_port']} "
                                f"within {WEB_START_TIMEOUT} s.")
        time.sleep(0.2)


def n_ctx(cfg, api_key):
    result = http_request(cfg["llama_port"], "GET", "/props", headers=auth_headers(api_key), timeout=10)
    try:
        return int(json.loads(result[1])["default_generation_settings"]["n_ctx"])
    except (TypeError, KeyError, ValueError):
        return None


# -------------------------------------------------------------------- modes

def check_conflicts(mode, cfg, api_key):
    """Raise LauncherError if the ports are taken.  Returns True if the Web UI is running
    already and only the browser has to be opened (webui mode)."""
    web, llama = cfg["web_port"], cfg["llama_port"]
    if mode in ("webui", "check") and port_open(web):
        if is_our_web(web):
            if mode == "webui":
                return True
            raise LauncherError(f"{APP_TITLE} is running (port {web}); close its window first.")
        raise LauncherError(f"TCP port {web} (web server) is in use by another program.\n"
                            "Close that program, or run the installer again with another --web-port.")
    state = llama_state(llama, api_key)
    if state == "free":
        return False
    if mode == "llama" and is_our_web(web):
        raise LauncherError(
            f"{APP_TITLE} Web UI is running, and its llama-server is on port {llama}:\n"
            "plamo-3-translate-client.py can be used as it is.\n"
            "To run llama-server alone, close the Web UI window first.")
    if state in ("ours", "loading"):
        text = f"llama-server is already running on TCP port {llama}."
        if mode == "webui":
            text += (f"\nThe Web UI starts its own llama-server: close the window of "
                     f"\"{APP_TITLE} llama-server\" (or of a Web UI that is still starting) "
                     "and try again.")
        raise LauncherError(text)
    raise LauncherError(f"TCP port {llama} (llama-server) is in use by another program.\n"
                        "Close that program, or run the installer again with another --llama-port.")


def attach(cfg, open_browser):
    """The Web UI is running already: wait for its model if needed and open the browser."""
    url = f"http://127.0.0.1:{cfg['web_port']}/"
    info(f"{APP_TITLE} is already running.")
    limit = HEALTH_TIMEOUT.get(cfg["model"], 1800)
    start = time.monotonic()
    waiting = False
    while not llama_ready(cfg["llama_port"]):
        if not port_open(cfg["web_port"]):
            raise LauncherError("the running Web UI has stopped; start it again.")
        if time.monotonic() - start > limit:
            raise LauncherError(f"llama-server did not become ready within {limit} s.")
        if not waiting:
            info("It is still loading the model; waiting ...")
            waiting = True
        time.sleep(0.5)
    if open_browser:
        info(f"Opening {url}")
        webbrowser.open(url)
    return 0


def translation_test(cfg):
    body = json.dumps({"text": TEST_TEXT, "target_lang": "en"}).encode("utf-8")
    result = http_request(cfg["web_port"], "POST", "/api/translate", body=body,
                          headers={"Content-Type": "application/json"}, timeout=TEST_TIMEOUT)
    if not result:
        raise LauncherError(f"cannot connect to the web server on port {cfg['web_port']}")
    status, data = result
    text = data.decode("utf-8", "replace")
    if status != 200:
        raise LauncherError(f"translation test failed (HTTP {status}): {text}")
    try:
        translation = str(json.loads(data).get("translation", ""))
    except (ValueError, AttributeError):
        translation = ""
    if not translation.strip():
        raise LauncherError(f"translation test returned an empty result: {text}")
    return translation


def run(mode, root, open_browser=True, result_file=None):
    cfg = load_config(root)
    api_key = read_api_key(root) if cfg["expose_llama"] else ""
    if check_conflicts(mode, cfg, api_key):
        return attach(cfg, open_browser)

    cmd = llama_command(root, cfg)
    server_py = os.path.join(root, "app", SERVER_PY)
    if mode != "llama" and not os.path.isfile(server_py):
        raise LauncherError(f"{server_py} not found.\nRun the installer again.")
    log_path, log_file = open_log()
    web_url = f"http://127.0.0.1:{cfg['web_port']}/"
    llama_url = f"http://127.0.0.1:{cfg['llama_port']}"
    quiet_error_dialogs()
    children = Children()
    interrupted = False
    try:
        info(f"Starting llama-server ({cfg['backend']}, plamo-3-translate-{cfg['model']}-{cfg['quant']})")
        info(f"  log: {log_path}")
        env = dict(os.environ)
        if api_key:
            env["LLAMA_API_KEY"] = api_key
        else:
            env.pop("LLAMA_API_KEY", None)
        with log_file:
            children.start("llama-server", cmd, env, stdout=log_file)
        if mode != "llama":
            env = dict(os.environ)
            env["PLAMO3_LLAMA_URL"] = llama_url
            if api_key:
                env["PLAMO3_LLAMA_API_KEY"] = api_key
            else:
                env.pop("PLAMO3_LLAMA_API_KEY", None)
            web = children.start("the web server", [
                sys.executable, server_py,
                "--host", "0.0.0.0" if cfg["listen"] else "127.0.0.1",
                "--port", str(cfg["web_port"])], env)
            wait_web(cfg, children, web)
        wait_health(cfg, children, log_path)

        if mode == "check":
            return check(cfg, api_key, log_path, result_file)

        info("")
        if mode == "webui":
            info(f"{APP_TITLE} is running.")
            info(f"  Web UI      : {web_url}")
            if cfg["listen"]:
                info(f"                (other hosts: port {cfg['web_port']} of this PC, "
                     "if the firewall allows it)")
        else:
            info(f"llama-server is running (model \"{MODEL_ALIAS}\").")
        info(f"  llama-server: {llama_url}"
             + ("  (API key required: " + os.path.join(root, API_KEY_NAME) + ")" if api_key else ""))
        info("  Client      : plamo-3-translate-client FILE   "
             f"(in \"{APP_TITLE} Command Prompt\")")
        info("")
        info("Close this window (or press Ctrl-C) to stop.")
        info("")
        if mode == "webui" and open_browser:
            webbrowser.open(web_url)
        while True:
            exited = children.exited()
            if exited:
                raise LauncherError(exit_text(exited[0], exited[1], log_path))
            time.sleep(0.5)
    except KeyboardInterrupt:
        interrupted = True
        raise
    finally:
        children.stop(interrupted)


def check(cfg, api_key, log_path, result_file):
    info("Translating a test sentence through the web server")
    translation = translation_test(cfg)
    info(f"Translation test passed: {TEST_TEXT} -> {translation}")
    if api_key:
        result = http_request(cfg["llama_port"], "GET", "/v1/models", timeout=10)
        status = result[0] if result else None
        if status != 401:
            raise LauncherError(f"llama-server accepted a request without the API key (HTTP {status})")
        info("llama-server rejects requests without the API key")
    gpu_lines = []
    try:
        with open(log_path, encoding="utf-8", errors="replace") as f:
            gpu_lines = [line[line.index("using device"):].strip() for line in f if "using device" in line]
    except OSError:
        pass
    if result_file:
        result = {"n_ctx": n_ctx(cfg, api_key), "translation": translation, "gpu_lines": gpu_lines}
        with open(result_file, "w", encoding="utf-8", newline="\n") as f:
            json.dump(result, f, ensure_ascii=False)
    return 0


def _interrupt(signum, frame):
    raise KeyboardInterrupt


def main(argv=None):
    parser = argparse.ArgumentParser(description="PLaMo 3 Translate launcher.", allow_abbrev=False)
    parser.add_argument("--mode", choices=("webui", "llama", "check"), required=True)
    parser.add_argument("--root", default=default_root(), metavar="DIR",
                        help="install directory (default: %(default)s)")
    parser.add_argument("--no-browser", action="store_true", help="do not open the web browser")
    parser.add_argument("--result-file", metavar="FILE", help="check mode: write the result here")
    args = parser.parse_args(argv)

    # A console with a legacy code page cannot show every character: escape instead of
    # raising UnicodeEncodeError.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="backslashreplace")
        except (AttributeError, ValueError):
            pass
    for name in ("SIGTERM", "SIGBREAK"):
        if hasattr(signal, name):
            try:
                signal.signal(getattr(signal, name), _interrupt)
            except (OSError, ValueError):
                pass
    set_title({"webui": f"{APP_TITLE} Web UI", "llama": f"{APP_TITLE} llama-server",
               "check": f"{APP_TITLE} check"}[args.mode])
    try:
        return run(args.mode, os.path.abspath(args.root), open_browser=not args.no_browser,
                   result_file=args.result_file)
    except KeyboardInterrupt:
        info("\nStopped.")
        return 0
    except LauncherError as e:
        print(f"\nError: {e}", file=sys.stderr, flush=True)
        if args.mode != "check":
            wait_for_key()
        return 1


if __name__ == "__main__":
    sys.exit(main())
