"""Tests of plamo-3-translate-launcher.py against the stub llama-server (run on Linux)."""

import http.client
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import unittest

from support import LAUNCHER, load_module, make_install_tree, port_open, wait_until

launcher = load_module("plamo3_launcher", LAUNCHER)


class LauncherCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.join(self.tmp.name, "root")
        self.procs = []
        self.addCleanup(self.kill_all)

    def kill_all(self):
        for proc in self.procs:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
            for stream in (proc.stdout, proc.stderr):
                if stream:
                    stream.close()

    def env(self, **extra):
        env = dict(os.environ)
        env["LOCALAPPDATA"] = os.path.join(self.tmp.name, "localappdata")
        env.update(extra)
        return env

    def start(self, mode, *args, **extra):
        proc = subprocess.Popen(
            [sys.executable, LAUNCHER, "--mode", mode, "--root", self.root, "--no-browser", *args],
            env=self.env(**extra), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8")
        self.procs.append(proc)
        return proc

    def run_launcher(self, mode, *args, timeout=60, **extra):
        proc = self.start(mode, *args, **extra)
        out, err = proc.communicate(timeout=timeout)
        return proc.returncode, out, err

    def interrupt(self, proc):
        proc.send_signal(signal.SIGINT)
        out, err = proc.communicate(timeout=30)
        return proc.returncode, out, err

    def web_ready(self, cfg):
        return launcher.is_our_web(cfg["web_port"]) and launcher.llama_ready(cfg["llama_port"])


class CheckMode(LauncherCase):
    def test_check_translates_and_stops(self):
        cfg = make_install_tree(self.root)
        result_file = os.path.join(self.tmp.name, "result.json")
        status, out, err = self.run_launcher("check", "--result-file", result_file, STUB_N_CTX="50000")
        self.assertEqual(status, 0, err)
        self.assertIn("Translation test passed", out)
        with open(result_file, encoding="utf-8") as f:
            result = json.load(f)
        self.assertEqual(result["n_ctx"], 50000)
        self.assertEqual(result["translation"], "Hello")
        self.assertTrue(wait_until(lambda: not port_open(cfg["web_port"]) and not port_open(cfg["llama_port"])))
        log = os.path.join(self.tmp.name, "localappdata", "PLaMo3Translate", "logs", "llama-server.log")
        with open(log, encoding="utf-8") as f:
            self.assertIn("stub llama-server: listening", f.read())

    def test_check_with_api_key(self):
        make_install_tree(self.root, expose_llama="yes", ctx_size="8192", backend="vulkan", gpus="Vulkan1")
        args_file = os.path.join(self.tmp.name, "args.json")
        status, out, err = self.run_launcher("check", STUB_ARGS_FILE=args_file)
        self.assertEqual(status, 0, err)
        self.assertIn("rejects requests without the API key", out)
        with open(args_file, encoding="utf-8") as f:
            seen = json.load(f)
        self.assertEqual(seen["api_key"], "0123456789abcdef" * 4)
        argv = seen["argv"]
        self.assertEqual(argv[argv.index("--host") + 1], "0.0.0.0")
        self.assertEqual(argv[argv.index("-c") + 1], "8192")
        self.assertEqual(argv[argv.index("--device") + 1], "Vulkan1")
        self.assertEqual(argv[-4:], ["--jinja", "--no-prefill-assistant", "--alias", "plamo-3-translate"])

    def test_llama_server_dies_while_loading(self):
        make_install_tree(self.root)
        status, out, err = self.run_launcher("check", STUB_LOAD_SECONDS="1", STUB_EXIT_STATUS="3")
        self.assertEqual(status, 1)
        self.assertIn("llama-server stopped unexpectedly (exit status 3)", err)
        self.assertIn("failing as requested", err)  # the tail of the log

    def test_missing_dll_hint(self):
        text = launcher.exit_text("llama-server", 0xC0000135, os.path.join(self.tmp.name, "none.log"))
        self.assertIn("Visual C++", text)

    def test_missing_model(self):
        make_install_tree(self.root)
        os.remove(os.path.join(self.root, "models", "plamo-3-translate-0.8b-Q4_K_M.gguf"))
        status, out, err = self.run_launcher("check")
        self.assertEqual(status, 1)
        self.assertIn("Run the installer again", err)

    def test_missing_config(self):
        os.makedirs(self.root)
        status, out, err = self.run_launcher("webui")
        self.assertEqual(status, 1)
        self.assertIn("install.json", err)


class Conflicts(LauncherCase):
    def test_webui_then_second_start_and_llama_shortcut(self):
        cfg = make_install_tree(self.root)
        first = self.start("webui")
        self.assertTrue(wait_until(lambda: self.web_ready(cfg)), "the Web UI did not start")
        # The JSON API works through the web server.
        conn = http.client.HTTPConnection("127.0.0.1", cfg["web_port"], timeout=30)
        conn.request("POST", "/api/translate", body=json.dumps({"text": "こんにちは", "target_lang": "en"}),
                     headers={"Content-Type": "application/json"})
        response = conn.getresponse()
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(response.read())["translation"], "Hello")
        conn.close()

        status, out, err = self.run_launcher("webui")
        self.assertEqual(status, 0, err)
        self.assertIn("already running", out)

        status, out, err = self.run_launcher("llama")
        self.assertEqual(status, 1)
        self.assertIn("plamo-3-translate-client.py can be used as it is", err)

        status, out, err = self.run_launcher("check")
        self.assertEqual(status, 1)
        self.assertIn("close its window first", err)

        status, out, err = self.interrupt(first)
        self.assertEqual(status, 0, err)
        self.assertIn("Close this window", out)
        self.assertTrue(wait_until(lambda: not port_open(cfg["web_port"]) and not port_open(cfg["llama_port"])))

    def test_second_start_while_loading_waits(self):
        cfg = make_install_tree(self.root)
        first = self.start("webui", STUB_LOAD_SECONDS="3")
        self.assertTrue(wait_until(lambda: launcher.is_our_web(cfg["web_port"])))
        self.assertFalse(launcher.llama_ready(cfg["llama_port"]))
        status, out, err = self.run_launcher("webui")
        self.assertEqual(status, 0, err)
        self.assertIn("still loading the model", out)
        self.assertTrue(launcher.llama_ready(cfg["llama_port"]))
        self.interrupt(first)

    def test_llama_only_then_webui(self):
        cfg = make_install_tree(self.root)
        first = self.start("llama")
        self.assertTrue(wait_until(lambda: launcher.llama_ready(cfg["llama_port"])))
        self.assertFalse(port_open(cfg["web_port"]))
        self.assertEqual(launcher.llama_state(cfg["llama_port"]), "ours")

        status, out, err = self.run_launcher("webui")
        self.assertEqual(status, 1)
        self.assertIn(f"llama-server is already running on TCP port {cfg['llama_port']}", err)
        self.assertIn("starts its own llama-server", err)

        status, out, err = self.run_launcher("llama")
        self.assertEqual(status, 1)
        self.assertIn("already running", err)

        status, out, err = self.interrupt(first)
        self.assertEqual(status, 0, err)
        self.assertTrue(wait_until(lambda: not port_open(cfg["llama_port"])))

    def test_ports_used_by_another_program(self):
        cfg = make_install_tree(self.root)
        with socket.socket() as other:
            other.bind(("127.0.0.1", cfg["web_port"]))
            other.listen()
            status, out, err = self.run_launcher("webui")
            self.assertEqual(status, 1)
            self.assertIn(f"TCP port {cfg['web_port']} (web server) is in use by another program", err)
        with socket.socket() as other:
            other.bind(("127.0.0.1", cfg["llama_port"]))
            other.listen()
            other.settimeout(0.1)
            self.assertEqual(launcher.llama_state(cfg["llama_port"]), "other")
            for mode in ("webui", "llama"):
                status, out, err = self.run_launcher(mode)
                self.assertEqual(status, 1)
                self.assertIn(f"TCP port {cfg['llama_port']} (llama-server) is in use by another program", err)

    def test_terminated_launcher_stops_children(self):
        cfg = make_install_tree(self.root)
        first = self.start("webui")
        self.assertTrue(wait_until(lambda: self.web_ready(cfg)))
        first.terminate()
        first.communicate(timeout=30)
        self.assertTrue(wait_until(lambda: not port_open(cfg["web_port"]) and not port_open(cfg["llama_port"])))


if __name__ == "__main__":
    unittest.main()
