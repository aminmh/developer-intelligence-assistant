"""Offline bootstrap contracts; run: python3 -m unittest discover -s scripts/installer/tests -p test_bootstrap.py."""
import hashlib
import io
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest


SOURCE = Path(__file__).resolve().parents[3]
PINS = {
    ("Darwin", "arm64"): ("aarch64-apple-darwin", "50ae3e996c974a0bf32ea7d10f495070df33f1b43e0616b2769e3d4821ed8f48"),
    ("Darwin", "x86_64"): ("x86_64-apple-darwin", "9a09cfef66aaa79da58203970103a0684307716caaabd3e9844cacc4dc0f4023"),
    ("Linux", "aarch64"): ("aarch64-unknown-linux-musl", "748237128c4c40cbdabc65e841d05ceba13cc23a91eaba395495894c1d9764df"),
    ("Linux", "x86_64"): ("x86_64-unknown-linux-musl", "4a5cc2f53e6f0f8c59092a6cc38291eb729d46a7dd95d3ae582008881b84931d"),
}


class BootstrapTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="bootstrap test ")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "application folder"
        (self.root / "scripts/installer").mkdir(parents=True)
        for name in ("install.sh", "scripts/installer/bootstrap.sh"):
            shutil.copy2(SOURCE / name, self.root / name)
        (self.root / "justfile").write_text("# fixture\n")
        self.bin = self.base / "tools"
        self.bin.mkdir()
        self.log = self.base / "log"
        self.env = dict(os.environ, PATH=f"{self.bin}:/usr/bin:/bin", BOOT_LOG=str(self.log),
                        BOOT_OS="Linux", BOOT_ARCH="x86_64", BOOT_FAILURES="0")
        self.just_body = '#!/bin/bash\nif [[ "$1" = --version ]]; then echo "just 1.58.0"; else printf "%s\\n" "$@" >> "$BOOT_LOG"; fi\n'
        self.archive = self.base / "fixture.tar.gz"
        self.make_archive()
        self.stub("uname", 'if [[ "$1" = -s ]]; then echo "$BOOT_OS"; else echo "$BOOT_ARCH"; fi')
        self.stub("sleep", 'echo "sleep:$1" >> "$BOOT_LOG"')
        self.stub("curl", '''echo "curl:$*" >> "$BOOT_LOG"
count=0
[[ ! -f "$BOOT_COUNT" ]] || read -r count < "$BOOT_COUNT"
count=$((count + 1))
echo "$count" > "$BOOT_COUNT"
output=""
while (( $# )); do
    if [[ "$1" = --output ]]; then output=$2; shift; fi
    shift
done
if (( count <= BOOT_FAILURES )); then printf partial >> "$output"; exit 7; fi
cp "$BOOT_ARCHIVE" "$output"
''')
        self.env.update(BOOT_ARCHIVE=str(self.archive), BOOT_COUNT=str(self.base / "count"))

    def stub(self, name, body):
        path = self.bin / name
        path.write_text("#!/bin/bash\nset -eu\n" + body + "\n")
        path.chmod(0o755)

    def make_archive(self, symlink=False, traversal=False):
        with tarfile.open(self.archive, "w:gz") as tar:
            item = tarfile.TarInfo("just")
            if symlink:
                item.type = tarfile.SYMTYPE
                item.linkname = "../../escaped"
                tar.addfile(item)
            else:
                data = self.just_body.encode()
                item.size = len(data)
                item.mode = 0o755
                tar.addfile(item, io.BytesIO(data))
            if traversal:
                item = tarfile.TarInfo("../../escaped")
                item.size = 3
                tar.addfile(item, io.BytesIO(b"bad"))
        self.fixture_digest = hashlib.sha256(self.archive.read_bytes()).hexdigest()
        # The fake checksum tool recognizes only the exact fixture bytes. Real
        # release pin values still have to match the implementation per target.
        self.stub("sha256sum", f'''actual=$(/usr/bin/shasum -a 256 "$1")
if [[ "${{actual%% *}}" = {self.fixture_digest} ]]; then
    case "$BOOT_OS:$BOOT_ARCH" in
''' + "\n".join(f'        {os_name}:{arch}) digest={pin} ;;' for (os_name, arch), (_, pin) in PINS.items()) + '''
        *) exit 1 ;;
    esac
else digest=000000; fi
printf '%s  %s\\n' "$digest" "$1"
''')

    def run_bootstrap(self, *args):
        return subprocess.run(["/bin/bash", str(self.root / "install.sh"), *args], env=self.env,
                              cwd=self.base, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def output(self):
        return self.log.read_text() if self.log.exists() else ""

    def cached_path(self):
        target = PINS[(self.env["BOOT_OS"], self.env["BOOT_ARCH"])][0]
        return self.root / f".install/downloads/just-1.58.0-{target}.tar.gz"

    def test_compatible_system_just_runs_default_without_download(self):
        self.stub("just", self.just_body.split("\n", 1)[1])
        result = self.run_bootstrap()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.output().splitlines(), ["--justfile", str(self.root / "justfile"), "install-resume"])
        self.assertFalse((self.root / ".install").exists())

    def test_installs_all_four_pinned_platform_archives(self):
        for os_name, arch in PINS:
            with self.subTest(os=os_name, arch=arch):
                self.env.update(BOOT_OS=os_name, BOOT_ARCH=arch)
                shutil.rmtree(self.root / ".install", ignore_errors=True)
                result = self.run_bootstrap("install-check")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue(self.cached_path().exists())
                self.assertTrue((self.root / ".install/bin/just").is_file())
                self.assertIn("install-check", self.output())

    def test_old_or_prerelease_system_just_is_replaced_locally(self):
        for version in ("1.39.9", "1.40.0-beta", "unexpected", "2.0.0"):
            with self.subTest(version=version):
                shutil.rmtree(self.root / ".install", ignore_errors=True)
                self.stub("just", f'echo "just {version}"')
                result = self.run_bootstrap()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue((self.root / ".install/bin/just").exists())

    def test_minimum_system_just_and_forwarded_commands(self):
        self.stub("just", self.just_body.split("\n", 1)[1].replace("1.58.0", "1.40.0"))
        for command in ("install-check", "install-status", "install-info", "status", "info", "check"):
            result = self.run_bootstrap(command)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(self.output().endswith("install-" + command.removeprefix("install-") + "\n"))

    def test_valid_cached_archive_avoids_network(self):
        cached = self.cached_path()
        cached.parent.mkdir(parents=True)
        shutil.copyfile(self.archive, cached)
        result = self.run_bootstrap()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("curl:", self.output())

    def test_corrupt_cached_archive_retained_and_replaced(self):
        cached = self.cached_path()
        cached.parent.mkdir(parents=True)
        cached.write_text("corrupt")
        result = self.run_bootstrap()
        self.assertEqual(result.returncode, 0, result.stderr)
        rejected = list(cached.parent.glob("*.rejected.*"))
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0].read_text(), "corrupt")

    def test_completed_partial_is_verified_without_network(self):
        cached = self.cached_path()
        cached.parent.mkdir(parents=True)
        shutil.copyfile(self.archive, str(cached) + ".part")
        result = self.run_bootstrap()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(cached.exists())
        self.assertNotIn("curl:", self.output())

    def test_three_retries_use_resume_and_exponential_backoff(self):
        self.env["BOOT_FAILURES"] = "3"
        result = self.run_bootstrap()
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = self.output().splitlines()
        self.assertEqual(sum(line.startswith("curl:") for line in lines), 4)
        self.assertEqual([line for line in lines if line.startswith("sleep:")], ["sleep:1", "sleep:2", "sleep:4"])
        self.assertEqual(self.output().count("--continue-at -"), 4)

    def test_exhausted_retries_preserve_partial_for_next_run(self):
        self.env["BOOT_FAILURES"] = "9"
        result = self.run_bootstrap()
        self.assertNotEqual(result.returncode, 0)
        partial = Path(str(self.cached_path()) + ".part")
        self.assertTrue(partial.exists())
        self.assertIn("Partial download retained", result.stderr)
        self.env["BOOT_FAILURES"] = "0"
        self.assertEqual(self.run_bootstrap().returncode, 0)

    def range_curl_stub(self):
        self.stub("curl", '''output=""
while (( $# )); do
    if [[ "$1" = --output ]]; then output=$2; shift; fi
    shift
done
offset=0
[[ ! -f "$output" ]] || offset=$(wc -c < "$output")
offset=${offset// /}
echo "offset:$offset" >> "$BOOT_LOG"
if (( offset > 0 )); then
    if [[ "${BOOT_RANGE_REFUSED:-0}" = 1 ]]; then printf 200; exit 33; fi
    size=$(wc -c < "$BOOT_ARCHIVE")
    if (( offset >= size )); then printf 416; exit "${BOOT_RANGE_EXIT:-22}"; fi
    # Match curl --continue-at semantics: append only the requested suffix.
    dd if="$BOOT_ARCHIVE" bs=1 skip="$offset" 2>/dev/null >> "$output"
    printf 206
else
    cp "$BOOT_ARCHIVE" "$output"
    printf 200
fi
''')

    def test_oversized_partial_or_range_refusal_is_retained_and_retried_fresh(self):
        self.range_curl_stub()
        for exit_status in ("22", "0", "33"):
            with self.subTest(exit_status=exit_status):
                shutil.rmtree(self.root / ".install", ignore_errors=True)
                self.log.write_text("")
                cached = self.cached_path()
                cached.parent.mkdir(parents=True)
                damaged = b"corrupt" * len(self.archive.read_bytes())
                Path(str(cached) + ".part").write_bytes(damaged)
                self.env["BOOT_RANGE_REFUSED"] = "1" if exit_status == "33" else "0"
                self.env["BOOT_RANGE_EXIT"] = exit_status
                result = self.run_bootstrap()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(cached.read_bytes(), self.archive.read_bytes())
                rejected = list(cached.parent.glob("*.rejected.*"))
                self.assertEqual(len(rejected), 1)
                self.assertEqual(rejected[0].read_bytes(), damaged)
                self.assertIn("offset:0\n", self.output())
                self.assertEqual(self.output().count("offset:"), 2)

    def test_valid_partial_appends_only_remaining_range(self):
        self.range_curl_stub()
        cached = self.cached_path()
        cached.parent.mkdir(parents=True)
        original = self.archive.read_bytes()
        offset = len(original) // 2
        Path(str(cached) + ".part").write_bytes(original[:offset])
        result = self.run_bootstrap()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(cached.read_bytes(), original)
        self.assertIn(f"offset:{offset}\n", self.output())
        self.assertEqual(self.output().count("offset:"), 1)
        self.assertEqual(list(cached.parent.glob("*.rejected.*")), [])

    def test_download_checksum_mismatch_never_executes(self):
        self.archive.write_text("tampered")
        result = self.run_bootstrap()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SHA-256", result.stderr)
        self.assertNotIn("--justfile\n", self.output())
        self.assertEqual(len(list(self.cached_path().parent.glob("*.rejected.*"))), 1)

    def test_symlink_binary_rejected(self):
        self.make_archive(symlink=True)
        result = self.run_bootstrap()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("one regular just binary", result.stderr)
        self.assertFalse((self.root / ".install/bin/just").exists())

    def test_unrelated_archive_paths_are_never_extracted(self):
        self.make_archive(traversal=True)
        result = self.run_bootstrap()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.base / "escaped").exists())
        self.assertFalse((self.root / "escaped").exists())

    def test_unsupported_platform_and_bad_arguments_fail(self):
        self.env["BOOT_ARCH"] = "riscv64"
        result = self.run_bootstrap()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Unsupported platform", result.stderr)
        self.assertNotIn("curl:", self.output())
        for args in (("arbitrary-recipe",), ("install-info", "extra")):
            self.assertNotEqual(self.run_bootstrap(*args).returncode, 0)


if __name__ == "__main__":
    unittest.main()
