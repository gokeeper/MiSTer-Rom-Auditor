# MiSTer ROM Auditor

Audit the arcade ROMs on a [MiSTer FPGA](https://github.com/MiSTer-devel) and fill in the gaps from your local ROM collection.

The tool connects to your MiSTer over SSH. It reads every `.mra` file under `/media/fat/_Arcade` and works out which MAME ROM zips each game needs. Then it checks whether those zips are in `/media/fat/games/mame` or `/media/fat/games/hbmame`, uploads any missing zip it can find locally, and prints a report:

- games that were already fine
- games fixed by this run
- games that are still missing ROMs, and which zips they need

---

## Contents

- [Features](#features)
- [Requirements](#requirements)
- [Installation](#installation)
- [Configuration](#configuration)
- [Usage](#usage)
- [Command-line options](#command-line-options)
- [Example output](#example-output)
- [How it works](#how-it-works)
- [JSON report](#json-report)
- [Exit codes](#exit-codes)
- [Troubleshooting](#troubleshooting)
- [Limitations](#limitations)
- [Development](#development)

---

## Features

- **Remote scan over SSH/SFTP.** Nothing is installed on the MiSTer. All MRAs are fetched in one `tar` stream, with a per-file SFTP fallback.
- **Understands zip search lists.** An MRA like `zip="galaga.zip|namco51.zip|namco54.zip"` lists the zips the MiSTer searches for ROM parts (game, parent, devices/BIOS). Without `--crc`, the game counts as OK if any one of them is present.
- **Three levels of statistics.** Games (MRA files), ROM sets (unique zip lists, often shared by many MRAs) and individual zip files are counted separately.
- **mame and hbmame.** Both `games/mame` and `games/hbmame` count as present locations. Zips found in your local hbmame folder are uploaded to `games/hbmame`.
- **Safe uploads.** Each file is uploaded as `name.zip.part` and renamed when complete, so an interrupted run never leaves a truncated zip.
- **Deduplicated.** Each zip is uploaded at most once, even if dozens of MRAs reference it.
- **Dry run.** `--dry-run` shows exactly what would be copied without touching the MiSTer.
- **Optional CRC audit.** `--crc` checks that each zip actually contains the ROM parts the MRA asks for, so it catches wrong-version or incomplete sets. It reads only the zip's central directory, not the ROM data.
- **Optional repair.** `--crc --fix-incomplete` overwrites MiSTer zips that fail the CRC check when your local copy covers more of the required parts.
- **Tolerant MRA parsing.** Tag and attribute names are case-insensitive, like the MiSTer's own loader (e.g. `<rom>` … `</ROM>`).
- **JSON report** for scripting or keeping a history.

---

## Requirements

- Python **3.10+**
- [`paramiko`](https://www.paramiko.org/) (SSH/SFTP) and [`PyYAML`](https://pyyaml.org/)
- A MiSTer reachable over the network with SSH enabled. Stock MiSTer Linux has it on, with user `root` and password `1`.
- A local folder of MAME ROM zips. It is scanned recursively, so sub-folders are fine.

---

## Installation

```sh
git clone git@github.com:gokeeper/MiSTer-Rom-Auditor.git
cd MiSTer-Rom-Auditor
python -m pip install -r requirements.txt
cp config.example.yaml config.yaml
```

Then edit `config.yaml` (see below).

> `config.yaml` is in `.gitignore` because it contains your MiSTer password. Don't commit it.

---

## Configuration

`config.yaml`:

```yaml
mister:
  host: 192.168.1.50        # MiSTer IP or hostname              (required)
  port: 22                  # SSH port                           (default 22)
  user: root                # SSH user                           (default root)
  password: "1"             # SSH password; see below
  mra_dir: /media/fat/_Arcade        # scanned recursively for *.mra
  mame_dir: /media/fat/games/mame    # where MAME zips live on the MiSTer
  hbmame_dir: /media/fat/games/hbmame

roms:
  path: /mnt/roms/mame           # local MAME zips, searched recursively   (required*)
  hbmame_path: /mnt/roms/hbmame  # local HBMAME zips (optional)
```

\* At least one of `roms.path` / `roms.hbmame_path` must be set.

### Authentication

The password is chosen in this order:

1. the `MISTER_PASSWORD` environment variable, if set
2. `mister.password` in `config.yaml`
3. if neither is set, SSH keys (`~/.ssh/id_*`) and `ssh-agent`

```sh
MISTER_PASSWORD=1 python mister_rom_audit.py --dry-run
```

New host keys are accepted automatically (paramiko `AutoAddPolicy`). This is convenient on a home LAN but not suitable for untrusted networks.

---

## Usage

Always start with a dry run:

```sh
python mister_rom_audit.py --dry-run
```

If the list of zips to copy looks right, run it for real:

```sh
python mister_rom_audit.py
```

Other common invocations:

```sh
# Deep audit: verify zip contents against the CRCs in each MRA
python mister_rom_audit.py --crc --dry-run

# Deep audit + replace incomplete zips on the MiSTer with better local copies
python mister_rom_audit.py --crc --fix-incomplete

# Full per-game listing and a machine-readable report
python mister_rom_audit.py -v --report audit.json

# Use a different config file
python mister_rom_audit.py -c ~/mister-living-room.yaml
```

Progress messages go to **stderr** and the report goes to **stdout**, so you can save only the report:

```sh
python mister_rom_audit.py --dry-run > audit.txt
```

---

## Command-line options

| Option | Description |
|---|---|
| `-c`, `--config FILE` | Config file. Default: `config.yaml` next to the script. |
| `-n`, `--dry-run` | Audit only. Nothing is uploaded or changed. The report says "would copy" / "would be fulfilled". |
| `--crc` | Also verify that each zip contains the CRCs listed in the MRA `<part>` entries. Slower: it opens every referenced zip on the MiSTer over SFTP. |
| `--fix-incomplete` | Requires `--crc`. When a zip on the MiSTer fails the CRC check and your local zip of the same name covers more of the required parts, overwrite the MiSTer copy. **This overwrites files.** Try it with `--dry-run` first. |
| `--report FILE` | Write a JSON report to `FILE`. |
| `-v`, `--verbose` | Also list every OK game and every still-missing game by name. |

---

## Example output

(Numbers from a real dry run. The copy count is illustrative.)

```
================================================================
 MiSTer ROM audit (dry run)  —  mode: existence
================================================================
 MRA files scanned                 : 3331
   parse errors                    : 0
   no zip required                 : 7

 Games (MRAs needing zips)         : 3324
   OK before run                   : 3213
   would be fulfilled              : 44
   still missing                   : 67

 ROM sets (unique zip lists)       : 2956
   OK before run                   : 2883
   would be fulfilled              : 34
   still missing                   : 39

 Zip files referenced              : 3004
   on MiSTer                       : 1013
   would copy                      : 44  (173.9 MB)
   absent fallbacks (not needed) * : 1907
   absent, needed                  : 40
   upload errors                   : 0

 * a MRA zip list is a search path (e.g. game|parent|device); these zips are
   absent but every list that names them already has another zip present.
   Without --crc that is not verified (split sets may still need them).

--- Zips that would be copied (44) ---
  1942.zip  <- /mnt/roms/mame/1942.zip
  ...

--- ROM sets still missing — no zip in the list found locally or on MiSTer (39) ---
  aerofgt.zip  <- Aero Fighters (FF, Video System, 1992)
  atarisy1.zip  <- Indiana Jones (cocktail), Indiana Jones (german), Indiana Jones (set 1), +23 more
  bigbang.zip|tdragon2.zip  <- Big Bang (9th Nov. 1993, set 1)
  ...
```

### Games vs. ROM sets vs. zip files

These three counts measure different things, so their numbers differ:

| Term | What it is | Why its count differs |
|---|---|---|
| **Game** | One `.mra` file. | Many MRAs share a ROM set: `puckman.zip` alone is used by ~130 Pac-Man hacks, and alternative versions of a game usually share one too. |
| **ROM set** | One unique `zip="…"` list, e.g. `galaga.zip\|namco51.zip\|namco54.zip`. It is counted once however many MRAs use it. | One MRA can reference several sets, e.g. a `<part zip="…">` override. |
| **Zip file** | One file name that appears in any list. | A list names several zips, and the same zip (a parent or a BIOS like `neogeo.zip`) appears in many lists. |

A zip list is a **search path**, not a list of required files. The MiSTer looks for each ROM part in every zip in the list. With **non-merged** sets, the first zip usually contains everything, so the parent and device zips after it are never opened. That is why most referenced zip names can be absent without anything being broken.

### What the numbers mean

| Line | Meaning |
|---|---|
| **MRA files scanned** | Regular `.mra` files found under `mra_dir`. Symlinks such as the `_Organized` folders created by update_all are skipped, so games aren't counted twice. |
| **parse errors** | MRAs that aren't valid XML even after case normalisation. These are listed at the bottom. |
| **no zip required** | MRAs with no `zip=` attribute, e.g. games with all ROM data inline. They are ignored. |
| **Games: OK before run** | Every ROM set the MRA needs was already satisfied. |
| **Games: fulfilled** | The game was broken before, and is complete after the copy. |
| **Games: still missing** | At least one of the game's ROM sets is still unsatisfied. |
| **ROM sets: OK / fulfilled / still missing** | The same three states, counted per unique zip list. This is the number of distinct things you still need to obtain. |
| **Zip files referenced** | Distinct zip names across all lists. |
| **on MiSTer** | Referenced zips already in `mame_dir` or `hbmame_dir`. |
| **copied** | Zips uploaded in this run, with total size. |
| **absent fallbacks (not needed)** | Not on the MiSTer, but every list that names them is satisfied by another zip. Without `--crc` this is assumed, not verified. |
| **absent, needed** | Not on the MiSTer or local, and named in a list that is still missing. Getting any one zip from each such list fixes it. |

The four zip lines (on MiSTer + copied + absent fallbacks + absent needed) add up to **Zip files referenced**.

The report can have these sections:

- **ROM sets still missing — no zip in the list found locally or on MiSTer**: none of the listed zips exist anywhere. You need to obtain one of them. Each line shows the games that use the set.
- **ROM sets still missing — zip present but CRCs don't match** (`--crc` only): a zip exists but doesn't contain the right ROM parts. Usually this is a different MAME version of the set.
- **Upload errors**: SFTP failures, such as a full disk or a permission problem.
- **Unreadable zips** (`--crc` only): corrupt zips, on the MiSTer or local.
- **MRA parse errors**: MRAs that couldn't be read.

---

## How it works

```
 local PC                                   MiSTer (SSH)
 ─────────                                  ─────────────
 scan roms.path / roms.hbmame_path   ┌───── find _Arcade -type f -name '*.mra' | tar
   → {zip name → local file}         │      (fallback: SFTP per file)
                                     ▼
                      parse MRAs → per game: list of requirements
                                     │
                     listdir games/mame, games/hbmame → present zips
                                     │
             ┌───────────────────────┴───────────────────────┐
             ▼                                               ▼
   evaluate each game BEFORE                   plan uploads: every referenced zip
                                               missing on MiSTer but found locally
                                                             │
                                               upload (.part → rename)
                                                             │
                                             evaluate each game AFTER
                                                             │
                                            OK / fulfilled / still missing report
```

### 1. MRA parsing

In each MRA, every `<rom>` element that has a `zip="…"` attribute becomes a **requirement**:

- `zip="a.zip|b.zip"` is split on `|` into a search list (game, parent, devices/BIOS). The MiSTer looks for each part in all of them. In existence mode, one present zip satisfies the requirement. `--crc` checks the parts properly.
- A path prefix such as `hbmame/foo.zip` or `mame/foo.zip` is kept as a hint for where to look and where to upload.
- Every `<part crc="…">` inside the `<rom>` (including parts nested in `<interleave>`) adds a required CRC. A part without a CRC but with a `name` adds a required file name.
- A `<part zip="other.zip">` with its own `zip` attribute starts a separate requirement for that zip.
- `<rom>` elements without `zip` (inline hex data, NVRAM, etc.) are ignored.

The game name comes from the MRA's `<name>` element, or from the file name if that's missing.

### 2. What gets copied

A zip is uploaded when all of these are true:

- some MRA references it (anywhere in a zip list),
- it is not in `mame_dir` or `hbmame_dir` on the MiSTer,
- a zip with the same name (case-insensitive) exists in your local ROM folders.

Every missing zip in the list is copied, not just the first one. With **split** ROM sets, a clone zip such as `mspacmnf.zip` only works when its parent `mspacman.zip` is also present, and a name-only check can't tell whether that's needed. Parent zips are shared by many games, so the extra disk use is small.

Upload destination:

- a zip found under `roms.hbmame_path` goes to `hbmame_dir`
- everything else goes to `mame_dir`, keeping the local file name

### 3. Existence mode vs. CRC mode

| | Existence (default) | `--crc` |
|---|---|---|
| A requirement is met when… | at least one listed zip exists on the MiSTer | the union of the CRCs inside all present listed zips includes every CRC the MRA needs |
| Speed | fast: one directory listing per ROM folder | slower: opens every referenced zip over SFTP to read its directory |
| Catches | missing zips | missing zips, wrong MAME version, incomplete or corrupt sets |

CRC mode combines contents across all zips in the list, in the same way the MiSTer loader searches every listed zip for each part. A split child plus its parent is therefore evaluated correctly.

### 4. Game status

- **OK**: every requirement was met before the run.
- **Fulfilled**: at least one requirement was unmet before, and all are met after the uploads (or would be, in a dry run).
- **Still missing**: at least one requirement is still unmet.

---

## JSON report

`--report audit.json` writes:

```json
{
  "dry_run": false,
  "mode": "existence",
  "summary": {
    "mra_files": 3331,
    "parse_errors": 0,
    "no_zip_required": 7,
    "games": 3324,
    "games_ok": 3213,
    "games_fulfilled": 44,
    "games_still_missing": 67,
    "rom_sets": 2956,
    "rom_sets_ok": 2883,
    "rom_sets_fulfilled": 34,
    "rom_sets_still_missing": 39,
    "zips_referenced": 3004,
    "zips_already_present": 1013,
    "zips_copied": 44,
    "zips_replaced": 0,
    "zips_absent_not_needed": 1907,
    "zips_absent_needed": 40,
    "bytes_copied": 182347366,
    "upload_errors": 0
  },
  "copied": ["1942.zip", "..."],
  "replaced": [],
  "games_fulfilled": [{"name": "1942 (Revision B)", "mra": "1942.mra"}],
  "games_still_missing": [
    {"name": "Galaga", "mra": "_alternatives/_Galaga/Galaga.mra", "unmet": ["galaga.zip"]}
  ],
  "rom_sets_still_missing": ["aerofgt.zip", "bigbang.zip|tdragon2.zip"],
  "absent_needed_zips": ["aerofgt.zip", "bigbang.zip", "tdragon2.zip"],
  "unavailable_zips": {"galaga.zip": ["Galaga"]},
  "incomplete_zips": {},
  "upload_errors": {},
  "unreadable_zips": {},
  "parse_errors": {}
}
```

The keys of `unavailable_zips` and `incomplete_zips` (and the entries of `rom_sets_still_missing`) are ROM-set labels: the zip list joined with `|`.

---

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Every game is OK or was fulfilled, and there were no upload errors. |
| `1` | At least one game is still missing ROMs, an upload failed, or the run aborted (config error, SSH failure; the message or traceback is printed to stderr). |
| `2` | Invalid command-line arguments (e.g. `--fix-incomplete` without `--crc`). |

This makes it easy to use in scripts, e.g. `python mister_rom_audit.py || notify-send "MiSTer ROMs incomplete"`.

---

## Troubleshooting

**`Authentication failed`**
Check `user` and `password`. The stock MiSTer login is `root` / `1`. If you changed it, make sure `MISTER_PASSWORD` isn't set to an old value.

**`cannot list MRAs in /media/fat/_Arcade`**
Check `mister.mra_dir`. MRAs live in `_Arcade/`. `_Arcade/cores/` holds only `.rbf` cores.

**"tar unavailable, fetching MRAs via SFTP (slower)"**
The MiSTer's `tar` didn't accept `-T -`. Everything still works, it just takes longer for a few thousand MRAs.

**MRA parse errors**
The tool already accepts mixed-case tags (`<rom>` … `</ROM>`), which the MiSTer allows but strict XML doesn't. Any MRA still listed is genuinely malformed: check the reported line and column.

**A game is "still missing" but I have the zip locally**
- The file name must match the name in the MRA (case doesn't matter). `Pac-Man.zip` ≠ `pacman.zip`.
- Make sure the zip is under `roms.path` or `roms.hbmame_path`.
- If two local folders have the same zip name, the first one found wins.

**`--crc` says a zip is incomplete**
Your ROM set is probably from a different MAME version than the MRA expects. Get the set version the MRA was made for (MiSTer arcade MRAs usually follow a specific MAME release). If your local copy is the right one, use `--fix-incomplete`.

**The upload fails with a disk-space error**
The SD card or USB drive is full. The partial `.part` file is left behind and can be deleted.

---

## Limitations

- Only zips directly inside `mame_dir` / `hbmame_dir` count as present. Sub-folders there are not searched.
- Local zips are matched **by file name only**. In existence mode a zip with the right name but wrong contents counts as good; use `--crc` to catch that.
- Merged sets: if the MRA's zip list doesn't include your merged parent zip, the tool can't know that the parent contains the clone.
- CHDs, samples and other non-zip assets are not handled.
- Symlinked MRAs are skipped by design, to avoid counting `_Organized` duplicates.
- SSH host keys are not verified.

---

## Development

Run the tests (no MiSTer needed):

```sh
python -m unittest discover -s tests -v
```

The tests cover:

- MRA parsing: zip search lists, interleaved parts, per-part `zip` overrides, `hbmame/` prefix hints, mixed-case tags
- existence-mode copy planning
- CRC-mode evaluation and `--fix-incomplete` replacement planning

### Project layout

```
mister_rom_audit.py    # the tool: parsing, SSH access, resolution, reporting
config.example.yaml    # config template (copy to config.yaml)
requirements.txt       # paramiko, pyyaml
tests/test_audit.py    # unit tests (fake inventories, no network)
```
