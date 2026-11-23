# Contributing to WDG-Bristle

WDG-Bristle is built on [Watch Dogs Go by LOCOSP](https://github.com/LOCOSP/WatchDogsGo).
Contributions should keep the original game's style and never put a player's data at risk. Use a
separate checkout for development: see [DEVELOPMENT.md](DEVELOPMENT.md).

## In short

- **Bug reports**: open an [issue](https://github.com/Hardscrubbin/WDG-Bristle/issues) and attach
  only reviewed, redacted excerpts from `~/.watchdogs/last_run.log` and `~/.watchdogs/launcher.log`.
- **Feature ideas**: open an issue first, and let's talk before you write code.
- **Pull requests**: target `main`. Small focused PRs merge faster than big ones.
- **A fault that is also in the original game** belongs upstream too:
  [LOCOSP/WatchDogsGo](https://github.com/LOCOSP/WatchDogsGo).
- **Be nice.** This is meant to be fun.

---

## Reporting bugs

Before opening an issue:

1. Check the [feature guide](docs/FEATURE_GUIDE.md) and the dated history in
   [CHANGELOG.md](CHANGELOG.md). BLE HID, HID Type, BlueDucky and RACE Attack remain disabled WIP
   menu entries. **SYSTEM → Download Map is implemented**; it requires a configured Stadia API key.
2. Note the version the game shows at startup and the commit you are on (`git rev-parse --short HEAD`).
   If you are behind, [update](docs/UPDATING.md) and try again.
3. Include the hardware the failing feature uses. Most UI tests work without an ESP32; WiFi/BLE
   hardware features need the compatible ESP32 and firmware, and LoRa needs its own module.

**A good bug report contains:**

- What you tried to do (one sentence)
- What happened instead (one sentence)
- A reviewed, redacted excerpt of the last 30 lines of `~/.watchdogs/last_run.log`
- A reviewed, redacted excerpt of the last 20 lines of `~/.watchdogs/launcher.log` if the game
  crashed before opening
- Your hardware: `uname -a`, `lsusb`, `python3 --version`
- A screenshot if it's a UI bug
- For Bristle Nodes: the board type, and what the node prints for `status` on its serial console

The log files contain GPS coordinates, BSSIDs, and sometimes captured credentials. **Review them
before pasting into a public issue.**

---

## Setting up a dev environment

```bash
git clone https://github.com/Hardscrubbin/WDG-Bristle.git wdg-bristle-dev
cd wdg-bristle-dev
bash setup.sh        # creates .venv and installs dependencies in this separate checkout
./run.sh             # the launcher obtains sudo for GPIO/serial
```

You don't need an ESP32 to run the software tests. They use fake transports, temporary player data
and recorded drawing calls, without opening a radio. An interactive game startup can attempt
hardware detection and share home-directory settings with another checkout, so it is not an
isolated test by itself.

```bash
.venv/bin/python3 -B -m unittest discover -s tests
.venv/bin/python3 -B -m unittest test_meshtastic_rx_probe test_spi_mutation_regression
.venv/bin/python3 -B -m unittest discover -s bristle/tests
python3 -B tools/public_check.py
```

Optional platform dependencies can cause explicit skips. Keep private captures outside Git. Record
the exact command, commit, failures and skips with your change.

For LoRa and AIO v2 features you need a ClockworkPi uConsole with the AIO v2 module. For the
game's own WiFi and BLE features you need an ESP32-C5 running
[projectZero](https://github.com/LOCOSP/projectZero). For Bristle Nodes you need an ESP32-C5 board
with 8 MB flash: see `bristle/docs/BOARDS.md`.

---

## Code style

- **Python 3.10+**, no type-checking required but typed signatures welcome.
- Match the surrounding style of the file you're editing.
- Keep functions small. `app.py` is already large: prefer a new module in `watchdogs/` or a plugin.
- A hand edit to one of the original game's files carries a `[bristle]` tag in a comment, so it
  can be found again.
- The node protocol has one source, `bristle/spec/bristle_proto.yaml`. Change it there and run
  `bristle/tools/gen_proto.py`; never edit the generated files.
- **No emoji in code or commit messages** unless the surrounding context uses them.
- **No `Co-Authored-By` trailers for tools** in commits: the commit author is you.

## Commit messages

Format: `<type>: <one-line summary>`

Where `<type>` is one of `feat`, `fix`, `docs`, `refactor`, `chore`, `perf`, `test`. Examples from
the original game's history:

```
feat: badge sync — push game badges to server, pull server badges to game
fix: every fresh user broadcasted as literal 'WatchDogs' on LoRa mesh
docs: add one-liner install command to README
chore: bump pyxel to 2.5.0
```

The body (after a blank line) should explain *why*, not *what*: the diff shows what.

---

## Plugin development

Plugins live in `plugins/` and are auto-discovered. The simplest plugin:

```python
# plugins/my_plugin.py
from plugins.plugin_base import PluginBase, PluginMenuItem

class MyPlugin(PluginBase):
    NAME = "My Plugin"
    VERSION = "1.0"
    AUTHOR = "yourname"

    def menu_items(self):
        return [PluginMenuItem("m", "My Plugin", "open_overlay")]

    def open_overlay(self):
        self.msg("Hello from my plugin!", 11)
```

Drop it in `plugins/`, restart the game, and it appears under the **PLUGINS** menu tab. Select the
entry with the normal arrows/Enter menu controls; its displayed key prefix is metadata, not a
global key binding. See `plugins/wardrive_upload.py` for a real-world example with a UI, threaded
HTTP requests, persistent state and background workers. Read `plugins/plugin_base.py` for the full
base class.

---

## Licence

By contributing, you agree that your contributions are licensed under the MIT licence, like the
rest of the repository: see [LICENSE](LICENSE) (LOCOSP's game) and
[LICENSE-HARDSCRUBBIN](LICENSE-HARDSCRUBBIN) (the additions).

---

## Project contacts

- **[Issues](https://github.com/Hardscrubbin/WDG-Bristle/issues)**: bugs and feature discussions for WDG-Bristle.
- **[LOCOSP](https://github.com/LOCOSP)**: original Watch Dogs Go author; original credits and licence are retained.
