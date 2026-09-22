#!/usr/bin/env python3
"""MiSTer arcade ROM audit / copy tool.

Scans every .mra on a MiSTer over SSH, works out which ROM zips each game
needs, uploads the missing ones from a local ROM collection, and prints a
report of what was already there, what got fulfilled and what is still missing.
"""
from __future__ import annotations

import argparse
import json
import os
import posixpath
import re
import shlex
import stat
import sys
import tarfile
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field

import yaml

HBMAME = "hbmame"
# Jotego beta-core unlock key (Patreon), referenced by jt* MRAs; not a MAME ROM set.
BETA_KEY = "jtbeta.zip"
MAME = "mame"


# --------------------------------------------------------------------------
# MRA model / parsing
# --------------------------------------------------------------------------

@dataclass
class ZipRef:
    key: str            # lowercased basename, e.g. "pacman.zip"
    name: str           # basename as written in the MRA
    hint: str | None    # "mame" / "hbmame" when the MRA used a subdir prefix


@dataclass
class Requirement:
    """One set of ROM parts that must be found in any of the listed zips."""
    zips: list[ZipRef]
    crcs: set[int] = field(default_factory=set)
    names: set[str] = field(default_factory=set)   # parts that have no crc

    @property
    def beta(self) -> bool:
        return all(z.key == BETA_KEY for z in self.zips)

    def label(self) -> str:
        return "|".join(z.name for z in self.zips)


@dataclass
class Game:
    mra: str                  # path relative to mra_dir
    name: str
    reqs: list[Requirement]


def parse_zip_attr(value: str) -> list[ZipRef]:
    refs = []
    for item in value.split("|"):
        item = item.strip().replace("\\", "/")
        if not item:
            continue
        parts = [p for p in item.split("/") if p]
        hint = None
        if len(parts) > 1 and parts[-2].lower() in (MAME, HBMAME):
            hint = parts[-2].lower()
        base = parts[-1]
        refs.append(ZipRef(key=base.lower(), name=base, hint=hint))
    return refs


_TAG_RE = re.compile(rb"<(/?)([A-Za-z_][\w.:-]*)")


def parse_mra(data: bytes, rel_path: str) -> Game:
    # MiSTer's MRA loader is case-insensitive (e.g. <rom> ... </ROM> is accepted),
    # so normalize tag and attribute names before strict XML parsing.
    root = ET.fromstring(_TAG_RE.sub(lambda m: b"<" + m[1] + m[2].lower(), data))
    for el in root.iter():
        if any(k != k.lower() for k in el.attrib):
            el.attrib = {k.lower(): v for k, v in el.attrib.items()}
    name = (root.findtext("name") or "").strip() or posixpath.splitext(posixpath.basename(rel_path))[0]
    reqs: list[Requirement] = []
    for rom in root.iter("rom"):
        zip_attr = rom.get("zip")
        if not zip_attr or not parse_zip_attr(zip_attr):
            continue
        groups: dict[str, Requirement] = {}

        def group(attr: str) -> Requirement:
            if attr not in groups:
                groups[attr] = Requirement(zips=parse_zip_attr(attr))
            return groups[attr]

        group(zip_attr)
        for part in rom.iter("part"):
            req = group(part.get("zip") or zip_attr)
            crc = (part.get("crc") or "").strip()
            pname = (part.get("name") or "").strip()
            if crc:
                try:
                    req.crcs.add(int(crc, 16))
                    continue
                except ValueError:
                    pass
            if pname:
                req.names.add(pname.lower())
        reqs.extend(r for r in groups.values() if r.zips)
    return Game(mra=rel_path, name=name, reqs=reqs)


# --------------------------------------------------------------------------
# Zip content helpers
# --------------------------------------------------------------------------

@dataclass
class ZipContents:
    crcs: set[int]
    names: set[str]


def read_zip_contents(fileobj) -> ZipContents:
    with zipfile.ZipFile(fileobj) as zf:
        infos = zf.infolist()
    return ZipContents(
        crcs={i.CRC for i in infos},
        names={posixpath.basename(i.filename).lower() for i in infos},
    )


# --------------------------------------------------------------------------
# MiSTer access
# --------------------------------------------------------------------------

