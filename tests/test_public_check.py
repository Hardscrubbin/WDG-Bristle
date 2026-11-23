"""[bristle] tools/public_check.py: what it reports and what it lets through. Real files in a
temporary folder. The wording tests use a made-up word list; key material is generated when the
test runs, so nothing in this file looks like a secret."""
import base64
import hashlib
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import public_check  # noqa: E402

WORDS = public_check.Words(any_case=("quagga", "zebra"), cased=("ZZ",), credits=("quagga",))
REAL_LIST = ROOT / "tools" / "public_words.txt"          # only in the working build, never exported


class PublicCheck(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def write(self, rel, text):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def hits(self, words=WORDS):
        return public_check.check_folder(str(self.root), words)[0]

    def test_a_clean_folder_passes(self):
        self.write("README.md", "Built on Watch Dogs Go by LOCOSP. zebras and a zzz are fine.\n")
        self.write("code.py", "for zz, act in enumerate(actions):\n    pass\n")
        self.assertEqual(self.hits(), [])
        self.assertEqual(public_check.main([str(self.root)]), 0)

    def test_each_word_is_reported_with_file_and_line(self):
        for word in ("quagga", "Zebra", "ZEBRA", "ZZ"):
            with self.subTest(word=word):
                self.write("notes.md", "first line\nsomething %s something\n" % word)
                found = self.hits()
                self.assertEqual(len(found), 1, found)
                self.assertTrue(found[0].startswith("notes.md:2: wording"), found[0])
        self.write("notes.md", "the zebra_firmware module and wdg-zebra-old\n")
        self.assertEqual(len(self.hits()), 1)                    # as part of an identifier or a name too
        self.write("notes.md", "ok\n")
        self.write("docs/old-zebra-name.txt", "ok\n")
        self.assertEqual(self.hits(), ["docs/old-zebra-name.txt: wording in the name"])

    def test_without_a_word_list_only_secrets_are_checked(self):
        self.write("notes.md", "a zebra and a quagga\n")
        self.assertEqual(self.hits(words=None), [])
        self.assertEqual(public_check.main([str(self.root)]), 0)

    def test_only_one_credits_line_is_let_through(self):
        line = "Parts of this build come from Quagga's edition: https://example.invalid/quagga-edition\n"
        self.write("THIRD_PARTY_NOTICES.md", "# Notices\n\n" + line)
        self.assertEqual(self.hits(), [])
        self.write("THIRD_PARTY_NOTICES.md", "# Notices\n\n" + line + "\nAnd quagga again.\n")
        self.assertEqual([h.split(":")[1] for h in self.hits()], ["5"])
        self.write("THIRD_PARTY_NOTICES.md", "# Notices\n\nquagga and a zebra on one line\n")
        self.assertEqual(len(self.hits()), 1)                    # a credits line may hold credits words only
        self.write("THIRD_PARTY_NOTICES.md", "# Notices\n")
        self.write("README.md", line)                              # the same line anywhere else is not exempt
        self.assertEqual(len(self.hits()), 1)

    def test_one_file_may_hold_the_old_names_and_nothing_else_is_excused_there(self):
        self.assertEqual(public_check.ALLOWED_FILES, ("watchdogs/legacy_names.py",))
        key = hashlib.sha256(b"fixture for the allowed file").hexdigest()[:32]
        self.write("watchdogs/legacy_names.py", 'OLD = "wdg-zebra-v3"\nAPI_KEY = "%s"\n' % key)
        self.write("watchdogs/other.py", 'OLD = "wdg-zebra-v3"\n')
        self.assertEqual(self.hits(), ["watchdogs/legacy_names.py:2: looks like a key or token (32 characters)",
                                       "watchdogs/other.py:1: wording: zebra"])

    def test_a_word_list_file(self):
        self.write("words.txt", "# comment\nzebra\ncredits quagga   # the credits word\ncased ZZ\n\n")
        words = public_check.Words.from_file(str(self.root / "words.txt"))
        self.assertEqual((words.any_case, words.cased, words.credits), (("zebra", "quagga"), ("ZZ",), ("quagga",)))
        (self.root / "words.txt").unlink()
        self.write("a.txt", "Zebra\nzz\nZZ\n")
        self.assertEqual(self.hits(words), ["a.txt:1: wording: Zebra", "a.txt:3: wording: ZZ"])
        self.write("words.txt", "zebra\n")
        self.assertEqual(public_check.main([str(self.root), "--words", str(self.root / "words.txt")]), 1)
        self.assertEqual(public_check.main([str(self.root), "--words"]), 2)

    def test_secrets_and_keys(self):
        hex_key = hashlib.sha256(b"fixture one").hexdigest()[:32]
        mixed = base64.b64encode(hashlib.sha256(b"fixture 3").digest()).decode()[:32].replace("+", "A").replace("/", "b")
        self.assertTrue(public_check.looks_random(hex_key))
        self.assertTrue(public_check.looks_random(mixed), mixed)
        self.write("secrets.conf", "X=1\n")
        self.write("conf.py", 'API_KEY = "%s"\ntoken: %s\n' % (hex_key, mixed))
        found = "\n".join(self.hits())
        self.assertIn("secrets.conf: a secrets.conf file", found)
        self.assertIn("conf.py:1: looks like a key or token", found)
        self.assertIn("conf.py:2: looks like a key or token", found)

    def test_a_private_key_block(self):
        try:
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric import ed25519
        except ImportError:
            self.skipTest("the cryptography package is not installed")
        pem = ed25519.Ed25519PrivateKey.generate().private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
        self.write("keys/a.pem", pem)
        self.assertEqual(self.hits(), ["keys/a.pem:1: a private key block"])

    def test_placeholders_and_test_vectors_are_not_keys(self):
        self.write("secrets.conf.example", "# BRISTLE_OWNER_KEY=%s\nWIGLE_API_TOKEN=your-token-here\n" % ("0" * 32))
        self.write("vectors.yaml", 'key: "000102030405060708090a0b0c0d0e0f"\nTOKEN = "0123456789abcdef0123456789abcdef"\n'
                   'api_key_path = "/home/user/some/long/path/to/a/file.txt"\n'
                   'token_reader = SomeLongIdentifierNameForVersion2Reader\n')
        self.assertEqual(self.hits(), [])
        self.assertFalse(public_check.looks_random("SomeLongIdentifierNameForVersion2Reader"))

    def test_key_material_with_a_long_run_of_letters_is_still_a_key(self):
        for seed in range(40):
            value = base64.b64encode(hashlib.sha256(b"run %d" % seed).digest()).decode()[:40]
            value = value.replace("+", "A").replace("/", "b")
            if len(set(value.lower())) >= 8 and all(any(c in group for c in value) for group in
                                                    ("abcdefghijklmnopqrstuvwxyz", "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "0123456789")):
                with self.subTest(seed=seed):
                    self.assertTrue(public_check.looks_random(value), value)

    def test_dead_relative_links_in_markdown(self):
        self.write("docs/guide.md", "ok\n")
        self.write("docs/pic.png", "x")
        self.write("LICENSE", "x")
        good = ("[guide](docs/guide.md) [part](docs/guide.md#install) ![p](docs/pic.png) [top](#here)\n"
                "[site](https://example.invalid/a/b.md) <https://example.invalid> [mail](mailto:a@example.invalid)\n"
                "[lic](LICENSE) [abs](/docs/guide.md) [spaced](docs/guide.md \"title\") [dir](docs/)\n"
                "```\n[example](not/there.md)\n```\n"
                "an inline `[example](not/there.md)` too\n"
                "[ref]: docs/guide.md\n")
        self.write("README.md", good)
        self.write("docs/inner.md", "[up](../LICENSE) [same](guide.md) [pic](./pic.png)\n")
        self.assertEqual(self.hits(words=None), [])
        self.write("README.md", good + "\nsee [gone](docs/gone.md) and ![shot](docs/media/shot.png)\n"
                   "[`code name`](docs/also-gone.md)\n[ref2]: docs/nope.md\n[case](docs/Guide.md)\n")
        self.write("docs/inner.md", "[out](../../elsewhere.md)\n")
        self.assertEqual(self.hits(words=None), [
            "README.md:10: dead link: docs/gone.md",
            "README.md:10: dead link: docs/media/shot.png",
            "README.md:11: dead link: docs/also-gone.md",
            "README.md:12: dead link: docs/nope.md",
            "README.md:13: dead link: docs/Guide.md",
            "docs/inner.md:1: link leaves the folder: ../../elsewhere.md"])
        self.assertEqual(public_check.main([str(self.root)]), 1)
        self.write("notes.txt", "[gone](docs/gone.md)\n")          # only Markdown files are read for links
        self.assertEqual(len(self.hits(words=None)), 6)

    def test_github_owners(self):
        # every address in this file has an owner that is on the real list; the test narrows the list
        only = ("fcambus", "nvkelso")
        text = ("font: https://github.com/fcambus/spleen and https://raw.githubusercontent.com/nvkelso/natural-earth-vector/x\n"
                "game: https://github.com/LOCOSP/WatchDogsGo\n"
                "api: https://api.github.com/repos/espressif/arduino-esp32 pages: https://locosp.github.io/x\n"
                "clone: git@github.com:h2zero/NimBLE-Arduino.git\n"
                "not accounts: https://github.com/user-attachments/assets/1 https://docs.github.com/en/x\n"
                "built at run time: https://github.com/{owner}/{repo} https://github.com/<owner>\n")
        self.write("docs/a.md", text)
        found = public_check.check_folder(str(self.root), None, owners=only)[0]
        self.assertEqual(found, ["docs/a.md:2: GitHub owner not on the allowed list: LOCOSP",
                                 "docs/a.md:3: GitHub owner not on the allowed list: espressif",
                                 "docs/a.md:3: GitHub owner not on the allowed list: locosp",
                                 "docs/a.md:4: GitHub owner not on the allowed list: h2zero"])
        self.assertEqual(self.hits(words=None), [])                       # the real list has all of them
        self.assertEqual(public_check.check_folder(str(self.root), None, owners=None)[0], [])   # check off
        self.write("docs/a.md", "ok\n")
        self.write("bristle/firmware/licenses/x.txt", text)               # another project's own text
        self.assertEqual(public_check.check_folder(str(self.root), None, owners=only)[0], [])
        self.write("bristle/firmware/licenses/x.txt", "ok\n")
        credit = "From [an edition](https://github.com/LOCOSP/WDGoNG) and https://github.com/espressif/esptool\n"
        self.write("THIRD_PARTY_NOTICES.md", "# Notices\n" + credit)
        self.assertEqual(public_check.check_folder(str(self.root), None, owners=only)[0], [])   # one credits line
        self.write("THIRD_PARTY_NOTICES.md", "# Notices\n" + credit + "again https://github.com/LOCOSP/projectZero\n")
        self.assertEqual(public_check.check_folder(str(self.root), None, owners=only)[0],
                         ["THIRD_PARTY_NOTICES.md:3: GitHub owner not on the allowed list: LOCOSP"])

    def test_binaries_are_read_for_machine_paths_and_words(self):
        user = "Users" + chr(92) + "someone"
        blob = (b"\x00\x01\xff" * 50 + ("C:" + chr(92) + user + chr(92) + "build" + chr(92) + "main.c").encode() + b"\x00" * 9
                + b"/home/someone/project/x.c\x00\xfe\xfd" + "D:/Users/x/y".encode("utf-16-le") + b"\x00\x00"
                + b"\x89PNG fine text here, nothing to see\x00")
        (self.root / "fw").mkdir()
        (self.root / "fw" / "image.bin").write_bytes(blob)
        (self.root / "fw" / "clean.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00\x10 plain strings only /toolchain/x.c\x00")
        found = self.hits(words=None)
        self.assertEqual(len(found), 3, found)
        self.assertTrue(all(hit.startswith("fw/image.bin: byte ") and "a folder of the build machine" in hit
                            for hit in found), found)
        (self.root / "fw" / "image.bin").write_bytes(b"\x00" * 40 + b"made by a Zebra on a quagga farm\x00\x01" + b"x\x00ZZ;q\x00")
        self.assertEqual(self.hits(), ["fw/image.bin: byte 40: wording: Zebra, wording: quagga: made by a Zebra on a quagga farm"])
        # two or three letters turn up by chance in compressed data: they count only in readable text
        (self.root / "fw" / "image.bin").write_bytes(b"\x00k#ZZ;%\x00\x02" + b"the ZZ unit of this board is fine\x00")
        self.assertEqual(len(self.hits()), 1)
        many = b"".join(b"\x00/home/u%d/a.c\x00" % n for n in range(9))
        (self.root / "fw" / "image.bin").write_bytes(many)
        found = self.hits(words=None)
        self.assertEqual(len(found), public_check.BINARY_SHOWN + 1)
        self.assertTrue(found[-1].endswith("... and 4 more"), found[-1])

    def test_four_original_icons_are_excused_by_hash_and_only_while_unchanged(self):
        words = public_check.Words(cased=["Marker"])
        marked = b"\x89PNG....Created by Some Marker tool.....IEND"
        rel = "assets/scan.png"
        self.assertTrue(public_check.check_binary(rel, marked, words))             # same path, other bytes
        with patch.dict(public_check.EXCUSED_BINARIES, {rel: hashlib.sha256(marked).hexdigest()}):
            self.assertEqual(public_check.check_binary(rel, marked, words), [])    # the exact file
            self.assertTrue(public_check.check_binary(rel, marked + b" ", words))  # changed by one byte
            self.assertTrue(public_check.check_binary("assets/other.png", marked, words))   # another path
            leak = marked + b" /home/someone/icons/scan.png "
            with patch.dict(public_check.EXCUSED_BINARIES, {rel: hashlib.sha256(leak).hexdigest()}):
                self.assertEqual(len(public_check.check_binary(rel, leak, words)), 1)   # a machine path still counts
        self.assertEqual(sorted(public_check.EXCUSED_BINARIES),
                         ["assets/addons.png", "assets/attack.png", "assets/scan.png", "assets/sniff.png"])
        for name, digest in public_check.EXCUSED_BINARIES.items():
            path = ROOT / name
            if path.is_file():
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest, name)

    @unittest.skipUnless(REAL_LIST.is_file(), "the word list is not part of this tree")
    def test_the_real_word_list_reads_and_the_tool_and_this_test_pass_it(self):
        words = public_check.Words.from_file(str(REAL_LIST))
        self.assertGreaterEqual(len(words.any_case), 8)
        self.assertEqual(len(words.credits), 2)
        for rel in ("tools/public_check.py", "tests/test_public_check.py"):
            text = (ROOT / rel).read_text(encoding="utf-8")
            self.assertEqual(public_check.check_text(rel, text, words), [], rel)


if __name__ == "__main__":
    unittest.main()
