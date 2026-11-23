#!/usr/bin/env python3
"""Check a folder before it is published.

    python3 -B tools/public_check.py [FOLDER] [--words FILE]

It fails (exit 1) and prints ``file:line: what`` for every one of these:

  * a file called secrets.conf
  * a private key block
  * something that looks like an API key: a long random value given to a name such as key, token,
    secret or password, or a token in one of the well-known vendor formats
  * a relative link in a Markdown file (a link or a picture) to a file that is not in the folder,
    or whose letter case differs from the real name (that works on Windows and breaks elsewhere)
  * a GitHub address whose owner is not on the allowed list (ALLOWED_OWNERS below): a link to an
    account that does not exist can be registered by anyone
  * inside a binary file (an image, a firmware image, a font, a sound): a folder of the machine it
    was made on ("Users\\..." or "/home/...")
  * with --words FILE: any word from that list, as a whole word, in a text file, in a file name or
    in the readable strings of a binary file.

The word list is not part of this tool. It is a plain text file, one entry per line:

    word            any letter case
    cased Word      exactly this letter case
    credits word    as "word", and also allowed on ONE line of THIRD_PARTY_NOTICES.md (the credits
                    line). A second line with such a word is reported like any other.

One file is the allowed place for names that devices and earlier installs still use
(ALLOWED_FILES below). Its wording is not checked. Everything else about it is.

Folders called "licenses" hold other projects' licence texts, word for word; the addresses in
them are theirs and are not checked against the owner list. ONE line of THIRD_PARTY_NOTICES.md
may link to an owner that is not on the list (the credits line).
"""
import os
import re
import sys
from urllib.parse import unquote

CREDITS_FILE = "THIRD_PARTY_NOTICES.md"
ALLOWED_FILES = ("watchdogs/legacy_names.py",)
_EDGE = r"(?<![A-Za-z0-9])(?:%s)(?![A-Za-z0-9])"
PRIVATE_KEY_RE = re.compile(r"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY-----")
NAMED_VALUE_RE = re.compile(
    r"""(?ix)
    (?:api[_-]?key|apikey|token|secret|passw(?:or)?d|passwd|owner[_-]?key|private[_-]?key|bearer|credential)
    [A-Za-z0-9_]*                       # the rest of the name
    ["']?\s*[:=]\s*["']?                # = or :
    ([A-Za-z0-9+/_\-]{24,}={0,2})       # the value
    """)
VENDOR_RE = re.compile(r"(?<![A-Za-z0-9])(?:AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_\-]{35}|gh[pousr]_[A-Za-z0-9]{36,}"
                       r"|xox[baprs]-[A-Za-z0-9-]{10,}|sk-[A-Za-z0-9]{32,})(?![A-Za-z0-9])")
SKIP_DIRS = {"__pycache__", ".git"}
ALLOWED_OWNERS = (
    "LOCOSP", "Hardscrubbin",                                   # the game's author; this repository
    "espressif", "h2zero", "apache", "Mbed-TLS",                # what the node firmware is built from
    "kismetwireless", "flightaware", "meshtastic", "meshcore-dev", "hackergadgets",   # tools the game drives
    "fcambus", "nvkelso",                                       # the Spleen font, Natural Earth
    "justcallmekoko", "dark3d", "FusedStamen",                  # credited authors and contributors
)
NOT_OWNERS = {"user-attachments", "orgs", "features", "about", "login", "settings", "sponsors", "marketplace",
              "topics", "search", "apps"}                       # parts of github.com that are not accounts
VERBATIM_DIRS = {"licenses"}
OWNER_RE = re.compile(
    r"(?<![A-Za-z0-9.@\-])(?:(?:www\.)?github\.com/|raw\.githubusercontent\.com/|codeload\.github\.com/"
    r"|api\.github\.com/repos/)([A-Za-z0-9][A-Za-z0-9\-]*)"
    r"|git@github\.com:([A-Za-z0-9][A-Za-z0-9\-]*)"
    r"|(?<![A-Za-z0-9.\-])([A-Za-z0-9][A-Za-z0-9\-]*)\.github\.io", re.I)
