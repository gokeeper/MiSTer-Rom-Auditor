#!/usr/bin/env python3
"""MiSTer arcade ROM audit / copy tool.

Scans every .mra on a MiSTer over SSH, works out which ROM zips each game
needs, uploads the missing ones from a local ROM collection, and prints a
report of what was already there, what got fulfilled and what is still missing.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import shutil
import os
import posixpath
import re
import shlex
import stat
import sys
import tarfile
import tempfile
import xml.etree.ElementTree as ET
import zipfile
import zlib
from collections import Counter
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
    part_names: dict[int, list[str]] = field(default_factory=dict)   # crc -> file names in the MRA
    # Several <rom> elements with the same index are either/or options (e.g. a
    # merged and a non-merged zip). Each option is the list of requirements of
    # one <rom>; zips then holds the union of all options' zips.
    alts: list[list[Requirement]] | None = None

    @property
    def beta(self) -> bool:
        return all(z.key == BETA_KEY for z in self.zips)

    def leaves(self) -> list[Requirement]:
        return [r for opt in self.alts for r in opt] if self.alts else [self]

    def label(self) -> str:
        if self.alts:
            return " or ".join(" + ".join(r.label() for r in opt) for opt in self.alts)
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
    by_index: dict[object, list[list[Requirement]]] = {}
    for n, rom in enumerate(root.iter("rom")):
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
                    value = int(crc, 16)
                except ValueError:
                    pass
                else:
                    req.crcs.add(value)
                    names = req.part_names.setdefault(value, [])
                    if pname and pname not in names:
                        names.append(pname)
                    continue
            if pname:
                req.names.add(pname.lower())
        index = (rom.get("index") or "").strip() or ("noindex", n)
        by_index.setdefault(index, []).append([r for r in groups.values() if r.zips])

    reqs: list[Requirement] = []
    for options in by_index.values():
        if len(options) == 1:
            reqs.extend(options[0])
        else:
            zips: dict[str, ZipRef] = {}
            for opt in options:
                for r in opt:
                    for z in r.zips:
                        zips.setdefault(z.key, z)
            reqs.append(Requirement(zips=list(zips.values()), alts=options))
    return Game(mra=rel_path, name=name, reqs=reqs)


# Parsed MRAs are cached between runs. Bump this whenever parse_mra() or the
# Requirement/Game structures change, so stale cache entries are re-parsed.
MRA_CACHE_VERSION = 1


def req_to_dict(r: Requirement) -> dict:
    d = {"zips": [[z.key, z.name, z.hint] for z in r.zips]}
    if r.crcs:
        d["crcs"] = sorted(r.crcs)
    if r.names:
        d["names"] = sorted(r.names)
    if r.part_names:
        d["part_names"] = {str(k): v for k, v in r.part_names.items()}
    if r.alts:
        d["alts"] = [[req_to_dict(x) for x in opt] for opt in r.alts]
    return d


def req_from_dict(d: dict) -> Requirement:
    return Requirement(
        zips=[ZipRef(key=k, name=n, hint=h) for k, n, h in d["zips"]],
        crcs=set(d.get("crcs", ())),
        names=set(d.get("names", ())),
        part_names={int(k): v for k, v in d.get("part_names", {}).items()},
        alts=[[req_from_dict(x) for x in opt] for opt in d["alts"]] if "alts" in d else None,
    )


class MraCache:
    """Parsed MRAs keyed by path, valid while the file's size and mtime are unchanged."""

    def __init__(self, path: str | None):
        self.path = path
        self.entries: dict[str, dict] = {}
        if path:
            try:
                with open(path) as f:
                    data = json.load(f)
                if data.get("version") == MRA_CACHE_VERSION:
                    self.entries = data["mras"]
            except (OSError, ValueError, KeyError):
                pass

    def get(self, rel: str, stamp: tuple[int, int]) -> dict | None:
        e = self.entries.get(rel)
        return e if e and tuple(e["stamp"]) == stamp else None

    def put(self, rel: str, stamp: tuple[int, int], game: Game | None, error: str | None):
        e = {"stamp": list(stamp)}
        if error is not None:
            e["error"] = error
        else:
            e["name"] = game.name
            e["reqs"] = [req_to_dict(r) for r in game.reqs]
        self.entries[rel] = e

    def save(self, keep: set[str]):
        if not self.path:
            return
        self.entries = {k: v for k, v in self.entries.items() if k in keep}
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"version": MRA_CACHE_VERSION, "mras": self.entries}, f)
        os.replace(tmp, self.path)


