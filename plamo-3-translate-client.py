#!/usr/bin/env python3
"""PLaMo 3 Translate command-line client.

Translates a plain text or Markdown file (or standard input) by talking to llama-server
directly (the TCP port set up by install-plamo-3-translate.sh on Ubuntu and by
plamo-3-translate-installer-for-windows on Windows, 127.0.0.1:28080 by default), keeping the
layout of the document.  Python standard library only; runs on Linux and Windows.  Files,
standard input and a translation on standard output are UTF-8 on both.

Long documents are split into chunks (paragraphs, then lines) of at most --chunk-chars
characters; each request carries the preceding source/translation pairs as history so that
the translation stays consistent.  In Markdown, front matter, fenced code blocks, HTML
blocks, link reference definitions and $$ math blocks are copied unchanged.

With --intermediate LANG the document is translated twice, source -> LANG -> --to (a round
trip that flattens the personal style of the text): first every chunk into LANG, then every
intermediate chunk into the target language.  The chunks are those of the source in both
passes, and each pass has its own translation history.

Environment variables:
    PLAMO3_LLAMA_URL        llama-server address, http://<host>:<port> (default
                            http://127.0.0.1:28080)
    PLAMO3_LLAMA_API_KEY    llama-server API key (needed when llama-server is exposed);
                            --api-key-file overrides it

Exit status: 0 success, 1 translation error (connection, API key, llama-server error, an
output file that cannot be put in place, the reader of standard output gone), 2 invalid
arguments or input, 130 interrupted.
"""

import argparse
import errno
import http.client
import json
import os
import queue
import re
import socket
import sys
import tempfile
import threading
import time
import urllib.parse

WINDOWS = sys.platform == "win32"
DEFAULT_URL = "http://127.0.0.1:28080"
TIMEOUT = 3600
# The main thread waits for llama-server in steps of this many seconds: on Windows Ctrl-C
# interrupts neither a blocking socket call nor a wait on a lock, only the time between two.
POLL_SECONDS = 0.5
# install-plamo-3-translate.sh warns when the llama-server context is smaller than these
# defaults may need (PLAMO3INS_CLIENT_CHUNK_CHARS, PLAMO3INS_CLIENT_CTX); keep them in sync.
DEFAULT_CHUNK_CHARS = 20000
MIN_CHUNK_CHARS = 100
# Translation history per request: source + translation characters, x --chunk-chars.
HISTORY_FACTOR = 2.5
MAX_TOKENS = 262144
MAX_GLOSSARY_ENTRIES = 500
MAX_GLOSSARY_CHARS = 1000
MODEL_ALIAS = "plamo-3-translate"
STOP = ["<|plamo:tag|>", "<|plamo:bos|>"]
RESERVED = "<|plamo:"
MARKDOWN_EXTENSIONS = (".md", ".markdown")

# Supported languages (model card, "Supported Languages").  Same list as
# plamo-3-translate-server.py.
LANGUAGES = [
    ("ja", "Japanese"), ("en", "English"),
    ("zh-hans", "Chinese (Simplified)"), ("zh-hant", "Chinese (Traditional)"),
    ("ko", "Korean"), ("ar", "Arabic"), ("az", "Azerbaijani"),
    ("be", "Belarusian"), ("bg", "Bulgarian"), ("bn", "Bengali"),
    ("ca", "Catalan"), ("ceb", "Cebuano"), ("de", "German"),
    ("es", "Spanish"), ("et", "Estonian"), ("eu", "Basque"),
    ("fa", "Persian"), ("fr", "French"), ("gu", "Gujarati"),
    ("he", "Hebrew"), ("hi", "Hindi"), ("hr", "Croatian"),
    ("hu", "Hungarian"), ("hy", "Armenian"), ("id", "Indonesian"),
    ("it", "Italian"), ("jv", "Javanese"), ("ka", "Georgian"),
    ("kk", "Kazakh"), ("km", "Khmer"), ("lt", "Lithuanian"),
    ("lv", "Latvian"), ("ml", "Malayalam"), ("mr", "Marathi"),
    ("ms", "Malay"), ("my", "Burmese"), ("ne", "Nepali"),
    ("nl", "Dutch"), ("pa", "Punjabi"), ("pl", "Polish"),
    ("ru", "Russian"), ("si", "Sinhala"), ("sk", "Slovak"),
    ("sl", "Slovenian"), ("sr", "Serbian"), ("sw", "Swahili"),
    ("ta", "Tamil"), ("te", "Telugu"), ("th", "Thai"),
    ("tl", "Tagalog"), ("ur", "Urdu"), ("uz", "Uzbek"),
    ("vi", "Vietnamese"),
]
LANGUAGE_CODES = {code for code, _ in LANGUAGES}
FORMS = ("plain", "polite")
# Hiragana, katakana (incl. phonetic extensions) and half-width katakana.
KANA = re.compile("[ぁ-ゟ゠-ヿㇰ-ㇿｦ-ﾟ]")
# Kanji (CJK unified ideographs, extension A, compatibility ideographs, extensions B and
# later) and the marks 々 〆 〇.
KANJI = re.compile("[々-〇㐀-䶿一-鿿豈-﫿\U00020000-\U0003134f]")
# Hangul syllables and jamo: Korean text may contain hanja.
HANGUL = re.compile("[ᄀ-ᇿ㄰-㆏가-힯]")


