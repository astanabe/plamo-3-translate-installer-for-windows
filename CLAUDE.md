# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## State of the repository

The installer is implemented. The development host is Linux (no PowerShell, no `cmd.exe`), but the product runs only on Windows 11: say so instead of claiming that a change was tested on Windows. The user tests on Windows by downloading the repository from GitHub as a ZIP file, so a change has to be pushed before it can be tried there.

- `python3 -m unittest discover tests` runs all tests (about 40 seconds; Python 3.12 or later, standard library only). One class or test: `python3 -m unittest discover tests -k EndToEnd`.
- `python3 -m py_compile plamo-3-translate-*.py` and `file install-plamo-3-translate.bat` (must say ASCII text, with CRLF line terminators) after editing.
- What the tests cover: option/environment/saved-value precedence, validation, the license gate, downloads (resume, checksum, reuse) against a local HTTP server, the launcher against `tests/stub_llama_server.py` (start, `/health`, crash, every conflict case, check mode), and a whole install / update / uninstall into a temporary directory.
- First run on Windows 11 (2026-11-16, commit `7205ab2`, `cpu`, 0.8B Q4_0, by hand): the `.bat`, interactive installation, `icacls` result (Users `RX` on the install directory and on the created `C:\LLM`), both launcher modes, both conflict error screens, children gone after closing the window and after killing the launcher, the client wrapper with `| more` quit by `q` (no traceback) all worked. The PC had the Visual C++ v14 runtime, so the missing-DLL path is still unseen. Not yet seen on Windows: installer re-run with options, `--expose-llama`, uninstalling (`schedule_delete`), the open items of the two handover notes.
- `tests/windows-check.bat` (runs `tests/windows_check.py` elevated, with the Python the installer `.bat` keeps in `%TEMP%`) automates most of what is left and writes `%USERPROFILE%\plamo-3-translate-check.log`; it is itself unrun so far, so a failure it reports may be its own bug. Ask the user for that log rather than for manual checks.
- What the Linux tests cannot cover: `install-plamo-3-translate.bat`, and the functions in the "Windows specifics" section of `plamo-3-translate-installer.py` (`icacls`, shortcuts through PowerShell, elevation, `schedule_delete`, memory size, GPU names) and the `ctypes` code of the launcher (job object, console title, error mode). The tests replace those functions; when changing them, check the Windows API or command documentation, because nothing here will catch a mistake.

`README.md` is the user documentation and must be kept in step with the implementation (options table, shortcuts, file layout, the "Status" section about what is untested).

## What it is

An installer for Windows 11 that sets up PLaMo 3 Translate (GGUF models from Preferred Networks) on llama.cpp:

- Purpose: translation for the user of that PC, not a network service. The servers are started on demand, not as always-on services. Use from other hosts on the intranet is not the aim, but must not be made impossible (the user may open the ports in the firewall themselves).
- Entry point: `install-plamo-3-translate.bat`, run as Administrator. Default install path `C:\LLM\PLaMo3Translate`.
- Engine: llama.cpp release `b12366` (pinned). Models: `pfnet/plamo-3-translate-{0.8b,2b,8b,31b}-GGUF` on Hugging Face; default 2B. Quantization: `Q2_K`, `Q3_K_M`, `Q4_K_M` (default), `Q4_0` (0.8B and 2B only).
- Two listeners: a web server for browsers on TCP 18080 (`plamo-3-translate-server.py`) and llama-server on TCP 28080, which `plamo-3-translate-client.py` talks to.
- The installer does not install GPU drivers; they are a prerequisite for GPU backends.
- License gate: the models are under the PLaMo Community License, not MIT. The installer must show a summary of that license and abort unless the user types `yes`; in non-interactive mode `--accept-license` is required (`--yes` does not accept it). Do not weaken this.

This is the Windows counterpart of `plamo-3-translate-installer-for-resolute` (the Ubuntu installer, `install-plamo-3-translate.sh`), a separate repository. The installer follows its steps, option names, environment variables, messages and tables (model revisions, GGUF checksums, memory estimates) on purpose: when something is changed there, look whether it applies here too.

## How the pieces fit

- `install-plamo-3-translate.bat` (thin): elevation (`net session`, then `Start-Process -Verb RunAs` with an `--elevated` marker so that it cannot loop; only without arguments, since arguments do not survive it), fetches the embeddable Python zip into `%TEMP%\plamo-3-translate-installer` with `curl.exe`, checks it with `certutil`, unpacks it with `tar.exe`, runs the installer with `%*`. It pauses at the end only when started without arguments.
- `plamo-3-translate-installer.py`: class `Installer` (`install()` and `uninstall()` list the steps in order). Everything Windows-only is a module-level function in the "Windows specifics" section so that the tests can replace it. `--elevate` and `--pause` are internal options used by the Uninstall shortcut.
- `plamo-3-translate-launcher.py`: `--mode webui|llama|check`, reads `install.json` from the install directory. `check` is what the installer runs for its translation test (result in `--result-file`). Conflicts are told by probing the ports: the web server by its page title, llama-server by `/health` and the model alias in `/v1/models`.
- `plamo-3-translate-server.py`, `plamo-3-translate-client.py`: copies from the resolute repository (see below).
- `tests/`: `support.py` (helpers, a file server with Range support, a fake install tree), `stub_llama_server.py`, `test_launcher.py`, `test_installer.py`. On Linux a shell script named `llama-server.exe` that runs the stub takes the place of the real binary, so the product code needs no test hooks.