def load_mras(mister, mra_dir: str, cache: MraCache):
    """Return (games, no_rom, parse_errors, total), fetching only new/changed MRAs."""
    stamps = mister.list_mras(mra_dir)
    changed = [p for p, st in stamps.items() if cache.get(p, st) is None]
    log(f"  {len(stamps)} MRAs: {len(stamps) - len(changed)} unchanged (cached), "
        f"{len(changed)} new/changed to download")
    raw = mister.read_mras(mra_dir, changed) if changed else {}
    for p in changed:
        try:
            cache.put(p, stamps[p], parse_mra(raw[p], p), None)
        except ET.ParseError as e:
            cache.put(p, stamps[p], None, str(e))
    cache.save(set(stamps))

    games, no_rom, parse_errors = [], [], []
    for p, st in stamps.items():
        e = cache.get(p, st)
        if "error" in e:
            parse_errors.append((p, e["error"]))
            continue
        g = Game(mra=p, name=e["name"], reqs=[req_from_dict(d) for d in e["reqs"]])
        (games if g.reqs else no_rom).append(g)
    return games, no_rom, parse_errors, len(stamps)


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

    def list_mras(self, mra_dir: str) -> dict[str, tuple[int, int]]:
        """Return {relative path: (size, mtime)} for every MRA under mra_dir."""
        code, out, err = self.run(f"cd {shlex.quote(mra_dir)} && "
                                  "find . -type f -iname '*.mra' -exec stat -c '%s %Y %n' {} +")
        if code != 0:
            raise RuntimeError(f"cannot list MRAs in {mra_dir}: {err.decode(errors='replace').strip()}")
        result = {}
        for line in out.decode("utf-8", errors="surrogateescape").splitlines():
            size, mtime, path = line.split(" ", 2)
            result[path[2:] if path.startswith("./") else path] = (int(size), int(mtime))
        return dict(sorted(result.items()))

    def read_mras(self, mra_dir: str, rel_paths: list[str]) -> dict[str, bytes]:
        """Fetch the given MRAs in one tar stream, falling back to SFTP per file."""
        result: dict[str, bytes] = {}
        list_file = f"/tmp/mister-rom-audit-{os.getpid()}.lst"
        try:
            # File list goes via a temp file: piping it through stdin while
            # reading the tar from stdout could deadlock on large lists.
            self.sftp.putfo(io.BytesIO("".join(f"./{p}\n" for p in rel_paths)
                                       .encode("utf-8", errors="surrogateescape")), list_file)
            cmd = f"cd {shlex.quote(mra_dir)} && tar -cf - -T {list_file} 2>/dev/null"
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
        finally:
            try:
                self.sftp.remove(list_file)
            except OSError:
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


def scan_local(paths: list[tuple[str, str, bool]]) -> tuple[dict[str, LocalZip], dict[str, LocalZip]]:
    """Return (mame, hbmame) maps of lowercase zip name -> LocalZip.

    paths are (dir, kind, required); earlier entries win on duplicate names.
    Optional dirs (build_path) may be missing and don't count towards "no zips found".
    """
    found: dict[str, dict[str, LocalZip]] = {MAME: {}, HBMAME: {}}
    required_found = 0
    for root_dir, kind, required in paths:
        if not root_dir:
            continue
        if not os.path.isdir(root_dir):
            if required:
                raise SystemExit(f"error: local ROM path does not exist (not mounted?): {root_dir}")
            continue
        for dirpath, _, files in os.walk(root_dir):
            for fn in files:
                if fn.lower().endswith(".zip"):
                    found[kind].setdefault(fn.lower(), LocalZip(os.path.join(dirpath, fn), kind))
                    required_found += required
    if not required_found:
        raise SystemExit("error: no .zip files found in the configured local ROM path(s): "
                         + ", ".join(p for p, _, req in paths if p and req))
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

    def register(self, where: str, path: str, contents: ZipContents):
        """Pre-seed contents for a zip that doesn't exist yet (a planned rebuild)."""
        self._cache[(where, path)] = contents

    def contents(self, where: str, path: str) -> ZipContents | None:
        k = (where, path)
        if k not in self._cache:
            fn = self.remote_contents if where == "remote" else self.local_contents
            self._cache[k] = fn(path)
        return self._cache[k]

    def satisfied(self, req: Requirement, copied: dict[str, str], replaced: dict[str, str]) -> bool:
        """copied/replaced map zip key -> local path of the file that is (or will be) on MiSTer."""
        if req.alts:
            return any(all(self.satisfied(r, copied, replaced) for r in opt) for opt in req.alts)
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
            for req in (leaf for r in g.reqs for leaf in r.leaves()):
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
# Rebuild missing zips from CRC-matching files in other local zips
# --------------------------------------------------------------------------

