#!/usr/bin/env python3
"""PLaMo 3 Translate installer for Windows 11 (x64).

    install-plamo-3-translate.bat [options]       install or update in place
    install-plamo-3-translate.bat --uninstall     uninstall

install-plamo-3-translate.bat fetches the embeddable Python package and runs this file
with it as Administrator.  Standard library only.  See README.md or --help for the options.

The steps and terms follow install-plamo-3-translate.sh of
plamo-3-translate-installer-for-resolute (the Ubuntu installer).  What differs: the
servers are not services but are started on demand by plamo-3-translate-launcher.py from
Start menu shortcuts, and the settings are kept in install.json in the install directory.
"""

import argparse
import base64
import hashlib
import http.client
import json
import ntpath
import os
import re
import secrets
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile

WINDOWS = sys.platform == "win32"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
APP_TITLE = "PLaMo 3 Translate"
MARKER = ".plamo-3-translate-installation"
CONFIG_NAME = "install.json"
API_KEY_NAME = "llama-api-key.txt"
CLIENT_BAT = "plamo-3-translate-client.bat"
PROMPT_BAT = "command-prompt.bat"
SERVER_PY = "plamo-3-translate-server.py"
CLIENT_PY = "plamo-3-translate-client.py"
LAUNCHER_PY = "plamo-3-translate-launcher.py"
INSTALLER_PY = "plamo-3-translate-installer.py"
APP_FILES = (SERVER_PY, CLIENT_PY, LAUNCHER_PY, INSTALLER_PY)
LLAMA_SERVER_EXE = "llama-server.exe"
MODEL_ALIAS = "plamo-3-translate"
LICENSE_URL = "https://plamo.preferredai.jp/info/plamo-community-license-en"
VC_REDIST_URL = "https://aka.ms/vs/17/release/vc_redist.x64.exe"
WEB_TITLE = b"<title>PLaMo 3 Translate</title>"
USER_AGENT = "plamo-3-translate-installer"

# Embeddable Python package.  install-plamo-3-translate.bat pins the same file (the tests
# compare the two).
PYTHON_VERSION = "3.14.10"
PYTHON_ZIP = f"python-{PYTHON_VERSION}-embed-amd64.zip"
PYTHON_URL = f"https://www.python.org/ftp/python/{PYTHON_VERSION}/{PYTHON_ZIP}"
PYTHON_SHA256 = "90905e64a840557d8c1bc4f5b74db3d7ee85e3ffb217611cac1c7813acea4159"
PYTHON_SIZE = 12619664

LLAMA_TAG = "b12366"
LLAMA_BASE_URL = f"https://github.com/ggml-org/llama.cpp/releases/download/{LLAMA_TAG}"
# Prebuilt llama.cpp archives: (file, sha256, size).
LLAMA_ASSETS = {
    "cpu": [
        ("llama-b12366-bin-win-cpu-x64.zip",
         "b6b83e46546c04af52a700ea09638201493448cf52de207b2efefc1cecab625e", 25116739)],
    "vulkan": [
        ("llama-b12366-bin-win-vulkan-x64.zip",
         "1708e3125afc80bcad84b571a3d1563fc776cad39acb9f2f3937f9bfbba50028", 38113369)],
    "cuda12": [
        ("llama-b12366-bin-win-cuda-12.4-x64.zip",
         "aba2321dcd0c02090e6d6cb504f0c77a35b6436a3276c4af5e4f250f414ad034", 319402123),
        ("cudart-llama-bin-win-cuda-12.4-x64.zip",
         "8c79a9b226de4b3cacfd1f83d24f962d0773be79f1e7b75c6af4ded7e32ae1d6", 391443627)],
    "cuda13": [
        ("llama-b12366-bin-win-cuda-13.5-x64.zip",
         "0daccf3ee656174c0b93110330b8abb4274f48b0f0ddce8f8555f659fea996c2", 179728961),
        ("cudart-llama-bin-win-cuda-13.5-x64.zip",
         "c2bf4cc202bfbaaa7a0b75f5082457c3811326947bb216059b72839d844e9cba", 426314199)],
}
BACKEND_DESC = {
    "cpu": "CPU only",
    "vulkan": "Vulkan (AMD / Intel / NVIDIA GPUs; untested)",
    "cuda12": "CUDA 12.4 (NVIDIA; untested)",
    "cuda13": "CUDA 13.5 (NVIDIA, driver 580 or later; untested)",
}
BACKENDS = ("cpu", "vulkan", "cuda12", "cuda13")

# Hugging Face repository revisions (pinned) and GGUF files: (sha256, size).  The same
# table as in the Ubuntu installer.
HF_BASE_URL = "https://huggingface.co"
MODEL_REVISION = {
    "0.8b": "1cf96ae02133958a20074bfdfab76517bb737d00",
    "2b": "4f672106b847200bfdc0f6af6c321cd94eba1aed",
    "8b": "c67efa4eefdc14d3c97bf8f4f35ee58e2ecd2f42",
    "31b": "8b797b290907c84403877cb9d03150929f10b467",
}
GGUF = {
    ("0.8b", "Q2_K"): ("97f0fd4a68d3d8eda39b018fd2f1ce2c58a6f86fad7dafd6d8b17cbdfab60709", 410138816),
    ("0.8b", "Q3_K_M"): ("6c64dd53a367966ec00a7335ff34bb77ad5a893b6fbc7bf9ae51f7241e0dbfbe", 486385856),
    ("0.8b", "Q4_0"): ("33deed2822f799de4d15bb03bea1ac40efa7d663d9239ee54cc7f65f5ab92917", 547543232),
    ("0.8b", "Q4_K_M"): ("70891ec70dc7ab4205745a38307867c2881b99239f20327927633abfe4bc9ec6", 573126848),
    ("2b", "Q2_K"): ("238232fe01810b5a0b42e93a4788bedb6b8831cc0baa4e1213ffb8394053b20d", 1225612000),
    ("2b", "Q3_K_M"): ("fd79d48441cbb6eed8377084b43077ed006c9575ca55e47c002a8ee2f7e9faff", 1495886560),
    ("2b", "Q4_0"): ("429b7934c88332262b4cca9fd5de36eb02e00f42dcd08ceb511a23e7ee4abafb", 1698577120),
    ("2b", "Q4_K_M"): ("1ffd9073afcc70e35ccc942428aa2b73d7aa2b9ea779bf504b775369e0276c13", 1807203040),
    ("8b", "Q2_K"): ("3322a15c142d2a30e552fe1ba054fa84174996c4537ee9161c1a1c96657dc127", 3450746144),
    ("8b", "Q3_K_M"): ("ddd9697162fe3c8e6ece064863ae694fb28430ee98c0addfbd241f56a7f45578", 4310218016),
    ("8b", "Q4_K_M"): ("94e5d3f91b421f7c8900ddaac27b11a19f7441426c954bbb25a2fd60a1640187", 5280445728),
    ("31b", "Q2_K"): ("27b50ef2359e908aec72e0fc589535442eca8b17406c585d7f73db1d3e88cd65", 12609924512),
    ("31b", "Q3_K_M"): ("2b91a6dc48a3544f08150e4e979a67af6ea489ac06ead39d0a3343bad2a2e330", 15863131552),
    ("31b", "Q4_K_M"): ("47788c3eb71b830919a8454022381de943a43edfab65dad9e742997028d1f9df", 19526593952),
}
MODELS = ("0.8b", "2b", "8b", "31b")
QUANTS = ("Q2_K", "Q3_K_M", "Q4_K_M", "Q4_0")
# Native context length of all PLaMo 3 Translate models (GGUF plamo3.context_length).
NATIVE_CTX = 262144
# KV cache bytes per context token (see the Ubuntu installer).
KV_BYTES = {"0.8b": 1024, "2b": 6144, "8b": 8192, "31b": 32768}
# Context (tokens) that one request of plamo-3-translate-client.py with its defaults can
# need for long Japanese/English documents.  Keep in sync with the client.
CLIENT_CHUNK_CHARS = 20000
CLIENT_CTX = 40000

