"""Behavioral tests for the Pelican adapter; no Wine or game files required."""

from __future__ import annotations

import contextlib
import copy
import datetime
import importlib.util
import io
import math
import os
from pathlib import Path
import signal
import stat
import subprocess
import tempfile
import tomllib
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("reclaimer_runtime", ROOT / "runtime" / "reclaimer.py")
assert SPEC is not None and SPEC.loader is not None
runtime = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runtime)

ENV = {"SERVER_PORT": "49176"}
TEMPLATE = '''\
game = "custom-game"
auto_reload = true
auto_update = true
upnp = true
admins = ["example-admin"]
timestamp = 2026-10-16T12:34:56Z

[master]
enabled = false
port = 5000
peers = ["example.net:49175"]

[rcon]
password = "old-password"
address = "127.0.0.1"
custom_option = true

[defaults]
anti_cheat = "enforce"
share_rate = 20
advanced = { values = [1, 2, 3], voice = true }

[[server]]
name = "Old server"
port = 5001
map = "Old map"
game = "Old mode"
playlist = "old-playlist"
variant = "old-variant"
base_map = "old-base-map"
rcon_port = 5002
rcon_password = "old-per-server-password"
min_players = 2
custom = { setting = "preserved" }
'''


class ConfigurationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = tomllib.loads(TEMPLATE)

    def test_panel_controls_preserve_advanced_settings_and_source(self) -> None:
        original = copy.deepcopy(self.source)
        env = {
            **ENV,
            "RECLAIMER_MASTER_PORT": "50000",
            "RECLAIMER_SERVER_NAME": "Community Halo 3",
            "RECLAIMER_MAX_PLAYERS": "24",
            "RECLAIMER_RCON_PASSWORD": "new-password",
            "RECLAIMER_PUBLIC_ADDRESS": "halo.example.net",
        }
        result = runtime.build_config(self.source, env)
        self.assertEqual(self.source, original)
        self.assertEqual(result["game"], "custom-game")
        self.assertEqual(result["defaults"], original["defaults"])
        self.assertEqual(result["admins"], ["example-admin"])
        self.assertEqual(result["timestamp"], original["timestamp"])
        self.assertTrue(result["auto_reload"])
        self.assertFalse(result["auto_update"])
        self.assertFalse(result["upnp"])
        self.assertEqual(result["public_address"], "halo.example.net")
        self.assertEqual(result["master"], {
            "enabled": True, "port": 50000, "peers": ["example.net:49175"],
        })
        self.assertEqual(result["rcon"], {
            "password": "new-password", "address": "0.0.0.0", "custom_option": True,
        })
        server = result["server"][0]
        self.assertEqual(server["name"], "Community Halo 3")
        self.assertEqual(server["port"], 49176)
        self.assertEqual(server["max_players"], 24)
        self.assertTrue(server["enabled"])
        self.assertEqual(server["min_players"], 2)
        self.assertEqual(server["custom"], {"setting": "preserved"})
        self.assertNotIn("rcon_port", server)
        self.assertNotIn("rcon_password", server)
        # Returning aliased nested tables would let later edits corrupt the template.
        server["custom"]["setting"] = "changed"
        result["defaults"]["advanced"]["values"].append(4)
        self.assertEqual(self.source, original)

    def test_playlist_and_map_mode_are_mutually_exclusive(self) -> None:
        playlist_server = runtime.build_config(self.source, {
            **ENV, "RECLAIMER_PLAYLIST": "  rotations/community.toml  ",
            "RECLAIMER_MAP": "Ignored", "RECLAIMER_GAME_MODE": "Ignored",
        })["server"][0]
        self.assertEqual(playlist_server["playlist"], "rotations/community.toml")
        for key in ("map", "game", "variant", "base_map"):
            self.assertNotIn(key, playlist_server)
        map_server = runtime.build_config(self.source, {
            **ENV, "RECLAIMER_PLAYLIST": "   ",
            "RECLAIMER_MAP": "Valhalla", "RECLAIMER_GAME_MODE": "CTF",
        })["server"][0]
        self.assertEqual((map_server["map"], map_server["game"]), ("Valhalla", "CTF"))
        for key in ("playlist", "variant", "base_map"):
            self.assertNotIn(key, map_server)

    def test_campaign_keeps_advanced_campaign_definition(self) -> None:
        campaign = {"map": "arrival", "difficulty": "legendary", "skulls": ["iron"]}
        self.source["server"][0]["campaign"] = campaign
        result = runtime.build_config(self.source, {**ENV, "RECLAIMER_MAX_PLAYERS": "16"})
        server = result["server"][0]
        self.assertEqual(server["campaign"], campaign)
        for key in ("map", "game", "playlist", "variant", "base_map"):
            self.assertNotIn(key, server)

    def test_exactly_one_server_table_is_required(self) -> None:
        for servers in (None, {}, [], [{}, {}], ["invalid"]):
            with self.subTest(servers=servers), self.assertRaises(ValueError):
                runtime.build_config({"server": servers}, ENV)
        with self.assertRaises(ValueError):
            runtime.build_config({}, ENV)

    def test_port_and_player_validation(self) -> None:
        cases = [
            ({}, "SERVER_PORT"),
            ({**ENV, "SERVER_PORT": "not-a-port"}, "SERVER_PORT"),
            ({**ENV, "SERVER_PORT": "1023"}, "SERVER_PORT"),
            ({**ENV, "SERVER_PORT": "65536"}, "SERVER_PORT"),
            ({**ENV, "RECLAIMER_MASTER_PORT": "0"}, "RECLAIMER_MASTER_PORT"),
            ({**ENV, "RECLAIMER_MASTER_PORT": "65536"}, "RECLAIMER_MASTER_PORT"),
            ({**ENV, "RECLAIMER_MAX_PLAYERS": "0"}, "RECLAIMER_MAX_PLAYERS"),
            ({**ENV, "RECLAIMER_MAX_PLAYERS": "128"}, "RECLAIMER_MAX_PLAYERS"),
            ({**ENV, "RECLAIMER_MAX_PLAYERS": "1.5"}, "RECLAIMER_MAX_PLAYERS"),
        ]
        for env, key in cases:
            with self.subTest(env=env), self.assertRaisesRegex(ValueError, key):
                runtime.build_config(self.source, env)
        for port, maximum in ((1024, 1), (65535, 127)):
            with self.subTest(port=port, maximum=maximum):
                server = runtime.build_config(self.source, {
                    "SERVER_PORT": str(port), "RECLAIMER_MAX_PLAYERS": str(maximum),
                })["server"][0]
                self.assertEqual((server["port"], server["max_players"]), (port, maximum))

    def test_name_password_and_conflicting_tcp_ports_are_rejected(self) -> None:
        for name in ("", "   ", "x" * 49, "name\ncommand", "name\x00", "name\x7f"):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "RECLAIMER_SERVER_NAME"):
                runtime.build_config(self.source, {**ENV, "RECLAIMER_SERVER_NAME": name})
        with self.assertRaisesRegex(ValueError, "RECLAIMER_RCON_PASSWORD"):
            runtime.build_config(self.source, {**ENV, "RECLAIMER_RCON_PASSWORD": "short"})
        with self.assertRaisesRegex(ValueError, "different TCP ports"):
            runtime.build_config(self.source, {
                **ENV, "RECLAIMER_MASTER_PORT": ENV["SERVER_PORT"],
                "RECLAIMER_RCON_PASSWORD": "long-password",
            })
        # An empty password disables RCON, so its TCP port cannot conflict.
        runtime.build_config(self.source, {**ENV, "RECLAIMER_MASTER_PORT": ENV["SERVER_PORT"]})

    def test_campaign_rejects_excess_players_and_playlist(self) -> None:
        self.source["server"][0]["campaign"] = {"map": "arrival"}
        for override, message in (
            ({"RECLAIMER_MAX_PLAYERS": "17"}, "at most 16"),
            ({"RECLAIMER_PLAYLIST": "rotation.toml"}, "Clear RECLAIMER_PLAYLIST"),
        ):
            with self.subTest(override=override), self.assertRaisesRegex(ValueError, message):
                runtime.build_config(self.source, {**ENV, **override})