RUN_RE = re.compile(rb"[\x20-\x7e]{4,}")                         # what the "strings" tool prints
WIDE_RUN_RE = re.compile(rb"(?:[\x20-\x7e]\x00){4,}")            # the same in two-byte text
MACHINE_PATH_RE = re.compile(r"Users[\\/]|/home/")
BINARY_SHOWN = 5                                                # findings printed per binary file
MARKDOWN_EXT = {".md", ".markdown"}
LINK_RE = re.compile(r"\]\(\s*<?([^)\s>]+)")                           # [text](target) and ![alt](target)
LINK_DEF_RE = re.compile(r"^ {0,3}\[[^\]]+\]:\s*<?([^\s>]+)")            # [name]: target
FENCE_RE = re.compile(r"^ {0,3}(```|~~~)")
ELSEWHERE_RE = re.compile(r"^(?:[A-Za-z][A-Za-z0-9+.\-]*:|//|#)")       # a URL, mailto:, or a place on this page
BINARY_EXT = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".mp3", ".wav", ".mp4", ".mov", ".ttf", ".bin", ".zip",
              ".gz", ".pyc", ".pyxres", ".pyxpal", ".mbtiles", ".elf", ".ogg", ".bdf.gz"}


class Words:
    """The wording list: what to look for, and which of it the credits line may hold."""

    def __init__(self, any_case=(), cased=(), credits=()):
        self.any_case = tuple(w for w in any_case if w)
        self.cased = tuple(w for w in cased if w)
        self.credits = tuple(w for w in credits if w)
        self._any = re.compile(_EDGE % "|".join(map(re.escape, self.any_case)), re.I) if self.any_case else None
        self._cased = re.compile(_EDGE % "|".join(map(re.escape, self.cased))) if self.cased else None
        self._credits = re.compile(_EDGE % "|".join(map(re.escape, self.credits)), re.I) if self.credits else None
        short = [w for w in self.any_case if len(w) < 4], [w for w in self.cased if len(w) < 4]
        long_ = [w for w in self.any_case if len(w) >= 4], [w for w in self.cased if len(w) >= 4]
        self._short = (re.compile(_EDGE % "|".join(map(re.escape, short[0])), re.I) if short[0] else None,
                       re.compile(_EDGE % "|".join(map(re.escape, short[1]))) if short[1] else None)
        self._long = (re.compile(_EDGE % "|".join(map(re.escape, long_[0])), re.I) if long_[0] else None,
                      re.compile(_EDGE % "|".join(map(re.escape, long_[1]))) if long_[1] else None)

    @classmethod
    def from_file(cls, path):
        any_case, cased, credits = [], [], []
        with open(path, encoding="utf-8") as handle:
            for raw in handle:
                line = raw.split("#", 1)[0].strip()
                if not line:
                    continue
                kind, _, rest = line.partition(" ")
                if kind == "cased" and rest.strip():
                    cased.append(rest.strip())
                elif kind == "credits" and rest.strip():
                    any_case.append(rest.strip())
                    credits.append(rest.strip())
                else:
                    any_case.append(line)
        return cls(any_case, cased, credits)

    def __bool__(self):
        return bool(self.any_case or self.cased)

    def found(self, text) -> list:
        hits = set()
        if self._any is not None:
            hits |= {m.group(0) for m in self._any.finditer(text)}
        if self._cased is not None:
            hits |= {m.group(0) for m in self._cased.finditer(text)}
        return sorted(hits)

    def found_in_binary(self, run: str) -> list:
        """Words in one readable run of a binary file. A word of two or three letters counts only
        in a run that reads as text (16 characters or more, nearly all letters, digits and spaces):
        any compressed data has every two-letter pair in it somewhere, by chance."""
        hits = set()
        for pattern in self._long:
            if pattern is not None:
                hits |= {m.group(0) for m in pattern.finditer(run)}
        if len(run) >= 16 and sum(c.isalnum() or c == " " for c in run) >= 0.85 * len(run):
            for pattern in self._short:
                if pattern is not None:
                    hits |= {m.group(0) for m in pattern.finditer(run)}
        return sorted(hits)

    def only_credits(self, words) -> bool:
        return bool(self._credits is not None and words and all(self._credits.fullmatch(w) for w in words))


