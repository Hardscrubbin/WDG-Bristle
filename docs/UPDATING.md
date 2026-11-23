# Updating WDG-Bristle

WDG-Bristle has no in-game updater. **SYSTEM → Update Game** shows the installed version and says
**Updates: managed manually**. It never downloads or installs anything. You update by pulling this
repository and running the installer again.

## Update

Close the game first. Then, in the folder you cloned:

```bash
cd ~/wdg-bristle
git pull
./install.sh
./selftest.sh --tests-only
```

`install.sh` can be run again at any time. It sets up again what it set up the first time and
leaves your data alone.

## What is kept

These live in the install folder and are not part of the repository, so `git pull` does not touch
them:

| Yours | What it is |
|---|---|
| `loot/` | everything you collected, your XP, and Bristle's Node Memory and Block list (`loot/bristle/`) |
| `maps/` | downloaded map tiles |
| `secrets.conf` | your API keys and the Bristle owner key |
| `whitelist.json`, `bristle.conf`, `kismet_adapters.json` | your settings |

For a copy somewhere else before a big update, use **PLUGINS > Loot: Backup / Restore**.

## If `git pull` refuses

You changed a file that belongs to the repository. `git status` shows which one. Put your change
aside, pull, and bring it back:

```bash
git stash
git pull
git stash pop
```

## Going back

```bash
git log --oneline -5      # find the version you were on
git checkout <commit>
./install.sh
```

`git checkout main` returns to the newest version.

## Bristle Nodes

Updating the game does not update the nodes. Node firmware is in `bristle/firmware/releases/`, and
`bristle/docs/FLASHING.md` says how to write the app image to a node that already runs Bristle. Its
owner, node number and stored finds are kept.

## Installed from a zip

If you installed from `wdg-bristle.zip` and not from a clone, follow the
[install guide](../INSTALL_WDG_BRISTLE.md) again with the new zip.