class SerializationTests(unittest.TestCase):
    def test_user_strings_roundtrip_without_injecting_toml(self) -> None:
        malicious = '\\path\\"\n[unexpected]\nadmin = true\n# café 🐧\t\b\f\r\x00\x1f'
        value = {"quoted.key": malicious, "nested": [{"x": malicious}]}
        parsed = tomllib.loads("value = " + runtime.toml_value(value))
        self.assertEqual(parsed, {"value": value})

    def test_non_bmp_unicode_keys_and_del_strings_roundtrip(self) -> None:
        value = {"🐧": "before\x7fafter", "日本語": "雪"}
        self.assertEqual(tomllib.loads("value = " + runtime.toml_value(value))["value"], value)

    def test_toml_native_types_roundtrip(self) -> None:
        value = {
            "bool": True, "integer": -123, "float": 2.5,
            "date": datetime.date(2026, 10, 16),
            "time": datetime.time(12, 34, 56, 123456),
            "local": datetime.datetime(2026, 10, 16, 12, 34, 56),
            "offset": datetime.datetime(2026, 10, 16, tzinfo=datetime.timezone.utc),
            "array": [1, "two", False], "empty_table": {}, "empty_array": [],
        }
        self.assertEqual(tomllib.loads("value = " + runtime.toml_value(value))["value"], value)
        for value in (float("inf"), float("-inf")):
            self.assertEqual(tomllib.loads("value = " + runtime.toml_value(value))["value"], value)
        self.assertTrue(math.isnan(tomllib.loads("value = " + runtime.toml_value(float("nan")))["value"]))

    def test_unsupported_values_are_rejected(self) -> None:
        for value in (None, object(), {1, 2}):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "Unsupported TOML value"):
                runtime.toml_value(value)


