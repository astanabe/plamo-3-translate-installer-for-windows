"""Tests of plamo-3-translate-installer.py (run on Linux).

Everything that needs Windows (access rights, shortcuts, elevation, the embeddable
Python) is replaced here, so these tests cover the logic of the installer, not those
operations themselves."""

import contextlib
import hashlib
import io
import json
import os
import shutil
import ssl
import stat
import sys
import tempfile
import unittest
import urllib.error
import zipfile
from unittest import mock

from support import (BAT, CLIENT, INSTALLER, SERVER, STUB, FileServer, free_port, load_module, port_open,
                     stub_exe_text, wait_until)

ins = load_module("plamo3_installer", INSTALLER)

# The files copied unchanged from plamo-3-translate-installer-for-resolute, commit 3902612
# (git blob ids).  Update them together with CLAUDE.md when new copies are taken.
RESOLUTE_BLOBS = {
    SERVER: "3a7dfa6f715875e31c9397bbb228b9fb5b51d86b",
    CLIENT: "40b0298ea60ba077518e3e40da05185a2e8df285",
}


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def make_installer(*argv, environ=None, interactive=False):
    return ins.Installer(ins.parse_args(list(argv)), environ=environ or {}, interactive=interactive)


def quiet(function, *args, **kwargs):
    """Call function; returns (result or exception, standard output, standard error)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            result = function(*args, **kwargs)
        except ins.Die as e:
            result = e
    return result, out.getvalue(), err.getvalue()


class TempCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name

    def patch(self, name, value):
        patcher = mock.patch.object(ins, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def serve(self, files):
        server = FileServer(files)
        self.addCleanup(server.close)
        return server


class PinnedFiles(unittest.TestCase):
    def test_bat_is_ascii_with_crlf(self):
        with open(BAT, "rb") as f:
            data = f.read()
        self.assertTrue(data.isascii())
        self.assertNotIn(b"\n", data.replace(b"\r\n", b""))
        self.assertNotIn(b"\r", data.replace(b"\r\n", b""))

    def test_bat_and_installer_pin_the_same_python(self):
        with open(BAT, encoding="ascii", newline="") as f:
            text = f.read()
        self.assertIn(f'set "PY_VERSION={ins.PYTHON_VERSION}"\r\n', text)
        self.assertIn(f'set "PY_SHA256={ins.PYTHON_SHA256}"\r\n', text)
        self.assertIn('set "PY_ZIP=python-%PY_VERSION%-embed-amd64.zip"', text)
        self.assertEqual(ins.PYTHON_URL,
                         f"https://www.python.org/ftp/python/{ins.PYTHON_VERSION}/{ins.PYTHON_ZIP}")

    def test_windows_check_bat(self):
        path = os.path.join(os.path.dirname(BAT), "tests", "windows-check.bat")
        with open(path, "rb") as f:
            data = f.read()
        self.assertTrue(data.isascii())
        self.assertNotIn(b"\n", data.replace(b"\r\n", b""))
        self.assertIn(f"python-{ins.PYTHON_VERSION}\\python.exe".encode(), data)

    def test_copies_from_resolute_are_unchanged(self):
        for path, blob in RESOLUTE_BLOBS.items():
            with open(path, "rb") as f:
                data = f.read()
            self.assertEqual(hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest(), blob, path)

    def test_tables(self):
        self.assertEqual(set(ins.LLAMA_ASSETS), set(ins.BACKENDS))
        for assets in ins.LLAMA_ASSETS.values():
            for name, digest, size in assets:
                self.assertRegex(name, r"^(llama-b12366|cudart-llama)-bin-win-.*-x64\.zip$")
                self.assertRegex(digest, r"^[0-9a-f]{64}$")
                self.assertGreater(size, 0)
        for (model, quant), (digest, size) in ins.GGUF.items():
            self.assertIn(model, ins.MODEL_REVISION)
            self.assertIn(quant, ins.QUANTS)
            self.assertRegex(digest, r"^[0-9a-f]{64}$")
        self.assertEqual(ins.gguf_url("2b", "Q4_K_M"),
                         "https://huggingface.co/pfnet/plamo-3-translate-2b-GGUF/resolve/"
                         "4f672106b847200bfdc0f6af6c321cd94eba1aed/plamo-3-translate-2b-Q4_K_M.gguf")

    def test_generated_batch_files_are_ascii(self):
        for text in (ins.client_bat("28080"), ins.prompt_bat()):
            self.assertTrue(text.isascii())
        self.assertIn('"%~dp0python\\python.exe" "%~dp0app\\plamo-3-translate-client.py" %*', ins.client_bat("28080"))
        self.assertIn("http://127.0.0.1:28081", ins.client_bat("28081"))


class Destdir(unittest.TestCase):
    def test_accepted(self):
        self.assertEqual(ins.validate_destdir(r"C:\LLM\PLaMo3Translate"), r"C:\LLM\PLaMo3Translate")
        self.assertEqual(ins.validate_destdir('"d:/apps/PLaMo 3\\"'), r"D:\apps\PLaMo 3")
        self.assertEqual(ins.validate_destdir(r"C:\Program Files\PLaMo3Translate"), r"C:\Program Files\PLaMo3Translate")

    def test_rejected(self):
        for value in ("PLaMo", r"\LLM\x", "C:", "C:\\", r"C:\Windows", r"C:\WINDOWS\Temp\x", r"C:\Users",
                      r"C:\Program Files", r"C:\ProgramData", r"C:\a\..\b", r"C:\a\.", r"C:\a&b", r"C:\a%b%",
                      "C:\\翻訳", r"C:\a\ b", r"C:\a.\b", r"\\server\share\x", r"C:\a\\b"):
            with self.assertRaises(ins.Die, msg=value):
                ins.validate_destdir(value)


class Values(TempCase):
    def setUp(self):
        super().setUp()
        self.patch("validate_destdir", os.path.abspath)
        self.patch("detect_backend", lambda: "cpu")

    def resolve(self, *argv, environ=None, saved=None):
        destdir = os.path.join(self.dir, "dest")
        if saved is not None:
            os.makedirs(destdir, exist_ok=True)
            with open(os.path.join(destdir, "install.json"), "w", encoding="utf-8") as f:
                json.dump(saved, f)
        inst = make_installer("--destdir", destdir, *argv, environ=environ)
        inst.resolve_destdir()
        inst.load_saved()
        inst.resolve_values()
        inst.validate()
        return inst

    def test_defaults(self):
        inst = self.resolve()
        self.assertEqual({k: v for k, v in inst.val.items() if k != "destdir"},
                         {"model": "2b", "quant": "Q4_K_M", "backend": "cpu", "gpus": "", "ctx_size": "auto",
                          "web_port": "18080", "llama_port": "28080", "listen": "no", "expose_llama": "no"})

    def test_option_beats_environment_beats_saved_beats_default(self):
        saved = {"model": "8b", "quant": "Q2_K", "web_port": 18081, "llama_port": 28081, "ctx_size": "8192",
                 "listen": "yes", "expose_llama": "yes", "backend": "vulkan", "gpus": "Vulkan0"}
        environ = {"PLAMO3_MODEL": "0.8b", "PLAMO3_QUANT": "Q4_0", "PLAMO3_WEB_PORT": "18082",
                   "PLAMO3_EXPOSE_LLAMA": "no"}
        inst = self.resolve("--model", "2b", "--no-listen", environ=environ, saved=saved)
        self.assertEqual(inst.val["model"], "2b")          # option
        self.assertEqual(inst.val["quant"], "Q4_0")        # environment
        self.assertEqual(inst.val["web_port"], "18082")    # environment
        self.assertEqual(inst.val["llama_port"], "28081")  # saved
        self.assertEqual(inst.val["ctx_size"], "8192")     # saved
        self.assertEqual(inst.val["backend"], "vulkan")    # saved
        self.assertEqual(inst.val["gpus"], "Vulkan0")      # saved
        self.assertEqual(inst.val["listen"], "no")         # option over saved
        self.assertEqual(inst.val["expose_llama"], "no")   # environment over saved
        self.assertEqual(inst.from_user, {"destdir", "model", "quant", "web_port", "listen", "expose_llama"})

    def test_equals_form_and_normalization(self):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            inst = self.resolve("--model=0.8B", "--quant=q4_0", "--ctx-size=04096", "--web-port=08080",
                                "--backend=CPU", "--gpus", "Vulkan0")
        self.assertIn("--gpus is ignored with the cpu backend", err.getvalue())
        self.assertEqual((inst.val["model"], inst.val["quant"], inst.val["ctx_size"], inst.val["web_port"],
                          inst.val["backend"], inst.val["gpus"]), ("0.8b", "Q4_0", "4096", "8080", "cpu", ""))

    def test_invalid_values(self):
        for argv, text in (
                (("--model", "8b", "--quant", "Q4_0"), "only for the 0.8b and 2b"),
                (("--model", "31b", "--quant", "Q4_0"), "only for the 0.8b and 2b"),
                (("--model", "4b"), "model must be one of"),
                (("--quant", "Q8_0"), "quantization must be one of"),
                (("--backend", "rocm"), "backend must be one of"),
                (("--ctx-size", "511"), "between 512 and"),
                (("--ctx-size", "262145"), "between 512 and"),
                (("--ctx-size", "big"), "auto or an integer"),
                (("--web-port", "0"), "invalid port number"),
                (("--llama-port", "65536"), "invalid port number"),
                (("--web-port", "28080"), "ports must differ"),
                (("--backend", "vulkan", "--gpus", "Vulkan0;x"), "invalid --gpus")):
            with self.assertRaises(ins.Die, msg=argv) as caught:
                self.resolve(*argv)
            self.assertIn(text, str(caught.exception))
        with self.assertRaises(ins.Die) as caught:
            self.resolve(environ={"PLAMO3_LISTEN": "maybe"})
        self.assertIn("use yes or no", str(caught.exception))

    def test_unknown_option(self):
        with self.assertRaises(SystemExit) as caught, contextlib.redirect_stderr(io.StringIO()) as err:
            ins.parse_args(["--user", "x"])
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("Usage:", err.getvalue())
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            ins.parse_args(["--mod", "2b"])  # no abbreviations

    def test_interactive_answers(self):
        destdir = os.path.join(self.dir, "asked")
        answers = iter([destdir, "0.8b", "", "", "8192", "", "28090", "yes", "no"])
        self.patch("gpu_vendors", lambda: [])
        inst = make_installer(interactive=True)
        with mock.patch("builtins.input", lambda prompt: next(answers)):
            inst.resolve_destdir()
            inst.load_saved()
            inst.resolve_values()
            quiet(inst.prompt_values)
            inst.validate()
        self.assertEqual(inst.val, {"destdir": destdir, "model": "0.8b", "quant": "Q4_K_M", "backend": "cpu",
                                    "gpus": "", "ctx_size": "8192", "web_port": "18080", "llama_port": "28090",
                                    "listen": "yes", "expose_llama": "no"})

    def test_values_given_are_not_asked(self):
        inst = self.resolve("--model", "0.8b", "--quant", "Q4_0", "--backend", "cpu", "--ctx-size", "auto",
                            "--web-port", "1", "--llama-port", "2", "--listen", "--expose-llama")
        inst.interactive = True
        with mock.patch("builtins.input", side_effect=AssertionError("asked")):
            inst.prompt_values()


class License(unittest.TestCase):
    def test_non_interactive_needs_accept_license(self):
        result, out, err = quiet(make_installer("--yes").confirm_license)
        self.assertIsInstance(result, ins.Die)
        self.assertIn("--accept-license", str(result))

    def test_accept_license_option_and_environment(self):
        for inst in (make_installer("--accept-license"),
                     make_installer(environ={"PLAMO3_ACCEPT_LICENSE": "yes"})):
            result, out, err = quiet(inst.confirm_license)
            self.assertIsNone(result)
            self.assertIn("accepted", out)

    def test_interactive_needs_the_word_yes(self):
        for answer, accepted in (("yes", True), ("y", False), ("YES", False), ("", False)):
            inst = make_installer("--yes", interactive=True)
            with mock.patch("builtins.input", lambda prompt: answer):
                result, out, err = quiet(inst.confirm_license)
            self.assertIn("PLaMo Community License", out)
            self.assertIn(ins.LICENSE_URL, out)
            self.assertEqual(result is None, accepted, answer)

    def test_install_stops_without_license_before_touching_anything(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(ins, "check_platform", lambda: None), \
                mock.patch.object(ins, "validate_destdir", os.path.abspath), \
                mock.patch.object(ins, "detect_backend", lambda: "cpu"):
            destdir = os.path.join(tmp, "dest")
            result, out, err = quiet(make_installer("--destdir", destdir, "--yes").install)
            self.assertIsInstance(result, ins.Die)
            self.assertFalse(os.path.exists(destdir))


class Download(TempCase):
    def setUp(self):
        super().setUp()
        self.patch("DOWNLOAD_RETRY_DELAY", 0)
        self.data = os.urandom(300000)
        self.server = self.serve({"/file.bin": self.data})
        self.url = self.server.url + "/file.bin"
        self.dest = os.path.join(self.dir, "file.bin")

    def download(self, data=None, url=None):
        data = self.data if data is None else data
        return quiet(ins.download_verified, url or self.url, self.dest, sha256(data), len(data))

    def read(self, path=None):
        with open(path or self.dest, "rb") as f:
            return f.read()

    def test_plain(self):
        result, out, err = self.download()
        self.assertIsNone(result)
        self.assertEqual(self.read(), self.data)
        self.assertFalse(os.path.exists(self.dest + ".part"))
        self.assertEqual(self.server.requests, [("/file.bin", None)])

    def test_reuses_verified_file(self):
        with open(self.dest, "wb") as f:
            f.write(self.data)
        result, out, err = self.download()
        self.assertIsNone(result)
        self.assertIn("Reusing file.bin", out)
        self.assertEqual(self.server.requests, [])

    def test_replaces_damaged_file(self):
        with open(self.dest, "wb") as f:
            f.write(self.data[:-1] + b"x")
        result, out, err = self.download()
        self.assertIsNone(result)
        self.assertIn("does not match", err)
        self.assertEqual(self.read(), self.data)

    def test_resumes_part_file(self):
        with open(self.dest + ".part", "wb") as f:
            f.write(self.data[:100000])
        result, out, err = self.download()
        self.assertIsNone(result)
        self.assertEqual(self.read(), self.data)
        self.assertEqual(self.server.requests, [("/file.bin", "bytes=100000-")])

    def test_complete_part_file_is_only_verified(self):
        with open(self.dest + ".part", "wb") as f:
            f.write(self.data)
        result, out, err = self.download()
        self.assertIsNone(result)
        self.assertEqual(self.server.requests, [])
        self.assertEqual(self.read(), self.data)

    def test_resumes_after_interruption(self):
        self.server.cut["/file.bin"] = 120000
        result, out, err = self.download()
        self.assertIsNone(result, err)
        self.assertIn("download interrupted", err)
        self.assertEqual(self.read(), self.data)
        self.assertEqual(len(self.server.requests), 2)
        self.assertEqual(self.server.requests[1][1], "bytes=120000-")

    def test_server_without_range_support(self):
        self.server.ignore_range = True
        with open(self.dest + ".part", "wb") as f:
            f.write(b"x" * 1000)  # would corrupt the file if it were appended to
        result, out, err = self.download()
        self.assertIsNone(result)
        self.assertEqual(self.read(), self.data)

    def test_checksum_mismatch(self):
        result, out, err = quiet(ins.download_verified, self.url, self.dest, sha256(b"other"), len(self.data))
        self.assertIsInstance(result, ins.Die)
        self.assertIn("checksum mismatch", str(result))
        self.assertFalse(os.path.exists(self.dest))
        self.assertFalse(os.path.exists(self.dest + ".part"))

    def test_too_large(self):
        result, out, err = quiet(ins.download_verified, self.url, self.dest, sha256(self.data), 1000)
        self.assertIsInstance(result, ins.Die)
        self.assertIn("larger than expected", str(result))

    def test_not_found(self):
        result, out, err = self.download(url=self.server.url + "/missing")
        self.assertIsInstance(result, ins.Die)
        self.assertIn("HTTP 404", str(result))
        self.assertEqual(len(self.server.requests), 1)  # not retried

    def test_gives_up_when_nothing_arrives(self):
        port = free_port()
        result, out, err = self.download(url=f"http://127.0.0.1:{port}/file.bin")
        self.assertIsInstance(result, ins.Die)
        self.assertIn("re-run the installer to resume", str(result))
        self.assertEqual(err.count("download interrupted"), ins.DOWNLOAD_RETRIES - 1)

    @unittest.skipUnless(shutil.which("curl"), "curl not installed")
    def test_certificate_error_falls_back_to_curl(self):
        calls = []

        def failing(url, part, have, size, progress):
            calls.append(have)
            with open(part, "ab") as f:  # some bytes arrived before, as in a resumed download
                f.write(self.data[have:have + 50000])
            raise urllib.error.URLError(ssl.SSLCertVerificationError("unable to get local issuer certificate"))

        self.patch("fetch_once", failing)
        result, out, err = self.download()
        self.assertIsNone(result, err)
        self.assertIn("using curl", err)
        self.assertEqual(calls, [0])
        self.assertEqual(self.read(), self.data)
        self.assertEqual(self.server.requests[-1], ("/file.bin", "bytes=50000-"))


class Devices(TempCase):
    LISTING = ("Available devices:\n"
               "  Vulkan0: Intel(R) UHD Graphics 770 (16203 MiB, 15000 MiB free)\n"
               "  Vulkan1: NVIDIA GeForce RTX 4070 (12282 MiB, 11000 MiB free)\n"
               "  Vulkan2: AMD Radeon (TM) RX 7900 (24000 MiB, 11000 MiB free)\n")

    def installer(self, *argv, devices=LISTING, interactive=False, saved_gpus=None):
        inst = make_installer("--backend", "vulkan", *argv, interactive=interactive)
        inst.val = {"destdir": self.dir, "backend": "vulkan", "gpus": saved_gpus or inst.args.gpus or ""}
        if inst.args.gpus:
            inst.from_user.add("gpus")
        if isinstance(devices, str):
            self.patch("list_devices", lambda exe: ins.parse_devices(devices))
        else:
            self.patch("list_devices", mock.Mock(side_effect=devices))
        return inst

    def test_parse_and_rank(self):
        devices = ins.parse_devices(self.LISTING)
        self.assertEqual(devices[1], ("Vulkan1", "NVIDIA GeForce RTX 4070", 12282, 11000))
        self.assertEqual([d[0] for d in ins.rank_devices(devices)], ["Vulkan0", "Vulkan2", "Vulkan1"])
        self.assertEqual(ins.parse_devices("Available devices:\n  (none)\n"), [])

    def test_list_devices_runs_llama_server(self):
        exe = os.path.join(self.dir, "llama-server.exe")
        with open(exe, "w", encoding="utf-8") as f:
            f.write(stub_exe_text())
        os.chmod(exe, 0o755)
        with mock.patch.dict(os.environ, {"STUB_DEVICES": "CUDA0: NVIDIA GeForce RTX 4070 (12282 MiB, 11000 MiB free)"}):
            self.assertEqual(ins.list_devices(exe), [("CUDA0", "NVIDIA GeForce RTX 4070", 12282, 11000)])

    def test_default_is_the_gpu_with_the_most_free_memory(self):
        inst = self.installer()
        quiet(inst.select_gpus)
        self.assertEqual(inst.val["gpus"], "Vulkan0")

    def test_names_given(self):
        inst = self.installer("--gpus", "vulkan2,Vulkan1,Vulkan2")
        quiet(inst.select_gpus)
        self.assertEqual(inst.val["gpus"], "Vulkan2,Vulkan1")
        inst = self.installer("--gpus", "CUDA0")
        result, out, err = quiet(inst.select_gpus)
        self.assertIn("GPU CUDA0 is not available for the vulkan backend (available: Vulkan0 Vulkan1 Vulkan2)",
                      str(result))

    def test_saved_selection_that_does_not_match(self):
        inst = self.installer(saved_gpus="CUDA0")
        result, out, err = quiet(inst.select_gpus)
        self.assertIn("does not match this machine", err)
        self.assertEqual(inst.val["gpus"], "Vulkan0")

    def test_all_is_not_checked(self):
        inst = self.installer("--gpus", "all", devices=AssertionError("listed"))
        inst.val["gpus"] = "all"
        quiet(inst.select_gpus)
        self.assertEqual(inst.val["gpus"], "all")

    def test_listing_fails(self):
        inst = self.installer(devices=OSError("cannot run"))
        result, out, err = quiet(inst.select_gpus)
        self.assertIn("cannot list the GPUs", err)
        self.assertEqual(inst.val["gpus"], "all")

    def test_no_gpu_found(self):
        inst = self.installer(devices="Available devices:\n  (none)\n")
        result, out, err = quiet(inst.select_gpus)
        self.assertIn("found no GPU", err)
        self.assertEqual(inst.val["gpus"], "all")

    def test_asked_when_two_or_more(self):
        for answers, expected in ((["3,2"], "Vulkan2,Vulkan1"), ([""], "Vulkan0"), (["x", "9", "all"], "all")):
            inst = self.installer(interactive=True)
            replies = iter(answers)
            with mock.patch("builtins.input", lambda prompt: next(replies)):
                result, out, err = quiet(inst.select_gpus)
            self.assertIn("2) Vulkan1  NVIDIA GeForce RTX 4070 (12282 MiB, 11000 MiB free)", out)
            self.assertEqual(inst.val["gpus"], expected)

    def test_cpu_backend_never_looks_at_gpus(self):
        self.patch("list_devices", mock.Mock(side_effect=AssertionError("listed")))
        inst = make_installer("--backend", "cpu")
        inst.val = {"destdir": self.dir, "backend": "cpu", "gpus": ""}
        inst.select_gpus()
        self.assertEqual(inst.val["gpus"], "")

    def test_backend_suggestion(self):
        self.assertEqual(ins.vendors_from_names(["NVIDIA GeForce RTX 4070", "Intel(R) UHD Graphics", "x"]),
                         ["intel", "nvidia"])
        cases = ((["nvidia"], 591, "cuda13"), (["nvidia"], 572, "cuda12"), (["nvidia"], None, "cpu"),
                 (["amd", "nvidia"], None, "vulkan"), (["intel"], None, "vulkan"), ([], None, "cpu"))
        for vendors, major, expected in cases:
            with mock.patch.object(ins, "gpu_vendors", lambda: vendors), \
                    mock.patch.object(ins, "nvidia_driver_major", lambda: major):
                self.assertEqual(ins.detect_backend(), expected, (vendors, major))
        with mock.patch.object(ins, "gpu_vendors", mock.Mock(side_effect=RuntimeError("boom"))):
            self.assertEqual(ins.detect_backend(), "cpu")

    def test_backend_given_skips_detection(self):
        self.patch("validate_destdir", os.path.abspath)
        self.patch("detect_backend", mock.Mock(side_effect=AssertionError("detected")))
        self.patch("gpu_vendors", mock.Mock(side_effect=AssertionError("detected")))
        inst = make_installer("--destdir", self.dir, "--backend", "cpu", interactive=True)
        inst.resolve_destdir()
        inst.resolve_values()
        with mock.patch("builtins.input", lambda prompt: ""):
            quiet(inst.prompt_values)
        self.assertEqual(inst.val["backend"], "cpu")


class WindowsCommands(unittest.TestCase):
    def test_shortcuts(self):
        destdir = r"C:\LLM\PLaMo3Translate"
        with mock.patch.dict(os.environ, {"ComSpec": r"C:\Windows\System32\cmd.exe", "SystemRoot": r"C:\Windows"}), \
                mock.patch.object(ins.os.path, "join", ins.ntpath.join):
            items = ins.shortcut_items(destdir)
            script = ins.shortcut_script(r"C:\ProgramData\Menu\PLaMo 3 Translate", items)
        self.assertEqual([item[0] for item in items],
                         ["PLaMo 3 Translate Web UI", "PLaMo 3 Translate llama-server",
                          "PLaMo 3 Translate Command Prompt", "Uninstall PLaMo 3 Translate"])
        self.assertEqual(items[0][1], r"C:\LLM\PLaMo3Translate\python\python.exe")
        self.assertEqual(items[0][2], r'"C:\LLM\PLaMo3Translate\app\plamo-3-translate-launcher.py" --mode webui')
        self.assertEqual(items[2][2], r'/k ""C:\LLM\PLaMo3Translate\command-prompt.bat""')
        self.assertEqual(items[3][2], r'"C:\LLM\PLaMo3Translate\app\plamo-3-translate-installer.py" --uninstall '
                                      r'--destdir "C:\LLM\PLaMo3Translate" --elevate --pause')
        self.assertIn(r"$s = $shell.CreateShortcut('C:\ProgramData\Menu\PLaMo 3 Translate\PLaMo 3 Translate Web UI.lnk')",
                      script)
        self.assertEqual(script.count("$s.Save()"), 4)
        self.assertEqual(ins.ps_quote("it's"), "'it''s'")

    def test_permissions(self):
        commands = ins.permission_commands(r"C:\LLM\PLaMo3Translate", recursive=False)
        self.assertEqual(commands[1], ["icacls", r"C:\LLM\PLaMo3Translate", "/inheritance:r", "/grant:r",
                                       "*S-1-5-32-544:(OI)(CI)F", "*S-1-5-18:(OI)(CI)F", "*S-1-5-32-545:(OI)(CI)RX",
                                       "/Q"])
        flat = " ".join(" ".join(c) for c in ins.permission_commands(r"C:\x", recursive=True))
        self.assertNotRegex(flat, r"S-1-5-32-545:\S*[MFW]")  # Users never get write access


class EndToEnd(TempCase):
    """Install into a temporary directory with the stub llama-server, update, uninstall."""

    def setUp(self):
        super().setUp()
        self.destdir = os.path.join(self.dir, "LLM", "PLaMo3Translate")
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("llama-server.exe", stub_exe_text())
            z.writestr("llama.dll", b"dll")
        archive = archive.getvalue()
        self.ggufs = {("0.8b", "Q4_K_M"): os.urandom(5000), ("0.8b", "Q4_0"): os.urandom(4000)}
        files = {"/llama/llama-stub.zip": archive}
        for (model, quant), data in self.ggufs.items():
            name = f"plamo-3-translate-{model}-{quant}.gguf"
            files[f"/hf/pfnet/plamo-3-translate-{model}-GGUF/resolve/{ins.MODEL_REVISION[model]}/{name}"] = data
        self.server = self.serve(files)
        self.patch("LLAMA_BASE_URL", self.server.url + "/llama")
        self.patch("HF_BASE_URL", self.server.url + "/hf")
        self.patch("LLAMA_ASSETS", {"cpu": [("llama-stub.zip", sha256(archive), len(archive))],
                                    "vulkan": [("llama-stub.zip", sha256(archive), len(archive))]})
        self.patch("GGUF", {key: (sha256(data), len(data)) for key, data in self.ggufs.items()})
        self.patch("check_platform", lambda: None)
        self.patch("windows_build", lambda: 26100)
        self.patch("validate_destdir", os.path.abspath)
        self.patch("installed_python", lambda destdir: sys.executable)
        self.permissions = []
        self.patch("set_permissions", lambda path, recursive=False: self.permissions.append((path, recursive)))
        self.scripts = []
        self.patch("run_powershell", lambda script, timeout=120: self.scripts.append(script) or "")
        self.patch("schedule_delete", mock.Mock(side_effect=AssertionError("not inside the install directory")))
        extract = ins.extract_zip

        def extract_executable(archive, directory):
            extract(archive, directory)
            exe = os.path.join(directory, "llama-server.exe")
            if os.path.exists(exe):
                os.chmod(exe, os.stat(exe).st_mode | stat.S_IXUSR)
        self.patch("extract_zip", extract_executable)
        mock.patch.object(ins.Installer, "install_python", lambda self: None).start()
        self.addCleanup(mock.patch.stopall)
        environ = mock.patch.dict(os.environ, {"ProgramData": os.path.join(self.dir, "ProgramData"),
                                               "LOCALAPPDATA": os.path.join(self.dir, "LocalAppData"),
                                               "STUB_N_CTX": "30000"})
        environ.start()
        self.addCleanup(environ.stop)
        self.web_port, self.llama_port = free_port(), free_port()
        self.menu = os.path.join(self.dir, "ProgramData", "Microsoft", "Windows", "Start Menu", "Programs",
                                 "PLaMo 3 Translate")

    def install(self, *argv):
        inst = make_installer("--destdir", self.destdir, *argv)
        result, out, err = quiet(inst.install)
        return inst, result, out, err

    def config(self):
        with open(os.path.join(self.destdir, "install.json"), encoding="utf-8") as f:
            return json.load(f)

    def test_install_update_uninstall(self):
        inst, result, out, err = self.install("--model", "0.8b", "--backend", "cpu", "--accept-license",
                                              "--web-port", str(self.web_port), "--llama-port", str(self.llama_port))
        self.assertIsNone(result, err + out)
        self.assertIn("PLaMo 3 Translate is installed.", out)
        self.assertIn("Context    : 30000 tokens", out)
        self.assertIn("smaller than plamo-3-translate-client.py may need", err)
        self.assertIn("--chunk-chars 15000", err)
        self.assertEqual(self.config(), {
            "version": 1, "destdir": self.destdir, "model": "0.8b", "quant": "Q4_K_M", "backend": "cpu", "gpus": "",
            "ctx_size": "auto", "web_port": self.web_port, "llama_port": self.llama_port, "listen": "no",
            "expose_llama": "no", "llama_tag": "b12366"})
        for name in (".plamo-3-translate-installation", "plamo-3-translate-client.bat", "command-prompt.bat",
                     "app/plamo-3-translate-server.py", "app/plamo-3-translate-client.py",
                     "app/plamo-3-translate-launcher.py", "app/plamo-3-translate-installer.py",
                     "llama.cpp/b12366-cpu/llama-server.exe", "llama.cpp/b12366-cpu/.installed",
                     "models/plamo-3-translate-0.8b-Q4_K_M.gguf"):
            self.assertTrue(os.path.isfile(os.path.join(self.destdir, name)), name)
        self.assertFalse(os.path.exists(os.path.join(self.destdir, "llama-api-key.txt")))
        self.assertFalse(os.path.exists(os.path.join(self.destdir, "llama.cpp", ".download")))
        with open(os.path.join(self.destdir, "plamo-3-translate-client.bat"), "rb") as f:
            bat = f.read()
        self.assertIn(b"\r\n", bat)
        self.assertIn(f"http://127.0.0.1:{self.llama_port}".encode(), bat)
        with open(os.path.join(self.destdir, "app", "plamo-3-translate-server.py"), "rb") as f, \
                open(SERVER, "rb") as g:
            self.assertEqual(f.read(), g.read())
        # The parent created by the installer and the install directory are protected first,
        # everything in it at the end.
        self.assertEqual(self.permissions, [(os.path.join(self.dir, "LLM"), False), (self.destdir, False),
                                            (self.destdir, True)])
        self.assertEqual(len(self.scripts), 1)
        self.assertIn("PLaMo 3 Translate Web UI.lnk", self.scripts[0])
        self.assertTrue(os.path.isdir(self.menu))
        # The servers of the translation test are gone.
        self.assertTrue(wait_until(lambda: not port_open(self.web_port) and not port_open(self.llama_port)))
        requests = len(self.server.requests)

        # Re-run without options: the saved settings are used and nothing is downloaded again.
        inst, result, out, err = self.install("--accept-license")
        self.assertIsNone(result, err + out)
        self.assertEqual(inst.val["model"], "0.8b")
        self.assertEqual(inst.val["web_port"], str(self.web_port))
        self.assertIn("llama.cpp b12366 (cpu) is already installed", out)
        self.assertIn("Reusing plamo-3-translate-0.8b-Q4_K_M.gguf", out)
        self.assertEqual(len(self.server.requests), requests)

        # Change quantization and backend, expose llama-server; --yes deletes the unused model.
        inst, result, out, err = self.install("--quant", "Q4_0", "--backend", "vulkan", "--gpus", "all",
                                              "--expose-llama", "--listen", "--accept-license", "--yes")
        self.assertIsNone(result, err + out)
        self.assertIn("Deleted plamo-3-translate-0.8b-Q4_K_M.gguf", out)
        self.assertIn("does not use any GPU", err)
        self.assertEqual(os.listdir(os.path.join(self.destdir, "models")), ["plamo-3-translate-0.8b-Q4_0.gguf"])
        self.assertEqual(os.listdir(os.path.join(self.destdir, "llama.cpp")), ["b12366-vulkan"])
        config = self.config()
        self.assertEqual((config["quant"], config["backend"], config["gpus"], config["listen"],
                          config["expose_llama"]), ("Q4_0", "vulkan", "all", "yes", "yes"))
        with open(os.path.join(self.destdir, "llama-api-key.txt"), encoding="ascii") as f:
            key = f.read()
        self.assertRegex(key, r"^[0-9a-f]{64}\n$")

        # The API key survives a re-run; without --yes the unused model would be kept.
        inst, result, out, err = self.install("--accept-license")
        self.assertIsNone(result, err + out)
        with open(os.path.join(self.destdir, "llama-api-key.txt"), encoding="ascii") as f:
            self.assertEqual(f.read(), key)

        # Uninstall.
        logs = os.path.join(self.dir, "LocalAppData", "PLaMo3Translate")
        self.assertTrue(os.path.isdir(logs))
        result, out, err = quiet(make_installer("--uninstall", "--destdir", self.destdir).uninstall)
        self.assertIsInstance(result, ins.Die)  # not confirmed
        self.assertTrue(os.path.isdir(self.destdir))
        result, out, err = quiet(make_installer("--uninstall", "--destdir", self.destdir, "--yes").uninstall)
        self.assertIsNone(result, err + out)
        self.assertFalse(os.path.exists(self.destdir))
        self.assertFalse(os.path.exists(self.menu))
        self.assertFalse(os.path.exists(logs))
        self.assertTrue(os.path.isdir(os.path.join(self.dir, "LLM")))

    def test_ports_in_use_and_foreign_directory(self):
        import socket
        with socket.socket() as other:
            other.bind(("127.0.0.1", self.web_port))
            other.listen()
            other.settimeout(0.1)
            inst, result, out, err = self.install("--model", "0.8b", "--backend", "cpu", "--accept-license",
                                                  "--web-port", str(self.web_port),
                                                  "--llama-port", str(self.llama_port))
        self.assertIn(f"TCP port {self.web_port} is already in use by another program", str(result))
        self.assertFalse(os.path.exists(self.destdir))

        os.makedirs(self.destdir)
        with open(os.path.join(self.destdir, "precious.txt"), "w") as f:
            f.write("keep")
        inst, result, out, err = self.install("--model", "0.8b", "--backend", "cpu", "--accept-license",
                                              "--web-port", str(self.web_port), "--llama-port", str(self.llama_port))
        self.assertIn("exists and is not empty", str(result))
        result, out, err = quiet(make_installer("--uninstall", "--destdir", self.destdir, "--yes").uninstall)
        self.assertIsNone(result)
        self.assertIn("does not look like a PLaMo 3 Translate installation", err)
        self.assertTrue(os.path.isfile(os.path.join(self.destdir, "precious.txt")))

    def test_running_installation_is_not_touched(self):
        args = ("--model", "0.8b", "--backend", "cpu", "--accept-license",
                "--web-port", str(self.web_port), "--llama-port", str(self.llama_port))
        inst, result, out, err = self.install(*args)
        self.assertIsNone(result, err + out)
        import subprocess
        from support import LAUNCHER
        proc = subprocess.Popen([sys.executable, LAUNCHER, "--mode", "llama", "--root", self.destdir],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            self.assertTrue(wait_until(lambda: ins.port_user(self.llama_port) == "llama"))
            inst, result, out, err = self.install("--accept-license")
            self.assertIn("PLaMo 3 Translate is running", str(result))
            result, out, err = quiet(make_installer("--uninstall", "--destdir", self.destdir, "--yes").uninstall)
            self.assertIn("PLaMo 3 Translate is running", str(result))
            self.assertTrue(os.path.isdir(self.destdir))
            self.assertTrue(os.path.isdir(self.menu))
        finally:
            proc.terminate()
            proc.wait(timeout=30)

    def test_missing_dll(self):
        inst = make_installer()
        completed = mock.Mock(returncode=0xC0000135, stdout=b"", stderr=b"")
        with mock.patch.object(ins.subprocess, "run", lambda *a, **k: completed):
            result, out, err = quiet(inst.check_llama_runs, "llama-server.exe")
        self.assertIn("Visual C++ Redistributable", str(result))
        self.assertIn(ins.VC_REDIST_URL, str(result))


class PythonPackage(TempCase):
    def test_install_python_from_the_archive_next_to_the_interpreter(self):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("python.exe", b"MZ")
            z.writestr("python314.zip", b"PK")
        data = archive.getvalue()
        self.patch("PYTHON_SHA256", sha256(data))
        self.patch("PYTHON_SIZE", len(data))
        work = os.path.join(self.dir, "work")
        os.makedirs(os.path.join(work, "python-x"))
        with open(os.path.join(work, ins.PYTHON_ZIP), "wb") as f:
            f.write(data)
        destdir = os.path.join(self.dir, "dest")
        os.makedirs(destdir)
        inst = make_installer()
        inst.val["destdir"] = destdir
        with mock.patch.object(ins.sys, "executable", os.path.join(work, "python-x", "python.exe")):
            result, out, err = quiet(inst.install_python)
            self.assertIsNone(result, err)
            self.assertTrue(os.path.isfile(os.path.join(destdir, "python", "python.exe")))
            result, out, err = quiet(inst.install_python)
            self.assertIn("already installed", out)
            # Without the archive it is downloaded (and verified).
            os.remove(os.path.join(work, ins.PYTHON_ZIP))
            shutil.rmtree(os.path.join(destdir, "python"))
            server = self.serve({"/" + ins.PYTHON_ZIP: data})
            self.patch("PYTHON_URL", server.url + "/" + ins.PYTHON_ZIP)
            result, out, err = quiet(inst.install_python)
            self.assertIsNone(result, err)
            self.assertTrue(os.path.isfile(os.path.join(destdir, "python", "python.exe")))
            self.assertFalse(os.path.exists(os.path.join(destdir, "python.download")))


if __name__ == "__main__":
    unittest.main()
