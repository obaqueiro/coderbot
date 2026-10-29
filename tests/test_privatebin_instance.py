"""Temporary PrivateBin lifecycle: bounded, cleaned up on failure, never raises from stop/cleanup."""
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

import privatebin
from privatebin import Unavailable


def completed(args, stdout=""):
    return subprocess.CompletedProcess(args, 0, stdout, "")


class FakeTunnel:
    """Popen stand-in that writes localhost.run's banner to the log file it is given."""

    def __init__(self, args, stdout=None, **kwargs):
        self.args, self.pid, self.returncode = args, 4242, None
        self.banner = FakeTunnel.banner
        if self.banner is not None:
            stdout.write(self.banner)
            stdout.flush()

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.returncode = 0
        return 0


class Start(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.calls = []
        patches = [
            patch.object(privatebin.shutil, "which", return_value="/usr/bin/x"),
            patch.object(privatebin.operations, "tool", side_effect=self.fake_run),
            patch.object(privatebin.subprocess, "Popen", side_effect=FakeTunnel),
            patch.object(privatebin.os, "killpg"),
            patch.object(privatebin.time, "sleep"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(privatebin._processes.clear)
        FakeTunnel.banner = ('{"data":"abc123def.lhr.life tunneled with tls termination, '
                             'https://abc123def.lhr.life\\r\\n\\u001b[7m  QR"}\n')
        self.port_states = iter([False, True])  # free before start, then listening

    def fake_run(self, args, **kwargs):
        self.calls.append(args)
        if args[:2] == ["docker", "cp"] and args[2].endswith(":/etc/nginx/http.d/site.conf"):
            with open(args[3], "w") as handle:  # copying the image's site.conf out
                handle.write("server {\n    listen 8080 default_server;\n    listen [::]:8080 default_server;\n}\n")
        return completed(args, "cid123\n" if args[:2] == ["docker", "create"] else "")

    def busy(self, port):
        return next(self.port_states, True)

    def test_outside_docker_publishes_loopback_and_parses_the_tunnel_url(self):
        with patch.object(privatebin, "_in_container", return_value=False), \
                patch.object(privatebin, "_port_busy", side_effect=self.busy):
            session = privatebin.start("bot-1", self.dir, ttl=600)
        create = self.calls[0]
        self.assertIn("127.0.0.1:48080:8080", create)
        self.assertIn("codebot.privatebin=bot-1", create)
        self.assertEqual(create[-1], privatebin.config.PRIVATEBIN_IMAGE)
        self.assertEqual(session["public_url"], "https://abc123def.lhr.life/")
        self.assertEqual(session["local_url"], "http://127.0.0.1:48080/")
        self.assertEqual(session["container_id"], "cid123")
        self.assertEqual(privatebin.accepted_bases(session),
                         {"https://abc123def.lhr.life/": "http://127.0.0.1:48080/"})

    def test_inside_docker_joins_the_bot_network_namespace(self):
        with patch.object(privatebin, "_in_container", return_value=True), \
                patch.object(privatebin.socket, "gethostname", return_value="botcid"), \
                patch.object(privatebin, "_port_busy", side_effect=self.busy):
            session = privatebin.start("bot-1", self.dir)
        create = self.calls[0]
        self.assertIn("container:botcid", create)
        self.assertNotIn("-p", create)
        self.assertEqual(session["local_url"], "http://127.0.0.1:48080/")
        with open(f"{self.dir}/site.conf") as handle:
            site = handle.read()
        self.assertIn("listen 48080 default_server;", site)
        self.assertIn("listen [::]:48080 default_server;", site)
        self.assertIn(["docker", "cp", f"{self.dir}/site.conf", "cid123:/etc/nginx/http.d/site.conf"], self.calls)

    def test_tunnel_without_url_times_out_and_cleans_up(self):
        FakeTunnel.banner = "Welcome to localhost.run!\n"
        clock = iter(range(0, 10_000, 5))
        with patch.object(privatebin, "_in_container", return_value=False), \
                patch.object(privatebin, "_port_busy", side_effect=self.busy), \
                patch.object(privatebin.time, "time", side_effect=lambda: next(clock)), \
                self.assertRaisesRegex(Unavailable, "tunnel"):
            privatebin.start("bot-1", self.dir)
        self.assertIn(["docker", "rm", "-f", "cid123"], self.calls)

    def test_missing_tools_busy_port_and_docker_errors_are_unavailable(self):
        with patch.object(privatebin.shutil, "which", return_value=None), \
                self.assertRaisesRegex(Unavailable, "docker or ssh"):
            privatebin.start("bot-1", self.dir)
        with patch.object(privatebin, "_port_busy", return_value=True), \
                self.assertRaisesRegex(Unavailable, "already in use"):
            privatebin.start("bot-1", self.dir)

        def failing(args, **kwargs):
            self.calls.append(args)
            if args[:2] == ["docker", "start"]:
                raise subprocess.CalledProcessError(1, args)
            return completed(args, "cid9\n")
        with patch.object(privatebin.operations, "tool", side_effect=failing), \
                patch.object(privatebin, "_port_busy", return_value=False), \
                self.assertRaises(Unavailable):
            privatebin.start("bot-1", self.dir)
        self.assertIn(["docker", "rm", "-f", "cid9"], self.calls)


class StopAndCleanup(unittest.TestCase):
    def test_stop_and_cleanup_never_raise(self):
        with patch.object(privatebin.operations, "tool", side_effect=OSError("no docker")), \
                patch.object(privatebin.shutil, "which", return_value="/usr/bin/docker"):
            privatebin.stop({"container_id": "c", "tunnel_pid": 999999})
            privatebin.stop(None)
            privatebin.cleanup_stale("bot-1")

    def test_cleanup_removes_only_labelled_containers(self):
        calls = []

        def run(args, **kwargs):
            calls.append(args)
            return completed(args, "a1\nb2\n" if args[:2] == ["docker", "ps"] else "")
        with patch.object(privatebin.operations, "tool", side_effect=run), \
                patch.object(privatebin.shutil, "which", return_value="/usr/bin/docker"):
            privatebin.cleanup_stale("bot-1")
        self.assertIn("label=codebot.privatebin=bot-1", calls[0])
        self.assertEqual(calls[1], ["docker", "rm", "-f", "a1", "b2"])

    def test_a_reused_pid_is_neither_alive_nor_signalled(self):
        with patch.object(privatebin, "_is_tunnel", return_value=False), \
                patch.object(privatebin.os, "kill") as kill, \
                patch.object(privatebin.operations, "tool", return_value=completed([])):
            self.assertFalse(privatebin.running({"public_url": "https://x.lhr.life/", "tunnel_pid": 1234,
                                                 "expires_at": time.time() + 60}))
            privatebin.stop({"tunnel_pid": 1234})
        self.assertTrue(all(call.args[1] == 0 for call in kill.call_args_list))

    def test_is_tunnel_reads_proc_cmdline_without_ps(self):
        import os
        if not os.path.isdir("/proc/self"):
            self.skipTest("no /proc on this platform")
        self.assertFalse(privatebin._is_tunnel(os.getpid()))

    def test_running_requires_live_tunnel_and_unexpired_session(self):
        session = {"public_url": "https://x.lhr.life/", "tunnel_pid": 1, "expires_at": time.time() + 60}
        with patch.object(privatebin, "_alive", return_value=True):
            self.assertTrue(privatebin.running(session))
            self.assertFalse(privatebin.running({**session, "expires_at": time.time() - 1}))
        with patch.object(privatebin, "_alive", return_value=False):
            self.assertFalse(privatebin.running(session))
        self.assertFalse(privatebin.running(None))


if __name__ == "__main__":
    unittest.main()