def looks_random(value: str) -> bool:
    """A real key is long and mixed. Placeholders and test patterns are not: one repeated
    character, a counting sequence (000102...), or plain words."""
    core = value.rstrip("=")
    if len(set(core.lower())) < 8:
        return False
    if core.lower() in "0123456789abcdef" * 8:
        return False                             # 0123456789abcdef...: a test fixture
    if re.fullmatch(r"[0-9a-fA-F]+", core) and len(core) % 2 == 0:
        data = bytes.fromhex(core)
        steps = {(b - a) % 256 for a, b in zip(data, data[1:])}
        if len(steps) == 1:                      # 00 01 02 ... or ff ee dd ...: a test vector
            return False
        return len(core) >= 32
    kinds = sum(bool(re.search(p, core)) for p in (r"[a-z]", r"[A-Z]", r"[0-9]"))
    if kinds < 3:
        return False                             # a path, a sentence, an identifier
    if not re.search(r"[A-Za-z]{12,}", core):
        return True
    # A long run of letters is either words (SomeLongIdentifierName: few, long pieces) or key
    # material (xQmhyjwGYmeSkBQOs: many short pieces, the case changes every letter or two).
    pieces = re.findall(r"[A-Z]?[a-z]+|[A-Z]+", core)
    return sum(map(len, pieces)) / len(pieces) < 3.5


def check_text(rel: str, text: str, words=None, owners=ALLOWED_OWNERS) -> list:
    hits = []
    credits_used = False
    credits_link_line = None
    wording = bool(words) and rel not in ALLOWED_FILES
    allowed = None if owners is None or VERBATIM_DIRS & set(rel.split("/")[:-1]) \
        else {owner.lower() for owner in owners} | NOT_OWNERS
    for number, line in enumerate(text.splitlines(), 1):
        where = "%s:%d" % (rel, number)
        found = words.found(line) if wording else []
        if found:
            if rel == CREDITS_FILE and words.only_credits(found) and not credits_used:
                credits_used = True              # the one credits line
            else:
                hits.append("%s: wording: %s" % (where, ", ".join(found)))
        for match in OWNER_RE.finditer(line) if allowed is not None else ():
            owner = match.group(1) or match.group(2) or match.group(3)
            if owner.lower() in allowed:
                continue
            if rel == CREDITS_FILE and credits_link_line in (None, number):
                credits_link_line = number       # the one credits line
                continue
            hits.append("%s: GitHub owner not on the allowed list: %s" % (where, owner))
        if PRIVATE_KEY_RE.search(line):
            hits.append("%s: a private key block" % where)
        for match in NAMED_VALUE_RE.finditer(line):
            if looks_random(match.group(1)):
                hits.append("%s: looks like a key or token (%d characters)" % (where, len(match.group(1))))
        if VENDOR_RE.search(line):
            hits.append("%s: a token in a known vendor format" % where)
    return hits


def check_binary(rel: str, data: bytes, words=None) -> list:
    """The readable strings of a binary file: folders of the machine it was made on, and (with a
    word list) the words. At most BINARY_SHOWN findings are spelled out per file."""
    found = []
    runs = [(m.start(), m.group().decode("ascii")) for m in RUN_RE.finditer(data)]
    runs += [(m.start(), m.group().decode("utf-16-le")) for m in WIDE_RUN_RE.finditer(data)]
    for offset, run in sorted(runs):
        what = []
        if MACHINE_PATH_RE.search(run):
            what.append("a folder of the build machine")
        if words and rel not in ALLOWED_FILES:
            what += ["wording: " + word for word in words.found_in_binary(run)]
        if what:
            found.append("%s: byte %d: %s: %s" % (rel, offset, ", ".join(what), run.strip()[:90]))
    if len(found) > BINARY_SHOWN:
        found[BINARY_SHOWN:] = ["%s: ... and %d more" % (rel, len(found) - BINARY_SHOWN)]
    return found