class GeneratedConfigTests(unittest.TestCase):
    def test_generated_file_atomically_replaces_previous_file_and_keeps_template(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "dedicated.toml"
            original = b"\xef\xbb\xbf" + TEMPLATE.replace("\n", "\r\n").encode("utf-8")
            source.write_bytes(original)
            target = root / runtime.GENERATED_CONFIG
            target.write_text("previous generated config", encoding="utf-8")
            real_replace = Path.replace
            observed = []

            def observe_replace(pending: Path, destination: Path) -> Path:
                self.assertEqual(destination, target)
                self.assertEqual(target.read_text(encoding="utf-8"), "previous generated config")
                self.assertEqual(source.read_bytes(), original)
                tomllib.loads(pending.read_text(encoding="utf-8"))
                observed.append(pending)
                return real_replace(pending, destination)

            with patch.object(Path, "replace", observe_replace):
                self.assertEqual(runtime.write_config(root, ENV), target)
            self.assertEqual(len(observed), 1)
            self.assertFalse(observed[0].exists())
            self.assertEqual(source.read_bytes(), original)
            parsed = tomllib.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(parsed["server"][0]["port"], 49176)
            self.assertEqual(parsed["defaults"], tomllib.loads(TEMPLATE)["defaults"])
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)

    def test_invalid_settings_do_not_replace_existing_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "dedicated.toml"
            source.write_text(TEMPLATE, encoding="utf-8")
            original = source.read_bytes()
            target = root / runtime.GENERATED_CONFIG
            target.write_bytes(b"last known good configuration")
            with self.assertRaises(ValueError):
                runtime.write_config(root, {**ENV, "SERVER_PORT": "invalid"})
            self.assertEqual(target.read_bytes(), b"last known good configuration")
            self.assertEqual(source.read_bytes(), original)
            self.assertFalse(target.with_suffix(".toml.new").exists())

    def test_non_bmp_top_level_advanced_key_survives_generation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "dedicated.toml").write_text('"🐧" = "custom"\n' + TEMPLATE, encoding="utf-8")
            target = runtime.write_config(root, ENV)
            self.assertEqual(tomllib.loads(target.read_text(encoding="utf-8"))["🐧"], "custom")


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, duration: float) -> None:
        self.now += duration


class FakeProcess:
    def __init__(self, clock: FakeClock, exit_at: float = 12, returncode: int = 0) -> None:
        self.clock = clock
        self.exit_at = exit_at
        self.returncode = returncode
        self.pid = 12345
        self.killed = False

    def poll(self) -> int | None:
        return self.returncode if self.killed or self.clock.now >= self.exit_at else None

    def wait(self) -> int:
        return self.returncode


