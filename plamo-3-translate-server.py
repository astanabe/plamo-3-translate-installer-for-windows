#!/usr/bin/python3
"""PLaMo 3 Translate web server (WSGI application for uWSGI; stand-alone server).

GET  /               translation form (HTML)
POST /api/translate  JSON API:
    request  {"text": str, "target_lang": str, "source_lang": str?,
              "form": "plain"|"polite"?, "glossary": [{"source", "target"}]?,
              "context": str?}
    target_lang "auto": Japanese -> English, any other language -> Japanese.  The source
             is Japanese if source_lang is "ja", or (without source_lang) if the text
             contains kana, or kanji but no hangul (Japanese is preferred, so Chinese
             text is also taken as Japanese); form is ignored when the result is not
             Japanese.
             "stream": bool? (default false)
    response {"translation": str, "target_lang": str (the target actually used)}
             or {"error": str}
             (400 for invalid input, including text that does not fit in the
             llama-server context; 502 for other llama-server errors)
             A translation cut off at the token limit (finish_reason "length") is an
             error, not a partial result: 400 when the context is full, 502 when the
             output reached max_tokens (e.g. repetition); the token counts are logged
             to stderr.
    With "stream": true, a 200 response is NDJSON (application/x-ndjson), one object
             per line, sent as llama-server makes progress:
             {"target_lang": str}                            first
             {"progress": {"processed": int, "total": int}}  prompt processing, in tokens
             {"delta": str}                                  a piece of the translation
             {"translation": str} or {"error": str}          last
             Errors found before the stream starts are returned as without "stream".

The request is forwarded to llama-server's /v1/chat/completions with the
translation settings in chat_template_kwargs.  Python standard library only.

Under uWSGI the entry point is the "application" callable.  Without uWSGI (Windows,
development) the file is run as a script and serves the same application itself, one
thread per request:
    plamo-3-translate-server.py [--host ADDR] [--port PORT] [PORT]
        --host ADDR   address to listen on (default 127.0.0.1; 0.0.0.0 for all)
        --port PORT   port to listen on (default 8080); the positional PORT is the same
    It prints "Serving on http://<host>:<port>/ (llama-server: <PLAMO3_LLAMA_URL>)" to
    stdout once it is listening, exits with status 1 (and a one-line message on stderr)
    when it cannot listen on the address, and with status 0 on Ctrl-C.

Environment variables:
    PLAMO3_LLAMA_URL        llama-server address: unix:<socket path> or http://<host>:<port>
                            (default unix:/run/plamo-3-translate-llama/llama.sock)
    PLAMO3_LLAMA_API_KEY    llama-server API key (default: none)
    PLAMO3_LLAMA_TIMEOUT    timeout in seconds for llama-server (default 3600)
    PLAMO3_MAX_TEXT_CHARS   maximum length of text/context in characters (default 1000000);
                            the token limit is the llama-server context size
    PLAMO3_MAX_TOKENS       maximum number of generated tokens (default 262144); each
                            request is also limited to 4 x len(text) + 256 tokens
"""

import html
import http.client
import json
import os
import re
import socket
import sys
import urllib.parse

LLAMA_URL = (os.environ.get("PLAMO3_LLAMA_URL") or "unix:/run/plamo-3-translate-llama/llama.sock").rstrip("/")
LLAMA_API_KEY = os.environ.get("PLAMO3_LLAMA_API_KEY", "")
LLAMA_TIMEOUT = float(os.environ.get("PLAMO3_LLAMA_TIMEOUT", "3600"))
MAX_TEXT_CHARS = int(os.environ.get("PLAMO3_MAX_TEXT_CHARS", "1000000"))
MAX_TOKENS = int(os.environ.get("PLAMO3_MAX_TOKENS", "262144"))
MAX_GLOSSARY_ENTRIES = 500
MAX_BODY_BYTES = 8 * 1024 * 1024
MODEL_ALIAS = "plamo-3-translate"
STOP = ["<|plamo:tag|>", "<|plamo:bos|>"]

# Supported languages (model card, "Supported Languages").
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
KANA = re.compile("[\u3041-\u309f\u30a0-\u30ff\u31f0-\u31ff\uff66-\uff9f]")
# Kanji (CJK unified ideographs, extension A, compatibility ideographs, extensions B and
# later) and the marks 々 〆 〇.
KANJI = re.compile("[\u3005-\u3007\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U0003134f]")
# Hangul syllables and jamo: Korean text may contain hanja.
HANGUL = re.compile("[\u1100-\u11ff\u3130-\u318f\uac00-\ud7af]")


def looks_japanese(text):
    """Japanese preferred: kana, or kanji without hangul (Chinese counts as Japanese)."""
    if KANA.search(text):
        return True
    return bool(KANJI.search(text)) and not HANGUL.search(text)


def auto_target(text, source):
    """Target language for target_lang "auto": English for Japanese, Japanese otherwise."""
    is_japanese = source == "ja" if source else looks_japanese(text)
    return "en" if is_japanese else "ja"