class Mister:
    def __init__(self, cfg: dict):
        import paramiko

        self.cfg = cfg
        self.ssh = paramiko.SSHClient()
        self.ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        password = os.environ.get("MISTER_PASSWORD", cfg.get("password"))
        self.ssh.connect(
            cfg["host"],
            port=int(cfg.get("port", 22)),
            username=cfg.get("user", "root"),
            password=str(password) if password is not None else None,
            look_for_keys=password is None,
            allow_agent=password is None,
            timeout=15,
        )
        self.sftp = self.ssh.open_sftp()

    def close(self):
        self.sftp.close()
        self.ssh.close()

    def run(self, cmd: str) -> tuple[int, bytes, bytes]:
        _, stdout, stderr = self.ssh.exec_command(cmd)
        out = stdout.read()
        err = stderr.read()
        return stdout.channel.recv_exit_status(), out, err

    def list_mras(self, mra_dir: str) -> list[str]:
        code, out, err = self.run(f"cd {shlex.quote(mra_dir)} && find . -type f -iname '*.mra'")
        if code != 0:
            raise RuntimeError(f"cannot list MRAs in {mra_dir}: {err.decode(errors='replace').strip()}")
        return sorted(line[2:] if line.startswith("./") else line
                      for line in out.decode("utf-8", errors="surrogateescape").splitlines() if line)

    def read_mras(self, mra_dir: str, rel_paths: list[str]) -> dict[str, bytes]:
        """Fetch all MRAs in one tar stream, falling back to SFTP per file."""
        result: dict[str, bytes] = {}
        try:
            cmd = (f"cd {shlex.quote(mra_dir)} && "
                   "find . -type f -iname '*.mra' | tar -cf - -T - 2>/dev/null")
            _, stdout, _ = self.ssh.exec_command(cmd)
            with tarfile.open(fileobj=stdout, mode="r|") as tf:
                for member in tf:
                    if not member.isfile():
                        continue
                    f = tf.extractfile(member)
                    if f is None:
                        continue
                    path = member.name[2:] if member.name.startswith("./") else member.name
                    result[path] = f.read()
        except (tarfile.TarError, OSError, EOFError):
            pass
        missing = [p for p in rel_paths if p not in result]
        if missing:
            if result:
                log(f"  tar stream missed {len(missing)} files, fetching via SFTP")
            else:
                log("  tar unavailable, fetching MRAs via SFTP (slower)")
            for i, p in enumerate(missing, 1):
                with self.sftp.open(posixpath.join(mra_dir, p), "rb") as f:
                    result[p] = f.read()
                if i % 200 == 0:
                    log(f"  {i}/{len(missing)}")
        return {p: result[p] for p in rel_paths}

    def list_zips(self, directory: str) -> dict[str, str]:
        try:
            entries = self.sftp.listdir_attr(directory)
        except FileNotFoundError:
            return {}
        return {e.filename.lower(): posixpath.join(directory, e.filename)
                for e in entries
                if e.filename.lower().endswith(".zip") and not stat.S_ISDIR(e.st_mode or 0)}

    def zip_contents(self, path: str) -> ZipContents:
        with self.sftp.open(path, "rb") as f:
            return read_zip_contents(f)

    def ensure_dir(self, directory: str):
        try:
            self.sftp.stat(directory)
        except FileNotFoundError:
            self.run(f"mkdir -p {shlex.quote(directory)}")

    def upload(self, local: str, remote: str):
        tmp = remote + ".part"
        self.sftp.put(local, tmp)
        try:
            self.sftp.remove(remote)
        except FileNotFoundError:
            pass
        self.sftp.rename(tmp, remote)


# --------------------------------------------------------------------------
# Inventory & resolution
# --------------------------------------------------------------------------

@dataclass
class LocalZip:
    path: str
    kind: str   # "mame" or "hbmame"


def scan_local(paths: list[tuple[str, str]]) -> tuple[dict[str, LocalZip], dict[str, LocalZip]]:
    """Return (mame, hbmame) maps of lowercase zip name -> LocalZip."""
    found: dict[str, dict[str, LocalZip]] = {MAME: {}, HBMAME: {}}
    for root_dir, kind in paths:
        if not root_dir:
            continue
        if not os.path.isdir(root_dir):
            log(f"warning: local ROM path does not exist: {root_dir}")
            continue
        for dirpath, _, files in os.walk(root_dir):
            for fn in files:
                if fn.lower().endswith(".zip"):
                    found[kind].setdefault(fn.lower(), LocalZip(os.path.join(dirpath, fn), kind))
    return found[MAME], found[HBMAME]