# Settings: key -> default.  Values come from options > environment (PLAMO3_<KEY>) >
# install.json of the previous installation > default.  backend: empty = suggested from
# the detected GPU.  gpus: empty = one GPU, chosen when llama.cpp is installed.
KEYS = ("destdir", "model", "quant", "backend", "gpus", "ctx_size", "web_port", "llama_port",
        "listen", "expose_llama")
DEFAULTS = {
    "destdir": r"C:\LLM\PLaMo3Translate", "model": "2b", "quant": "Q4_K_M", "backend": "",
    "gpus": "", "ctx_size": "auto", "web_port": "18080", "llama_port": "28080",
    "listen": "no", "expose_llama": "no",
}
FLAG_KEYS = ("listen", "expose_llama")

DOWNLOAD_RETRIES = 5     # consecutive attempts without progress before giving up
DOWNLOAD_RETRY_DELAY = 5
CURL_TRIES = 50
SID_ADMINISTRATORS = "*S-1-5-32-544"
SID_SYSTEM = "*S-1-5-18"
SID_USERS = "*S-1-5-32-545"


class Die(Exception):
    """A fatal error to show to the user (no traceback)."""


def info(message):
    print(f"==> {message}", flush=True)


def warn(message):
    sys.stdout.flush()
    print(f"Warning: {message}", file=sys.stderr, flush=True)


USAGE = f"""\
Usage: install-plamo-3-translate.bat [options]

Installs PLaMo 3 Translate (llama.cpp, a web translation page and a command-line
client) for the users of this PC.  Run it as Administrator.  Values that are not
given are asked interactively; when standard input is not a terminal, defaults
are used.  Re-running updates the installation.

Options (environment variable in brackets):
  --destdir DIR            install directory [PLAMO3_DESTDIR] (default {DEFAULTS['destdir']})
  --model SIZE             0.8b, 2b, 8b or 31b [PLAMO3_MODEL] (default {DEFAULTS['model']})
  --quant TYPE             Q2_K, Q3_K_M, Q4_K_M or Q4_0 (0.8b/2b only)
                           [PLAMO3_QUANT] (default {DEFAULTS['quant']})
  --backend NAME           cpu, vulkan, cuda12 or cuda13 [PLAMO3_BACKEND]
                           (default: suggested from the detected GPU)
  --gpus NAME[,NAME...]|all
                           GPUs used by llama-server, by the device names that
                           "llama-server --list-devices" prints, such as Vulkan0
                           or CUDA0,CUDA1 [PLAMO3_GPUS] (default: one GPU, the one
                           with the most free memory; asked when the backend sees
                           two or more)
  --ctx-size N|auto        llama-server context size [PLAMO3_CTX_SIZE] (default auto: the
                           model maximum {NATIVE_CTX}, reduced by llama.cpp to fit the memory)
  --web-port PORT          web server port [PLAMO3_WEB_PORT] (default {DEFAULTS['web_port']})
  --llama-port PORT        llama-server port [PLAMO3_LLAMA_PORT] (default {DEFAULTS['llama_port']})
  --listen                 let the web server listen on 0.0.0.0 (all interfaces)
  --no-listen              let the web server listen on 127.0.0.1 only (default)
                           [PLAMO3_LISTEN=yes|no]
  --expose-llama           let llama-server listen on 0.0.0.0 with an API key
  --no-expose-llama        let llama-server listen on 127.0.0.1 only (default)
                           [PLAMO3_EXPOSE_LLAMA=yes|no]
  --accept-license         accept the PLaMo Community License [PLAMO3_ACCEPT_LICENSE=yes]
  --yes                    answer yes to warnings, deleting unused model files and
                           uninstalling
  --uninstall              uninstall
  -h, --help               show this help

The installer never changes the Windows firewall: with --listen or --expose-llama,
open the ports yourself if other hosts are to connect.
"""


def yesno(value):
    """Normalize yes/no; returns "yes", "no" or "" for anything else."""
    value = str(value).strip().lower()
    if value in ("y", "yes", "true", "1", "on"):
        return "yes"
    if value in ("n", "no", "false", "0", "off"):
        return "no"
    return ""


def human(size):
    """A size in bytes with a binary unit, e.g. 1.7GiB."""
    size = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024


def gguf_name(model, quant):
    return f"plamo-3-translate-{model}-{quant}.gguf"


def gguf_url(model, quant):
    return (f"{HF_BASE_URL}/pfnet/plamo-3-translate-{model}-GGUF/resolve/"
            f"{MODEL_REVISION[model]}/{gguf_name(model, quant)}")