RESERVED = "<|plamo:"


class UnixHTTPConnection(http.client.HTTPConnection):
    """HTTP over a UNIX domain socket."""

    def __init__(self, path, timeout):
        super().__init__("localhost", timeout=timeout)
        self.socket_path = path

    def connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(self.socket_path)
        except OSError:
            sock.close()
            raise
        self.sock = sock


def llama_connection():
    """Return (connection, path prefix) for PLAMO3_LLAMA_URL."""
    if LLAMA_URL.startswith("unix:"):
        if not hasattr(socket, "AF_UNIX"):  # e.g. Python on Windows
            raise UpstreamError("PLAMO3_LLAMA_URL is a UNIX domain socket (unix:<path>), which "
                                "Python does not support on this platform; set it to "
                                "http://<host>:<port>")
        return UnixHTTPConnection(LLAMA_URL[len("unix:"):], LLAMA_TIMEOUT), ""
    url = urllib.parse.urlsplit(LLAMA_URL)
    if url.scheme != "http" or not url.hostname:
        raise UpstreamError("PLAMO3_LLAMA_URL must be unix:<path> or http://<host>:<port>")
    return http.client.HTTPConnection(url.hostname, url.port or 80, timeout=LLAMA_TIMEOUT), url.path


class BadRequest(Exception):
    pass


class UpstreamError(Exception):
    pass


def _check_text(value, name, allow_empty=False, max_chars=MAX_TEXT_CHARS):
    if not isinstance(value, str):
        raise BadRequest(f"{name} must be a string")
    if not allow_empty and not value.strip():
        raise BadRequest(f"{name} must not be empty")
    if len(value) > max_chars:
        raise BadRequest(f"{name} is too long (max {max_chars} characters)")
    if RESERVED in value:
        raise BadRequest(f"{name} must not contain reserved tokens ({RESERVED}...)")
    return value


def _optional(req, key):
    value = req.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return value


def validate_request(req):
    """Validate the API request and return chat_template_kwargs and the text."""
    if not isinstance(req, dict):
        raise BadRequest("request body must be a JSON object")
    unknown = set(req) - {"text", "target_lang", "source_lang", "form", "glossary", "context"}
    if unknown:
        raise BadRequest("unknown field(s): " + ", ".join(sorted(unknown)))

    text = _check_text(req.get("text"), "text")
    kwargs = {}

    target = req.get("target_lang")
    if not isinstance(target, str) or (target not in LANGUAGE_CODES and target != "auto"):
        raise BadRequest('target_lang must be "auto" or one of the supported language codes')

    source = _optional(req, "source_lang")
    if source is not None:
        if not isinstance(source, str) or source not in LANGUAGE_CODES:
            raise BadRequest("source_lang must be one of the supported language codes")
        kwargs["source_lang"] = source

    automatic = target == "auto"
    if automatic:
        target = auto_target(text, source)
    kwargs["target_lang"] = target

    form = _optional(req, "form")
    if form is not None:
        if form not in FORMS:
            raise BadRequest("form must be plain or polite")
        if target == "ja":
            kwargs["form"] = form
        elif not automatic:
            raise BadRequest('form requires target_lang "ja"')

    context = _optional(req, "context")
    if context is not None:
        kwargs["context"] = _check_text(context, "context")

    glossary = req.get("glossary")
    if glossary:
        if not isinstance(glossary, list):
            raise BadRequest("glossary must be a list of {source, target} objects")
        if len(glossary) > MAX_GLOSSARY_ENTRIES:
            raise BadRequest(f"glossary has too many entries (max {MAX_GLOSSARY_ENTRIES})")
        entries = []
        for term in glossary:
            if not isinstance(term, dict) or set(term) != {"source", "target"}:
                raise BadRequest("each glossary entry must have exactly source and target")
            entries.append({
                "source": _check_text(term["source"], "glossary source", max_chars=1000).strip(),
                "target": _check_text(term["target"], "glossary target", max_chars=1000).strip(),
            })
        kwargs["glossary"] = entries

    return text, kwargs


def _max_tokens(text):
    # A translation is not much longer than its source; the bound stops runaway repetition.
    return min(MAX_TOKENS, 4 * len(text) + 256)


CUT_OFF_CONTEXT = ("The translation was cut off: the text is too long for this server (the source "
                   "and the translation must fit in its context).  Translate the text in smaller "
                   "parts; if you need to translate longer texts at once, see the server "
                   "log or contact the administrator.")
CUT_OFF_OUTPUT = ("The translation was cut off: the output reached its limit (the model may have "
                  "kept repeating itself).  Try again with a shorter or reworded text; if this "
                  "keeps happening, see the server log or contact the administrator.")
CUT_OFF_UNKNOWN = ("The translation was cut off at the token limit.  Try again with a shorter "
                   "text; if this keeps happening, see the server log or contact the "
                   "administrator.")