class Resolver:
    """Decides what is present/missing and what to copy for a set of games."""

    def __init__(self, remote: dict[str, dict[str, str]], local: dict[str, dict[str, LocalZip]],
                 remote_contents=None, local_contents=None):
        self.remote = remote              # {"mame": {key: path}, "hbmame": {...}}
        self.local = local                # {"mame": {key: LocalZip}, "hbmame": {...}}
        self.remote_contents = remote_contents   # callable(path) -> ZipContents | None (CRC mode)
        self.local_contents = local_contents     # callable(path) -> ZipContents | None
        self._cache: dict[tuple[str, str], ZipContents | None] = {}

    @property
    def crc_mode(self) -> bool:
        return self.remote_contents is not None

    def remote_path(self, ref: ZipRef) -> str | None:
        order = [ref.hint] if ref.hint else [MAME, HBMAME]
        if ref.hint:
            order += [k for k in (MAME, HBMAME) if k != ref.hint]
        for kind in order:
            if ref.key in self.remote[kind]:
                return self.remote[kind][ref.key]
        return None

    def local_zip(self, ref: ZipRef) -> LocalZip | None:
        order = [HBMAME, MAME] if ref.hint == HBMAME else [MAME, HBMAME]
        for kind in order:
            if ref.key in self.local[kind]:
                return self.local[kind][ref.key]
        return None

    def contents(self, where: str, path: str) -> ZipContents | None:
        k = (where, path)
        if k not in self._cache:
            fn = self.remote_contents if where == "remote" else self.local_contents
            self._cache[k] = fn(path)
        return self._cache[k]

    def satisfied(self, req: Requirement, copied: dict[str, str], replaced: dict[str, str]) -> bool:
        """copied/replaced map zip key -> local path of the file that is (or will be) on MiSTer."""
        present = []
        for ref in req.zips:
            if ref.key in copied or ref.key in replaced:
                present.append(("local", (copied.get(ref.key) or replaced[ref.key])))
            elif (p := self.remote_path(ref)) is not None:
                present.append(("remote", p))
        if not present:
            return False
        if not self.crc_mode or (not req.crcs and not req.names):
            return True
        crcs: set[int] = set()
        names: set[str] = set()
        for where, path in present:
            c = self.contents(where, path)
            if c:
                crcs |= c.crcs
                names |= c.names
        return req.crcs <= crcs and req.names <= names

    def plan_copies(self, games: list[Game]) -> dict[str, LocalZip]:
        """Every referenced zip that is absent on MiSTer but available locally."""
        plan: dict[str, LocalZip] = {}
        for g in games:
            for req in g.reqs:
                for ref in req.zips:
                    if ref.key in plan or self.remote_path(ref) is not None:
                        continue
                    lz = self.local_zip(ref)
                    if lz:
                        plan[ref.key] = lz
        return plan

    def plan_replacements(self, games: list[Game], copied: dict[str, str]) -> dict[str, LocalZip]:
        """CRC mode: remote zips whose local copy covers more of an unmet requirement."""
        plan: dict[str, LocalZip] = {}
        for g in games:
            for req in g.reqs:
                if self.satisfied(req, copied, {}):
                    continue
                for ref in req.zips:
                    rp = self.remote_path(ref)
                    lz = self.local_zip(ref)
                    if rp is None or lz is None or ref.key in plan:
                        continue
                    rc = self.contents("remote", rp)
                    lc = self.contents("local", lz.path)
                    if not lc:
                        continue
                    r_hit = len(req.crcs & rc.crcs) if rc else -1
                    if len(req.crcs & lc.crcs) > r_hit:
                        plan[ref.key] = lz
        return plan


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------

def log(msg: str):
    print(msg, file=sys.stderr, flush=True)


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return str(n)