def looks_japanese(text):
    """Japanese preferred: kana, or kanji without hangul (Chinese counts as Japanese)."""
    if KANA.search(text):
        return True
    return bool(KANJI.search(text)) and not HANGUL.search(text)


def auto_target(text, source):
    """Target language for --to auto: English for Japanese, Japanese otherwise."""
    is_japanese = source == "ja" if source else looks_japanese(text)
    return "en" if is_japanese else "ja"


class UsageError(Exception):
    """Invalid arguments or input (exit status 2)."""


class TranslateError(Exception):
    """Failure while translating (exit status 1)."""


# ---------------------------------------------------------------------------
# Input


def lines_of(text):
    """Split into lines keeping "\\n" (only "\\n" ends a line, unlike str.splitlines)."""
    return re.findall(r"[^\n]*\n|[^\n]+$", text)


def decode_utf8(data, name, stdin=False):
    """Decode UTF-8 (a BOM is dropped); name is what the error message calls the data."""
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        message = f"{name} is not valid UTF-8 (byte {e.start})"
        if not stdin:
            message += "; save the file as UTF-8"
        elif WINDOWS:
            # e.g. "echo ... | client -" in cmd.exe: the pipe carries the console code page.
            message += ("\nStandard input must be UTF-8.  Save the text as a UTF-8 file and "
                        "give the file instead.")
        raise UsageError(message)


def read_text_file(path, what):
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError as e:
        raise UsageError(f"cannot read {what} {path}: {e.strerror}")
    return decode_utf8(data, f"{what} {path}")


def check_reserved(text, what):
    if RESERVED in text:
        raise UsageError(f"{what} must not contain the reserved token {RESERVED}...")