def _cut_off(text, timings):
    """Exception for a translation stopped at the token limit (finish_reason "length").
    The cause is told from llama-server's timings: the output reached max_tokens, or else
    the context is full.  The token counts are logged for the administrator."""
    limit = _max_tokens(text)
    try:
        prompt = int(timings.get("cache_n", 0)) + int(timings["prompt_n"])
        generated = int(timings["predicted_n"])
    except (AttributeError, KeyError, TypeError, ValueError):
        prompt = generated = None
    if generated is None:
        cause, error = "unknown cause", UpstreamError(CUT_OFF_UNKNOWN)
    elif generated >= limit:
        cause, error = "output limit", UpstreamError(CUT_OFF_OUTPUT)
    else:
        cause, error = "context full", BadRequest(CUT_OFF_CONTEXT)
    print(f"plamo-3-translate-server: translation cut off ({cause}): prompt {prompt} tokens, "
          f"generated {generated} tokens, max_tokens {limit}, source {len(text)} characters",
          file=sys.stderr, flush=True)
    return error


def _open_completion(text, kwargs, stream):
    """POST to llama-server's /v1/chat/completions; return (connection, response) for a
    200 response, or raise BadRequest/UpstreamError (the connection is then closed)."""
    payload = {
        "model": MODEL_ALIAS,
        "messages": [{"role": "user", "content": text}],
        "chat_template_kwargs": kwargs,
        "temperature": 0,
        "max_tokens": _max_tokens(text),
        "stop": STOP,
        "stream": stream,
    }
    if stream:
        payload["return_progress"] = True  # prompt processing progress
    headers = {"Content-Type": "application/json"}
    if LLAMA_API_KEY:
        headers["Authorization"] = "Bearer " + LLAMA_API_KEY
    conn, prefix = llama_connection()
    try:
        conn.request("POST", prefix + "/v1/chat/completions",
                     body=json.dumps(payload).encode("utf-8"), headers=headers)
        response = conn.getresponse()
        if response.status == 200:
            return conn, response
        data = response.read()
    except (OSError, http.client.HTTPException) as e:
        conn.close()
        raise UpstreamError(f"cannot reach llama-server ({LLAMA_URL}): {e}")
    conn.close()
    try:
        body = json.loads(data)
    except ValueError:
        body = None
    detail = ""
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        detail = body["error"].get("message", "")
    message = f"llama-server returned HTTP {response.status}" + (f": {detail}" if detail else "")
    if response.status == 400:  # e.g. the request exceeds the context size
        raise BadRequest(message)
    raise UpstreamError(message)