The installer and the launcher are each a single file and share no module (the embeddable Python does not put the script directory on `sys.path`); small helpers and constants such as the file names, the model alias and the page title are therefore duplicated and must be changed in both.

## Design decisions (agreed; don't change without asking)

- **Language**: all messages and code comments are in English.
- **No PowerShell script files**: a short `.bat` plus Python. PowerShell is called only for short inline commands (`-EncodedCommand` from Python; `Start-Process -Verb RunAs` in the `.bat`), so the execution policy never matters, and most of the logic can be tested on Linux.
- **Backends**: `cpu`, `vulkan`, `cuda12` (CUDA 12.4 on Windows, not 12.8 as on Ubuntu), `cuda13` (CUDA 13.5). x64 only. No ROCm, SYCL, OpenVINO or arm64.
- **Web server**: no nginx or uWSGI. `plamo-3-translate-server.py` runs in its own threaded stand-alone mode and reaches llama-server over TCP (`PLAMO3_LLAMA_URL=http://127.0.0.1:28080`; there is no `AF_UNIX` in Python on Windows).
- **Copies from the resolute repository**: `plamo-3-translate-server.py` and `plamo-3-translate-client.py` are byte-identical copies, **taken from commit `3902612`** (git blobs `3a7dfa6f` and `40b0298e`; a test compares them). Changes to them are made by the resolute developer, not here; to update, copy with `git show <commit>:<file>` and record the new commit and blob ids here, in `README.md` and in `tests/test_installer.py`. `plamo-3-translate-server.md` and `plamo-3-translate-client.md` are that developer's handovers: the launcher contract (start-up line, exit statuses, the bind-failure message), known limitations, and lists of checks that could only be run on Windows and are still open. The client's input and output are UTF-8 bytes on purpose; do not wrap it in anything that changes the code page handling without reading that note (the wrapper `.bat` only sets two environment variables and passes `%*`).
- **Python**: the embeddable package from python.org (3.14.10), unpacked into the install directory and pinned with SHA-256 in both the `.bat` and the installer (a test compares them). No winget, no system Python. Standard library only. The embeddable package runs isolated: `PYTHON*` environment variables are ignored, so do not rely on `PYTHONUTF8` or `PYTHONIOENCODING`.
- **Shortcuts** (Start menu, all users): (1) Web UI: starts llama-server and the web server in a visible console window, waits for `/health`, opens the browser; closing the window stops both; started a second time, it only opens the browser (and waits for the model first if it is still loading). (2) llama-server only, for users of `plamo-3-translate-client.py`. If (1) is started while (2) is running, (1) shows an error and waits for a key before its window closes. (3) Command Prompt with the client on `PATH` and `chcp 65001`. (4) Uninstall.
- **Listening**: both ports on `127.0.0.1` by default; `--listen` (web server) and `--expose-llama` (llama-server, with an API key) switch to `0.0.0.0`. The installer never touches the firewall.
- **Permissions**: binaries and models are read-only for ordinary users (`icacls`: inheritance cut, Administrators and SYSTEM full control, Users read and execute); logs and other run-time files go under `%LOCALAPPDATA%\PLaMo3Translate`. Do not grant Users modify rights on the install directory as the reference installer does. The API key file is readable by Users on purpose: they start llama-server.
- **Downloads**: SHA-256 checksums built into the installer (llama.cpp assets, Python, pinned Hugging Face revisions of the GGUF files). Python's `urllib` with resume in a retry loop of the installer's own; `curl.exe -C -` only as a fallback when Python cannot verify a certificate (the Windows certificate store may lack a root until Windows itself has needed it).
- **Options**: GNU style as in the Ubuntu installer (`--destdir`, `--model`, `--accept-license`, ...), not PowerShell style. Every prompt has an option and a `PLAMO3_*` environment variable. Precedence: option, environment, `install.json` of the previous installation, default.
- **Re-running is safe**: verified downloads are reused, generated files are overwritten, the API key is kept.
- **Visual C++ runtime**: not installed by the installer. If `llama-server --version` fails with `0xC0000135`, the installer stops and names the download; whether to install it automatically is to be decided after the first test on Windows.

## Testing on Windows

Tests on real Windows are done by the user, for now with the `cpu` backend only (0.8B or 2B is the practical choice there). `vulkan`, `cuda12`, `cuda13` and GPU selection (`--gpus`, by the device names of `llama-server --list-devices`, passed with `--device`) are implemented but untested until a machine with a GPU is available: `README.md` says so, and the `cpu` path must stay independent of GPU detection (a failure to detect or list GPUs must not stop a `cpu` installation; with `--backend cpu` nothing GPU-related is run, and a test checks that).

## filesfromotherrepo/

Reference material copied from another project (a Qwen3 Swallow + llama-cli installer for Windows, PowerShell based). It is untracked and is not part of the product. It was the pattern for the elevation in the `.bat` file, the Start menu shortcuts and the download conventions; leave it alone.

## File encodings

- `.bat` (also the ones the installer generates): ASCII only, CRLF line ends; `cmd.exe` mishandles LF-only batch files. They are stored in git with CRLF as they are (`*.bat -text` in `.gitattributes`), so that a ZIP download gives CRLF too. Write them with an explicit `newline="\r\n"`, and check with `file <path>` after editing: an editor or tool can silently change the line ends.
- `.py`: UTF-8 without BOM, LF, standard library only.
- Files the installer writes at run time (`install.json`, the API key): UTF-8 without BOM.