def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def write_text(path, text, newline="\n"):
    """Write a generated file atomically: UTF-8 without BOM."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline=newline) as f:
        f.write(text)
    retrying(os.replace, tmp, path)


def remove_tree(path):
    """shutil.rmtree that also removes read-only files (Windows)."""
    def retry(function, target, _exc):
        os.chmod(target, 0o700)
        function(target)
    if os.path.lexists(path):
        shutil.rmtree(path, onexc=retry)


def retrying(function, *args):
    """Call function(*args); on Windows a file that was just written can be held open for
    a moment by a virus scanner, which makes a rename fail with PermissionError."""
    for attempt in range(10):
        try:
            return function(*args)
        except PermissionError:
            if attempt == 9:
                raise
            time.sleep(1)


def existing_dir(path):
    """The nearest existing ancestor of path."""
    while not os.path.isdir(path):
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    return path


def validate_destdir(value):
    """Check and normalize the install directory (a Windows path)."""
    path = value.strip().strip('"').replace("/", "\\")
    while len(path) > 3 and path.endswith("\\"):
        path = path[:-1]
    if not re.fullmatch(r"[A-Za-z]:\\.+", path):
        raise Die(f"destdir must be an absolute path with a drive letter, such as {DEFAULTS['destdir']}: {value}")
    path = path[0].upper() + path[1:]
    for part in path[3:].split("\\"):
        if (not re.fullmatch(r"[A-Za-z0-9._ -]+", part) or part.startswith(" ")
                or part.endswith((" ", ".")) or part in (".", "..")):
            raise Die("destdir may contain only ASCII letters, digits, spaces and . _ - "
                      f"in its folder names: {value}")
    lower = path.lower()
    system_root = (os.environ.get("SystemRoot") or r"C:\Windows").lower().rstrip("\\")
    forbidden = [ntpath.join(lower[:3], name) for name in
                 ("windows", "users", "program files", "program files (x86)", "programdata")]
    if lower in forbidden or lower == system_root or lower.startswith((system_root + "\\", lower[:3] + "windows\\")):
        raise Die(f"destdir must not be a system directory: {path}")
    return path


# -------------------------------------------------------- Windows specifics
#
# Nothing in this section can be run on the development host (Linux); the tests replace
# these functions.

def is_admin():
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def check_platform():
    if not WINDOWS:
        raise Die("this installer runs on Windows only")
    arch = (os.environ.get("PROCESSOR_ARCHITEW6432") or os.environ.get("PROCESSOR_ARCHITECTURE") or "").upper()
    if arch != "AMD64":
        raise Die(f"only x64 (AMD64) is supported (found {arch or 'unknown'})")
    if not is_admin():
        raise Die("run install-plamo-3-translate.bat as Administrator")


def windows_build():
    try:
        return sys.getwindowsversion().build
    except Exception:
        return None


def relaunch_elevated(argv):
    """Start this script again elevated (UAC prompt), in a new console window."""
    import ctypes
    params = subprocess.list2cmdline([os.path.abspath(__file__)] + list(argv))
    result = ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, params, None, 1)
    if result <= 32:
        raise Die("administrator privileges were not granted")


def wait_for_key():
    try:
        if WINDOWS and sys.stdin and sys.stdin.isatty():
            import msvcrt
            print("\nPress any key to close this window.", flush=True)
            msvcrt.getch()
    except Exception:
        pass


def quiet_error_dialogs():
    """No dialog box when a program cannot start (e.g. a missing DLL); the exit status tells."""
    if not WINDOWS:
        return
    try:
        import ctypes
        ctypes.WinDLL("kernel32").SetErrorMode(0x8003)
    except Exception:
        pass


def total_memory():
    """Installed memory in bytes, or None if it cannot be found out."""
    try:
        if WINDOWS:
            import ctypes

            class MemoryStatus(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_uint32), ("dwMemoryLoad", ctypes.c_uint32)] + [
                    (name, ctypes.c_uint64) for name in (
                        "ullTotalPhys", "ullAvailPhys", "ullTotalPageFile", "ullAvailPageFile",
                        "ullTotalVirtual", "ullAvailVirtual", "ullAvailExtendedVirtual")]

            status = MemoryStatus()
            status.dwLength = ctypes.sizeof(status)
            if not ctypes.WinDLL("kernel32").GlobalMemoryStatusEx(ctypes.byref(status)):
                return None
            return int(status.ullTotalPhys)
        return os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
    except Exception:
        return None


def file_in_use(path):
    """True if a program file is running (Windows does not let it be opened for writing)."""
    if not WINDOWS or not os.path.isfile(path):
        return False
    try:
        with open(path, "r+b"):
            return False
    except PermissionError:
        return True
    except OSError:
        return False


def run_powershell(script, timeout=120):
    """Run a short PowerShell script given inline (no .ps1 file, so the execution policy
    does not apply); returns its standard output.  Raises OSError or
    subprocess.SubprocessError on failure."""
    exe = os.path.join(os.environ.get("SystemRoot") or r"C:\Windows",
                       "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    script = "$ErrorActionPreference = 'Stop'\n$ProgressPreference = 'SilentlyContinue'\n" + script
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    result = subprocess.run([exe, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                            stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout)
    output = result.stdout.decode("utf-8", "replace")
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip() or output.strip()
        raise subprocess.SubprocessError(f"PowerShell failed (exit status {result.returncode}): {detail}")
    return output


def ps_quote(text):
    """A PowerShell single-quoted string literal."""
    return "'" + str(text).replace("'", "''") + "'"


def gpu_vendors():
    """Vendors of the display adapters (nvidia, amd, intel), sorted.  Empty when they
    cannot be listed: detection must never stop an installation."""
    try:
        output = run_powershell("Get-CimInstance Win32_VideoController | ForEach-Object { $_.Name }", timeout=60)
    except Exception:
        return []
    return vendors_from_names(output.splitlines())


def vendors_from_names(names):
    vendors = set()
    for name in names:
        lower = name.lower()
        if "nvidia" in lower:
            vendors.add("nvidia")
        elif "amd" in lower or "radeon" in lower:
            vendors.add("amd")
        elif "intel" in lower:
            vendors.add("intel")
    return sorted(vendors)


def nvidia_driver_major():
    """The NVIDIA driver major version, or None if nvidia-smi does not work."""
    try:
        result = subprocess.run(["nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader"],
                                stdin=subprocess.DEVNULL, capture_output=True, timeout=60)
        match = re.match(r"\s*(\d+)", result.stdout.decode("ascii", "replace"))
        return int(match.group(1)) if result.returncode == 0 and match else None
    except Exception:
        return None


def detect_backend():
    """Suggest a backend from the detected GPUs.  CUDA is suggested only when the NVIDIA
    driver works (nvidia-smi), since the installer does not install drivers."""
    try:
        vendors = gpu_vendors()
        if "nvidia" in vendors:
            major = nvidia_driver_major()
            if major is not None:
                return "cuda13" if major >= 580 else "cuda12"
        if "amd" in vendors or "intel" in vendors:
            return "vulkan"
    except Exception:
        pass
    return "cpu"


def vulkan_runtime_present():
    return os.path.isfile(os.path.join(os.environ.get("SystemRoot") or r"C:\Windows", "System32", "vulkan-1.dll"))


def permission_commands(path, recursive):
    """icacls command lines that make path read-only for ordinary users: inheritance is
    cut, Administrators and SYSTEM get full control, Users read and execute.  (A folder
    made directly under C:\\ would otherwise be writable by every user.)  With recursive,
    everything below is reset to inherit just that."""
    commands = [
        ["icacls", path, "/reset", "/Q"],
        ["icacls", path, "/inheritance:r", "/grant:r",
         f"{SID_ADMINISTRATORS}:(OI)(CI)F", f"{SID_SYSTEM}:(OI)(CI)F", f"{SID_USERS}:(OI)(CI)RX", "/Q"],
    ]
    if recursive:
        commands += [
            ["icacls", path, "/setowner", SID_ADMINISTRATORS, "/T", "/C", "/Q"],
            ["icacls", os.path.join(path, "*"), "/reset", "/T", "/C", "/Q"],
        ]
    return commands


def set_permissions(path, recursive=False):
    for command in permission_commands(path, recursive):
        try:
            result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, timeout=600)
            failed = result.returncode != 0
            detail = (result.stdout + result.stderr).decode("oem" if WINDOWS else "utf-8", "replace").strip()
        except (OSError, subprocess.SubprocessError) as e:
            failed, detail = True, str(e)
        if failed:
            warn(f"could not set the permissions of {path} ({' '.join(command[2:4])}): {detail}\n"
                 "Ordinary users may be able to change the installed files (is the drive NTFS?).")
            return False
    return True


def menu_dir():
    base = os.environ.get("ProgramData") or r"C:\ProgramData"
    return os.path.join(base, "Microsoft", "Windows", "Start Menu", "Programs", APP_TITLE)


def cmd_exe():
    return os.environ.get("ComSpec") or os.path.join(os.environ.get("SystemRoot") or r"C:\Windows", "System32", "cmd.exe")


def shortcut_items(destdir):
    """The Start menu shortcuts: (name, target, arguments, working directory, description)."""
    python = installed_python(destdir)
    launcher = os.path.join(destdir, "app", LAUNCHER_PY)
    installer = os.path.join(destdir, "app", INSTALLER_PY)
    prompt = os.path.join(destdir, PROMPT_BAT)
    return [
        (f"{APP_TITLE} Web UI", python, f'"{launcher}" --mode webui', destdir,
         "Start PLaMo 3 Translate and open the translation page in the web browser"),
        (f"{APP_TITLE} llama-server", python, f'"{launcher}" --mode llama', destdir,
         "Start llama-server only, for plamo-3-translate-client.py"),
        (f"{APP_TITLE} Command Prompt", cmd_exe(), f'/k ""{prompt}""', destdir,
         "Command prompt (UTF-8) for plamo-3-translate-client"),
        (f"Uninstall {APP_TITLE}", python,
         f'"{installer}" --uninstall --destdir "{destdir}" --elevate --pause',
         os.environ.get("SystemRoot") or r"C:\Windows",
         "Remove PLaMo 3 Translate from this PC"),
    ]


def shortcut_script(folder, items):
    """PowerShell that writes the .lnk files (only PowerShell/COM can make them)."""
    lines = ["$shell = New-Object -ComObject WScript.Shell"]
    for name, target, arguments, workdir, description in items:
        lines += [
            f"$s = $shell.CreateShortcut({ps_quote(os.path.join(folder, name + '.lnk'))})",
            f"$s.TargetPath = {ps_quote(target)}",
            f"$s.Arguments = {ps_quote(arguments)}",
            f"$s.WorkingDirectory = {ps_quote(workdir)}",
            f"$s.Description = {ps_quote(description)}",
            "$s.Save()",
        ]
    return "\n".join(lines) + "\n"


def create_shortcuts(destdir):
    folder = menu_dir()
    os.makedirs(folder, exist_ok=True)
    for name in os.listdir(folder):  # shortcuts of an older version
        if name.lower().endswith(".lnk"):
            os.remove(os.path.join(folder, name))
    try:
        run_powershell(shortcut_script(folder, shortcut_items(destdir)))
    except (OSError, subprocess.SubprocessError) as e:
        raise Die(f"cannot create the Start menu shortcuts in {folder}: {e}") from None
    return folder


def schedule_delete(path):
    """Delete path from a detached cmd.exe once this process is gone (a running
    python.exe cannot delete its own directory)."""
    command = (f'"{cmd_exe()}" /d /s /c "for /l %i in (1,1,30) do @if exist "{path}\\" '
               f'(rmdir /s /q "{path}" 2>nul & ping -n 2 127.0.0.1 >nul)"')
    flags = 0x00000200 | 0x08000000  # CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
    subprocess.Popen(command, cwd=os.environ.get("SystemRoot") or "C:\\", creationflags=flags, close_fds=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def installed_python(destdir):
    return os.path.join(destdir, "python", "python.exe")


def local_data_dir():
    """Run-time files of the current user (logs)."""
    base = os.environ.get("LOCALAPPDATA")
    return os.path.join(base, "PLaMo3Translate") if base else None


# ----------------------------------------------------------------- probes

def port_open(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def port_user(port):
    """Who listens on 127.0.0.1:port: "" (nobody), "web" (our web server), "llama"
    (a llama-server) or "other"."""
    if not port_open(port):
        return ""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("GET", "/")
        response = conn.getresponse()
        body = response.read()
        if response.status == 200 and WEB_TITLE in body:
            return "web"
        if "llama.cpp" in (response.getheader("Server") or "") or b"Loading model" in body:
            return "llama"
        conn.close()
        conn.request("GET", "/health")
        response = conn.getresponse()
        body = response.read()
        if response.status == 200 and b'"status"' in body:
            return "llama"
    except (OSError, http.client.HTTPException):
        pass
    finally:
        conn.close()
    return "other"


# ---------------------------------------------------------------- download

class Progress:
    """Download progress: a line rewritten in place on a terminal, else a line per 10%."""

    def __init__(self, total):
        self.total = total
        self.tty = bool(sys.stdout and sys.stdout.isatty())
        self.last_time = 0.0
        self.last_step = -1

    def update(self, done):
        percent = done * 100 / self.total if self.total else 100.0
        if self.tty:
            now = time.monotonic()
            if now - self.last_time >= 0.5 or done >= self.total:
                print(f"\r  {percent:5.1f}%  {human(done)} of {human(self.total)}   ", end="", flush=True)
                self.last_time = now
        elif int(percent // 10) > self.last_step:
            self.last_step = int(percent // 10)
            print(f"  {int(percent)}%  {human(done)} of {human(self.total)}", flush=True)

    def close(self):
        if self.tty:
            print(flush=True)


def is_certificate_error(error):
    reason = getattr(error, "reason", error)
    return isinstance(reason, ssl.SSLCertVerificationError)


def fetch_once(url, part, have, size, progress):
    """One attempt: append to part from offset have (or start again if the server ignores
    the range).  Raises OSError (also urllib.error.URLError) or http.client.HTTPException."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    if have:
        request.add_header("Range", f"bytes={have}-")
    with urllib.request.urlopen(request, timeout=60) as response:
        if have and response.status != 206:
            have = 0
        with open(part, "ab" if have else "wb") as f:
            done = have
            while True:
                block = response.read(1 << 20)
                if not block:
                    break
                f.write(block)
                done += len(block)
                if done > size:
                    raise Die(f"{url} is larger than expected ({size} bytes); the download was stopped")
                progress.update(done)
    if done < size:
        raise ConnectionError("the connection was closed before the end of the file")


