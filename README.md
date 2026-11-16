# plamo-3-translate-installer-for-windows

PLaMo 3 Translate installer script for Windows

It installs [PLaMo 3 Translate](https://huggingface.co/pfnet) (GGUF models from Preferred Networks) on llama.cpp for the users of one PC: a translation page for the web browser and a command-line client for files. Nothing runs in the background: the servers are started from the Start menu when they are needed and stop when their window is closed.

This is the Windows counterpart of plamo-3-translate-installer-for-resolute (Ubuntu), which runs the same model as an always-on network service.

## Status

- Developed on Linux. The logic of the installer and the launcher is covered by tests that run there (`python3 -m unittest discover tests`); the Windows-only parts (the `.bat` file, access rights, shortcuts, elevation, the job object) cannot be run there.
- Tested on Windows 11 with the `cpu` backend (0.8B, Q4_0), by hand and with `tests\windows-check.bat` (see [Development](#development); all 29 checks passed on 2026-11-16): interactive and non-interactive installation, re-running, `--expose-llama`, uninstalling, the shortcuts, the error screens when one server is started while the other runs, the access rights of the install directory, and the client. The PC had the Visual C++ runtime installed, so it is not known whether llama-server starts without it.
- **The `vulkan`, `cuda12` and `cuda13` backends and `--gpus` are implemented but untested**: no Windows machine with a GPU was available.

## Prerequisites

- Microsoft Windows 11, x64
- Administrator privileges (for the installation; not for use)
- Internet access to python.org (Python), GitHub (llama.cpp) and Hugging Face (models)
- Enough disk space and memory for the selected model; the installer checks them and warns if they look insufficient
- **GPU driver installed in advance** (only for GPU backends). The installer does not install GPU drivers
  - NVIDIA (`cuda12`, `cuda13`): `nvidia-smi` must work; CUDA 13.5 needs driver 580 or later. The installer stops if `nvidia-smi` does not work.
- If llama-server cannot start because a DLL is missing, the installer stops and asks for the [Microsoft Visual C++ Redistributable (x64)](https://aka.ms/vs/17/release/vc_redist.x64.exe); install it and run the installer again.

Python does not have to be installed: the installer fetches the embeddable package from python.org (Python 3.14.10, verified by SHA-256) into the install directory and uses nothing else. A Python that is already on the PC is neither used nor changed.

## Usage

Just run `install-plamo-3-translate.bat` as Administrator: double-click it (it asks for elevation itself) and answer the questions.

```
install-plamo-3-translate.bat                               interactive install
install-plamo-3-translate.bat --accept-license --model 2b   non-interactive install (options as needed)
install-plamo-3-translate.bat --uninstall                   uninstall
```

- Keep the files of this repository together: `plamo-3-translate-installer.py`, `plamo-3-translate-launcher.py`, `plamo-3-translate-server.py` and `plamo-3-translate-client.py` must be next to the `.bat` file.
- With options, run it from a command prompt opened with "Run as administrator" (the options would not survive the elevation prompt).
- Every question has a corresponding command-line option (and environment variable). Only values that are not given are asked. When standard input is not a terminal, the defaults are used without asking.
- Values are taken in this order: command-line option, environment variable, the previous installation (`install.json` in the install directory), default. The previous installation is looked for in the install directory in use, so give `--destdir` again when it is not the default.
- Re-running the installer updates the installation in place (the files and shortcuts are overwritten; a GGUF file and a llama.cpp build that are already there are reused after checksum verification). If the model or quantization changed, the installer offers to delete the unused GGUF file. An API key generated for llama-server is kept. Close the PLaMo 3 Translate windows first: the installer stops if the servers are running.
- At the end the installer starts the servers once, translates a test sentence through the web server and stops them again.
- If Windows shows "Windows protected your PC" (SmartScreen) for a `.bat` file downloaded from the Internet, choose "More info" and "Run anyway", or open the file's Properties and tick "Unblock" first. When the repository was downloaded as a ZIP file, unblock the ZIP file before extracting it.

### After the installation

The Start menu folder "PLaMo 3 Translate" (for all users) contains:

| Shortcut | What it does |
|---|---|
| PLaMo 3 Translate Web UI | Starts llama-server and the web server in a console window, waits until the model is loaded and opens `http://127.0.0.1:18080/` in the web browser. Closing the window (or Ctrl-C) stops both. Started a second time, it only opens the browser. |
| PLaMo 3 Translate llama-server | Starts llama-server only, for `plamo-3-translate-client.py` and other clients of its API. Closing the window stops it. |
| PLaMo 3 Translate Command Prompt | A command prompt with `plamo-3-translate-client` on `PATH` and the UTF-8 code page (65001). |
| Uninstall PLaMo 3 Translate | Runs the uninstaller (asks for elevation). |

- The Web UI starts its own llama-server, so the client can be used while the Web UI is running. If the Web UI is started while "PLaMo 3 Translate llama-server" is running (or the other way round), an error is shown until a key is pressed; close the other window first.
- The log of llama-server is written to `%LOCALAPPDATA%\PLaMo3Translate\logs\llama-server.log` (the previous one is kept as `llama-server.log.1`). The requests to the web server are shown in the console window.
- The installed files are read-only for ordinary users.

## Options

| Option | Environment variable | Default |
|---|---|---|
| `--destdir DIR` | `PLAMO3_DESTDIR` | `C:\LLM\PLaMo3Translate` |
| `--model 0.8b\|2b\|8b\|31b` | `PLAMO3_MODEL` | `2b` |
| `--quant Q2_K\|Q3_K_M\|Q4_K_M\|Q4_0` | `PLAMO3_QUANT` | `Q4_K_M` |
| `--backend cpu\|vulkan\|cuda12\|cuda13` | `PLAMO3_BACKEND` | suggested from the detected GPU |
| `--gpus NAME[,NAME...]\|all` | `PLAMO3_GPUS` | one GPU, the one with the most free memory (asked when the backend sees two or more GPUs) |
| `--ctx-size N\|auto` | `PLAMO3_CTX_SIZE` | `auto` (model maximum 262144, reduced to fit the memory) |
| `--web-port PORT` | `PLAMO3_WEB_PORT` | `18080` |
| `--llama-port PORT` | `PLAMO3_LLAMA_PORT` | `28080` |
| `--listen` / `--no-listen` | `PLAMO3_LISTEN` (`yes`/`no`) | `no` |
| `--expose-llama` / `--no-expose-llama` | `PLAMO3_EXPOSE_LLAMA` (`yes`/`no`) | `no` |
| `--accept-license` | `PLAMO3_ACCEPT_LICENSE` (`yes`) | — |
| `--yes` | — | answer yes to warnings, deleting unused model files and uninstalling |
| `--uninstall` | — | — |
| `--help` | — | — |

Install directory: an absolute path with a drive letter; folder names may contain only ASCII letters, digits, spaces and `. _ -`. An existing directory must be empty or a previous installation. The installer cuts the inheritance of access rights on it and gives Administrators and SYSTEM full control and Users read and execute only (a folder made directly under `C:\` would otherwise be writable by every user); parent folders it has to create (e.g. `C:\LLM`) get the same rights.

Backend suggestion: an NVIDIA GPU with a working driver (`nvidia-smi`) gives `cuda13` for driver 580 or later and `cuda12` otherwise; an AMD or Intel GPU gives `vulkan`; otherwise `cpu`. A failure to detect GPUs gives `cpu`, and with `--backend cpu` nothing about GPUs is asked from the system at all.

GPU selection (untested): `--gpus` takes the device names that `llama-server --list-devices` prints for the installed backend (e.g. `Vulkan0`, or `CUDA0,CUDA1`), which are passed to llama-server with `--device`. When the backend sees two or more GPUs, the installer lists them and asks which to use (numbers such as `1` or `1,2`, or `all`); this happens after llama.cpp is installed, because the list comes from llama.cpp itself.

Listening: by default both servers listen on `127.0.0.1` only. `--listen` lets the web server listen on `0.0.0.0` (all interfaces); it has no login, so anyone who can reach the port can translate. `--expose-llama` lets llama-server listen on `0.0.0.0` and protects it with an API key (see [below](#plamo-3-translate-clientpy)). **The installer never changes the Windows firewall**: other hosts can connect only if you allow the port yourself (Windows may ask about that when a server first listens on `0.0.0.0`).

Uninstalling: `--uninstall` (or the Start menu shortcut) removes the shortcuts, the install directory and the logs of the user who runs it. Logs of other users (`%LOCALAPPDATA%\PLaMo3Translate` in their profiles) and the copy of Python that the `.bat` file keeps in `%TEMP%\plamo-3-translate-installer` are kept.

## Models

- PLaMo 3 Translate 0.8B
  - https://huggingface.co/pfnet/plamo-3-translate-0.8b-GGUF
- PLaMo 3 Translate 2B (default)
  - https://huggingface.co/pfnet/plamo-3-translate-2b-GGUF
- PLaMo 3 Translate 8B
  - https://huggingface.co/pfnet/plamo-3-translate-8b-GGUF
- PLaMo 3 Translate 31B
  - https://huggingface.co/pfnet/plamo-3-translate-31b-GGUF

## License

### License of this repository

The files in this repository (the installer, plamo-3-translate-launcher.py, plamo-3-translate-server.py, plamo-3-translate-client.py and the documentation) are distributed under the MIT License. See [LICENSE](LICENSE).

### License of PLaMo 3 Translate models

The PLaMo 3 Translate models downloaded by the installer are not part of this repository. They are distributed by Preferred Networks, Inc. (PFN) under the PLaMo Community License, not under the MIT License.

- https://plamo.preferredai.jp/info/plamo-community-license-en

Personal and non-commercial use is free. Commercial use requires registration with PFN. Read the full license text before using the models. The installer shows a summary of the license and aborts unless the user types `yes`. In non-interactive mode, `--accept-license` is required.

## Engine

- llama.cpp b12366
  - https://github.com/ggml-org/llama.cpp/releases/tag/b12366

| Backend | llama.cpp build |
|---|---|
| `cpu` | `llama-b12366-bin-win-cpu-x64.zip` |
| `vulkan` | `llama-b12366-bin-win-vulkan-x64.zip` |
| `cuda12` | `llama-b12366-bin-win-cuda-12.4-x64.zip` and `cudart-llama-bin-win-cuda-12.4-x64.zip` |
| `cuda13` | `llama-b12366-bin-win-cuda-13.5-x64.zip` and `cudart-llama-bin-win-cuda-13.5-x64.zip` |

## Quantization

- Q2_K
- Q3_K_M
- Q4_K_M (default)
- Q4_0 (0.8B and 2B models only)

## Architecture

```
Start menu shortcut
  └─ python.exe plamo-3-translate-launcher.py --mode webui      (console window)
       ├─ llama-server.exe            127.0.0.1:28080   model, OpenAI-compatible API
       └─ python.exe plamo-3-translate-server.py
                                      127.0.0.1:18080   translation page and JSON API
                                                        (talks to llama-server over TCP)
web browser ──────────────────────────> 18080
plamo-3-translate-client.py ──────────> 28080
```

- `install-plamo-3-translate.bat` only checks for administrator privileges, fetches the embeddable Python package (pinned by SHA-256) into `%TEMP%\plamo-3-translate-installer` and runs `plamo-3-translate-installer.py` with it. There is no PowerShell script file, so the PowerShell execution policy does not matter; PowerShell is used only for short inline commands (elevation, the `.lnk` files, the names of the display adapters).
- `plamo-3-translate-installer.py` does the work: questions, checks, downloads (resumed after interruptions, verified by SHA-256), files, access rights, shortcuts, the translation test, and uninstalling.
- `plamo-3-translate-launcher.py` is what the shortcuts run. It starts llama-server (`--jinja --no-prefill-assistant --alias plamo-3-translate`) and, for the Web UI, `plamo-3-translate-server.py` in its stand-alone mode (no nginx or uWSGI), waits for llama-server's `/health`, and opens the browser. The children are stopped with the window; a job object stops them even if the launcher is killed.
- `plamo-3-translate-server.py` and `plamo-3-translate-client.py` are byte-identical copies from plamo-3-translate-installer-for-resolute (commit `3902612`); changes to them are made there.
- Only the Python standard library is used.

### Files

```
<destdir>\                                  default C:\LLM\PLaMo3Translate
  python\                                   embeddable Python 3.14.10
  llama.cpp\b12366-<backend>\               llama.cpp prebuilt binaries
  models\plamo-3-translate-<size>-<quant>.gguf
  app\plamo-3-translate-server.py
  app\plamo-3-translate-client.py
  app\plamo-3-translate-launcher.py
  app\plamo-3-translate-installer.py        used by the Uninstall shortcut
  plamo-3-translate-client.bat              runs the client with the installed Python
  command-prompt.bat                        start-up file of the Command Prompt shortcut
  install.json                              settings of the installation
  llama-api-key.txt                         API key (only when llama-server is exposed)
%LOCALAPPDATA%\PLaMo3Translate\logs\        llama-server log (per user)
%ProgramData%\Microsoft\Windows\Start Menu\Programs\PLaMo 3 Translate\
                                            the shortcuts (all users)
```

Downloads are verified with SHA-256 checksums built into the installer (the Python package, the llama.cpp release assets and pinned Hugging Face revisions of the GGUF files).

## Client

- Web browser (web server, TCP 18080)
- plamo-3-translate-client.py (connects to llama-server, TCP 28080)

### plamo-3-translate-client.py

A command-line client that translates a plain text or Markdown file (or standard input) through llama-server, keeping the layout of the document. It uses the Python standard library only. The installer puts it into the install directory together with `plamo-3-translate-client.bat`, which runs it with the installed Python; in "PLaMo 3 Translate Command Prompt" that wrapper is on `PATH`. Start "PLaMo 3 Translate llama-server" (or the Web UI) first.

```
plamo-3-translate-client doc.md                       -> doc.en.md (or doc.ja.md)
plamo-3-translate-client --to en -o out.txt in.txt
echo こんにちは | plamo-3-translate-client -           standard input -> standard output
```

- Input and output are UTF-8, also on Windows: files must be saved as UTF-8, and piping text through standard input or output works as expected only in a console with code page 65001, which "PLaMo 3 Translate Command Prompt" sets (`chcp 65001`). In another console, give files instead.
- The wrapper sets `PLAMO3_LLAMA_URL` to the llama-server port of the installation and, when llama-server is exposed, `PLAMO3_LLAMA_API_KEY` from `llama-api-key.txt`, unless these variables are already set.
- After Ctrl-C, `cmd.exe` asks "Terminate batch job (Y/N)?" because the client is run through a batch file; either answer is fine.
- From another host, run `plamo-3-translate-client.py` itself with Python 3 and `--url http://<this PC>:28080` (this needs `--expose-llama`, the API key and the port opened in the firewall).

| Option | Default |
|---|---|
| `INPUT` | file to translate, or `-` for standard input |
| `-o`, `--output PATH` | output file, or `-` for standard output. Default: for a file, an automatically named file (see below); for standard input, standard output |
| `-f`, `--force` | overwrite an existing output file (without it, an existing output file is an error) |
| `--from LANG` | detected (Japanese preferred, as in the web form) |
| `--to LANG\|auto` | `auto`: Japanese to English, anything else to Japanese (decided once for the whole document) |
| `--form plain\|polite` | style of Japanese translations (target `ja` only; ignored when `auto` gives another language) |
| `--format auto\|text\|markdown` | `auto`: `markdown` for `.md` and `.markdown` files, `text` otherwise (also for standard input) |
| `--glossary FILE` | one `source => target` per line (blank lines and lines starting with `#` are ignored; up to 500 entries) |
| `--context TEXT` / `--context-file FILE` | background information about the document passed to the model |
| `--chunk-chars N` | `20000`: maximum source characters per request |
| `--url URL` | `$PLAMO3_LLAMA_URL`, or `http://127.0.0.1:28080` (`http://` only) |
| `--api-key-file FILE` | the API key is taken from `$PLAMO3_LLAMA_API_KEY` |
| `-q`, `--quiet` | no progress output (warnings and errors are still shown) |
| `--list-languages` | print the language codes and exit |

- Output file names: `dir/stem.ext` becomes `dir/stem.<target>.ext` (`README` → `README.en`, `doc.md` → `doc.zh-hans.md`). A language code at the end of the stem is replaced when it matches the source language (`--from`, or `ja`/`en` as detected): `doc.ja.md` → `doc.en.md`, but an English `report.id.txt` → `report.id.ja.txt`. The output is written to a temporary file in the same directory and renamed when the translation is complete; on an error or Ctrl-C nothing is left behind. The input file itself is never overwritten.
- Input must be UTF-8. A BOM and CRLF line ends are kept in the output.
- Markdown: front matter (`---` or `+++` at the beginning), fenced code blocks (```` ``` ```` / `~~~`), HTML blocks (from a line starting with a tag or comment to the next blank line), link reference definitions (`[label]: url`) and `$$` math blocks are copied unchanged; everything else (headings, lists, tables, quotes, ...) is translated with its markup. Indented code blocks are not recognized (they cannot be told apart from list continuation lines with a line-based parser) and are translated.
- Long documents are split into chunks of at most `--chunk-chars` characters: paragraphs (separated by blank lines) are packed together; a paragraph longer than that is split between lines, keeping a Markdown table (consecutive lines starting with `|`) in one piece. Lines are never split: a line or table longer than `--chunk-chars` is sent alone with a warning. Blank lines and the whitespace around each chunk are kept as in the source. Each request carries the preceding source/translation pairs (source and translation, up to 2.5 × `--chunk-chars` characters in all) as translation history, as described in the model card, so that terms and style stay consistent. With the defaults, a request of a long Japanese or English document can need about 40000 tokens of context (the tokenizer uses about 0.4 tokens per Japanese character and 0.16 per English character; Chinese, Korean and some other scripts use 0.8 or more, so they need more). Lower `--chunk-chars` if llama-server reports that the request does not fit in its context; the installer warns when the context is smaller than that.
- Progress goes to standard error: the chunk number and the prompt processing percentage, then, when writing to a file, the translation as it is generated; when writing to standard output, only the number of characters generated, and each chunk is written to standard output as soon as it is translated.
- If llama-server stops a translation at the token limit (`finish_reason` `"length"`: the context is full, or the output reached the limit of 4 tokens per source character, e.g. because the model kept repeating itself), the client stops with an error instead of writing a truncated translation, and advises a smaller `--chunk-chars` (which also shortens the history) or breaking up a very long line or table.
- Exit status: 0 success, 1 translation error (connection, API key, llama-server error), 2 invalid arguments or input, 130 interrupted.
- API key: when llama-server is exposed (`--expose-llama`), every request needs the key the installer generated, which is stored in `llama-api-key.txt` in the install directory. On the PC itself the wrapper passes it automatically. The file is readable by every user of the PC (they need it to start llama-server), so it protects llama-server from other hosts, not from other users of the PC. On another host, copy the key into a file of your own and pass it with `--api-key-file` (or set `PLAMO3_LLAMA_API_KEY`):
  ```
  python3 plamo-3-translate-client.py --url http://<this PC>:28080 --api-key-file plamo3-key.txt doc.md
  ```
  Without a key (or with a wrong one) the client stops with an explanation of how to give it.

## Development

- `python3 -m unittest discover tests` runs the tests on Linux (Python 3.12 or later, standard library only). `tests/stub_llama_server.py` stands in for llama-server.
- `tests\windows-check.bat` checks an installation on Windows: install first, close the PLaMo 3 Translate windows and double-click it. It starts and stops the servers several times (also through the Start menu shortcuts: console windows appear and a browser tab opens), runs the client, re-runs the installer and, if you say so, tests `--expose-llama` and uninstalling followed by a fresh installation; the result is written to `%USERPROFILE%\plamo-3-translate-check.log`.
- File encodings: `.bat` files are ASCII with CRLF line ends; `.py` files are UTF-8 without BOM.
- [plamo-3-translate-server.md](plamo-3-translate-server.md) and [plamo-3-translate-client.md](plamo-3-translate-client.md) are the handover notes for the two files that come from plamo-3-translate-installer-for-resolute, with the checks that are still to be done on Windows.