class RunnerTests(unittest.TestCase):
    def serve_with_clock(self, runner: runtime.Runner, process: FakeProcess, clock: FakeClock) -> tuple[int, str]:
        output = io.StringIO()
        with patch.object(runtime.subprocess, "Popen", return_value=process) as popen, \
                patch.object(runtime.time, "monotonic", clock.monotonic), \
                patch.object(runtime.time, "sleep", clock.sleep), \
                contextlib.redirect_stdout(output):
            result = runner.serve(["server"], ["status"])
        popen.assert_called_once_with(["server"], start_new_session=True)
        self.assertIsNone(runner.active)
        return result, output.getvalue()

    def test_readiness_requires_successful_status_probe_and_prints_once(self) -> None:
        clock = FakeClock()
        process = FakeProcess(clock, returncode=7)
        runner = runtime.Runner()
        with patch.object(runner, "probe", side_effect=[False, True]) as probe:
            result, output = self.serve_with_clock(runner, process, clock)
        self.assertEqual(result, 7)
        self.assertEqual(output, runtime.READY + "\n")
        self.assertEqual(probe.call_count, 2)
        probe.assert_called_with(["status"])

    def test_clean_child_exit_without_panel_stop_is_failure(self) -> None:
        clock = FakeClock()
        runner = runtime.Runner()
        with patch.object(runner, "probe", return_value=False):
            result, output = self.serve_with_clock(runner, FakeProcess(clock, exit_at=4), clock)
        self.assertEqual(result, 1)
        self.assertEqual(output, "")

    def test_process_exiting_during_status_probe_is_never_reported_ready(self) -> None:
        clock = FakeClock()
        process = FakeProcess(clock)
        runner = runtime.Runner()

        def exit_during_probe(_args: list[str]) -> bool:
            process.killed = True
            return True

        with patch.object(runner, "probe", side_effect=exit_during_probe):
            result, output = self.serve_with_clock(runner, process, clock)
        self.assertEqual(result, 1)
        self.assertEqual(output, "")

    def test_stop_during_successful_status_probe_does_not_report_ready(self) -> None:
        clock = FakeClock()
        process = FakeProcess(clock, exit_at=4)
        runner = runtime.Runner()

        def stop_during_probe(_args: list[str]) -> bool:
            runner.stop(signal.SIGTERM, None)
            # The server remains alive when a successful status result arrives.
            self.assertIsNone(process.poll())
            return True

        with patch.object(runner, "probe", side_effect=stop_during_probe) as probe, \
                patch.object(runner, "send") as send:
            result, output = self.serve_with_clock(runner, process, clock)
        self.assertEqual(result, 0)
        self.assertEqual(output, "")
        probe.assert_called_once_with(["status"])
        send.assert_called_once_with(process, signal.SIGINT)

    def test_command_returns_exit_status_and_reaps_active_child(self) -> None:
        runner = runtime.Runner()
        process = Mock()
        process.wait.side_effect = [subprocess.TimeoutExpired(["check"], 0.2), 7]
        with patch.object(runtime.subprocess, "Popen", return_value=process) as popen:
            self.assertEqual(runner.command(["check"]), 7)
        popen.assert_called_once_with(["check"], start_new_session=True)
        self.assertEqual(process.wait.call_count, 2)
        process.wait.assert_called_with(timeout=0.2)
        self.assertIsNone(runner.active)

    @unittest.skipUnless(hasattr(signal, "SIGKILL"), "Escalation uses POSIX signals on the Linux node")
    def test_unresponsive_initialization_command_is_killed_after_stop_grace_period(self) -> None:
        clock = FakeClock()
        runner = runtime.Runner()
        process = Mock()
        sent = []
        killed = False

        def send(active: Mock, sig: int) -> None:
            nonlocal killed
            self.assertIs(active, process)
            sent.append((clock.now, sig))
            if sig == signal.SIGKILL:
                killed = True

        def wait(timeout: float) -> int:
            if killed:
                return -signal.SIGKILL
            clock.sleep(timeout)
            if not runner.stopping:
                runner.stop(signal.SIGTERM, None)
            raise subprocess.TimeoutExpired(["wineboot"], timeout)

        process.wait.side_effect = wait
        with patch.object(runtime.subprocess, "Popen", return_value=process) as popen, \
                patch.object(runtime.time, "monotonic", clock.monotonic), \
                patch.object(runner, "send", side_effect=send):
            self.assertEqual(runner.command(["wineboot"]), -signal.SIGKILL)
        popen.assert_called_once_with(["wineboot"], start_new_session=True)
        self.assertEqual([sig for _, sig in sent], [signal.SIGINT, signal.SIGKILL])
        self.assertGreaterEqual(sent[1][0] - sent[0][0], 25)
        self.assertLess(sent[1][0] - sent[0][0], 26)
        self.assertIsNone(runner.active)

    def test_panel_stop_forwards_interrupt_to_active_process(self) -> None:
        runner = runtime.Runner()
        process = Mock()
        runner.active = process
        with patch.object(runner, "send") as send:
            runner.stop(signal.SIGTERM, None)
        self.assertTrue(runner.stopping)
        send.assert_called_once_with(process, signal.SIGINT)

    @unittest.skipUnless(hasattr(signal, "SIGKILL"), "Escalation uses POSIX signals on the Linux node")
    def test_unresponsive_child_is_killed_after_shutdown_grace_period(self) -> None:
        clock = FakeClock()
        process = FakeProcess(clock, exit_at=float("inf"), returncode=-signal.SIGKILL)
        runner = runtime.Runner()
        sent = []

        def send(active: FakeProcess, sig: int) -> None:
            self.assertIs(active, process)
            sent.append((clock.now, sig))
            if sig == signal.SIGKILL:
                active.killed = True

        def sleep(duration: float) -> None:
            clock.now += duration
            if not runner.stopping:
                runner.stop(signal.SIGTERM, None)

        output = io.StringIO()
        with patch.object(runtime.subprocess, "Popen", return_value=process), \
                patch.object(runtime.time, "monotonic", clock.monotonic), \
                patch.object(runtime.time, "sleep", sleep), \
                patch.object(runner, "send", side_effect=send), \
                patch.object(runner, "probe") as probe, \
                contextlib.redirect_stdout(output):
            self.assertEqual(runner.serve(["server"], ["status"]), 0)
        self.assertEqual([sig for _, sig in sent], [signal.SIGINT, signal.SIGKILL])
        self.assertGreaterEqual(sent[1][0] - sent[0][0], 25)
        self.assertLess(sent[1][0] - sent[0][0], 26)
        self.assertEqual(output.getvalue(), "")
        probe.assert_not_called()
        self.assertIsNone(runner.active)

    def test_no_commands_or_server_are_launched_after_stop(self) -> None:
        runner = runtime.Runner()
        runner.stop(signal.SIGTERM, None)
        with patch.object(runtime.subprocess, "Popen") as popen:
            self.assertEqual(runner.command(["check"]), 0)
            self.assertEqual(runner.serve(["server"], ["status"]), 0)
        popen.assert_not_called()

    @unittest.skipUnless(hasattr(os, "killpg"), "Process-group delivery is used on the Linux node")
    def test_send_targets_live_process_group_and_tolerates_exit_race(self) -> None:
        process = Mock(pid=12345)
        process.poll.return_value = None
        with patch.object(runtime.os, "killpg", side_effect=ProcessLookupError) as killpg:
            runtime.Runner.send(process, signal.SIGINT)
        killpg.assert_called_once_with(process.pid, signal.SIGINT)
        process.poll.return_value = 0
        with patch.object(runtime.os, "killpg") as killpg:
            runtime.Runner.send(process, signal.SIGINT)
        killpg.assert_not_called()

    @unittest.skipUnless(hasattr(signal, "SIGKILL"), "Probe cleanup uses POSIX signals on the Linux node")
    def test_timed_out_status_probe_is_killed_and_reaped(self) -> None:
        process = Mock()
        process.wait.side_effect = [subprocess.TimeoutExpired(["status"], 5), -signal.SIGKILL]
        with patch.object(runtime.subprocess, "Popen", return_value=process) as popen, \
                patch.object(runtime.Runner, "send") as send:
            self.assertFalse(runtime.Runner.probe(["status"]))
        popen.assert_called_once_with(
            ["status"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
        )
        send.assert_called_once_with(process, signal.SIGKILL)
        self.assertEqual(process.wait.call_count, 2)
        self.assertEqual(process.wait.call_args_list[0].kwargs, {"timeout": 5})
        self.assertEqual(process.wait.call_args_list[1].kwargs, {})


if __name__ == "__main__":
    unittest.main()
