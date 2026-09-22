# MiSTer ROM audit / copy

Scans every `.mra` under `/media/fat/_Arcade` on a MiSTer (over SSH), checks that the
ROM zips it references exist in `games/mame` / `games/hbmame`, uploads missing ones from a
local ROM collection, and prints a summary.

```sh
/home/gkp/codes/.headless/bin/python -m pip install -r requirements.txt
cp config.example.yaml config.yaml   # edit host/password/rom paths

PY=/home/gkp/codes/.headless/bin/python
$PY mister_rom_audit.py --dry-run            # audit only, show what would be copied
$PY mister_rom_audit.py                      # copy missing zips
$PY mister_rom_audit.py --crc                # also verify zip contents against MRA CRCs
$PY mister_rom_audit.py --crc --fix-incomplete   # overwrite MiSTer zips that fail CRC with a better local copy
$PY mister_rom_audit.py --report report.json -v
```

Notes
- An MRA `zip="child.zip|parent.zip"` lists alternatives; every listed zip that is missing on
  the MiSTer but present locally is uploaded (split clone sets need the parent too).
- Existence mode counts a game OK if any listed zip is present. `--crc` checks that the
  required part CRCs are actually inside those zips (reads only the zip directory, not the data).
- Uploads go to `<name>.part` then are renamed, so an interrupted run leaves no truncated zips.
- Exit code is 1 when games are still missing or uploads failed.
