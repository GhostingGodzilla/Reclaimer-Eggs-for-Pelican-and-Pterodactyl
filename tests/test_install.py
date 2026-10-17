"""Offline installer fixtures using Bash, coreutils, and the real jq parser.

Set BASH_BINARY/JQ_BINARY if those executables are not discoverable. On Windows,
Git Bash is preferred. Only the installer's fixed SERVER_DIR line is replaced;
curl and apt-get are isolated fixture commands and never access the network or
install packages. The tests never create or inspect the real /mnt/server.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "install.sh"
API_BASE = "https://api.github.com/repos/ProjectReclaimer/project-reclaimer-releases"
DOWNLOAD_BASE = "https://github.com/ProjectReclaimer/project-reclaimer-releases/releases/download"
NEW_EXECUTABLE = b"MZ\x00verified dedicated-server fixture\x00\xff\n"


def find_bash() -> str | None:
    if os.environ.get("BASH_BINARY"):
        return os.environ["BASH_BINARY"]
    if os.name == "nt":
        for base in (os.environ.get("ProgramFiles"), os.environ.get("LOCALAPPDATA")):
            if base:
                for relative in ("Git/bin/bash.exe", "Programs/Git/bin/bash.exe"):
                    candidate = Path(base) / relative
                    if candidate.is_file():
                        return str(candidate)
    return shutil.which("bash")


class InstallerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.bash = find_bash()
        if not cls.bash:
            raise unittest.SkipTest("Bash is unavailable; set BASH_BINARY to run installer fixtures.")
        cls.jq = os.environ.get("JQ_BINARY") or shutil.which("jq")
        if not cls.jq:
            result = subprocess.run(
                [cls.bash, "--noprofile", "--norc", "-c", "command -v jq"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                cls.jq = result.stdout.strip()
        if not cls.jq:
            raise unittest.SkipTest("Real jq is unavailable; set JQ_BINARY to run installer fixtures.")
        cls.jq_shell = cls.shell_path(cls.jq)
        probe = subprocess.run(
            [cls.bash, "--noprofile", "--norc", "-c", '"$1" --version', "_", cls.jq_shell],
            capture_output=True, text=True, timeout=10,
        )
        if probe.returncode or not probe.stdout.startswith("jq-"):
            raise unittest.SkipTest(f"JQ_BINARY is not a working jq executable: {probe.stderr.strip()}")

    @classmethod
    def shell_path(cls, path: str | Path) -> str:
        path = str(path)
        if os.name != "nt" or path.startswith("/"):
            return path
        result = subprocess.run(
            [cls.bash, "--noprofile", "--norc", "-c", 'cygpath -u "$1"', "_", path],
            capture_output=True, text=True, check=True, timeout=10,
        )
        return result.stdout.strip()

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="reclaimer installer fixtures ")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.server = self.directory / "server files"
        self.fixtures = self.directory / "fixtures"
        self.bin = self.directory / "bin"
        self.process_cwd = self.directory / "unrelated working directory"
        for directory in (self.server, self.fixtures, self.bin, self.process_cwd):
            directory.mkdir()
        (self.server / "project-reclaimer.exe").write_bytes(b"previous working executable\n")
        (self.server / ".reclaimer-version").write_bytes(b"v0.9.17\n")
        (self.server / "config").mkdir()
        (self.server / "config" / "dedicated.toml").write_bytes(b'name = "My existing server"\n')
        (self.server / "game" / "halo3" / "maps").mkdir(parents=True)
        (self.server / "game" / "halo3" / "maps" / "guardian.map").write_bytes(b"owned game content\x00")
        self.original = self.snapshot()

        self.script = self.directory / "install.sh"
        self.write_installer(INSTALLER.read_text(encoding="utf-8"))
        self.write_command("curl", '''#!/bin/bash
set -euo pipefail
output=''
url="${@: -1}"
while (( $# )); do
    if [[ "$1" == --output ]]; then
        output="$2"
        shift 2
    else
        shift
    fi
done
[[ -n "$output" ]] || exit 90
printf '%s\\n' "$url" >> "$FIXTURE_CURL_LOG"
if [[ "${FIXTURE_FAIL_URL:-}" == "$url" ]]; then
    printf 'partial download' > "$output"
    exit 22
fi
case "$url" in
    "$FIXTURE_RELEASE_URL") source="$FIXTURE_ROOT/release.json" ;;
    "$FIXTURE_CHECKSUM_URL") source="$FIXTURE_ROOT/SHA256SUMS.txt" ;;
    "$FIXTURE_EXECUTABLE_URL") source="$FIXTURE_ROOT/dedicated.exe" ;;
    *) printf 'Unexpected fixture URL: %s\\n' "$url" >&2; exit 91 ;;
esac
cp -- "$source" "$output"
''')
        self.write_command("apt-get", '''#!/bin/bash
set -euo pipefail
printf '%s\\n' "$*" >> "$FIXTURE_APT_LOG"
case "$1" in update|install) exit 0 ;; *) exit 92 ;; esac
''')
        self.write_command("mv", '''#!/bin/bash
set -euo pipefail
if [[ "${FIXTURE_FAIL_FINAL_MOVE:-0}" == 1 && "${@: -1}" == "$FIXTURE_SERVER/project-reclaimer.exe" ]]; then
    printf 'Injected final rename failure\\n' >&2
    exit 93
fi
exec /usr/bin/mv "$@"
''')
        # Native Windows jq requires binary stdout so Bash sees Linux-style LF.
        binary_output = " -b" if os.name == "nt" and self.jq_shell.lower().endswith(".exe") else ""
        self.write_command("jq", f'#!/bin/bash\nexec "$FIXTURE_JQ"{binary_output} "$@"\n')
        self.release_version = "v0.9.18"
        self.make_release()

    def write_command(self, name: str, content: str) -> None:
        command = self.bin / name
        command.write_text(content, encoding="utf-8", newline="\n")
        command.chmod(0o755)

    def write_installer(self, source: str) -> None:
        fixed_directory = "readonly SERVER_DIR=/mnt/server"
        self.assertEqual(source.count(fixed_directory), 1, "Installer SERVER_DIR declaration changed.")
        self.script.write_text(
            source.replace(fixed_directory, f"readonly SERVER_DIR={shlex.quote(self.shell_path(self.server))}"),
            encoding="utf-8", newline="\n",
        )

    def make_release(self, version: str = "v0.9.18") -> dict:
        self.release_version = version
        self.asset_name = f"project-reclaimer-dedicated-{version}.exe"
        self.executable_url = f"{DOWNLOAD_BASE}/{version}/{self.asset_name}"
        self.checksum_url = f"{DOWNLOAD_BASE}/{version}/SHA256SUMS.txt"
        metadata = {
            "tag_name": version,
            "draft": False,
            "prerelease": False,
            "assets": [
                {"name": self.asset_name, "browser_download_url": self.executable_url},
                {"name": "SHA256SUMS.txt", "browser_download_url": self.checksum_url},
            ],
        }
        self.write_metadata(metadata)
        self.write_checksums(f"{hashlib.sha256(NEW_EXECUTABLE).hexdigest()}  {self.asset_name}\n")
        (self.fixtures / "dedicated.exe").write_bytes(NEW_EXECUTABLE)
        return metadata

    def write_metadata(self, metadata: dict) -> None:
        (self.fixtures / "release.json").write_text(json.dumps(metadata), encoding="utf-8")

    def write_checksums(self, content: str) -> None:
        (self.fixtures / "SHA256SUMS.txt").write_bytes(content.encode("utf-8"))

    def snapshot(self) -> dict[str, bytes]:
        return {
            str(path.relative_to(self.server)): path.read_bytes()
            for path in self.server.rglob("*") if path.is_file()
        }

    def run_install(self, version: str | None = None, **extra_env: str) -> subprocess.CompletedProcess:
        requested = version or "v0.9.18"
        normalized = requested if requested.startswith("v") else f"v{requested}"
        release_url = f"{API_BASE}/releases/latest" if requested == "latest" else f"{API_BASE}/releases/tags/{normalized}"
        env = os.environ.copy()
        env.pop("RECLAIMER_VERSION", None)
        env.update({
            "FIXTURE_ROOT": self.shell_path(self.fixtures),
            "FIXTURE_SERVER": self.shell_path(self.server),
            "FIXTURE_JQ": self.jq_shell,
            "FIXTURE_BIN": self.shell_path(self.bin),
            "FIXTURE_CURL_LOG": self.shell_path(self.directory / "curl.log"),
            "FIXTURE_APT_LOG": self.shell_path(self.directory / "apt.log"),
            "FIXTURE_RELEASE_URL": release_url,
            "FIXTURE_EXECUTABLE_URL": self.executable_url,
            "FIXTURE_CHECKSUM_URL": self.checksum_url,
            **extra_env,
        })
        if version is not None:
            env["RECLAIMER_VERSION"] = version
        return subprocess.run(
            [self.bash, "--noprofile", "--norc", "-c",
             'export PATH="$FIXTURE_BIN:$PATH"; exec /usr/bin/bash "$1"', "_", self.shell_path(self.script)],
            env=env, cwd=self.process_cwd, capture_output=True, text=True, timeout=30,
        )

    def downloaded_urls(self) -> list[str]:
        log = self.directory / "curl.log"
        return log.read_text(encoding="utf-8").splitlines() if log.exists() else []

    def assert_preserved(self, result: subprocess.CompletedProcess) -> None:
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.snapshot(), self.original, result.stdout + result.stderr)
        self.assertFalse(list(self.server.glob(".reclaimer-install.*")), "Staging directory leaked.")

    def assert_installed(self, result: subprocess.CompletedProcess) -> None:
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        expected = dict(self.original)
        expected["project-reclaimer.exe"] = NEW_EXECUTABLE
        expected[".reclaimer-version"] = f"{self.release_version}\n".encode()
        self.assertEqual(self.snapshot(), expected)
        self.assertFalse(list(self.server.glob(".reclaimer-install.*")), "Staging directory leaked.")

    def test_default_release_installs_verified_executable_and_preserves_content(self) -> None:
        self.assert_installed(self.run_install())
        self.assertEqual(self.downloaded_urls(), [
            f"{API_BASE}/releases/tags/v0.9.18", self.checksum_url, self.executable_url,
        ])

    def test_latest_release_resolves_metadata_tag(self) -> None:
        self.make_release("v0.9.19")
        self.assert_installed(self.run_install("latest"))
        self.assertEqual(self.downloaded_urls()[0], f"{API_BASE}/releases/latest")

    def test_bundled_egg_installs_assets_under_server_from_different_cwd(self) -> None:
        egg = json.loads((ROOT / "egg-project-reclaimer.json").read_text(encoding="utf-8"))
        self.write_installer(egg["scripts"]["installation"]["script"])
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.server / "project-reclaimer.exe").read_bytes(), NEW_EXECUTABLE)
        self.assertEqual((self.server / ".reclaimer-version").read_text(), "v0.9.18\n")
        for source, destination in (
            ("runtime/reclaimer.py", "reclaimer.py"),
            ("config/dedicated.toml", "dedicated.toml"),
            ("config/slayer.playlist.json", "playlists/slayer.playlist.json"),
        ):
            with self.subTest(bundled_file=destination):
                self.assertEqual(
                    (self.server / destination).read_bytes(),
                    (ROOT / source).read_text(encoding="utf-8").encode("utf-8"),
                )
        self.assertEqual(json.loads((self.server / "bans.json").read_text()), {"players": [], "ips": []})
        for relative in ("game/halo3/maps/info", "content/Maps", "content/Images", "content/Game Modes", "content/Mods", "data"):
            self.assertTrue((self.server / relative).is_dir(), relative)
        for relative, original in self.original.items():
            if relative not in ("project-reclaimer.exe", ".reclaimer-version"):
                self.assertEqual((self.server / relative).read_bytes(), original)
        self.assertEqual(list(self.process_cwd.iterdir()), [], "Bundled files escaped SERVER_DIR.")
        self.assertFalse(list(self.server.glob(".reclaimer-install.*")), "Staging directory leaked.")

    def test_version_without_v_prefix_is_normalized(self) -> None:
        self.assert_installed(self.run_install("0.9.18"))

    def test_checksum_accepts_crlf_uppercase_and_binary_marker(self) -> None:
        digest = hashlib.sha256(NEW_EXECUTABLE).hexdigest().upper()
        self.write_checksums(f"{'0' * 64}  different.exe\r\n{digest}\t*{self.asset_name}\r\n")
        self.assert_installed(self.run_install())

    def test_checksum_mismatch_preserves_existing_server(self) -> None:
        self.write_checksums(f"{'0' * 64}  {self.asset_name}\n")
        result = self.run_install()
        self.assert_preserved(result)
        self.assertIn("failed SHA-256 verification", result.stderr)

    def test_missing_duplicate_or_malformed_checksum_entry_rejects(self) -> None:
        digest = hashlib.sha256(NEW_EXECUTABLE).hexdigest()
        for content in (f"{digest}  another.exe\n", f"{digest}  {self.asset_name}\n" * 2,
                        f"invalid-hash  {self.asset_name}\n"):
            with self.subTest(checksum=content):
                self.write_checksums(content)
                self.assert_preserved(self.run_install())

    def test_missing_or_duplicate_release_assets_rejects(self) -> None:
        for name in (self.asset_name, "SHA256SUMS.txt"):
            for duplicate in (False, True):
                with self.subTest(asset=name, duplicate=duplicate):
                    metadata = self.make_release()
                    asset = next(item for item in metadata["assets"] if item["name"] == name)
                    if duplicate:
                        metadata["assets"].append(asset.copy())
                    else:
                        metadata["assets"].remove(asset)
                    self.write_metadata(metadata)
                    self.assert_preserved(self.run_install())

    def test_unofficial_asset_urls_reject_before_download(self) -> None:
        for name in (self.asset_name, "SHA256SUMS.txt"):
            with self.subTest(asset=name):
                metadata = self.make_release()
                next(item for item in metadata["assets"] if item["name"] == name)["browser_download_url"] = "https://example.invalid/file"
                self.write_metadata(metadata)
                self.assert_preserved(self.run_install())
        self.assertTrue(all(url.startswith(API_BASE) for url in self.downloaded_urls()))

    def test_unsupported_or_invalid_version_rejects_without_downloading(self) -> None:
        for version in ("v0.9.17", "0.8.99", "v0.9.18-rc.1", "v01.0.0", "../invalid"):
            with self.subTest(version=version):
                self.assert_preserved(self.run_install(version))
        self.assertEqual(self.downloaded_urls(), [])

    def test_latest_cannot_resolve_to_unsupported_release(self) -> None:
        self.make_release("v0.9.17")
        self.assert_preserved(self.run_install("latest"))
        self.assertEqual(self.downloaded_urls(), [f"{API_BASE}/releases/latest"])

    def test_partial_download_failures_preserve_existing_server(self) -> None:
        for url in (f"{API_BASE}/releases/tags/v0.9.18", self.checksum_url, self.executable_url):
            with self.subTest(url=url):
                self.assert_preserved(self.run_install(FIXTURE_FAIL_URL=url))

    def test_failed_final_rename_restores_previous_version_marker(self) -> None:
        self.assert_preserved(self.run_install(FIXTURE_FAIL_FINAL_MOVE="1"))

if __name__ == "__main__":
    unittest.main()