def curl_fetch(url, part):
    """Download with curl (resuming), in a retry loop of our own: curl's --retry discards
    what the failed attempt wrote."""
    curl = shutil.which("curl.exe" if WINDOWS else "curl")
    if not curl:
        raise Die(f"download failed: {url}: the server certificate could not be verified and curl was not found")
    for attempt in range(1, CURL_TRIES + 1):
        status = subprocess.run([curl, "-L", "--fail", "-C", "-", "--connect-timeout", "30",
                                 "--speed-limit", "1000", "--speed-time", "60",
                                 "-A", USER_AGENT, "-o", part, url], stdin=subprocess.DEVNULL).returncode
        if status == 0:
            return
        if attempt < CURL_TRIES:
            warn(f"download interrupted (curl exit status {status}); resuming in {DOWNLOAD_RETRY_DELAY} s "
                 f"({attempt}/{CURL_TRIES})")
            time.sleep(DOWNLOAD_RETRY_DELAY)
    raise Die(f"download failed: {url} (re-run the installer to resume)")


def fetch(url, part, size):
    """Download url into part, resuming after interruptions."""
    failures = 0
    progress = Progress(size)
    try:
        while True:
            have = os.path.getsize(part) if os.path.exists(part) else 0
            if have >= size:
                return
            try:
                fetch_once(url, part, have, size, progress)
                continue
            except urllib.error.HTTPError as e:
                e.close()
                if e.code == 416:  # the range is not valid for this file: start again
                    os.remove(part)
                elif 400 <= e.code < 500 and e.code not in (408, 429):
                    raise Die(f"download failed: {url}: HTTP {e.code} {e.reason}") from None
                error = e
            except (OSError, http.client.HTTPException) as e:
                if is_certificate_error(e):
                    # Python checks certificates against the Windows certificate store, which
                    # gets some root certificates only when Windows itself needs them.
                    # curl.exe uses the Windows TLS library, which fetches them.
                    progress.close()
                    warn(f"Python could not verify the server certificate ({getattr(e, 'reason', e)}); using curl")
                    curl_fetch(url, part)
                    return
                error = e
            now = os.path.getsize(part) if os.path.exists(part) else 0
            failures = 0 if now > have else failures + 1
            if failures >= DOWNLOAD_RETRIES:
                raise Die(f"download failed: {url}: {error} (re-run the installer to resume)")
            progress.close()
            warn(f"download interrupted ({error}); resuming in {DOWNLOAD_RETRY_DELAY} s")
            time.sleep(DOWNLOAD_RETRY_DELAY)
    finally:
        progress.close()


def download_verified(url, dest, sha256, size):
    """Download with resume and verify; a verified file that is there already is reused."""
    name = os.path.basename(dest)
    part = dest + ".part"
    if os.path.isfile(dest):
        info(f"Verifying existing {name}")
        if os.path.getsize(dest) == size and sha256_of(dest) == sha256:
            info(f"Reusing {name}")
            return
        warn(f"{name} does not match the expected checksum; downloading it again")
        os.remove(dest)
    if os.path.isfile(part) and os.path.getsize(part) > size:
        os.remove(part)
    if not os.path.isfile(part) or os.path.getsize(part) < size:
        info(f"Downloading {name} ({human(size)})")
        fetch(url, part, size)
    info(f"Verifying {name}")
    if os.path.getsize(part) != size or sha256_of(part) != sha256:
        os.remove(part)
        raise Die(f"checksum mismatch for {name}; the file was removed, re-run the installer to retry")
    retrying(os.replace, part, dest)


def extract_zip(archive, directory):
    with zipfile.ZipFile(archive) as z:
        z.extractall(directory)


def list_devices(exe):
    """The GPUs the installed llama.cpp sees: [(name, description, total MiB, free MiB)].
    Raises OSError or subprocess.SubprocessError if llama-server cannot be asked."""
    result = subprocess.run([exe, "--list-devices"], stdin=subprocess.DEVNULL, capture_output=True, timeout=120)
    if result.returncode != 0:
        raise subprocess.SubprocessError(f"exit status {result.returncode}")
    return parse_devices(result.stdout.decode("utf-8", "replace"))


def parse_devices(text):
    devices = []
    for line in text.splitlines():
        match = re.fullmatch(r"\s+(\S+): (.*) \((\d+) MiB, (\d+) MiB free\)\s*", line)
        if match:
            devices.append((match.group(1), match.group(2), int(match.group(3)), int(match.group(4))))
    return devices


def rank_devices(devices):
    """Most free memory first, then the largest memory, then the name."""
    return sorted(devices, key=lambda d: (-d[3], -d[2], d[0]))


# ---------------------------------------------------------------- installer