def parse_glossary(path):
    """One "source => target" per line; blank lines and lines starting with # are ignored."""
    entries = []
    for number, line in enumerate(read_text_file(path, "glossary file").split("\n"), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("=>")
        if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
            raise UsageError(f"{path}:{number}: glossary lines must be: source => target")
        source, target = parts[0].strip(), parts[1].strip()
        if len(source) > MAX_GLOSSARY_CHARS or len(target) > MAX_GLOSSARY_CHARS:
            raise UsageError(f"{path}:{number}: glossary terms are limited to "
                             f"{MAX_GLOSSARY_CHARS} characters")
        check_reserved(line, f"{path}:{number}: the glossary")
        entries.append({"source": source, "target": target})
    if len(entries) > MAX_GLOSSARY_ENTRIES:
        raise UsageError(f"{path}: too many glossary entries (max {MAX_GLOSSARY_ENTRIES})")
    return entries


# ---------------------------------------------------------------------------
# Splitting the document

FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
HTML_START = re.compile(r"^ {0,3}<(?:!--|![A-Za-z\[]|\?|/?[A-Za-z][A-Za-z0-9-]*(?:[\s/>]|$))")
LINK_REFERENCE = re.compile(r"^ {0,3}\[(?:[^\]\\]|\\.)+\]:[ \t]*\S")
TABLE_ROW = re.compile(r"^\s*\|")


def markdown_segments(text):
    """Return [(translate, text)] covering text: False for parts copied unchanged."""
    lines = lines_of(text)
    keep = [False] * len(lines)  # True: copied unchanged
    i = 0
    # Front matter: --- ... --- (or ...) / +++ ... +++ at the very beginning.
    if lines and lines[0].rstrip("\n").rstrip() in ("---", "+++"):
        opener = lines[0].strip()
        closers = ("---", "...") if opener == "---" else ("+++",)
        for j in range(1, len(lines)):
            if lines[j].strip() in closers:
                keep[:j + 1] = [True] * (j + 1)
                i = j + 1
                break
    while i < len(lines):
        line = lines[i].rstrip("\n")
        m = FENCE.match(line)
        if m and not (m.group(1)[0] == "`" and "`" in m.group(2)):
            mark = m.group(1)
            close = re.compile(r"^ {0,3}" + re.escape(mark[0]) + "{" + str(len(mark)) + r",}\s*$")
            j = i + 1
            while j < len(lines) and not close.match(lines[j].rstrip("\n")):
                j += 1
            end = min(j + 1, len(lines))  # unclosed: to the end of the document
            keep[i:end] = [True] * (end - i)
            i = end
            continue
        stripped = line.strip()
        if stripped.startswith("$$"):
            j = i
            if not (len(stripped) >= 4 and stripped.endswith("$$")):  # not "$$ ... $$"
                j = i + 1
                while j < len(lines) and not lines[j].strip().endswith("$$"):
                    j += 1
            end = min(j + 1, len(lines))
            keep[i:end] = [True] * (end - i)
            i = end
            continue
        if HTML_START.match(line):
            j = i
            while j < len(lines) and lines[j].strip():
                j += 1
            keep[i:j] = [True] * (j - i)
            i = j
            continue
        if LINK_REFERENCE.match(line):
            keep[i] = True
        i += 1

    segments = []
    for line, kept in zip(lines, keep):
        if segments and segments[-1][0] == (not kept):
            segments[-1][1] += line
        else:
            segments.append([not kept, line])
    return [(translate, part) for translate, part in segments]


def group(items):
    """Merge consecutive items with the same key: [(key, text)] -> [[key, text]]."""
    out = []
    for key, text in items:
        if out and out[-1][0] == key:
            out[-1][1] += text
        else:
            out.append([key, text])
    return out


def pack(units, limit):
    """Pack [(is_content, text)] into consecutive pieces whose content spans at most limit
    characters (a single unit may be longer).  Separators between pieces become pieces of
    their own; the concatenation of the result equals the input."""
    pieces, current, pending = [], "", ""
    for is_content, text in units:
        if not is_content:
            if current:
                pending += text
            else:
                pieces.append(text)
            continue
        if current and len(current) + len(pending) + len(text) > limit:
            pieces += [current, pending]
            current, pending = "", ""
        current = current + pending + text if current else text
        pending = ""
    return pieces + [current, pending]


def split_chunks(segment, limit, offset, text, warn):
    """Split a translatable segment into raw pieces (see pack()).  Paragraphs (separated by
    blank lines) are packed first; a paragraph longer than limit is packed by lines, a
    Markdown table (lines starting with |) counting as one line."""
    paragraphs = group((bool(line.strip()), line) for line in lines_of(segment))
    pieces, units, position = [], [], offset

    def flush():
        pieces.extend(pack(units, limit))
        units.clear()

    for is_content, part in paragraphs:
        if is_content and len(part) > limit:
            flush()
            rows = group(("table" if TABLE_ROW.match(line) else n, line)
                         for n, line in enumerate(lines_of(part)))
            line_position = position
            for _, row in rows:
                if len(row.strip()) > limit:
                    number = text.count("\n", 0, line_position) + 1
                    kind = "table at line" if TABLE_ROW.match(row) else "line"
                    warn(f"{kind} {number} is longer than --chunk-chars "
                         f"({len(row.strip())} characters); sent as one request")
                line_position += len(row)
            pieces.extend(pack([(True, row) for _, row in rows], limit))
        else:
            units.append((is_content, part))
        position += len(part)
    flush()
    return [p for p in pieces if p]


def build_pieces(text, markdown, limit, warn):
    """Return [(translate, text)]: pieces to translate (each one request) and to copy."""
    segments = markdown_segments(text) if markdown else [(True, text)]
    pieces, offset = [], 0
    for translate, segment in segments:
        if translate:
            for piece in split_chunks(segment, limit, offset, text, warn):
                pieces.append((bool(piece.strip()), piece))
        else:
            pieces.append((False, segment))
        offset += len(segment)
    return pieces


# ---------------------------------------------------------------------------
# llama-server


class Llama:
    def __init__(self, url, api_key):
        self.url = url
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != "http" or not parts.hostname:
            raise UsageError(f"llama-server URL must be http://<host>:<port>: {url}")
        try:
            self.port = parts.port or 80
        except ValueError:
            raise UsageError(f"invalid port in llama-server URL: {url}")
        self.host = parts.hostname
        self.prefix = parts.path.rstrip("/")
        self.api_key = api_key

    def _error(self, status, data):
        try:
            body = json.loads(data)
        except ValueError:
            body = None
        detail = ""
        if isinstance(body, dict) and isinstance(body.get("error"), dict):
            detail = body["error"].get("message", "")
        message = f"llama-server returned HTTP {status}" + (f": {detail}" if detail else "")
        if status == 401:
            if self.api_key:
                message += ("\nThe API key was rejected.  Check the key given with "
                            "PLAMO3_LLAMA_API_KEY or --api-key-file.")
            else:
                message += ("\nllama-server requires an API key (it is exposed to the "
                            "network).  Set PLAMO3_LLAMA_API_KEY or use --api-key-file.")
        elif status == 400:
            message += ("\nThe request may not fit in the llama-server context; "
                        "try a smaller --chunk-chars.")
        return TranslateError(message)

    def translate(self, history, text, kwargs, on_event):
        """Translate text (one chunk) with history [(source, translation)] and return the
        stripped translation.  on_event receives ("progress", processed, total) and
        ("delta", str)."""
        messages = []
        for source, translation in history:
            messages += [{"role": "user", "content": source},
                         {"role": "assistant", "content": translation}]
        messages.append({"role": "user", "content": text})
        payload = {
            "model": MODEL_ALIAS,
            "messages": messages,
            "chat_template_kwargs": kwargs,
            "temperature": 0,
            # A translation is not much longer than its source; the bound stops runaway
            # repetition.
            "max_tokens": min(MAX_TOKENS, 4 * len(text) + 256),
            "stop": STOP,
            "stream": True,
            "return_progress": True,  # prompt processing progress
        }
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        body = json.dumps(payload).encode("utf-8")
        conn = http.client.HTTPConnection(self.host, self.port, timeout=TIMEOUT)
        # The request is made in a daemon thread and its events are handed over through a
        # queue, so that the main thread never sits in a socket call and Ctrl-C works at
        # once on Windows too (see POLL_SECONDS).  on_event is called in the main thread.
        events = queue.Queue()

        def work():
            try:
                try:
                    try:
                        conn.request("POST", self.prefix + "/v1/chat/completions",
                                     body=body, headers=headers)
                        response = conn.getresponse()
                        if response.status != 200:
                            raise self._error(response.status, response.read())
                    except (OSError, http.client.HTTPException) as e:
                        raise TranslateError(
                            f"cannot connect to llama-server ({self.url}): {e}")
                    result = self._read_stream(
                        response, lambda *event: events.put(("event", event)))
                finally:
                    conn.close()
            except BaseException as e:
                events.put(("error", e))
            else:
                events.put(("result", result))

        threading.Thread(target=work, daemon=True).start()
        try:
            while True:
                try:
                    kind, value = events.get(timeout=POLL_SECONDS)
                except queue.Empty:
                    continue
                if kind == "event":
                    on_event(*value)
                elif kind == "result":
                    return value
                else:
                    raise value
        except BaseException:
            # Ctrl-C, or a failure in on_event: hang up so that llama-server stops
            # generating.  The thread is not waited for; the caller is on its way out, and
            # the end of the process closes whatever is left.
            sock = conn.sock
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            raise

    def _read_stream(self, response, on_event):
        parts = []
        finish_reason = None
        try:
            for raw in response:  # Server-Sent Events: "data: <json>" lines
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except ValueError:
                    continue
                if not isinstance(chunk, dict):
                    continue
                if chunk.get("error"):
                    err = chunk["error"]
                    detail = err.get("message", "") if isinstance(err, dict) else str(err)
                    raise TranslateError("llama-server error" + (f": {detail}" if detail else ""))
                progress = chunk.get("prompt_progress")
                if isinstance(progress, dict):
                    try:
                        cache = int(progress.get("cache", 0))
                        on_event("progress", int(progress["processed"]) - cache,
                                 int(progress["total"]) - cache)
                    except (KeyError, TypeError, ValueError):
                        pass
                try:
                    delta = chunk["choices"][0]["delta"].get("content")
                except (KeyError, IndexError, TypeError, AttributeError):
                    delta = None
                if isinstance(delta, str) and delta:
                    parts.append(delta)
                    on_event("delta", delta)
                try:
                    finish = chunk["choices"][0].get("finish_reason")
                except (KeyError, IndexError, TypeError, AttributeError):
                    finish = None
                if finish:
                    finish_reason = finish
            else:
                raise TranslateError("llama-server closed the connection before the end")
        except (OSError, http.client.HTTPException) as e:
            raise TranslateError(f"lost connection to llama-server ({self.url}): {e}")
        if finish_reason == "length":
            raise TranslateError(
                'the translation was cut off: llama-server stopped at the token limit '
                '(finish_reason "length").\n'
                "Either the llama-server context is full (the source, the translation history "
                "and the translation must fit in it) or the translation reached the output "
                "limit of 4 tokens per source character (e.g. the model kept repeating "
                "itself).  Use a smaller --chunk-chars, which also shortens the history; "
                "if the input has a very long line or table, break it up.")
        return "".join(parts).replace("\r\n", "\n").strip()


# ---------------------------------------------------------------------------
# Output and progress


def stdout_gone(error):
    """Whether an OSError from a write to standard output means that its reader went away
    (e.g. "| head").  On Windows that is EINVAL, not EPIPE (BrokenPipeError)."""
    return isinstance(error, BrokenPipeError) or (WINDOWS and error.errno == errno.EINVAL)


class Writer:
    """Writes text to a binary file, restoring the BOM and CRLF line ends of the input.
    With stdout (fh is standard output), a reader that went away raises BrokenPipeError on
    all platforms."""

    def __init__(self, fh, bom, crlf, stdout=False):
        self.fh, self.bom, self.crlf, self.stdout = fh, bom, crlf, stdout

    def write(self, text):
        if self.crlf:
            text = text.replace("\n", "\r\n")
        data = text.encode("utf-8")
        if self.bom:
            data = b"\xef\xbb\xbf" + data
            self.bom = False
        try:
            self.fh.write(data)
            self.fh.flush()
        except OSError as e:
            if self.stdout and stdout_gone(e) and not isinstance(e, BrokenPipeError):
                raise BrokenPipeError(errno.EPIPE, "standard output was closed") from None
            raise


def enable_escape_sequences(stream):
    """Whether the terminal behind stream handles escape sequences.  On Windows this turns
    on virtual terminal processing, which the older console host needs (Windows Terminal
    has it on); the mode is left on, as it belongs to the console and not to this process."""
    if not WINDOWS:
        return True
    try:
        import ctypes
        import msvcrt
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetConsoleMode.restype = wintypes.BOOL
        kernel32.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.SetConsoleMode.restype = wintypes.BOOL
        handle = msvcrt.get_osfhandle(stream.fileno())
        mode = wintypes.DWORD()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        if mode.value & 0x0004:  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
            return True
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:  # no ctypes, no console API, not a console, ...
        return False


class Progress:
    """Progress on standard error.  With show_text, the translation is streamed as it is
    generated (output to a file); otherwise only counts are shown (output to stdout)."""

    def __init__(self, quiet, show_text):
        self.quiet = quiet
        self.show_text = show_text
        self.tty = sys.stderr.isatty()
        # Without escape sequences the rest of the status line is erased with spaces.
        self.escapes = self.tty and enable_escape_sequences(sys.stderr)
        self.status = ""   # current \r status line (tty only)
        self.width = 0     # characters on the terminal line (the status lines are ASCII)

    def _put(self, text):
        sys.stderr.write(text)
        sys.stderr.flush()

    def _line(self, text):
        """Replace the terminal line with text, leaving the cursor after it."""
        erase = "\033[K" if self.escapes else " " * (self.width - len(text))
        self.width = len(text)
        return "\r" + text + erase

    def _set_status(self, text):
        if self.tty:
            self._put(self._line(text))
            self.status = text

    def _end_status(self, text=None):
        if self.status or text:
            self._put(self._line(text if text is not None else self.status) + "\n"
                      if self.tty else (text or "") + "\n")
        self.status = ""
        self.width = 0

    def warn(self, message):
        if self.status:
            self._end_status()
        self._put(f"warning: {message}\n")

    def info(self, message):
        if not self.quiet:
            self._put(message + "\n")

    def start(self, index, total, chars):
        if self.quiet:
            return
        self.label = f"[{index}/{total}] {chars} characters"
        self.generated = 0
        self.text_started = False
        self.at_line_start = True
        self.t0 = time.monotonic()
        if self.tty:
            self._set_status(self.label)
        else:
            self._put(self.label + "\n")

    def event(self, kind, *args):
        if self.quiet:
            return
        if kind == "progress":
            processed, total = args
            if total > 0 and not self.text_started:
                self._set_status(f"{self.label}: prompt {100 * processed // total}%")
        elif kind == "delta":
            text = args[0]
            self.generated += len(text)
            if self.show_text:
                if not self.text_started:
                    self._end_status(self.label if self.tty else None)
                    text = text.lstrip()
                if text:
                    self._put(text)
                    self.at_line_start = text.endswith("\n")
                self.text_started = True
            else:
                self.text_started = True
                self._set_status(f"{self.label}: {self.generated} characters generated")

    def finish(self, translation):
        if self.quiet:
            return
        elapsed = time.monotonic() - self.t0
        if self.show_text:
            if not self.text_started:
                self._end_status()
            elif not self.at_line_start:
                self._put("\n")
        else:
            self._end_status(f"{self.label} -> {len(translation)} characters, {elapsed:.1f} s")

    def abort(self):
        if self.status:
            self._end_status()
        elif not self.quiet and self.show_text and getattr(self, "text_started", False) \
                and not self.at_line_start:
            self._put("\n")


# ---------------------------------------------------------------------------
# Main


def auto_output_path(path, target, source):
    """dir/stem.ext -> dir/stem.<target>.ext; a trailing .<source> on stem is replaced."""
    directory, base = os.path.split(path)
    stem, ext = os.path.splitext(base)
    if ext[1:] == source:  # "notes.ja" (no extension besides the language)
        stem, ext = stem, ""
    else:
        inner, tag = os.path.splitext(stem)
        if tag[1:] == source and inner:
            stem = inner
    return os.path.join(directory, f"{stem}.{target}{ext}")


def same_file(a, b):
    try:
        return os.path.samefile(a, b)
    except OSError:
        return os.path.realpath(a) == os.path.realpath(b)


def check_output(path, input_path, force, what):
    if input_path is not None and same_file(input_path, path):
        raise UsageError(f"{what} {path} is the input file")
    if os.path.isdir(path):
        raise UsageError(f"{what} {path} is a directory")
    if os.path.lexists(path) and not force:
        raise UsageError(f"{what} {path} exists (use -f to overwrite)")
    if WINDOWS and os.path.lexists(path):
        # On Windows a file that is read-only, or open in a program that locks it, cannot
        # be replaced: say so before translating.  (OutputFile.commit() handles a file that
        # gets locked later.  Not done elsewhere: on POSIX replacing needs no write access.)
        try:
            with open(path, "r+b"):
                pass
        except OSError as e:
            raise UsageError(f"{what} {path} cannot be overwritten: {e.strerror} "
                             "(is it read-only, or open in another program?)")


class OutputFile:
    """A file written through a temporary file in the same directory: commit() renames it
    to path, discard() removes it (and does nothing after commit()).  When the temporary
    file cannot be renamed, commit() raises TranslateError and the file is kept."""

    def __init__(self, path):
        self.path = path
        directory = os.path.dirname(path) or "."
        try:
            fd, self.temp = tempfile.mkstemp(dir=directory,
                                             prefix="." + os.path.basename(path) + ".",
                                             suffix=".tmp")
        except OSError as e:
            raise UsageError(f"cannot write to {directory}: {e.strerror}")
        self.fh = os.fdopen(fd, "wb")

    def commit(self):
        self.fh.close()
        try:
            mode = os.stat(self.path).st_mode & 0o7777
        except OSError:
            umask = os.umask(0)
            os.umask(umask)
            mode = 0o666 & ~umask
        try:
            os.chmod(self.temp, mode)  # on Windows: only the read-only flag
        except OSError:
            pass
        temp, self.temp = self.temp, None  # from here on discard() keeps the file
        try:
            os.replace(temp, self.path)
        except OSError as e:
            # e.g. on Windows: the destination is open in another program, or read-only.
            raise TranslateError(f"cannot write {self.path}: {e.strerror or e}; "
                                 f"the text is kept in {os.path.abspath(temp)}")

    def discard(self):
        if self.temp is None:
            return
        try:
            self.fh.close()
        except OSError:
            pass
        try:
            os.unlink(self.temp)
        except OSError:
            pass
        self.temp = None


def layout(pieces):
    """Return (bodies, frames, tail) for [(translate, text)]: the stripped chunks to
    translate, the (prefix, suffix) to put around each translation (copied text and the
    whitespace around the chunk) and the copied text after the last chunk."""
    bodies, frames = [], []
    pending = ""  # copied text not yet assigned to a chunk
    for translate, piece in pieces:
        if not translate:
            pending += piece
            continue
        bodies.append(piece.strip())
        lead = piece[:len(piece) - len(piece.lstrip())]
        trail = piece[len(piece.rstrip()):]
        frames.append((pending + lead, trail))
        pending = ""
    return bodies, frames, pending


def template_kwargs(target, source, form, glossary, context):
    kwargs = {"target_lang": target}
    if source:
        kwargs["source_lang"] = source
    if form and target == "ja":
        kwargs["form"] = form
    if glossary:
        kwargs["glossary"] = glossary
    if context:
        kwargs["context"] = context
    return kwargs


def translate_pass(llama, bodies, kwargs, chunk_chars, progress, on_result):
    """Translate bodies in order, each request carrying the preceding pairs of this pass as
    history, and call on_result(index, translation) (index from 0) after each one."""
    history = []
    for index, body in enumerate(bodies):
        # History: the preceding pairs, newest first, up to 2.5 x --chunk-chars in all.
        context_pairs, size = [], 0
        for pair in reversed(history):
            size += len(pair[0]) + len(pair[1])
            if size > HISTORY_FACTOR * chunk_chars:
                break
            context_pairs.insert(0, pair)
        progress.start(index + 1, len(bodies), len(body))
        try:
            translation = llama.translate(context_pairs, body, kwargs, progress.event)
        except BaseException:
            progress.abort()
            raise
        progress.finish(translation)
        history.append((body, translation))
        on_result(index, translation)


def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="plamo-3-translate-client.py",
        description="Translate a plain text or Markdown file with PLaMo 3 Translate "
                    "through llama-server, keeping its layout.  With --intermediate, "
                    "translate it twice: into the intermediate language, then into --to.",
        epilog="Environment: PLAMO3_LLAMA_URL (default " + DEFAULT_URL + "), "
               "PLAMO3_LLAMA_API_KEY.  Exit status: 0 success, 1 translation error, "
               "2 invalid arguments or input, 130 interrupted.")
    parser.add_argument("input", nargs="?", metavar="INPUT",
                        help='file to translate, or "-" for standard input')
    parser.add_argument("-o", "--output", metavar="PATH",
                        help='output file, or "-" for standard output (default for a file: '
                             'INPUT with the target language code before the extension, '
                             'e.g. doc.md -> doc.en.md; for standard input: standard output; '
                             'required with --intermediate)')
    parser.add_argument("-f", "--force", action="store_true",
                        help="overwrite an existing output file")
    parser.add_argument("--from", dest="source", metavar="LANG",
                        help="source language (default: detected, Japanese preferred)")
    parser.add_argument("--to", dest="target", metavar="LANG", default="auto",
                        help="target language, or auto (default): Japanese to English, "
                             "anything else to Japanese (a language is required with "
                             "--intermediate)")
    parser.add_argument("--intermediate", metavar="LANG",
                        help="round trip: translate into LANG first, then from LANG into "
                             "--to (requires --to LANG and -o)")
    parser.add_argument("--intermediate-output", metavar="PATH",
                        help="with --intermediate, also write the whole document in the "
                             "intermediate language to PATH when the first pass is complete")
    parser.add_argument("--form", choices=FORMS,
                        help="style of Japanese translations (target ja only; with "
                             "--intermediate, used in the second pass)")
    parser.add_argument("--format", choices=("auto", "text", "markdown"), default="auto",
                        help="input format (default auto: markdown for .md and .markdown "
                             "files, text otherwise)")
    parser.add_argument("--glossary", metavar="FILE",
                        help='glossary file: one "source => target" per line, '
                             "blank lines and lines starting with # ignored (with "
                             "--intermediate: for the first pass, source to intermediate)")
    parser.add_argument("--glossary2", metavar="FILE",
                        help="with --intermediate, glossary file for the second pass "
                             "(intermediate to target), same format as --glossary")
    context = parser.add_mutually_exclusive_group()
    context.add_argument("--context", metavar="TEXT",
                         help="background information about the document")
    context.add_argument("--context-file", metavar="FILE",
                         help="read --context from a file")
    parser.add_argument("--chunk-chars", type=int, default=DEFAULT_CHUNK_CHARS, metavar="N",
                        help=f"maximum source characters per request "
                             f"(default {DEFAULT_CHUNK_CHARS})")
    parser.add_argument("--url", default=os.environ.get("PLAMO3_LLAMA_URL") or DEFAULT_URL,
                        help="llama-server URL (default $PLAMO3_LLAMA_URL or "
                             + DEFAULT_URL + ")")
    parser.add_argument("--api-key-file", metavar="FILE",
                        help="file containing the llama-server API key "
                             "(default $PLAMO3_LLAMA_API_KEY)")
    parser.add_argument("-q", "--quiet", action="store_true",
                        help="show no progress (warnings and errors are still shown)")
    parser.add_argument("--list-languages", action="store_true",
                        help="list the language codes and exit")
    args = parser.parse_args(argv)

    if args.list_languages:
        return args
    if args.input is None:
        parser.error("INPUT is required")
    if args.source is not None and args.source not in LANGUAGE_CODES:
        parser.error(f"unknown --from language: {args.source} (see --list-languages)")
    if args.target != "auto" and args.target not in LANGUAGE_CODES:
        parser.error(f"unknown --to language: {args.target} (see --list-languages)")
    if args.form and args.target not in ("ja", "auto"):
        parser.error("--form requires --to ja (or auto)")
    if args.chunk_chars < MIN_CHUNK_CHARS:
        parser.error(f"--chunk-chars must be at least {MIN_CHUNK_CHARS}")
    if args.intermediate is None:
        if args.glossary2:
            parser.error("--glossary2 requires --intermediate")
        if args.intermediate_output:
            parser.error("--intermediate-output requires --intermediate")
        return args
    if args.intermediate not in LANGUAGE_CODES:
        parser.error(f"unknown --intermediate language: {args.intermediate} "
                     "(see --list-languages)")
    if args.target == "auto":
        parser.error("--intermediate requires --to LANG (not auto)")
    if args.target == args.intermediate:
        parser.error("--intermediate and --to must be different languages")
    if args.output is None:
        parser.error('--intermediate requires -o PATH (or "-o -" for standard output)')
    if args.intermediate_output == "-":
        parser.error("--intermediate-output must be a file")
    return args