def _exact_path(root: str, parts) -> bool:
    """True when every part exists under root with exactly this letter case."""
    here = root
    for part in parts:
        try:
            if part not in os.listdir(here):
                return False
        except OSError:
            return False
        here = os.path.join(here, part)
    return True


def check_links(root: str, rel: str, text: str) -> list:
    """Relative links of one Markdown file that lead nowhere. Fenced blocks and `code` are examples,
    not links, and are skipped."""
    hits, fence = [], None
    folder = os.path.dirname(rel)
    for number, line in enumerate(text.splitlines(), 1):
        mark = FENCE_RE.match(line)
        if mark:
            fence = None if fence == mark.group(1) else (fence or mark.group(1))
            continue
        if fence:
            continue
        line = re.sub(r"`[^`]*`", "", line)
        for target in LINK_RE.findall(line) + LINK_DEF_RE.findall(line):
            if ELSEWHERE_RE.match(target):
                continue
            path = unquote(target.split("#", 1)[0].split("?", 1)[0])
            if not path:
                continue
            joined = path.lstrip("/") if path.startswith("/") else os.path.join(folder, path)
            parts = [p for p in os.path.normpath(joined).replace(os.sep, "/").split("/") if p not in ("", ".")]
            if parts and parts[0] == "..":
                hits.append("%s:%d: link leaves the folder: %s" % (rel, number, target))
            elif not _exact_path(root, parts):
                hits.append("%s:%d: dead link: %s" % (rel, number, target))
    return hits


def check_folder(root: str, words=None, owners=ALLOWED_OWNERS) -> tuple:
    """(hits, number of files looked at)."""
    hits, files = [], 0
    root = os.path.abspath(root)
    for folder, names, file_names in os.walk(root):
        names[:] = sorted(n for n in names if n not in SKIP_DIRS)
        if words:
            for name in sorted(names + file_names):
                rel = os.path.relpath(os.path.join(folder, name), root).replace(os.sep, "/")
                if words.found(name):
                    hits.append("%s: wording in the name" % rel)
        for name in sorted(file_names):
            path = os.path.join(folder, name)
            rel = os.path.relpath(path, root).replace(os.sep, "/")
            files += 1
            if name == "secrets.conf":
                hits.append("%s: a secrets.conf file" % rel)
            try:
                with open(path, "rb") as handle:
                    raw = handle.read()
            except OSError as exc:
                hits.append("%s: cannot be read (%s)" % (rel, exc))
                continue
            if os.path.splitext(name)[1].lower() in BINARY_EXT or b"\0" in raw[:8192]:
                hits.extend(check_binary(rel, raw, words))
                continue
            text = raw.decode("utf-8", "replace")
            hits.extend(check_text(rel, text, words, owners))
            if os.path.splitext(name)[1].lower() in MARKDOWN_EXT:
                hits.extend(check_links(root, rel, text))
    return hits, files


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    words = None
    if "--words" in args:
        index = args.index("--words")
        if index + 1 >= len(args):
            print("--words needs a file")
            return 2
        words = Words.from_file(args[index + 1])
        del args[index:index + 2]
    root = args[0] if args else os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
    hits, files = check_folder(root, words)
    for hit in hits:
        print(hit)
    scope = "wording (%d entries), secrets, keys, links, owners and binaries" % (len(words.any_case) + len(words.cased)) \
        if words else "secrets, keys, links, owners and binaries (no word list given)"
    print("public check, %s: %d file(s), %d problem(s)%s" % (scope, files, len(hits), "" if hits else " - OK"))
    return 1 if hits else 0


if __name__ == "__main__":
    raise SystemExit(main())