def cache_dir() -> str:
    return os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "mister-rom-audit")


class CrcIndex:
    """CRC -> [(zip path, member name, size)] over local zips.

    Only each zip's central directory is read. Results are cached on disk and
    re-read only for zips whose size or mtime changed.
    """

    VERSION = 1

    def __init__(self, cache_file: str | None = None):
        self.cache_file = cache_file
        self.by_crc: dict[int, list[tuple[str, str, int]]] = {}

    CHECKPOINT = 2000   # save the cache every N zips read, so an interrupted run keeps its progress

    def build(self, zip_paths: list[str], roots: list[str] = ()) -> int:
        """Index zip_paths (in priority order); return how many zips had to be (re)read.

        Cache entries for other paths are kept (another config may use them), except
        entries under one of `roots` that are no longer in zip_paths: those zips were deleted.
        """
        entries: dict = {}
        if self.cache_file:
            try:
                with open(self.cache_file) as f:
                    data = json.load(f)
                if data.get("version") == self.VERSION:
                    entries = data["zips"]
            except (OSError, ValueError, KeyError):
                pass
        current = set(zip_paths)
        prefixes = tuple(os.path.join(os.path.abspath(r), "") for r in roots if r)
        stale = [p for p in entries if p not in current and p.startswith(prefixes)] if prefixes else []
        for p in stale:
            del entries[p]

        to_read = []
        for path in zip_paths:
            try:
                st = os.stat(path)
            except OSError:
                continue
            e = entries.get(path)
            if not e or e[0] != st.st_size or e[1] != st.st_mtime_ns:
                to_read.append((path, st))
        log(f"  {len(zip_paths)} zips: {len(zip_paths) - len(to_read)} from cache, {len(to_read)} to read")

        for n, (path, st) in enumerate(to_read, 1):
            try:
                with zipfile.ZipFile(path) as zf:
                    members = [[i.CRC, i.filename, i.file_size] for i in zf.infolist() if not i.is_dir()]
            except (zipfile.BadZipFile, OSError) as e:
                log(f"  skipping unreadable zip {path}: {e}")
                entries.pop(path, None)
                continue
            entries[path] = [st.st_size, st.st_mtime_ns, members]
            if n % 500 == 0 or n == len(to_read):
                log(f"  read {n}/{len(to_read)}")
            if n % self.CHECKPOINT == 0:
                self._save(entries)
        if to_read or stale:
            self._save(entries)

        for path in zip_paths:
            e = entries.get(path)
            if e:
                for crc, name, size in e[2]:
                    self.by_crc.setdefault(crc, []).append((path, name, size))
        return len(to_read)

    def _save(self, entries: dict):
        if not self.cache_file:
            return
        os.makedirs(os.path.dirname(self.cache_file), exist_ok=True)
        tmp = self.cache_file + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"version": self.VERSION, "zips": entries}, f)
        os.replace(tmp, self.cache_file)


@dataclass
class RebuildTarget:
    """A zip named in an MRA that exists nowhere, and the parts it must contain."""
    ref: ZipRef
    crcs: set[int] = field(default_factory=set)
    part_names: dict[int, list[str]] = field(default_factory=dict)


@dataclass
class RebuildPlan:
    ref: ZipRef
    members: list[tuple[str, str, str, int]]   # (name in new zip, source zip, source member, crc)
    size: int                                   # uncompressed bytes

    @property
    def kind(self) -> str:
        return HBMAME if self.ref.hint == HBMAME else MAME

    @property
    def sources(self) -> list[str]:
        return sorted({os.path.basename(src) for _, src, _, _ in self.members})

    def contents(self) -> ZipContents:
        return ZipContents(crcs={m[3] for m in self.members}, names={m[0].lower() for m in self.members})


