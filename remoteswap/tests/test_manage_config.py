#!/usr/bin/env python3
"""Unprivileged script tests. Never load modules, access RDMA, or alter swap."""
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest


CLIENT = Path(__file__).resolve().parents[1] / "client"
DUAL = """NR_SERVERS=2
POOL_GIB=16
CONNECT_TIMEOUT_MS=15000
SERVER0_IP=10.0.0.13
SERVER0_PORT=9400
SERVER1_IP=10.0.0.20
SERVER1_PORT=9400
"""
SINGLE = """NR_SERVERS=1
POOL_GIB=8
CONNECT_TIMEOUT_MS=15000
SERVER0_IP=10.0.0.20
SERVER0_PORT=9400
"""


class ManageConfigTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hermit config test ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.swap = self.root / "swaps"
        self.swap.write_text("Filename Type Size Used Priority\n")
        self.module = self.root / "module"
        self.trace = self.root / "calls"
        self.bin = self.root / "bin"
        self.bin.mkdir()
        # Only the test copy uses fixtures in place of read-only kernel state.
        script = (CLIENT / "manage_rswap_client.sh").read_text()
        script = script.replace("/proc/swaps", shlex.quote(str(self.swap)))
        script = script.replace("/sys/module/rswap_client", str(self.module))
        # Fixture path contains spaces; quote the replacement in -d tests.
        script = script.replace(f"-d {self.module}", f'-d "{self.module}"')
        self.script = self.root / "manage_rswap_client.sh"
        self.stats = self.root / "stats"
        self.stats.write_text("fixture counters\n")
        script = script.replace("/sys/kernel/debug/rswap/stats", shlex.quote(str(self.stats)))
        self.script.write_text(script)
        self.config = self.root / "rswap_client.conf"
        self.config.write_text(DUAL)
        (self.root / "rswap-client.ko").touch()
        for name in ("insmod", "rmmod", "python3"):
            mock = self.bin / name
            mock.write_text(
                "#!/usr/bin/env bash\n"
                "printf '%q ' \"${0##*/}\" \"$@\" >> \"$MOCK_TRACE\"\n"
                "printf '\\n' >> \"$MOCK_TRACE\"\n"
                "exit \"${MOCK_EXIT:-0}\"\n"
            )
            mock.chmod(0o755)
        sudo = self.bin / "sudo"
        sudo.write_text('#!/usr/bin/env bash\n[[ ${1:-} != -- ]] || shift\nexec "$@"\n')
        sudo.chmod(0o755)
        self.env = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}",
                        MOCK_TRACE=str(self.trace), MOCK_EXIT="0")

    def run_script(self, action="install", *args, **env):
        return subprocess.run(["bash", str(self.script), action, *args],
                              env=dict(self.env, **env), text=True,
                              capture_output=True, timeout=5)

    def calls(self):
        return [shlex.split(line) for line in self.trace.read_text().splitlines()] \
            if self.trace.exists() else []

    def test_dual_config_wins_over_environment(self):
        result = self.run_script(NR_SERVERS="1", POOL_GIB="64", SERVER0_IP="wrong")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Server 0: 10.0.0.13:9400", result.stdout)
        args = self.calls()[0]
        for arg in ("nr_servers=2", "rmsize=16", "sip=10.0.0.13", "sip2=10.0.0.20"):
            self.assertIn(arg, args)
        self.assertEqual(len(self.calls()), 1)

    def test_single_needs_no_second_endpoint(self):
        self.config.write_text(SINGLE)
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        args = self.calls()[0]
        self.assertIn("nr_servers=1", args)
        self.assertIn("rmsize=8", args)
        self.assertFalse(any(arg.startswith(("sip2=", "sport2=")) for arg in args))
        self.assertNotIn("Server 1:", result.stdout)

    def test_unequal_total_is_forwarded(self):
        self.config.write_text(DUAL.replace("POOL_GIB=16", "POOL_GIB=24"))
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("rmsize=24", self.calls()[0])

    def test_invalid_or_missing_values_never_invoke_module_commands(self):
        cases = [DUAL.replace("NR_SERVERS=2", "NR_SERVERS=3"),
                 DUAL.replace("POOL_GIB=16", "POOL_GIB=12"),
                 DUAL.replace("POOL_GIB=16", "POOL_GIB=99999999999999999999"),
                 DUAL.replace("CONNECT_TIMEOUT_MS=15000", "CONNECT_TIMEOUT_MS=0"),
                 DUAL.replace("SERVER1_PORT=9400", "SERVER1_PORT=65536"),
                 DUAL.replace("SERVER1_IP=10.0.0.20", "SERVER1_IP="),
                 DUAL.replace("POOL_GIB=16\n", "")]
        for config in cases:
            with self.subTest(config=config):
                self.config.write_text(config)
                result = self.run_script(POOL_GIB="16")
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.calls(), [])

    def test_missing_config_and_active_swap_stop_install(self):
        self.config.unlink()
        self.assertNotEqual(self.run_script().returncode, 0)
        self.config.write_text(DUAL)
        self.swap.write_text("Filename Type Size Used Priority\n/ssd file 10 0 -2\n")
        self.assertNotEqual(self.run_script().returncode, 0)
        self.assertEqual(self.calls(), [])

    def test_uninstall_and_status_ignore_broken_config(self):
        self.config.write_text("this is not valid shell syntax (\n")
        parameters = self.module / "parameters"
        parameters.mkdir(parents=True)
        (parameters / "nr_servers").write_text("1\n")
        (parameters / "rmsize").write_text("8\n")
        result = self.run_script("status")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("nr_servers=1", result.stdout)
        self.assertIn("rmsize=8", result.stdout)
        result = self.run_script("uninstall")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(), [["rmmod", "rswap_client"]])

    def test_failed_insmod_propagates_exit_status(self):
        result = self.run_script(MOCK_EXIT="17")
        self.assertEqual(result.returncode, 17)
        self.assertNotIn("Client initialized.", result.stdout)

    def test_stats_ignores_config_and_does_not_change_swap(self):
        self.config.write_text("broken shell syntax (\n")
        self.swap.write_text("Filename Type Size Used Priority\n/ssd file 10 1 -2\n")
        result = self.run_script("stats")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "fixture counters\n")
        self.assertEqual(self.calls(), [])

    def test_monitor_forwards_interval_and_quoted_tool_path(self):
        self.config.unlink()
        result = self.run_script("monitor", "0.5")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(), [["python3", str(self.root / "../tools/rswap_stats.py"),
                                        "--interval", "0.5"]])

    def test_monitor_default_interval(self):
        result = self.run_script("monitor")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls()[0][-2:], ["--interval", "1"])


if __name__ == "__main__":
    unittest.main()