class Installer:
    def __init__(self, args, environ=None, interactive=None):
        self.args = args
        self.environ = os.environ if environ is None else environ
        self.interactive = bool(sys.stdin and sys.stdin.isatty()) if interactive is None else interactive
        self.assume_yes = args.yes
        self.accept_license = args.accept_license or yesno(self.environ.get("PLAMO3_ACCEPT_LICENSE", "")) == "yes"
        self.val = {}
        self.from_user = set()
        self.saved = {}
        self.n_ctx = None
        self.delete_at_exit = None  # uninstall: a directory to delete when this process ends

    # ---- questions

    def read_line(self, prompt):
        try:
            return input(prompt).strip()
        except EOFError:
            raise Die("no input") from None

    def confirm(self, question):
        """True for yes.  --yes answers yes; non-interactive answers no."""
        if self.assume_yes:
            return True
        if not self.interactive:
            return False
        return yesno(self.read_line(f"{question} [y/N] ")) == "yes"

    def warn_continue(self, message):
        warn(message)
        if not self.confirm("Continue anyway?"):
            raise Die("aborted (use --yes to continue despite warnings)")

    def ask(self, key, prompt, text=None):
        """Ask for a value unless it was given by an option or the environment."""
        if key in self.from_user or not self.interactive:
            return
        if text:
            print(text)
        answer = self.read_line(f"{prompt} [{self.val[key]}]: ")
        if answer:
            self.val[key] = answer

    # ---- values

    def option(self, key):
        """The value given by an option or the environment, or None."""
        value = getattr(self.args, key, None)
        if value is None:
            value = self.environ.get("PLAMO3_" + key.upper()) or None
        return value

    def resolve_destdir(self):
        value = self.option("destdir")
        if value is not None:
            self.from_user.add("destdir")
        self.val["destdir"] = value if value is not None else DEFAULTS["destdir"]
        self.ask("destdir", "Install directory")
        self.val["destdir"] = validate_destdir(self.val["destdir"])

    def load_saved(self):
        path = os.path.join(self.val["destdir"], CONFIG_NAME)
        if not os.path.isfile(path):
            return
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            self.saved = {key: str(data[key]) for key in KEYS if key != "destdir" and key in data}
        except (OSError, ValueError, TypeError, AttributeError) as e:
            warn(f"cannot read the previous settings in {path} ({e}); using the defaults")

    def resolve_values(self):
        for key in KEYS:
            if key == "destdir":
                continue
            value = self.option(key)
            if value is not None:
                self.val[key] = value
                self.from_user.add(key)
            elif self.saved.get(key):
                self.val[key] = self.saved[key]
            else:
                self.val[key] = DEFAULTS[key]
        for key in FLAG_KEYS:
            normalized = yesno(self.val[key])
            if not normalized:
                raise Die(f"invalid value for {key.replace('_', '-')}: {self.val[key]} (use yes or no)")
            self.val[key] = normalized
        if not self.val["backend"]:
            self.val["backend"] = detect_backend()

    def prompt_values(self):
        if not self.interactive:
            return
        self.ask("model", "Model size (0.8b, 2b, 8b, 31b)")
        self.ask("quant", "Quantization (Q2_K, Q3_K_M, Q4_K_M; Q4_0 for 0.8b/2b only)")
        if "backend" not in self.from_user:
            vendors = gpu_vendors()
            if "nvidia" in vendors and nvidia_driver_major() is None:
                warn("an NVIDIA GPU was found but nvidia-smi does not work; install the NVIDIA driver to use CUDA")
            lines = "\n".join(f"  {name:<7} {BACKEND_DESC[name]}" for name in BACKENDS)
            self.ask("backend", "llama.cpp backend",
                     f"Detected GPUs: {', '.join(vendors) or 'none'}.  Available llama.cpp backends:\n{lines}")
        self.ask("ctx_size", f"Context size (auto = model maximum {NATIVE_CTX}, reduced to fit the memory; "
                             f"or 512-{NATIVE_CTX})")
        self.ask("web_port", "Web server port")
        self.ask("llama_port", "llama-server port")
        self.ask("listen", "Let the web server accept connections from other hosts (no login)? (yes/no)")
        self.ask("expose_llama", "Expose llama-server to other hosts (API key required)? (yes/no)")
        for key in FLAG_KEYS:
            normalized = yesno(self.val[key])
            if not normalized:
                raise Die("answer yes or no")
            self.val[key] = normalized

    def validate(self):
        val = self.val
        val["model"] = val["model"].lower()
        if val["model"] not in MODELS:
            raise Die(f"model must be one of: {' '.join(MODELS)}")
        val["quant"] = val["quant"].upper()
        if val["quant"] not in QUANTS:
            raise Die(f"quantization must be one of: {' '.join(QUANTS)}")
        if (val["model"], val["quant"]) not in GGUF:
            raise Die(f"{val['quant']} is available only for the 0.8b and 2b models")
        val["backend"] = val["backend"].lower()
        if val["backend"] not in BACKENDS:
            raise Die(f"backend must be one of: {' '.join(BACKENDS)}")
        val["ctx_size"] = val["ctx_size"].lower()
        if val["ctx_size"] != "auto":
            if not re.fullmatch(r"[0-9]{1,7}", val["ctx_size"]):
                raise Die(f"context size must be auto or an integer: {val['ctx_size']}")
            val["ctx_size"] = str(int(val["ctx_size"]))
            if not 512 <= int(val["ctx_size"]) <= NATIVE_CTX:
                raise Die(f"context size must be auto or between 512 and {NATIVE_CTX}")
        for key in ("web_port", "llama_port"):
            if not re.fullmatch(r"[0-9]{1,5}", val[key]) or not 1 <= int(val[key]) <= 65535:
                raise Die(f"invalid port number: {val[key]}")
            val[key] = str(int(val[key]))
        if val["web_port"] == val["llama_port"]:
            raise Die("the web server and llama-server ports must differ")
        if val["backend"] == "cpu":
            if "gpus" in self.from_user:
                warn("--gpus is ignored with the cpu backend")
            val["gpus"] = ""
        elif val["gpus"].lower() == "all":
            val["gpus"] = "all"
        elif val["gpus"] and not re.fullmatch(r"[A-Za-z0-9_.:-]+(,[A-Za-z0-9_.:-]+)*", val["gpus"]):
            raise Die(f"invalid --gpus: {val['gpus']} (use device names such as Vulkan0 or CUDA0,CUDA1, or all)")

    def confirm_license(self):
        if self.accept_license:
            info("PLaMo Community License accepted (--accept-license)")
            return
        if not self.interactive:
            raise Die(f"non-interactive installation requires --accept-license (see {LICENSE_URL})")
        print(f"""
PLaMo 3 Translate is distributed under the PLaMo Community License.
  {LICENSE_URL}

  - Personal and non-commercial use is free.
  - Commercial use requires registration with Preferred Networks (PFN).
  - Read the full license text at the URL above before using the model.
""")
        if self.read_line("Type 'yes' to accept the license: ") != "yes":
            raise Die("the license was not accepted")

    def ctx_label(self):
        if self.val["ctx_size"] == "auto":
            return f"auto (up to {NATIVE_CTX}, reduced to fit the memory)"
        return self.val["ctx_size"]

    def gpus_label(self):
        if self.val["backend"] == "cpu":
            return "none (cpu backend)"
        if self.val["gpus"] == "all":
            return "all"
        return self.val["gpus"].replace(",", ", ") or "one GPU, chosen after llama.cpp is installed"

    def show_summary(self):
        val = self.val
        print(f"""
Installation settings:
  Install directory : {val['destdir']}
  Model             : {gguf_name(val['model'], val['quant'])}
  llama.cpp backend : {val['backend']} ({BACKEND_DESC[val['backend']]})
  GPUs              : {self.gpus_label()}
  Context size      : {self.ctx_label()}
  Web server        : port {val['web_port']}, {'0.0.0.0 (all interfaces, no login)' if val['listen'] == 'yes' else '127.0.0.1 only'}
  llama-server      : port {val['llama_port']}, {'0.0.0.0 (all interfaces, API key required)' if val['expose_llama'] == 'yes' else '127.0.0.1 only'}
""")
        if val["backend"] != "cpu":
            warn(f"the {val['backend']} backend and GPU selection have not been tested on Windows yet")
        if self.interactive and not self.assume_yes and not self.confirm("Proceed with the installation?"):
            raise Die("aborted")

    # ---- preflight

    def check_not_running(self, ports):
        """Stop if the installed servers are running, or if a port is taken."""
        destdir = self.val["destdir"]
        llama_dir = os.path.join(destdir, "llama.cpp")
        running = False
        if os.path.isdir(llama_dir):
            running = any(file_in_use(os.path.join(llama_dir, name, LLAMA_SERVER_EXE)) for name in os.listdir(llama_dir))
        users = {port: port_user(port) for port in ports}
        saved_ports = {self.saved.get("web_port"), self.saved.get("llama_port")}
        ours = [port for port, user in users.items() if user in ("web", "llama") and str(port) in saved_ports]
        if running or ours:
            raise Die(f"{APP_TITLE} is running; close its window (Web UI or llama-server) and run the installer again")
        for port, user in users.items():
            if user:
                what = {"web": f"a {APP_TITLE} web server", "llama": "a llama-server", "other": "another program"}[user]
                raise Die(f"TCP port {port} is already in use by {what}")

    def preflight(self):
        val = self.val
        info("Checking prerequisites")
        build = windows_build()
        if build is not None and build < 22000:
            self.warn_continue(f"only Windows 11 is supported (found Windows build {build})")
        self.check_not_running([int(val["web_port"]), int(val["llama_port"])])

        # An existing destdir must be empty or a previous installation (uninstall removes it).
        destdir = val["destdir"]
        if os.path.isdir(destdir) and not os.path.exists(os.path.join(destdir, MARKER)) and os.listdir(destdir):
            raise Die(f"{destdir} exists and is not empty; choose another directory")
        if os.path.exists(destdir) and not os.path.isdir(destdir):
            raise Die(f"{destdir} is not a directory")

        # Disk space: GGUF (unless already present) + llama.cpp archives and extracted files + Python.
        gguf = gguf_name(val["model"], val["quant"])
        size = GGUF[(val["model"], val["quant"])][1]
        need = 0 if os.path.isfile(os.path.join(destdir, "models", gguf)) else size
        need += sum(asset[2] * 3 for asset in LLAMA_ASSETS[val["backend"]]) + 512 * 1024 * 1024
        base = existing_dir(destdir)
        try:
            avail = shutil.disk_usage(base).free
        except OSError:
            avail = None
        if avail is not None and avail < need:
            self.warn_continue(f"not enough free disk space in {base}: {human(avail)} available, "
                               f"about {human(need)} needed")

        # Memory: model + KV cache + compute buffer (about 1 KiB per token) + 1 GiB margin.
        # With --ctx-size auto, llama.cpp reduces the context (down to 4096) to fit the
        # memory, so only the minimum is required; otherwise the given context size is.
        ctx = NATIVE_CTX if val["ctx_size"] == "auto" else int(val["ctx_size"])
        per_token = KV_BYTES[val["model"]] + 1024
        need_mem = size + ctx * per_token + (1 << 30)
        min_mem = size + 4096 * per_token + (1 << 30)
        if val["backend"] == "cpu":
            mem = total_memory()
            if mem is None:
                pass
            elif val["ctx_size"] != "auto" and mem < need_mem:
                self.warn_continue(f"{gguf} with context size {ctx} may not fit in memory: "
                                   f"{human(mem)} installed, about {human(need_mem)} needed")
            elif mem < min_mem:
                self.warn_continue(f"{gguf} may not fit in memory: {human(mem)} installed, "
                                   f"about {human(min_mem)} needed")
            elif mem < need_mem:
                info(f"The full context ({NATIVE_CTX}) needs about {human(need_mem)}; "
                     f"llama.cpp will reduce it to fit {human(mem)}")
        elif val["backend"] in ("cuda12", "cuda13"):
            major = nvidia_driver_major()
            if major is None:
                raise Die("nvidia-smi does not work; install the NVIDIA driver first and re-run")
            if val["backend"] == "cuda13" and major < 580:
                self.warn_continue(f"CUDA 13.5 needs NVIDIA driver 580 or later (found {major}); "
                                   "consider --backend cuda12")
        elif val["backend"] == "vulkan" and WINDOWS and not vulkan_runtime_present():
            self.warn_continue("the Vulkan runtime (vulkan-1.dll) was not found; install the GPU driver first")

    # ---- install

    def prepare_destdir(self):
        destdir = self.val["destdir"]
        top = None  # the topmost directory that has to be created
        path = destdir
        while not os.path.isdir(path):
            top = path
            parent = os.path.dirname(path)
            if parent == path:
                break
            path = parent
        try:
            for sub in ("", "llama.cpp", "models", "app"):
                os.makedirs(os.path.join(destdir, sub), exist_ok=True)
        except OSError as e:
            raise Die(f"cannot create {destdir}: {e}") from None
        if top and top != destdir:
            set_permissions(top)
        set_permissions(destdir)
        with open(os.path.join(destdir, MARKER), "a", encoding="ascii"):
            pass

    def install_python(self):
        destdir = self.val["destdir"]
        target = os.path.join(destdir, "python")
        stamp = os.path.join(target, ".installed")
        if os.path.isfile(installed_python(destdir)) and os.path.isfile(stamp):
            with open(stamp, encoding="ascii") as f:
                if f.read().strip() == PYTHON_SHA256:
                    info(f"Python {PYTHON_VERSION} is already installed")
                    return
        if os.path.abspath(sys.executable).lower().startswith(os.path.abspath(target).lower() + os.sep):
            raise Die("the Python of this installation cannot replace itself; "
                      "run install-plamo-3-translate.bat instead")
        # install-plamo-3-translate.bat keeps the archive next to the Python it unpacked.
        archive = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(sys.executable))), PYTHON_ZIP)
        download = None
        if not (os.path.isfile(archive) and os.path.getsize(archive) == PYTHON_SIZE
                and sha256_of(archive) == PYTHON_SHA256):
            download = os.path.join(destdir, "python.download")
            os.makedirs(download, exist_ok=True)
            archive = os.path.join(download, PYTHON_ZIP)
            download_verified(PYTHON_URL, archive, PYTHON_SHA256, PYTHON_SIZE)
        info(f"Installing Python {PYTHON_VERSION} (embeddable package)")
        remove_tree(target + ".tmp")
        os.makedirs(target + ".tmp")
        extract_zip(archive, target + ".tmp")
        if not os.path.isfile(os.path.join(target + ".tmp", "python.exe")):
            raise Die("python.exe not found in the Python archive")
        write_text(os.path.join(target + ".tmp", ".installed"), PYTHON_SHA256 + "\n")
        retrying(remove_tree, target)
        retrying(os.rename, target + ".tmp", target)
        if download:
            remove_tree(download)

    def llama_dir(self):
        return os.path.join(self.val["destdir"], "llama.cpp", f"{LLAMA_TAG}-{self.val['backend']}")

    def install_llama(self):
        backend = self.val["backend"]
        top = os.path.join(self.val["destdir"], "llama.cpp")
        directory = self.llama_dir()
        download = os.path.join(top, ".download")
        assets = LLAMA_ASSETS[backend]
        stamp = "".join(f"{name} {sha256} {size}\n" for name, sha256, size in assets)
        exe = os.path.join(directory, LLAMA_SERVER_EXE)
        installed = ""
        if os.path.isfile(exe) and os.path.isfile(os.path.join(directory, ".installed")):
            with open(os.path.join(directory, ".installed"), encoding="ascii") as f:
                installed = f.read()
        if installed == stamp:
            info(f"llama.cpp {LLAMA_TAG} ({backend}) is already installed")
        else:
            os.makedirs(download, exist_ok=True)
            remove_tree(directory + ".tmp")
            os.makedirs(directory + ".tmp")
            for name, sha256, size in assets:
                download_verified(f"{LLAMA_BASE_URL}/{name}", os.path.join(download, name), sha256, size)
                info(f"Extracting {name}")
                extract_zip(os.path.join(download, name), directory + ".tmp")
            if not os.path.isfile(os.path.join(directory + ".tmp", LLAMA_SERVER_EXE)):
                raise Die(f"{LLAMA_SERVER_EXE} not found in the llama.cpp archive")
            write_text(os.path.join(directory + ".tmp", ".installed"), stamp)
            retrying(remove_tree, directory)
            retrying(os.rename, directory + ".tmp", directory)
            remove_tree(download)
        # Remove builds for other backends and versions.
        for name in os.listdir(top):
            if name != os.path.basename(directory):
                remove_tree(os.path.join(top, name))
        self.check_llama_runs(exe)

    def check_llama_runs(self, exe):
        """llama-server must start; the usual reason why it does not is a missing
        Visual C++ runtime (exit status 0xC0000135, STATUS_DLL_NOT_FOUND)."""
        quiet_error_dialogs()
        try:
            result = subprocess.run([exe, "--version"], stdin=subprocess.DEVNULL, capture_output=True, timeout=120)
        except (OSError, subprocess.SubprocessError) as e:
            raise Die(f"cannot run {exe}: {e}") from None
        status = result.returncode
        if status in (0xC0000135, 0xC0000135 - (1 << 32)):
            raise Die(f"{LLAMA_SERVER_EXE} cannot start: a DLL that it needs was not found.\n"
                      "Install the Microsoft Visual C++ Redistributable (x64) and run the installer again:\n"
                      f"  {VC_REDIST_URL}")
        if status != 0:
            output = (result.stdout + result.stderr).decode("utf-8", "replace").strip()
            raise Die(f"{LLAMA_SERVER_EXE} --version failed (exit status {status}): {output}")

    def select_gpus(self):
        """Check the GPU selection against the devices the installed llama.cpp sees.  An
        empty selection (the default) becomes one GPU, the one with the most free
        memory, or "all" when the devices cannot be listed.  Untested on real GPUs."""
        val = self.val
        if val["backend"] == "cpu" or val["gpus"] == "all":
            return
        try:
            devices = list_devices(os.path.join(self.llama_dir(), LLAMA_SERVER_EXE))
        except (OSError, subprocess.SubprocessError) as e:
            devices = None
            warn(f"cannot list the GPUs of the {val['backend']} backend ({e})")
        if not devices:
            if devices is not None:
                warn(f"the {val['backend']} backend found no GPU; llama-server will run on the CPU")
            if val["gpus"] and "gpus" in self.from_user:
                raise Die(f"GPU {val['gpus']} is not available for the {val['backend']} backend; "
                          "re-run with --gpus all")
            val["gpus"] = "all"
            return
        names = [d[0] for d in devices]
        ranked = [d[0] for d in rank_devices(devices)]
        selected = []
        for name in filter(None, val["gpus"].split(",")):
            match = next((n for n in names if n.lower() == name.lower()), None)
            if match is None:
                if "gpus" in self.from_user:
                    raise Die(f"GPU {name} is not available for the {val['backend']} backend "
                              f"(available: {' '.join(names)})")
                warn(f"the saved GPU selection ({val['gpus']}) does not match this machine; choosing a GPU again")
                selected = []
                break
            if match not in selected:
                selected.append(match)
        if not selected:
            selected = ranked[:1]
        if len(devices) >= 2 and "gpus" not in self.from_user and self.interactive:
            print(f"GPUs available to the {val['backend']} backend:")
            for i, (name, description, total, free) in enumerate(devices, 1):
                print(f"  {i}) {name}  {description} ({total} MiB, {free} MiB free)")
            default = ",".join(str(names.index(n) + 1) for n in selected)
            while True:
                answer = self.read_line(f"GPUs to use (numbers such as 1 or 1,2; or all) [{default}]: ") or default
                if answer.lower() == "all":
                    val["gpus"] = "all"
                    return
                tokens = [t for t in re.split(r"[,\s]+", answer) if t]
                if tokens and all(t.isdigit() and 1 <= int(t) <= len(names) for t in tokens):
                    selected = list(dict.fromkeys(names[int(t) - 1] for t in tokens))
                    break
                warn(f"answer numbers between 1 and {len(names)} separated by commas, or all")
        val["gpus"] = ",".join(selected)
        info(f"GPUs: {val['gpus'].replace(',', ', ')}")

    def install_model(self):
        model, quant = self.val["model"], self.val["quant"]
        sha256, size = GGUF[(model, quant)]
        download_verified(gguf_url(model, quant),
                          os.path.join(self.val["destdir"], "models", gguf_name(model, quant)), sha256, size)

    def install_app(self):
        destdir = self.val["destdir"]
        info("Installing the application files")
        for name in APP_FILES:
            source = os.path.join(SCRIPT_DIR, name)
            target = os.path.join(destdir, "app", name)
            if not os.path.isfile(source):
                raise Die(f"{name} not found next to the installer ({SCRIPT_DIR})")
            if os.path.abspath(source).lower() != os.path.abspath(target).lower():
                shutil.copyfile(source, target)
        write_text(os.path.join(destdir, CLIENT_BAT), client_bat(self.val["llama_port"]), newline="\r\n")
        write_text(os.path.join(destdir, PROMPT_BAT), prompt_bat(), newline="\r\n")

    def write_api_key(self):
        """Keep the API key across re-runs so that clients keep working."""
        path = os.path.join(self.val["destdir"], API_KEY_NAME)
        if self.val["expose_llama"] != "yes":
            if os.path.exists(path):
                os.remove(path)
            return
        key = ""
        if os.path.isfile(path):
            try:
                with open(path, encoding="ascii") as f:
                    key = f.read().strip()
            except (OSError, ValueError):
                key = ""
        if not re.fullmatch(r"[0-9a-f]{64}", key):
            key = secrets.token_hex(32)
        write_text(path, key + "\n")

    def save_config(self):
        val = self.val
        data = {"version": 1}
        for key in KEYS:
            data[key] = int(val[key]) if key in ("web_port", "llama_port") else val[key]
        data["llama_tag"] = LLAMA_TAG
        write_text(os.path.join(val["destdir"], CONFIG_NAME), json.dumps(data, indent=2) + "\n")

    def smoke_test(self):
        destdir = self.val["destdir"]
        info("Starting the servers for a translation test")
        handle, result_file = tempfile.mkstemp(prefix="plamo-3-translate-check-", suffix=".json")
        os.close(handle)
        try:
            command = [installed_python(destdir), os.path.join(destdir, "app", LAUNCHER_PY),
                       "--mode", "check", "--root", destdir, "--result-file", result_file]
            try:
                status = subprocess.run(command, stdin=subprocess.DEVNULL).returncode
            except OSError as e:
                raise Die(f"cannot run the launcher: {e}") from None
            if status != 0:
                raise Die("the translation test failed (see the messages above); "
                          "the files are installed, re-run the installer after fixing the problem")
            try:
                with open(result_file, encoding="utf-8") as f:
                    result = json.load(f)
            except (OSError, ValueError):
                result = {}
        finally:
            try:
                os.remove(result_file)
            except OSError:
                pass
        self.n_ctx = result.get("n_ctx") if isinstance(result.get("n_ctx"), int) else None
        info(f"Context size: {self.n_ctx or 'unknown'}")
        if self.val["backend"] != "cpu":
            lines = result.get("gpu_lines") or []
            if not lines:
                warn("llama-server does not use any GPU (no \"using device\" in its log); it runs on the CPU")
            for line in lines:
                info(f"llama-server is {line}")

    def cleanup_old_models(self):
        """Offer to delete GGUF files of other models/quantizations."""
        directory = os.path.join(self.val["destdir"], "models")
        current = gguf_name(self.val["model"], self.val["quant"])
        for name in sorted(os.listdir(directory)):
            if name == current or not re.fullmatch(r"plamo-3-translate-.*\.gguf(\.part)?", name):
                continue
            path = os.path.join(directory, name)
            if self.confirm(f"Delete the unused model file {name} ({human(os.path.getsize(path))})?"):
                os.remove(path)
                info(f"Deleted {name}")

    def warn_small_context(self):
        """Warn when the context is too small for plamo-3-translate-client.py with its defaults."""
        if self.n_ctx is None or self.n_ctx >= CLIENT_CTX:
            return
        chunk = max(100, self.n_ctx * CLIENT_CHUNK_CHARS // CLIENT_CTX // 100 * 100)
        warn(f"the context size ({self.n_ctx} tokens) is smaller than plamo-3-translate-client.py may need "
             f"for long documents with its default --chunk-chars {CLIENT_CHUNK_CHARS} (about {CLIENT_CTX} "
             f"tokens); requests may fail. Use --chunk-chars {chunk} or less with the client.")
        if self.val["ctx_size"] == "auto":
            warn("llama.cpp reduced the context to fit the memory; a smaller model or quantization, "
                 "or more memory, gives a larger context.")
        else:
            warn(f"the context size was set with --ctx-size {self.val['ctx_size']}; re-run the installer "
                 "with --ctx-size auto or a larger value for a larger context.")

    def finish(self, folder):
        val = self.val
        print(f"""
PLaMo 3 Translate is installed.
  Start menu : {APP_TITLE} > {APP_TITLE} Web UI
               (starts the servers and opens http://127.0.0.1:{val['web_port']}/ ;
                closing its window stops them)
  Client     : {APP_TITLE} > {APP_TITLE} llama-server, then in
               {APP_TITLE} > {APP_TITLE} Command Prompt:
                 plamo-3-translate-client FILE
  Shortcuts  : {folder}
  Settings   : {os.path.join(val['destdir'], CONFIG_NAME)}
  Context    : {self.n_ctx or 'unknown'} tokens""")
        if val["expose_llama"] == "yes":
            print(f"  API key    : {os.path.join(val['destdir'], API_KEY_NAME)}")
        if val["listen"] == "yes" or val["expose_llama"] == "yes":
            print("  Other hosts can connect only if you open the port(s) in the Windows firewall.")
        print()
        self.warn_small_context()

    def install(self):
        check_platform()
        self.resolve_destdir()
        self.load_saved()
        self.resolve_values()
        self.prompt_values()
        self.validate()
        self.confirm_license()
        self.show_summary()
        self.preflight()
        self.prepare_destdir()
        self.install_python()
        self.install_llama()
        self.select_gpus()
        self.install_model()
        self.install_app()
        self.write_api_key()
        self.save_config()
        set_permissions(self.val["destdir"], recursive=True)
        info("Creating the Start menu shortcuts")
        folder = create_shortcuts(self.val["destdir"])
        self.smoke_test()
        self.cleanup_old_models()
        self.finish(folder)

    # ---- uninstall

    def uninstall(self):
        check_platform()
        value = self.option("destdir")
        destdir = validate_destdir(value if value is not None else DEFAULTS["destdir"])
        self.val["destdir"] = destdir
        self.load_saved()
        folder = menu_dir()
        data = local_data_dir()
        print("This will remove PLaMo 3 Translate and delete:")
        for path in (destdir, folder, data):
            if path:
                print(f"  {path}")
        if not self.confirm(f"Uninstall {APP_TITLE}?"):
            raise Die("aborted (use --yes to uninstall non-interactively)")
        self.check_running_for_uninstall()

        info("Removing the Start menu shortcuts")
        remove_tree(folder)
        if data:
            remove_tree(data)
        if os.path.isdir(destdir):
            if os.path.exists(os.path.join(destdir, MARKER)):
                info(f"Removing {destdir}")
                inside = os.path.abspath(sys.executable).lower().startswith(os.path.abspath(destdir).lower() + os.sep)
                if inside:
                    self.delete_at_exit = destdir  # done last, by main()
                    print(f"\n{destdir} is deleted a few seconds after this program ends.")
                else:
                    try:
                        remove_tree(destdir)
                    except OSError as e:
                        raise Die(f"cannot remove {destdir}: {e}") from None
            else:
                warn(f"{destdir} does not look like a PLaMo 3 Translate installation; not removed")
        print(f"""
PLaMo 3 Translate was uninstalled.
Logs of other users (%LOCALAPPDATA%\\PLaMo3Translate in their profiles) and the copy of
Python that install-plamo-3-translate.bat keeps in %TEMP%\\plamo-3-translate-installer
were kept; remove them manually if they are no longer needed.""")

    def check_running_for_uninstall(self):
        ports = []  # of the installation; without its settings there is nothing to ask
        for key in ("web_port", "llama_port"):
            try:
                ports.append(int(self.saved[key]))
            except (KeyError, ValueError):
                pass
        destdir = self.val["destdir"]
        llama_dir = os.path.join(destdir, "llama.cpp")
        running = False
        if os.path.isdir(llama_dir):
            running = any(file_in_use(os.path.join(llama_dir, name, LLAMA_SERVER_EXE)) for name in os.listdir(llama_dir))
        if running or any(port_user(port) in ("web", "llama") for port in ports):
            raise Die(f"{APP_TITLE} is running; close its window (Web UI or llama-server) and try again")


def client_bat(llama_port):
    """The wrapper that runs plamo-3-translate-client.py with the installed Python.  It
    must not change how bytes pass through: the client reads and writes UTF-8 bytes."""
    return f"""@echo off
rem Generated by the PLaMo 3 Translate installer; changes are overwritten on re-run.
rem Runs plamo-3-translate-client.py with the Python of this installation.
setlocal
if not defined PLAMO3_LLAMA_URL set "PLAMO3_LLAMA_URL=http://127.0.0.1:{llama_port}"
if not defined PLAMO3_LLAMA_API_KEY if exist "%~dp0{API_KEY_NAME}" set /p PLAMO3_LLAMA_API_KEY=<"%~dp0{API_KEY_NAME}"
"%~dp0python\\python.exe" "%~dp0app\\{CLIENT_PY}" %*
exit /b %ERRORLEVEL%
"""


def prompt_bat():
    """Start-up file of the "Command Prompt" shortcut: UTF-8 code page (the client reads
    and writes UTF-8) and the install directory on PATH."""
    return f"""@echo off
rem Generated by the PLaMo 3 Translate installer; changes are overwritten on re-run.
chcp 65001 >nul
set "PATH=%~dp0;%PATH%"
title {APP_TITLE} Command Prompt
cd /d "%USERPROFILE%"
echo {APP_TITLE} Command Prompt (code page 65001, UTF-8)
echo.
echo   plamo-3-translate-client FILE       translate a text or Markdown file
echo   plamo-3-translate-client --help     show the options
echo.
echo Start "{APP_TITLE} llama-server" (or the Web UI) from the Start menu first.
echo.
"""


def parse_args(argv):
    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False, usage=argparse.SUPPRESS)
    for key in ("destdir", "model", "quant", "backend", "gpus", "ctx-size", "web-port", "llama-port"):
        parser.add_argument("--" + key)
    parser.add_argument("--listen", dest="listen", action="store_const", const="yes")
    parser.add_argument("--no-listen", dest="listen", action="store_const", const="no")
    parser.add_argument("--expose-llama", dest="expose_llama", action="store_const", const="yes")
    parser.add_argument("--no-expose-llama", dest="expose_llama", action="store_const", const="no")
    parser.add_argument("--accept-license", action="store_true")
    parser.add_argument("--yes", "-y", action="store_true")
    parser.add_argument("--uninstall", action="store_true")
    parser.add_argument("-h", "--help", action="store_true")
    # Internal, used by the Uninstall shortcut: ask for elevation; wait for a key at the end.
    parser.add_argument("--elevate", action="store_true")
    parser.add_argument("--pause", action="store_true")
    try:
        return parser.parse_args(argv)
    except SystemExit:
        print("\n" + USAGE, file=sys.stderr)
        raise


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    # A console with a legacy code page cannot show every character: escape instead of
    # raising UnicodeEncodeError.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="backslashreplace")
        except (AttributeError, ValueError):
            pass
    args = parse_args(argv)
    if args.help:
        print(USAGE, end="")
        return 0
    status = 0
    installer = None
    try:
        if args.elevate and WINDOWS and not is_admin():
            relaunch_elevated([a for a in argv if a != "--elevate"])
            return 0
        installer = Installer(args)
        if args.uninstall:
            installer.uninstall()
        else:
            installer.install()
    except Die as e:
        print(f"Error: {e}", file=sys.stderr, flush=True)
        status = 1
    except OSError as e:
        print(f"Error: {e}", file=sys.stderr, flush=True)
        status = 1
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr, flush=True)
        status = 130
    if args.pause:
        wait_for_key()
    if installer is not None and installer.delete_at_exit:
        schedule_delete(installer.delete_at_exit)
    return status


if __name__ == "__main__":
    sys.exit(main())