def load_config(path: str) -> dict:
    with open(path) as f:
        cfg = yaml.safe_load(f) or {}
    m = cfg.setdefault("mister", {})
    if not m.get("host"):
        raise SystemExit(f"{path}: mister.host is required")
    m.setdefault("mra_dir", "/media/fat/_Arcade")
    m.setdefault("mame_dir", "/media/fat/games/mame")
    m.setdefault("hbmame_dir", "/media/fat/games/hbmame")
    cfg.setdefault("roms", {})
    if not cfg["roms"].get("path") and not cfg["roms"].get("hbmame_path"):
        raise SystemExit(f"{path}: roms.path is required")
    return cfg


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Audit MiSTer arcade ROMs and copy missing zips from a local collection.")
    ap.add_argument("-c", "--config", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml"))
    ap.add_argument("-n", "--dry-run", action="store_true", help="audit only, do not copy anything")
    ap.add_argument("--crc", action="store_true", help="verify zip contents against MRA part CRCs")
    ap.add_argument("--fix-incomplete", action="store_true",
                    help="with --crc: overwrite incomplete zips on MiSTer with a better local copy")
    ap.add_argument("--report", metavar="FILE", help="write a JSON report to FILE")
    ap.add_argument("-v", "--verbose", action="store_true", help="list every game in the report")
    args = ap.parse_args(argv)
    if args.fix_incomplete and not args.crc:
        ap.error("--fix-incomplete requires --crc")

    cfg = load_config(args.config)
    mc, rc = cfg["mister"], cfg["roms"]
    dirs = {MAME: mc["mame_dir"], HBMAME: mc["hbmame_dir"]}

    log("Scanning local ROMs ...")
    local_mame, local_hb = scan_local([(rc.get("path"), MAME), (rc.get("hbmame_path"), HBMAME)])
    local = {MAME: local_mame, HBMAME: local_hb}
    log(f"  {len(local_mame)} mame zips, {len(local_hb)} hbmame zips")

    log(f"Connecting to {mc.get('user', 'root')}@{mc['host']} ...")
    mister = Mister(mc)
    try:
        log(f"Reading MRAs from {mc['mra_dir']} ...")
        rel_paths = mister.list_mras(mc["mra_dir"])
        raw = mister.read_mras(mc["mra_dir"], rel_paths)
        games, parse_errors, no_rom = [], [], []
        for p in rel_paths:
            try:
                g = parse_mra(raw[p], p)
            except ET.ParseError as e:
                parse_errors.append((p, str(e)))
                continue
            (games if g.reqs else no_rom).append(g)
        log(f"  {len(rel_paths)} MRAs, {len(parse_errors)} parse errors")

        remote = {k: mister.list_zips(d) for k, d in dirs.items()}
        log(f"  MiSTer has {len(remote[MAME])} mame zips, {len(remote[HBMAME])} hbmame zips")

        bad_zips: dict[str, str] = {}

        def safe(fn, label):
            def inner(path):
                try:
                    return fn(path)
                except (zipfile.BadZipFile, OSError, EOFError) as e:
                    bad_zips[f"{label}:{path}"] = str(e) or type(e).__name__
                    return None
            return inner

        resolver = Resolver(
            remote, local,
            remote_contents=safe(mister.zip_contents, "mister") if args.crc else None,
            local_contents=safe(lambda p: read_zip_contents(p), "local") if args.crc else None,
        )
        if args.crc:
            log("Checking CRCs (this reads every referenced zip) ...")

        before = {g.mra: [resolver.satisfied(r, {}, {}) for r in g.reqs] for g in games}

        copy_plan = resolver.plan_copies(games)
        planned_copies = {k: lz.path for k, lz in copy_plan.items()}
        replace_plan = resolver.plan_replacements(games, planned_copies) if args.fix_incomplete else {}

        copied: dict[str, str] = {}
        replaced: dict[str, str] = {}
        upload_errors: dict[str, str] = {}
        bytes_sent = 0
        jobs = [(k, lz, False) for k, lz in sorted(copy_plan.items())] + \
               [(k, lz, True) for k, lz in sorted(replace_plan.items())]
        if jobs:
            verb = "Would copy" if args.dry_run else "Copying"
            log(f"{verb} {len(jobs)} zip(s) ...")
        for i, (key, lz, is_replace) in enumerate(jobs, 1):
            if is_replace:
                target = remote[MAME].get(key) or remote[HBMAME][key]
            else:
                target = posixpath.join(dirs[lz.kind], os.path.basename(lz.path))
            size = os.path.getsize(lz.path)
            if not args.dry_run:
                log(f"  [{i}/{len(jobs)}] {'replace' if is_replace else 'copy'} {os.path.basename(lz.path)} ({human(size)})")
                try:
                    mister.ensure_dir(posixpath.dirname(target))
                    mister.upload(lz.path, target)
                except (OSError, IOError) as e:
                    upload_errors[key] = str(e)
                    continue
            (replaced if is_replace else copied)[key] = lz.path
            bytes_sent += size

        after = {g.mra: [resolver.satisfied(r, copied, replaced) for r in g.reqs] for g in games}
    finally:
        mister.close()

    # ---- classify games ----
    ok, fulfilled, still_missing, needs_beta = [], [], [], []
    for g in games:
        if all(before[g.mra]):
            ok.append(g)
        elif all(after[g.mra]):
            fulfilled.append(g)
        elif all(a for r, a in zip(g.reqs, after[g.mra]) if not r.beta):
            needs_beta.append(g)
        else:
            still_missing.append(g)
    beta_users = [g for g in games if any(r.beta for r in g.reqs)]
    if BETA_KEY in copied:
        beta_status = "would copy" if args.dry_run else "copied"
    elif remote[MAME].get(BETA_KEY) or remote[HBMAME].get(BETA_KEY):
        beta_status = "on MiSTer"
    else:
        beta_status = "absent"

    unavailable: dict[str, list[str]] = {}
    incomplete: dict[str, list[str]] = {}
    for g in still_missing:
        for req, sat in zip(g.reqs, after[g.mra]):
            if sat or req.beta:
                continue
            present = [z for z in req.zips if z.key in copied or z.key in replaced or resolver.remote_path(z)]
            bucket = incomplete if present else unavailable
            bucket.setdefault(req.label(), []).append(g.name)

    # ---- classify ROM sets (unique zip search lists, shared by many MRAs) ----
    set_state: dict[tuple[str, ...], list[bool]] = {}   # key -> [ok_before, ok_after]
    set_label: dict[tuple[str, ...], str] = {}
    for g in games:
        for req, b, a in zip(g.reqs, before[g.mra], after[g.mra]):
            if req.beta:
                continue
            key = tuple(z.key for z in req.zips)
            set_label.setdefault(key, req.label())
            st = set_state.setdefault(key, [True, True])
            st[0] &= b
            st[1] &= a
    sets_ok = [k for k, (b, _) in set_state.items() if b]
    sets_fulfilled = [k for k, (b, a) in set_state.items() if not b and a]
    sets_missing = [k for k, (_, a) in set_state.items() if not a]

    # ---- classify zip files ----
    referenced = {z for k in set_state for z in k}
    already = {k for k in referenced if k in remote[MAME] or k in remote[HBMAME]}
    absent = referenced - already - set(copied)
    needed_zips = {z for k in sets_missing for z in k}
    absent_needed = absent & needed_zips
    absent_unneeded = absent - needed_zips

    # ---- print ----
    dry = " (dry run)" if args.dry_run else ""
    mode = "existence + CRC" if args.crc else "existence"
    out = []
    out.append("")
    out.append("=" * 64)
    out.append(f" MiSTer ROM audit{dry}  —  mode: {mode}")
    out.append("=" * 64)
    def row(label, value, indent=1):
        out.append(f"{' ' * indent}{label:<{35 - indent}}: {value}")

    row("MRA files scanned", len(rel_paths))
    row("parse errors", len(parse_errors), 3)
    row("no zip required", len(no_rom), 3)
    out.append("")
    row("Games (MRAs needing zips)", len(games))
    row("OK before run", len(ok), 3)
    row("would be fulfilled" if args.dry_run else "fulfilled by this run", len(fulfilled), 3)
    row("still missing ROMs", len(still_missing), 3)
    if beta_users:
        row("ROMs OK, need beta key only", len(needs_beta), 3)
    out.append("")
    row("ROM sets (unique zip lists)", len(set_state))
    row("OK before run", len(sets_ok), 3)
    row("would be fulfilled" if args.dry_run else "fulfilled by this run", len(sets_fulfilled), 3)
    row("still missing", len(sets_missing), 3)
    out.append("")
    row("Zip files referenced", len(referenced))
    row("on MiSTer", len(already), 3)
    row("would copy" if args.dry_run else "copied", f"{len(copied)}  ({human(bytes_sent)})", 3)
    if args.fix_incomplete:
        row("would replace" if args.dry_run else "replaced (incomplete)", len(replaced), 3)
    row("absent fallbacks (not needed) *", len(absent_unneeded), 3)
    row("absent, needed", len(absent_needed), 3)
    row("upload errors", len(upload_errors), 3)
    if beta_users:
        out.append("")
        row(f"Jotego beta key ({BETA_KEY})", beta_status)
        row("used by games", len(beta_users), 3)
        if beta_status == "absent":
            out.append("   not a MAME ROM: Jotego Patreon key that unlocks jt* beta cores;")
            out.append("   put it in roms.path to have it copied.")
    out.append("")
    out.append(" * a MRA zip list is a search path (e.g. game|parent|device); these zips are")
    out.append("   absent but every list that names them is already satisfied by another zip."
               if args.crc else
               "   absent but every list that names them already has another zip present.\n"
               "   Without --crc that is not verified (split sets may still need them).")

    def section(title, rows):
        if rows:
            out.append("")
            out.append(f"--- {title} ({len(rows)}) ---")
            out.extend(f"  {r}" for r in rows)

    section("Copied zips" if not args.dry_run else "Zips that would be copied",
            [f"{os.path.basename(p)}  <- {p}" for _, p in sorted(copied.items())])
    section("Replaced zips" if not args.dry_run else "Zips that would be replaced",
            [os.path.basename(p) for _, p in sorted(replaced.items())])
    section("Games fulfilled" if not args.dry_run else "Games that would be fulfilled",
            [f"{g.name}  [{g.mra}]" for g in sorted(fulfilled, key=lambda g: g.name.lower())])
    section("ROM sets still missing — no zip in the list found locally or on MiSTer",
            [f"{z}  <- {_games_str(names)}" for z, names in sorted(unavailable.items())])
    section("ROM sets still missing — zip present but CRCs don't match",
            [f"{z}  <- {_games_str(names)}" for z, names in sorted(incomplete.items())])
    section("Upload errors", [f"{k}: {v}" for k, v in sorted(upload_errors.items())])
    section("Unreadable zips", [f"{k}: {v}" for k, v in sorted(bad_zips.items())])
    section("MRA parse errors", [f"{p}: {e}" for p, e in parse_errors])
    if args.verbose:
        section("Games OK", [f"{g.name}  [{g.mra}]" for g in sorted(ok, key=lambda g: g.name.lower())])
        section("Games still missing", [f"{g.name}  [{g.mra}]" for g in sorted(still_missing, key=lambda g: g.name.lower())])
        section("Games needing only the Jotego beta key",
                [f"{g.name}  [{g.mra}]" for g in sorted(needs_beta, key=lambda g: g.name.lower())])
    out.append("")
    print("\n".join(out))

    if args.report:
        report = {
            "dry_run": args.dry_run,
            "mode": mode,
            "summary": {
                "mra_files": len(rel_paths),
                "parse_errors": len(parse_errors),
                "no_zip_required": len(no_rom),
                "games": len(games),
                "games_ok": len(ok),
                "games_fulfilled": len(fulfilled),
                "games_still_missing": len(still_missing),
                "games_need_beta_key_only": len(needs_beta),
                "beta_key": beta_status,
                "games_using_beta_key": len(beta_users),
                "rom_sets": len(set_state),
                "rom_sets_ok": len(sets_ok),
                "rom_sets_fulfilled": len(sets_fulfilled),
                "rom_sets_still_missing": len(sets_missing),
                "zips_referenced": len(referenced),
                "zips_already_present": len(already),
                "zips_copied": len(copied),
                "zips_replaced": len(replaced),
                "zips_absent_not_needed": len(absent_unneeded),
                "zips_absent_needed": len(absent_needed),
                "bytes_copied": bytes_sent,
                "upload_errors": len(upload_errors),
            },
            "copied": sorted(os.path.basename(p) for p in copied.values()),
            "replaced": sorted(os.path.basename(p) for p in replaced.values()),
            "games_fulfilled": [{"name": g.name, "mra": g.mra} for g in fulfilled],
            "games_need_beta_key_only": [{"name": g.name, "mra": g.mra} for g in needs_beta],
            "games_still_missing": [
                {"name": g.name, "mra": g.mra,
                 "unmet": [r.label() for r, s in zip(g.reqs, after[g.mra]) if not s]}
                for g in still_missing
            ],
            "rom_sets_still_missing": [set_label[k] for k in sorted(sets_missing)],
            "absent_needed_zips": sorted(absent_needed),
            "unavailable_zips": unavailable,
            "incomplete_zips": incomplete,
            "upload_errors": upload_errors,
            "unreadable_zips": bad_zips,
            "parse_errors": dict(parse_errors),
        }
        with open(args.report, "w") as f:
            json.dump(report, f, indent=2)
        log(f"Report written to {args.report}")

    return 1 if still_missing or needs_beta or upload_errors else 0


def _games_str(names: list[str], limit: int = 4) -> str:
    uniq = sorted(set(names), key=str.lower)
    s = ", ".join(uniq[:limit])
    return s + (f", +{len(uniq) - limit} more" if len(uniq) > limit else "")


if __name__ == "__main__":
    sys.exit(main())