def resolve_rebuild(target: RebuildTarget, index: CrcIndex) -> RebuildPlan | str:
    """Find a local source for every required CRC, or explain what is missing."""
    found = {c: index.by_crc[c] for c in target.crcs if c in index.by_crc}
    missing = sorted(target.crcs - found.keys())
    if missing:
        shown = ", ".join(f"{c:08x}" for c in missing[:5]) + (", ..." if len(missing) > 5 else "")
        return f"{len(found)}/{len(target.crcs)} parts found locally; missing CRCs {shown}"
    # Take each part from the zip that covers the most parts (normally the parent
    # set), so an unrelated file that merely shares a CRC32 isn't picked.
    coverage = Counter(src for cands in found.values() for src in {c[0] for c in cands})
    members, used, size = [], set(), 0
    for crc in sorted(target.crcs):
        src, member, fsize = max(found[crc], key=lambda c: coverage[c[0]])
        for name in target.part_names.get(crc) or [f"{crc:08x}"]:
            out = name if name.lower() not in used else f"{crc:08x}_{name}"
            used.add(out.lower())
            members.append((out, src, member, crc))
            size += fsize
    return RebuildPlan(target.ref, members, size)


def plan_rebuilds(resolver: Resolver, games: list[Game], planned: dict[str, str], get_index):
    """Plan zips to rebuild for requirements still unmet after the planned copies.

    Returns (plans: zip key -> RebuildPlan, failures: requirement label -> [reason, game names]).
    get_index() is only called when there is something to rebuild.
    """
    targets: dict[str, RebuildTarget] = {}
    unmet = []   # (game, req, [(target keys | None, reason)] per option)

    for g in games:
        for req in g.reqs:
            if req.beta or resolver.satisfied(req, planned, {}):
                continue
            options = []
            for opt in req.alts or [[req]]:
                leaves = [leaf for leaf in opt if not resolver.satisfied(leaf, planned, {})]
                reason = None
                for leaf in leaves:
                    if any(z.key in planned or resolver.remote_path(z) for z in leaf.zips):
                        reason = f"{leaf.label()}: zip exists but lacks parts (try --crc --fix-incomplete)"
                    elif not leaf.crcs:
                        reason = f"{leaf.label()}: MRA lists no part CRCs"
                    elif leaf.names:
                        reason = f"{leaf.label()}: some MRA parts have no CRC"
                if reason:
                    options.append((None, reason))
                    continue
                keys = []
                for leaf in leaves:
                    t = targets.setdefault(leaf.zips[0].key, RebuildTarget(leaf.zips[0]))
                    t.crcs |= leaf.crcs
                    for crc, names in leaf.part_names.items():
                        have = t.part_names.setdefault(crc, [])
                        have.extend(n for n in names if n not in have)
                    keys.append(leaf.zips[0].key)
                options.append((keys, None))
            unmet.append((g, req, options))

    resolved = {}
    if targets:
        index = get_index()
        resolved = {k: resolve_rebuild(t, index) for k, t in targets.items()}

    plans: dict[str, RebuildPlan] = {}
    failures: dict[str, list] = {}
    for g, req, options in unmet:
        for keys, _ in options:
            if keys is not None and all(isinstance(resolved[k], RebuildPlan) for k in keys):
                plans.update((k, resolved[k]) for k in keys)
                break
        else:
            reasons = []
            for keys, reason in options:
                reasons += [reason] if reason else [resolved[k] for k in keys if isinstance(resolved[k], str)]
            entry = failures.setdefault(req.label(), ["; ".join(dict.fromkeys(reasons)), []])
            entry[1].append(g.name)
    return plans, failures