def main(argv=None):
    args = parse_args(argv)
    if args.list_languages:
        try:
            for code, name in LANGUAGES:
                print(f"{code}\t{name}")
            sys.stdout.flush()
        except OSError as e:
            if stdout_gone(e):
                raise BrokenPipeError(errno.EPIPE, "standard output was closed") from None
            raise
        return 0

    from_stdin = args.input == "-"
    to_stdout = args.output == "-" or (args.output is None and from_stdin)
    progress = Progress(args.quiet, show_text=not to_stdout)

    # Settings
    if args.api_key_file:
        api_key = read_text_file(args.api_key_file, "API key file").strip()
        if not api_key:
            raise UsageError(f"API key file {args.api_key_file} is empty")
    else:
        api_key = os.environ.get("PLAMO3_LLAMA_API_KEY", "").strip()
    llama = Llama(args.url.rstrip("/"), api_key)
    glossary = parse_glossary(args.glossary) if args.glossary else []
    glossary2 = parse_glossary(args.glossary2) if args.glossary2 else []
    context = args.context
    if args.context_file:
        context = read_text_file(args.context_file, "context file")
    if context is not None:
        check_reserved(context, "the context")
        context = context.strip() or None

    # Input
    if from_stdin:
        if sys.stdin.isatty() and not args.quiet:
            progress.info("Reading standard input (end with "
                          + ("Ctrl-Z and Enter" if WINDOWS else "Ctrl-D") + ")...")
        data = sys.stdin.buffer.read()
        name = "standard input"
    else:
        try:
            with open(args.input, "rb") as f:
                data = f.read()
        except OSError as e:
            raise UsageError(f"cannot read {args.input}: {e.strerror}")
        name = args.input
    bom = data.startswith(b"\xef\xbb\xbf")
    text = decode_utf8(data, name, stdin=from_stdin)
    crlf = "\r\n" in text
    text = text.replace("\r\n", "\n")

    markdown = args.format == "markdown" or (
        args.format == "auto" and not from_stdin
        and os.path.splitext(args.input)[1].lower() in MARKDOWN_EXTENSIONS)
    pieces = build_pieces(text, markdown, args.chunk_chars, progress.warn)
    source_text = "".join(piece for translate, piece in pieces if translate)
    if not source_text.strip():
        raise UsageError(f"nothing to translate in {name}")
    check_reserved(source_text, f"{name}")

    # Languages
    intermediate = args.intermediate
    target = args.target
    if target == "auto":
        target = auto_target(source_text, args.source)
    if intermediate:
        # Round trip: source -> intermediate (first glossary), intermediate -> target
        # (second glossary, form).
        kwargs = template_kwargs(target, intermediate, args.form, glossary2, context)
        kwargs1 = template_kwargs(intermediate, args.source, None, glossary, context)
    else:
        kwargs = template_kwargs(target, args.source, args.form, glossary, context)

    # Output
    input_path = None if from_stdin else args.input
    output = None
    if not to_stdout:
        if args.output is not None:
            output = args.output
        else:
            source = args.source or ("ja" if looks_japanese(source_text) else "en")
            output = auto_output_path(args.input, target, source)
        check_output(output, input_path, args.force, "output")
    intermediate_output = args.intermediate_output
    if intermediate_output:
        check_output(intermediate_output, input_path, args.force, "intermediate output")
        if output and same_file(intermediate_output, output):
            raise UsageError(f"intermediate output {intermediate_output} is the output file")

    bodies, frames, tail = layout(pieces)
    count = f"{len(bodies)} chunk{'s' if len(bodies) != 1 else ''}"
    progress.info(f"Translating {name} into {target}"
                  + (f" via {intermediate}" if intermediate else "")
                  + f" ({count}, {len(source_text)} characters, "
                  f"{'markdown' if markdown else 'text'})"
                  + (f" -> {output}" if output else "")
                  + (f", intermediate -> {intermediate_output}" if intermediate_output else ""))

    files = []
    intermediate_error = None
    try:
        if output:
            out_file = OutputFile(output)
            files.append(out_file)
            fh = out_file.fh
        else:
            fh = sys.stdout.buffer
        if intermediate_output:
            intermediate_file = OutputFile(intermediate_output)
            files.append(intermediate_file)
        writer = Writer(fh, bom, crlf, stdout=not output)

        t0 = time.monotonic()
        if intermediate:
            progress.info(f"Pass 1/2: {args.source or 'auto'} -> {intermediate}")
            intermediates = []

            def keep(index, translation):
                if not translation:
                    raise TranslateError(f"chunk {index + 1}: the translation into "
                                         f"{intermediate} is empty")
                intermediates.append(translation)

            translate_pass(llama, bodies, kwargs1, args.chunk_chars, progress, keep)
            if intermediate_output:
                intermediate_writer = Writer(intermediate_file.fh, bom, crlf)
                for (prefix, suffix), translation in zip(frames, intermediates):
                    intermediate_writer.write(prefix + translation + suffix)
                intermediate_writer.write(tail)
                try:
                    intermediate_file.commit()
                except TranslateError as e:
                    # The intermediate text is kept in its temporary file; go on with the
                    # second pass and fail at the end.
                    intermediate_error = e
                    progress.warn(f"{e}; continuing with pass 2")
                else:
                    progress.info(f"Intermediate document written to {intermediate_output}")
            progress.info(f"Pass 2/2: {intermediate} -> {target}")
            # The chunks stay those of the source: the intermediate text is not split again.
            bodies = intermediates

        def emit(index, translation):
            prefix, suffix = frames[index]
            writer.write(prefix + translation + suffix)

        translate_pass(llama, bodies, kwargs, args.chunk_chars, progress, emit)
        writer.write(tail)

        if output:
            out_file.commit()
        if intermediate_error:
            progress.info("Pass 2 is complete" + (f", written to {output}" if output else ""))
            raise intermediate_error
        progress.info(f"Done: {count}{' x 2 passes' if intermediate else ''} in "
                      f"{time.monotonic() - t0:.1f} s" + (f", written to {output}" if output else ""))
    finally:
        for file in files:
            file.discard()
    return 0


def run():
    try:
        return main()
    except UsageError as e:
        print(f"plamo-3-translate-client.py: error: {e}", file=sys.stderr)
        return 2
    except TranslateError as e:
        print(f"plamo-3-translate-client.py: error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("plamo-3-translate-client.py: interrupted", file=sys.stderr)
        return 130
    except BrokenPipeError:
        # The reader of standard output went away (e.g. "| head").
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
        except OSError:
            pass
        return 1


if __name__ == "__main__":
    sys.exit(run())