def translate(text, kwargs):
    conn, response = _open_completion(text, kwargs, stream=False)
    try:
        data = response.read()
    except (OSError, http.client.HTTPException) as e:
        raise UpstreamError(f"cannot reach llama-server ({LLAMA_URL}): {e}")
    finally:
        conn.close()
    try:
        body = json.loads(data)
        choice = body["choices"][0]
        content = choice["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError):
        raise UpstreamError("unexpected response from llama-server")
    if not isinstance(content, str):
        raise UpstreamError("unexpected response from llama-server")
    if choice.get("finish_reason") == "length":
        raise _cut_off(text, body.get("timings"))
    return content.strip()


def translate_stream(text, kwargs):
    """Start a streaming translation.  Errors before the first byte raise like translate();
    the returned generator yields events:
        {"progress": {"processed": int, "total": int}}  prompt processing (tokens; cached
                                                         tokens are excluded)
        {"delta": str}                                   a piece of the translation
        {"translation": str}                             the end: the whole (stripped) text
        {"error": str}                                   the end: failure after the start
                                                         (including a translation cut off
                                                         at the token limit)
    """
    conn, response = _open_completion(text, kwargs, stream=True)

    def events():
        parts = []
        finish_reason = timings = None
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
                    yield {"error": "llama-server error" + (f": {detail}" if detail else "")}
                    return
                progress = chunk.get("prompt_progress")
                if isinstance(progress, dict):
                    try:
                        cache = int(progress.get("cache", 0))
                        yield {"progress": {"processed": int(progress["processed"]) - cache,
                                            "total": int(progress["total"]) - cache}}
                    except (KeyError, TypeError, ValueError):
                        pass
                try:
                    delta = chunk["choices"][0]["delta"].get("content")
                except (KeyError, IndexError, TypeError, AttributeError):
                    delta = None
                if isinstance(delta, str) and delta:
                    parts.append(delta)
                    yield {"delta": delta}
                try:
                    finish_reason = chunk["choices"][0].get("finish_reason") or finish_reason
                except (KeyError, IndexError, TypeError, AttributeError):
                    pass
                if isinstance(chunk.get("timings"), dict):
                    timings = chunk["timings"]
            else:
                yield {"error": "llama-server closed the connection before the end"}
                return
        except (OSError, http.client.HTTPException) as e:
            yield {"error": f"lost connection to llama-server ({LLAMA_URL}): {e}"}
            return
        finally:
            conn.close()
        if finish_reason == "length":
            yield {"error": str(_cut_off(text, timings))}
            return
        yield {"translation": "".join(parts).strip()}

    return events()


def _json_response(start_response, status, obj):
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    start_response(status, [
        ("Content-Type", "application/json; charset=utf-8"),
        ("Content-Length", str(len(body))),
        ("Cache-Control", "no-store"),
    ])
    return [body]


def _read_body(environ):
    try:
        length = int(environ.get("CONTENT_LENGTH") or 0)
    except ValueError:
        raise BadRequest("invalid Content-Length")
    if length <= 0:
        raise BadRequest("request body is empty")
    if length > MAX_BODY_BYTES:
        raise BadRequest(f"request body is too large (max {MAX_BODY_BYTES} bytes)")
    data = environ["wsgi.input"].read(length)
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise BadRequest("request body must be valid JSON")


def api_translate(environ, start_response):
    if environ.get("REQUEST_METHOD") != "POST":
        start_response("405 Method Not Allowed", [("Allow", "POST"), ("Content-Type", "application/json")])
        return [b'{"error": "method not allowed"}']
    content_type = environ.get("CONTENT_TYPE", "").split(";")[0].strip().lower()
    try:
        if content_type != "application/json":
            raise BadRequest("Content-Type must be application/json")
        req = _read_body(environ)
        stream = isinstance(req, dict) and req.pop("stream", False)
        if not isinstance(stream, bool):
            raise BadRequest("stream must be true or false")
        text, kwargs = validate_request(req)
        if stream:
            events = translate_stream(text, kwargs)
        else:
            translation = translate(text, kwargs)
    except BadRequest as e:
        return _json_response(start_response, "400 Bad Request", {"error": str(e)})
    except UpstreamError as e:
        return _json_response(start_response, "502 Bad Gateway", {"error": str(e)})
    if not stream:
        return _json_response(start_response, "200 OK",
                              {"translation": translation, "target_lang": kwargs["target_lang"]})
    start_response("200 OK", [
        ("Content-Type", "application/x-ndjson; charset=utf-8"),
        ("Cache-Control", "no-store"),
        ("X-Accel-Buffering", "no"),  # let nginx pass each line on at once
    ])
    return _ndjson_lines(events, kwargs["target_lang"])


def _ndjson_lines(events, target_lang):
    try:
        yield _ndjson({"target_lang": target_lang})
        for event in events:
            yield _ndjson(event)
    finally:
        events.close()  # the browser went away: drop the llama-server connection too


def _ndjson(obj):
    return (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")


_model_label = None


def model_label():
    """Model size and quantization from llama-server's /props, e.g. "0.8B, Q4_K_M"."""
    global _model_label
    if _model_label:
        return _model_label
    headers = {"Authorization": "Bearer " + LLAMA_API_KEY} if LLAMA_API_KEY else {}
    try:
        conn, prefix = llama_connection()
        conn.timeout = 3  # do not delay the page when llama-server is busy or down
        try:
            conn.request("GET", prefix + "/props", headers=headers)
            response = conn.getresponse()
            data = response.read()
        finally:
            conn.close()
        if response.status != 200:
            return "unknown"
        name = os.path.basename(str(json.loads(data).get("model_path", "")))
    except (OSError, http.client.HTTPException, ValueError, AttributeError, UpstreamError):
        return "unknown"
    m = re.fullmatch(r"plamo-3-translate-([0-9.]+)b-(.+)\.gguf", name, re.IGNORECASE)
    _model_label = f"{m.group(1)}B, {m.group(2)}" if m else (name or "unknown")
    return _model_label


def index(environ, start_response):
    if environ.get("REQUEST_METHOD") not in ("GET", "HEAD"):
        start_response("405 Method Not Allowed", [("Allow", "GET, HEAD"), ("Content-Type", "text/plain")])
        return [b"method not allowed\n"]
    options = "\n".join(
        f'<option value="{code}">{name} ({code})</option>' for code, name in LANGUAGES
    )
    body = (INDEX_HTML.replace("@@LANGUAGE_OPTIONS@@", options)
            .replace("@@MODEL@@", html.escape(model_label()))).encode("utf-8")
    start_response("200 OK", [
        ("Content-Type", "text/html; charset=utf-8"),
        ("Content-Length", str(len(body))),
        ("X-Content-Type-Options", "nosniff"),
        ("Content-Security-Policy",
         "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'"),
    ])
    return [] if environ.get("REQUEST_METHOD") == "HEAD" else [body]


def application(environ, start_response):
    path = environ.get("PATH_INFO", "/") or "/"
    if path == "/":
        return index(environ, start_response)
    if path == "/api/translate":
        return api_translate(environ, start_response)
    start_response("404 Not Found", [("Content-Type", "text/plain")])
    return [b"not found\n"]


INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PLaMo 3 Translate</title>
<style>
  :root { color-scheme: light dark; --fg: #1d1d1f; --bg: #fafafa; --muted: #666;
          --border: #ccc; --card: #fff; --accent: #2f5fd0; --err: #b00020; }
  @media (prefers-color-scheme: dark) {
    :root { --fg: #e8e8e8; --bg: #161618; --muted: #9a9a9a; --border: #444;
            --card: #1f1f22; --accent: #7aa2ff; --err: #ff6b81; }
  }
  * { box-sizing: border-box; }
  body { margin: 0; padding: 1.5rem 1rem; font: 15px/1.5 system-ui, sans-serif;
         color: var(--fg); background: var(--bg); }
  /* Leave room on both sides for the 150px side buttons (and the .5rem gaps), so that
     the textareas are exactly as wide as the language selectors above them. */
  main { width: min(72rem, calc(100% - 300px - 1rem)); margin: 0 auto; }
  header.site { display: flex; flex-wrap: wrap; align-items: baseline; gap: .25rem 1rem; margin: 0 0 1rem; }
  header.site h1 { font-size: 1.4rem; margin: 0; }
  header.site .model { margin: 0; color: var(--muted); font-size: .9rem; }
  .row { display: flex; flex-wrap: wrap; gap: 1rem; }
  .row > * { flex: 1 1 18rem; min-width: 0; }
  label { display: block; font-weight: 600; margin: .75rem 0 .25rem; }
  .hint { font-weight: normal; color: var(--muted); font-size: .85rem; }
  textarea, select, input { width: 100%; font: inherit; color: inherit; background: var(--card);
         border: 1px solid var(--border); border-radius: 6px; padding: .5rem; }
  textarea { resize: vertical; }
  #result { white-space: pre-wrap; }
  /* Source text and Translation, surrounded by four Translate buttons.  The middle
     column is as wide as main (the side columns extend into its margins); the middle
     row follows the textarea height, so resizing a textarea resizes the side buttons. */
  .translator { display: grid; grid-template-columns: 150px 1fr 150px;
                grid-template-rows: 150px auto 150px; gap: .5rem;
                margin: 1rem calc(-150px - .5rem) 0; }
  .pair { grid-column: 2; grid-row: 2; display: flex; gap: 1rem; align-items: flex-start; }
  .pair .pane { flex: 1 1 0; }
  .go-top { grid-column: 2; grid-row: 1; }
  .go-left { grid-column: 1; grid-row: 2; }
  .go-right { grid-column: 3; grid-row: 2; }
  .go-bottom { grid-column: 2; grid-row: 3; }
  .pane { position: relative; background: var(--card); border-radius: 6px; }
  .pane textarea { position: relative; z-index: 1; display: block; background: transparent;
                   resize: vertical; width: 100%; height: 600px; min-height: 8rem; }
  /* Titles drawn faintly behind the text. */
  .pane .watermark { position: absolute; inset: 0; z-index: 0; margin: 0;
                     display: flex; align-items: center; justify-content: center;
                     font-size: 2.5rem; font-weight: 700; color: var(--fg); opacity: .12;
                     pointer-events: none; user-select: none; text-align: center; }
  /* Copy buttons in the bottom-right corner of each textarea (left of the scrollbar
     and the resize handle). */
  .pane .copy { position: absolute; bottom: .4rem; right: 1.4rem; z-index: 2;
                font-size: .8rem; padding: .15rem .6rem; opacity: .75; color: var(--accent);
                background: color-mix(in srgb, var(--accent) 12%, var(--card));
                border: 1px solid color-mix(in srgb, var(--accent) 35%, var(--card)); }
  .pane .copy:hover, .pane .copy:focus-visible { opacity: 1; }
  details { margin-top: .75rem; }
  summary { cursor: pointer; font-weight: 600; }
  button { font: inherit; padding: .5rem 1.5rem; border: 0; border-radius: 6px;
           background: var(--accent); color: #fff; cursor: pointer; }
  /* Translate buttons: a pale tint of the accent colour, close to the surroundings. */
  .go { font-size: 1.25rem; font-weight: 600; padding: .5rem; color: var(--accent);
        background: color-mix(in srgb, var(--accent) 16%, var(--card));
        border: 1px solid color-mix(in srgb, var(--accent) 35%, var(--card)); }
  .go:hover:not(:disabled) { background: color-mix(in srgb, var(--accent) 26%, var(--card)); }
  /* Phones: one column -- selectors, Source text, a 50px Translate/Clear button,
     Translation, then the rest, and the title small at the bottom.  The script sizes the
     textareas so that the first screen ends with the button and, with the button
     scrolled to the top, the second screen ends with the title. */
  #status { display: block; min-height: 1.5em; margin-top: .75rem; text-align: center; color: var(--muted); }
  #status.error { color: var(--err); }
  @media (max-width: 760px) {
    body { padding: .5rem .75rem .75rem; }
    main { width: auto; display: flex; flex-direction: column; }
    header.site { order: 2; justify-content: center; margin: 1rem 0 0; gap: 0 .75rem; }
    header.site h1 { font-size: .9rem; }
    header.site .model { font-size: .8rem; }
    .row { gap: 0; }
    .row > * { flex-basis: 100%; }
    label { margin: .35rem 0 .15rem; }
    .translator { display: flex; flex-direction: column; gap: .5rem; margin: .5rem 0 0; }
    .pair { display: contents; }
    .pair .pane { flex: none; }
    .pane-src { order: 1; }
    .go-bottom { order: 2; height: 50px; }
    .pane-dst { order: 3; }
    .go-top, .go-left, .go-right { display: none; }
    .pane textarea { height: 40vh; resize: none; }
    .pane .watermark { font-size: 1.8rem; }
    /* No status line (its height goes to Translation): progress is shown on the
       button, and only errors are shown here. */
    #status { display: none; }
    #status.error { display: block; }
  }
  button:disabled { opacity: .5; cursor: progress; }
</style>
</head>
<body>
<main>
<header class="site">
  <h1>PLaMo 3 Translate</h1>
  <p class="model">Model: @@MODEL@@</p>
</header>
<form id="f">
  <div class="row">
    <div>
      <label for="source_lang">Source language</label>
      <select id="source_lang"><option value="">Auto-detect (Japanese preferred)</option>
@@LANGUAGE_OPTIONS@@
      </select>
    </div>
    <div>
      <label for="target_lang">Target language</label>
      <select id="target_lang">
        <option value="auto">Auto: Japanese &rarr; English, others &rarr; Japanese</option>
@@LANGUAGE_OPTIONS@@
      </select>
    </div>
    <div>
      <label for="form">Japanese style <span class="hint">(when the result is Japanese)</span></label>
      <select id="form"><option value="">Default</option>
        <option value="polite">Polite (desu/masu)</option>
        <option value="plain">Plain (da/dearu)</option>
      </select>
    </div>
  </div>
  <div class="translator">
    <button class="go go-top" type="submit">Translate</button>
    <button class="go go-left" type="submit">Translate</button>
    <div class="pair">
      <div class="pane pane-src">
        <label class="watermark" for="text">Source text</label>
        <textarea id="text" required></textarea>
        <button class="copy" type="button" data-target="text" aria-label="Copy the source text">Copy</button>
      </div>
      <div class="pane pane-dst">
        <label class="watermark" for="result">Translation</label>
        <textarea id="result" readonly></textarea>
        <button class="copy" type="button" data-target="result" aria-label="Copy the translation">Copy</button>
      </div>
    </div>
    <button class="go go-right" type="submit">Translate</button>
    <button class="go go-bottom" type="submit">Translate</button>
  </div>
  <span id="status" role="status"></span>
  <details>
    <summary>Glossary and context</summary>
    <div class="row">
      <div>
        <label for="glossary">Glossary <span class="hint">(one term per line: source =&gt; target)</span></label>
        <textarea id="glossary" rows="5" placeholder="管理画面 =&gt; dashboard"></textarea>
      </div>
      <div>
        <label for="context">Context <span class="hint">(surrounding text or situation; not translated)</span></label>
        <textarea id="context" rows="5"></textarea>
      </div>
    </div>
  </details>
</form>
</main>
<script>
(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };
  var buttons = document.querySelectorAll(".go");
  function setBusy(busy) { buttons.forEach(function (b) { b.disabled = busy; }); }

  // Phone layout: the bottom button is the single Translate/Clear button.
  var mobile = window.matchMedia("(max-width: 760px)");
  var mainBtn = document.querySelector(".go-bottom");
  var clearMode = false;
  function setClearMode(on) {
    clearMode = on && mobile.matches;
    mainBtn.textContent = clearMode ? "Clear" : "Translate";
  }
  function pageTop(el) { return el.getBoundingClientRect().top + window.scrollY; }
  var laidOutWidth = 0;
  function layout(force) {
    var text = $("text"), result = $("result");
    if (!mobile.matches) {
      text.style.height = ""; result.style.height = ""; laidOutWidth = 0;
      setClearMode(false);
      return;
    }
    // Only when the width changes (rotation): the height also changes when the address
    // bar or the keyboard appears, and re-laying out then would make the page jump.
    if (!force && window.innerWidth === laidOutWidth) return;
    laidOutWidth = window.innerWidth;
    var vh = window.innerHeight;
    var gap = parseFloat(getComputedStyle(mainBtn.parentNode).rowGap) || 0;
    var btnH = mainBtn.offsetHeight;
    // First screen: selectors, Source text and the button.
    text.style.height = Math.max(120, vh - pageTop(text) - gap - btnH) + "px";
    // Second screen (button at the top): button, Translation and everything after it.
    var pageEnd = pageTop(document.querySelector("main")) + document.querySelector("main").offsetHeight
                  + parseFloat(getComputedStyle(document.body).paddingBottom);
    var tail = pageEnd - (pageTop(result) + result.offsetHeight);
    result.style.height = Math.max(120, vh - btnH - gap - tail) + "px";
  }
  window.addEventListener("resize", function () { layout(false); });
  if (mobile.addEventListener) mobile.addEventListener("change", function () { layout(true); });
  window.addEventListener("load", function () { layout(true); });
  layout(true);

  mainBtn.addEventListener("click", function (ev) {
    if (!clearMode) return;
    ev.preventDefault();
    $("text").value = ""; $("result").value = "";
    $("status").textContent = ""; $("status").className = "";
    setClearMode(false);
    window.scrollTo({ top: 0, behavior: "smooth" });
  });
  $("text").addEventListener("input", function () { if (clearMode) setClearMode(false); });
  var KEY = "plamo-3-translate-settings";
  var fields = ["source_lang", "target_lang", "form"];
  try {
    var saved = JSON.parse(localStorage.getItem(KEY) || "{}");
    fields.forEach(function (k) { if (saved[k] !== undefined) $(k).value = saved[k]; });
  } catch (e) {}
  if (!$("target_lang").value) $("target_lang").value = "auto";
  // The style applies when the result is Japanese (with Auto: for non-Japanese source text).
  function syncForm() { var t = $("target_lang").value; $("form").disabled = t !== "ja" && t !== "auto"; }
  $("target_lang").addEventListener("change", syncForm);
  syncForm();

  // Copy a textarea to the clipboard.  navigator.clipboard needs a secure context
  // (HTTPS or localhost); over plain HTTP fall back to selecting the text and execCommand.
  function copyText(area) {
    var text = area.value;
    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(text);
    }
    return new Promise(function (resolve, reject) {
      var start = area.selectionStart, end = area.selectionEnd;
      area.focus();
      area.select();
      var ok = false;
      try { ok = document.execCommand("copy"); } catch (e) {}
      area.setSelectionRange(start, end);
      if (ok) { resolve(); } else { reject(new Error("copy failed")); }
    });
  }
  document.querySelectorAll(".copy").forEach(function (btn) {
    var timer = null;
    function flash(label) {
      btn.textContent = label;
      clearTimeout(timer);
      timer = setTimeout(function () { btn.textContent = "Copy"; }, 1500);
    }
    btn.addEventListener("click", function () {
      var area = $(btn.dataset.target);
      if (!area.value) { flash("Empty"); return; }
      copyText(area).then(function () { flash("Copied"); },
                          function () { flash("Failed"); });
    });
  });

  function parseGlossary(src) {
    var out = [];
    src.split("\\n").forEach(function (line, i) {
      if (!line.trim()) return;
      var p = line.split("=>");
      if (p.length !== 2 || !p[0].trim() || !p[1].trim())
        throw new Error("Glossary line " + (i + 1) + " must be: source => target");
      out.push({ source: p[0].trim(), target: p[1].trim() });
    });
    return out;
  }

  $("f").addEventListener("submit", function (ev) {
    ev.preventDefault();
    var status = $("status");
    status.className = "";
    var req = { text: $("text").value, target_lang: $("target_lang").value };
    if ($("source_lang").value) req.source_lang = $("source_lang").value;
    if (!$("form").disabled && $("form").value) req.form = $("form").value;
    if ($("context").value.trim()) req.context = $("context").value;
    try {
      var g = parseGlossary($("glossary").value);
      if (g.length) req.glossary = g;
    } catch (e) { status.className = "error"; status.textContent = e.message; return; }
    try {
      var s = {}; fields.forEach(function (k) { s[k] = $(k).value; });
      localStorage.setItem(KEY, JSON.stringify(s));
    } catch (e) {}
    setBusy(true);
    status.textContent = "Translating...";
    if (mobile.matches) {
      mainBtn.textContent = "Translating...";
      window.scrollTo({ top: pageTop(mainBtn), behavior: "smooth" });
    }
    var t0 = Date.now();
    var result = $("result");
    result.value = "";
    req.stream = true;
    function elapsed() { return ((Date.now() - t0) / 1000).toFixed(1) + " s"; }
    // Pieces of the translation are added to the textarea once per frame, keeping it
    // scrolled to the end unless the user has scrolled up.
    var pending = "", frame = 0;
    function flush() {
      frame = 0;
      if (!pending) return;
      var atEnd = result.scrollTop + result.clientHeight >= result.scrollHeight - 8;
      result.value += pending; pending = "";
      if (atEnd) result.scrollTop = result.scrollHeight;
    }
    var chars = 0;
    function handle(ev, d) {
      if (ev.target_lang) d.target_lang = ev.target_lang;
      if (ev.progress && ev.progress.total > 0) {
        var pct = Math.floor(100 * ev.progress.processed / ev.progress.total) + "%";
        status.textContent = "Reading the source text... " + pct + " (" + elapsed() + ")";
        if (mobile.matches) mainBtn.textContent = "Reading... " + pct;
      }
      if (typeof ev.delta === "string") {
        pending += ev.delta; chars += ev.delta.length;
        if (!frame) frame = requestAnimationFrame(flush);
        status.textContent = "Translating... " + chars + " characters (" + elapsed() + ")";
        if (mobile.matches) mainBtn.textContent = "Translating...";
      }
      if (typeof ev.translation === "string") d.translation = ev.translation;
      if (ev.error) d.error = ev.error;
    }
    // Read the NDJSON stream; resolves to {target_lang, translation} or {error}.
    function readStream(r) {
      var reader = r.body.getReader(), decoder = new TextDecoder(), buf = "", d = {};
      function line(s) {
        if (!s.trim()) return;
        var ev;
        try { ev = JSON.parse(s); } catch (e) { d.error = "invalid response from the server"; return; }
        handle(ev, d);
      }
      function pump() {
        return reader.read().then(function (x) {
          if (!x.done) buf += decoder.decode(x.value, { stream: true });
          var lines = buf.split("\\n");
          buf = x.done ? "" : lines.pop();
          lines.forEach(line);
          if (x.done || d.error) {
            if (!x.done) reader.cancel().catch(function () {});
            if (frame) { cancelAnimationFrame(frame); }
            flush();
            if (!d.error && typeof d.translation !== "string")
              d.error = "the connection was closed before the translation finished";
            return d;
          }
          return pump();
        });
      }
      return pump();
    }
    fetch("api/translate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(req)
    }).then(function (r) {
      var ct = r.headers.get("Content-Type") || "";
      if (r.ok && r.body && ct.indexOf("application/x-ndjson") === 0) return readStream(r);
      return r.json().catch(function () { return { error: "HTTP " + r.status }; });
    }).then(function (d) {
      if (d.error) throw new Error(d.error);
      if (result.value !== d.translation) result.value = d.translation;
      var opt = $("target_lang").querySelector('option[value="' + d.target_lang + '"]');
      status.textContent = "Done in " + ((Date.now() - t0) / 1000).toFixed(1) + " s" +
        (req.target_lang === "auto" && opt ? " (\u2192 " + opt.textContent.replace(/ \\(.*\\)$/, "") + ")" : "");
      setClearMode(true);
    }).catch(function (e) {
      status.className = "error";
      status.textContent = "Error: " + e.message;
      setClearMode(false);
    }).finally(function () { setBusy(false); });
  });
})();
</script>
</body>
</html>
"""


def main(argv=None):
    """Stand-alone server (no uWSGI): see the module docstring."""
    import argparse
    import socketserver
    from wsgiref.simple_server import WSGIRequestHandler, WSGIServer

    def port_number(value):
        port = int(value)
        if not 0 <= port <= 65535:
            raise ValueError(value)
        return port
    port_number.__name__ = "port"  # for argparse's "invalid port value" message

    parser = argparse.ArgumentParser(
        description="PLaMo 3 Translate web server (stand-alone mode, without uWSGI).")
    parser.add_argument("--host", default="127.0.0.1", metavar="ADDR",
                        help="address to listen on (default: %(default)s; 0.0.0.0 for all)")
    parser.add_argument("--port", type=port_number, metavar="PORT",
                        help="port to listen on (default: 8080)")
    parser.add_argument("positional_port", nargs="?", type=port_number, metavar="PORT",
                        help="the same as --port PORT")
    args = parser.parse_args(argv)
    if args.port is not None and args.positional_port is not None:
        parser.error("PORT given twice (--port PORT and PORT)")
    port = next((p for p in (args.port, args.positional_port) if p is not None), 8080)

    # A console with a legacy code page (e.g. cp932) cannot show every character:
    # escape instead of raising UnicodeEncodeError.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="backslashreplace")
        except (AttributeError, ValueError):
            pass

    class Server(socketserver.ThreadingMixIn, WSGIServer):
        daemon_threads = True  # do not wait for running translations on exit
        # On Windows SO_REUSEADDR would let this process listen on a port that another
        # process is already listening on; take the port exclusively there instead.
        allow_reuse_address = sys.platform != "win32"

        def server_bind(self):
            if sys.platform == "win32":
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            socketserver.TCPServer.server_bind(self)
            # Not WSGIServer.server_bind: its socket.getfqdn() can take seconds.
            self.server_name, self.server_port = self.server_address[:2]
            self.setup_environ()

        def handle_error(self, request, client_address):
            if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
                return  # the client went away: nothing to report
            super().handle_error(request, client_address)

    try:
        httpd = Server((args.host, port), WSGIRequestHandler)
    except OSError as e:
        print(f"plamo-3-translate-server: cannot listen on {args.host}:{port}: {e}",
              file=sys.stderr, flush=True)
        return 1
    try:
        with httpd:
            httpd.set_app(application)
            host, port = httpd.server_address[:2]
            print(f"Serving on http://{host}:{port}/ (llama-server: {LLAMA_URL})", flush=True)
            httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