def build_zip(plan: RebuildPlan, out_path: str):
    """Write plan's members into a new zip at out_path (atomically)."""
    tmp = out_path + ".tmp"
    sources: dict[str, zipfile.ZipFile] = {}
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as out:
            for name, src, member, crc in plan.members:
                if src not in sources:
                    sources[src] = zipfile.ZipFile(src)
                data = sources[src].read(member)   # zipfile checks the stored CRC while reading
                if zlib.crc32(data) != crc:
                    raise ValueError(f"CRC mismatch for {member} in {src}")
                out.writestr(name, data)
        os.replace(tmp, out_path)
    finally:
        for zf in sources.values():
            zf.close()
        if os.path.exists(tmp):
            os.remove(tmp)


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
    ap.add_argument("--rebuild", action="store_true",
                    help="build zips that exist nowhere from CRC-matching files in other local zips")
    ap.add_argument("--no-cache", action="store_true",
                    help="ignore the MRA cache and download/parse every MRA again "
                         "(does not affect the --rebuild CRC index, which revalidates itself)")
    ap.add_argument("--report", metavar="FILE", help="write a JSON report to FILE")
    ap.add_argument("-v", "--verbose", action="store_true", help="list every game in the report")
    args = ap.parse_args(argv)
    if args.fix_incomplete and not args.crc:
        ap.error("--fix-incomplete requires --crc")

    cfg = load_config(args.config)
    mc, rc = cfg["mister"], cfg["roms"]
    dirs = {MAME: mc["mame_dir"], HBMAME: mc["hbmame_dir"]}

    rom_paths = [os.path.expanduser(p) for p in (rc.get("path"), rc.get("hbmame_path")) if p]
    build_path = os.path.abspath(os.path.expanduser(rc["build_path"])) if rc.get("build_path") else None
    if build_path:
        for p in rom_paths:
            a, b = os.path.realpath(build_path), os.path.realpath(p)
            if os.path.commonpath([a, b]) in (a, b):
                raise SystemExit(f"error: roms.build_path must not be the same as, inside, or contain {p}")

    log("Scanning local ROMs ...")
    local_mame, local_hb = scan_local([
        (os.path.expanduser(rc["path"]) if rc.get("path") else None, MAME, True),
        (os.path.expanduser(rc["hbmame_path"]) if rc.get("hbmame_path") else None, HBMAME, True),
        (os.path.join(build_path, MAME) if build_path else None, MAME, False),
        (os.path.join(build_path, HBMAME) if build_path else None, HBMAME, False),
    ])
    local = {MAME: local_mame, HBMAME: local_hb}
    log(f"  {len(local_mame)} mame zips, {len(local_hb)} hbmame zips")

    log(f"Connecting to {mc.get('user', 'root')}@{mc['host']} ...")
    mister = Mister(mc)
    tmp_dir = None
    try:
        log(f"Reading MRAs from {mc['mra_dir']} ...")
        cache_key = hashlib.sha1(f"{mc['host']}:{mc.get('port', 22)}:{mc['mra_dir']}".encode()).hexdigest()[:12]
        cache = MraCache(os.path.join(cache_dir(), f"mra-{cache_key}.json"))
        if args.no_cache:
            cache.entries = {}
        games, no_rom, parse_errors, mra_count = load_mras(mister, mc["mra_dir"], cache)
        log(f"  {len(parse_errors)} parse errors")

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

        # ---- rebuild zips that exist nowhere from parts in other local zips ----
        rebuild_plans: dict[str, RebuildPlan] = {}
        rebuild_failed: dict[str, list] = {}
        rebuild_jobs: dict[str, LocalZip] = {}
        if args.rebuild:
            def get_index():
                zip_paths = list(dict.fromkeys(lz.path for kind in (MAME, HBMAME) for lz in local[kind].values()))
                log("Indexing local zips by CRC ...")
                idx = CrcIndex(os.path.join(cache_dir(), "crc_index.json"))
                idx.build(zip_paths, roots=[*rom_paths, *([build_path] if build_path else [])])
                return idx

            planned_all = {**planned_copies, **{k: lz.path for k, lz in replace_plan.items()}}
            rebuild_plans, rebuild_failed = plan_rebuilds(resolver, games, planned_all, get_index)
            if rebuild_plans:
                log(f"{'Would rebuild' if args.dry_run else 'Rebuilding'} {len(rebuild_plans)} zip(s) ...")
            for key, plan in sorted(rebuild_plans.items()):
                if args.dry_run:
                    path = f"<rebuild>/{plan.ref.name}"
                else:
                    if build_path:
                        out_dir = os.path.join(build_path, plan.kind)
                    else:
                        tmp_dir = tmp_dir or tempfile.mkdtemp(prefix="mister-rebuild-")
                        out_dir = os.path.join(tmp_dir, plan.kind)
                    os.makedirs(out_dir, exist_ok=True)
                    path = os.path.join(out_dir, plan.ref.name)
                    if os.path.exists(path):
                        rebuild_failed[plan.ref.name] = [f"{path} already exists, not overwriting", []]
                        continue
                    log(f"  build {plan.ref.name} <- {', '.join(plan.sources)} ({len(plan.members)} files)")
                    try:
                        build_zip(plan, path)
                    except (zipfile.BadZipFile, OSError, ValueError) as e:
                        rebuild_failed[plan.ref.name] = [f"build failed: {e}", []]
                        continue
                resolver.register("local", path, plan.contents())
                rebuild_jobs[key] = LocalZip(path, plan.kind)

        copied: dict[str, str] = {}
        replaced: dict[str, str] = {}
        rebuilt: dict[str, str] = {}
        upload_errors: dict[str, str] = {}
        sent = {"copy": 0, "replace": 0, "rebuild": 0}
        jobs = [(k, lz, "copy") for k, lz in sorted(copy_plan.items())] + \
               [(k, lz, "replace") for k, lz in sorted(replace_plan.items())] + \
               [(k, lz, "rebuild") for k, lz in sorted(rebuild_jobs.items())]
        if jobs:
            verb = "Would upload" if args.dry_run else "Uploading"
            log(f"{verb} {len(jobs)} zip(s) ...")
        done = {"copy": copied, "replace": replaced, "rebuild": rebuilt}
        for i, (key, lz, job) in enumerate(jobs, 1):
            if job == "replace":
                target = remote[MAME].get(key) or remote[HBMAME][key]
            else:
                target = posixpath.join(dirs[lz.kind], os.path.basename(lz.path))
            size = rebuild_plans[key].size if job == "rebuild" and args.dry_run else os.path.getsize(lz.path)
            if not args.dry_run:
                log(f"  [{i}/{len(jobs)}] {job} {os.path.basename(lz.path)} ({human(size)})")
                try:
                    mister.ensure_dir(posixpath.dirname(target))
                    mister.upload(lz.path, target)
                except (OSError, IOError) as e:
                    upload_errors[key] = str(e)
                    continue
            done[job][key] = lz.path
            sent[job] += size
        bytes_sent = sum(sent.values())

        after = {g.mra: [resolver.satisfied(r, {**copied, **rebuilt}, replaced) for r in g.reqs] for g in games}
    finally:
        mister.close()
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)

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
    absent = referenced - already - set(copied) - set(rebuilt)
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

    row("MRA files scanned", mra_count)
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
    row("would copy" if args.dry_run else "copied", f"{len(copied)}  ({human(sent['copy'])})", 3)
    if args.fix_incomplete:
        row("would replace" if args.dry_run else "replaced (incomplete)", len(replaced), 3)
    if args.rebuild:
        where = "kept in build_path" if build_path else "uploaded only, not kept"
        size = f"~{human(sent['rebuild'])} uncompressed" if args.dry_run else human(sent["rebuild"])
        row("would rebuild" if args.dry_run else "rebuilt", f"{len(rebuilt)}  ({size}, {where})", 3)
        row("rebuild not possible", len(rebuild_failed), 3)
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
    section("Zips that would be rebuilt" if args.dry_run else "Zips rebuilt from other local sets",
            [f"{rebuild_plans[k].ref.name}  <- {', '.join(rebuild_plans[k].sources)} "
             f"({len(rebuild_plans[k].members)} files)"
             + (f"  saved to {p}" if build_path and not args.dry_run else "")
             for k, p in sorted(rebuilt.items())])
    section("Rebuild not possible",
            [f"{label}: {reason}" + (f"  <- {_games_str(names)}" if names else "")
             for label, (reason, names) in sorted(rebuild_failed.items())])
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
                "mra_files": mra_count,
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
                "zips_rebuilt": len(rebuilt),
                "rebuild_not_possible": len(rebuild_failed),
                "zips_absent_not_needed": len(absent_unneeded),
                "zips_absent_needed": len(absent_needed),
                "bytes_copied": bytes_sent,
                "upload_errors": len(upload_errors),
            },
            "copied": sorted(os.path.basename(p) for p in copied.values()),
            "replaced": sorted(os.path.basename(p) for p in replaced.values()),
            "rebuilt": [
                {"zip": rebuild_plans[k].ref.name, "sources": rebuild_plans[k].sources,
                 "files": len(rebuild_plans[k].members),
                 "saved_to": p if build_path and not args.dry_run else None}
                for k, p in sorted(rebuilt.items())
            ],
            "rebuild_not_possible": {label: {"reason": reason, "games": names}
                                     for label, (reason, names) in rebuild_failed.items()},
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
