import io
import os
import sys
import tempfile
import unittest
import zipfile
import zlib

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from mister_rom_audit import (HBMAME, MAME, CrcIndex, LocalZip,  # noqa: E402
                              Resolver, build_zip, parse_mra, plan_rebuilds, read_zip_contents)

MRA = b"""<misterromdescription>
  <name>Ms. Pac-Man (bootleg)</name>
  <rom index="0" zip="mspacmnf.zip|mspacman.zip" md5="none">
    <part crc="0000000a" name="a.bin"/>
    <interleave output="16">
      <part crc="0000000b" name="b.bin" map="01"/>
    </interleave>
    <part zip="hbmame/extra.zip" crc="0000000c"/>
    <part>00 01 02</part>
  </rom>
  <rom index="1"><part>00</part></rom>
</misterromdescription>"""


def make_zip(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for n, d in files.items():
            zf.writestr(n, d)
    buf.seek(0)
    return buf


class ParseTest(unittest.TestCase):
    def test_parse(self):
        g = parse_mra(MRA, "sub/msp.mra")
        self.assertEqual(g.name, "Ms. Pac-Man (bootleg)")
        self.assertEqual(len(g.reqs), 2)
        main, extra = g.reqs
        self.assertEqual([z.key for z in main.zips], ["mspacmnf.zip", "mspacman.zip"])
        self.assertEqual(main.crcs, {0xA, 0xB})
        self.assertEqual(extra.zips[0].key, "extra.zip")
        self.assertEqual(extra.zips[0].hint, HBMAME)
        self.assertEqual(extra.crcs, {0xC})


class CaseInsensitiveTest(unittest.TestCase):
    def test_mixed_case_tags_and_attrs(self):
        data = b'''<misterromdescription><name>Space Demon</name>
          <rom index="0" ZIP="spacedem.zip"><PART CRC="0000000a" name="a"/></rom>
          <rom index="5"><PART>00 01</PART></ROM></misterromdescription>'''
        g = parse_mra(data, "Space Demon.mra")
        self.assertEqual([z.key for z in g.reqs[0].zips], ["spacedem.zip"])
        self.assertEqual(g.reqs[0].crcs, {0xA})


class BetaKeyTest(unittest.TestCase):
    def test_beta_requirement_flagged(self):
        data = b'''<misterromdescription><name>SF3</name>
          <rom index="0" zip="sfiii3n.zip|sfiii3.zip"><part crc="0000000a"/></rom>
          <rom index="17" zip="jtbeta.zip" md5="None"/></misterromdescription>'''
        g = parse_mra(data, "sf3.mra")
        self.assertEqual([r.beta for r in g.reqs], [False, True])


class SameIndexAlternativesTest(unittest.TestCase):
    DATA = b'''<misterromdescription><name>Two Tigers</name>
      <rom index="0" zip="twotiger.zip" type="merged"><part crc="0000000a"/></rom>
      <rom index="0" zip="twotigerc.zip" type="nonmerged"><part crc="0000000a"/></rom>
      <rom index="1"><part>00</part></rom></misterromdescription>'''

    def test_parsed_as_one_either_or_requirement(self):
        g = parse_mra(self.DATA, "tt.mra")
        self.assertEqual(len(g.reqs), 1)
        self.assertEqual([z.key for z in g.reqs[0].zips], ["twotiger.zip", "twotigerc.zip"])
        self.assertEqual(g.reqs[0].label(), "twotiger.zip or twotigerc.zip")

    def test_either_option_satisfies(self):
        g = parse_mra(self.DATA, "tt.mra")
        for present in ("twotiger.zip", "twotigerc.zip"):
            r = Resolver({MAME: {present: "/r/" + present}, HBMAME: {}}, {MAME: {}, HBMAME: {}})
            self.assertTrue(r.satisfied(g.reqs[0], {}, {}))
        r = Resolver({MAME: {}, HBMAME: {}}, {MAME: {}, HBMAME: {}})
        self.assertFalse(r.satisfied(g.reqs[0], {}, {}))


class ResolveTest(unittest.TestCase):
    def setUp(self):
        self.game = parse_mra(MRA, "msp.mra")

    def test_existence_mode(self):
        remote = {MAME: {"mspacman.zip": "/r/mame/mspacman.zip"}, HBMAME: {}}
        local = {MAME: {"mspacmnf.zip": LocalZip("/l/mspacmnf.zip", MAME)},
                 HBMAME: {"extra.zip": LocalZip("/l/hb/extra.zip", HBMAME)}}
        r = Resolver(remote, local)
        before = [r.satisfied(q, {}, {}) for q in self.game.reqs]
        self.assertEqual(before, [True, False])
        plan = r.plan_copies([self.game])
        self.assertEqual(set(plan), {"mspacmnf.zip", "extra.zip"})
        copied = {k: v.path for k, v in plan.items()}
        self.assertTrue(all(r.satisfied(q, copied, {}) for q in self.game.reqs))

    def test_still_missing(self):
        r = Resolver({MAME: {}, HBMAME: {}}, {MAME: {}, HBMAME: {}})
        self.assertEqual(r.plan_copies([self.game]), {})
        self.assertFalse(r.satisfied(self.game.reqs[0], {}, {}))

    def test_crc_mode(self):
        zips = {
            "/r/mspacman.zip": {"a.bin": b""},                    # crc 0 -> incomplete
            "/l/mspacman.zip": {"a.bin": b"x", "b.bin": b"y"},
        }
        want_a, want_b = (read_zip_contents(make_zip({"f": d})).crcs.pop() for d in (b"x", b"y"))
        mra = MRA.replace(b"0000000a", f"{want_a:08x}".encode()).replace(b"0000000b", f"{want_b:08x}".encode())
        game = parse_mra(mra, "msp.mra")
        contents = lambda p: read_zip_contents(make_zip(zips[p]))  # noqa: E731
        remote = {MAME: {"mspacman.zip": "/r/mspacman.zip"}, HBMAME: {}}
        local = {MAME: {"mspacman.zip": LocalZip("/l/mspacman.zip", MAME)}, HBMAME: {}}
        r = Resolver(remote, local, remote_contents=contents, local_contents=contents)
        self.assertFalse(r.satisfied(game.reqs[0], {}, {}))
        repl = r.plan_replacements([game], {})
        self.assertEqual(set(repl), {"mspacman.zip"})
        self.assertTrue(r.satisfied(game.reqs[0], {}, {"mspacman.zip": "/l/mspacman.zip"}))


def crc(data: bytes) -> int:
    return zlib.crc32(data)


RING = {"r1.bin": b"ring king 1", "r2.bin": b"ring king 2"}
PARENT_ONLY = {"k1.bin": b"king of boxer"}


def ring_mra(extra_part=b""):
    parts = b"".join(b'<part crc="%08x" name="%s"/>' % (crc(d), n.encode()) for n, d in RING.items())
    return b'<misterromdescription><name>Ring King</name><rom index="0" zip="ringking.zip">' \
        + parts + extra_part + b"</rom></misterromdescription>"


class RebuildTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        # merged parent: clone files stored under other names / in a subfolder
        self.kingofb = os.path.join(self.dir, "kingofb.zip")
        with zipfile.ZipFile(self.kingofb, "w") as zf:
            for n, d in PARENT_ONLY.items():
                zf.writestr(n, d)
            for n, d in RING.items():
                zf.writestr("ringking/" + n.upper(), d)
        # unrelated zip that happens to contain one of the CRCs
        self.decoy = os.path.join(self.dir, "aaa_decoy.zip")
        with zipfile.ZipFile(self.decoy, "w") as zf:
            zf.writestr("x.bin", RING["r1.bin"])
        self.index = CrcIndex()
        self.index.build([self.decoy, self.kingofb])
        self.resolver = Resolver({MAME: {}, HBMAME: {}}, {MAME: {}, HBMAME: {}})

    def tearDown(self):
        self.tmp.cleanup()

    def plan(self, games):
        return plan_rebuilds(self.resolver, games, {}, lambda: self.index)

    def test_plans_from_parent_and_prefers_best_donor(self):
        plans, failed = self.plan([parse_mra(ring_mra(), "rk.mra")])
        self.assertEqual(failed, {})
        p = plans["ringking.zip"]
        self.assertEqual(p.sources, ["kingofb.zip"])          # not the decoy
        self.assertEqual(sorted(m[0] for m in p.members), ["r1.bin", "r2.bin"])

    def test_missing_crc_reports_partial(self):
        plans, failed = self.plan([parse_mra(ring_mra(b'<part crc="deadbeef" name="x"/>'), "rk.mra")])
        self.assertEqual(plans, {})
        reason, games = failed["ringking.zip"]
        self.assertIn("2/3 parts found", reason)
        self.assertIn("deadbeef", reason)
        self.assertEqual(games, ["Ring King"])

    def test_index_not_built_when_nothing_to_rebuild(self):
        resolver = Resolver({MAME: {"ringking.zip": "/r/ringking.zip"}, HBMAME: {}}, {MAME: {}, HBMAME: {}})
        plans, failed = plan_rebuilds(resolver, [parse_mra(ring_mra(), "rk.mra")], {},
                                      lambda: self.fail("index should not be built"))
        self.assertEqual((plans, failed), ({}, {}))

    def test_alternatives_rebuild_only_first_buildable_option(self):
        data = ring_mra().replace(b'<rom index="0" zip="ringking.zip">',
                                  b'<rom index="0" zip="nothere.zip"><part crc="deadbeef"/></rom>'
                                  b'<rom index="0" zip="ringking.zip">')
        plans, failed = self.plan([parse_mra(data, "rk.mra")])
        self.assertEqual(set(plans), {"ringking.zip"})
        self.assertEqual(failed, {})

    def test_build_zip(self):
        plans, _ = self.plan([parse_mra(ring_mra(), "rk.mra")])
        out = os.path.join(self.dir, "ringking.zip")
        build_zip(plans["ringking.zip"], out)
        with zipfile.ZipFile(out) as zf:
            self.assertEqual({n: zf.read(n) for n in zf.namelist()}, RING)
        self.assertFalse(os.path.exists(out + ".tmp"))

    def test_index_cache_keeps_other_roots_and_drops_deleted(self):
        cache = os.path.join(self.dir, "cache", "idx.json")
        other = os.path.join(self.dir, "other")
        os.makedirs(other)
        other_zip = os.path.join(other, "x.zip")
        with zipfile.ZipFile(other_zip, "w") as zf:
            zf.writestr("x", b"x")
        CrcIndex(cache).build([other_zip], roots=[other])           # "config 2"
        CrcIndex(cache).build([self.kingofb, self.decoy], roots=[self.dir + "/nonexistent"])
        # config 2's entry survived config 1's run: nothing to re-read
        self.assertEqual(CrcIndex(cache).build([other_zip], roots=[other]), 0)
        # a zip deleted from config 2's root is dropped from the cache
        os.remove(other_zip)
        CrcIndex(cache).build([], roots=[other])
        import json
        with open(cache) as f:
            self.assertNotIn(other_zip, json.load(f)["zips"])

    def test_index_checkpoint_saves_progress(self):
        cache = os.path.join(self.dir, "cache", "idx.json")
        idx = CrcIndex(cache)
        idx.CHECKPOINT = 1
        saves = []
        orig = idx._save
        idx._save = lambda e: (saves.append(len(e)), orig(e))
        idx.build([self.decoy, self.kingofb])
        self.assertEqual(saves[:2], [1, 2])

    def test_index_cache_roundtrip(self):
        cache = os.path.join(self.dir, "cache", "idx.json")
        self.assertEqual(CrcIndex(cache).build([self.kingofb]), 1)
        again = CrcIndex(cache)
        self.assertEqual(again.build([self.kingofb]), 0)       # served from cache
        self.assertIn(crc(RING["r1.bin"]), again.by_crc)


class MraCacheTest(unittest.TestCase):
    def test_requirement_roundtrip(self):
        import mister_rom_audit as m
        for data in (MRA, SameIndexAlternativesTest.DATA, ring_mra()):
            g = parse_mra(data, "x.mra")
            self.assertEqual([m.req_from_dict(m.req_to_dict(r)) for r in g.reqs], g.reqs)

    def test_only_new_or_changed_mras_are_downloaded(self):
        import mister_rom_audit as m
        files = {"a.mra": (MRA, (10, 1)), "b.mra": (b"<broken", (5, 1))}

        class Fake:
            fetched = []
            def list_mras(self, d):
                return {p: st for p, (_, st) in files.items()}
            def read_mras(self, d, paths):
                Fake.fetched.append(sorted(paths))
                return {p: files[p][0] for p in paths}

        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "mra.json")
            games, no_rom, errors, n = m.load_mras(Fake(), "/x", m.MraCache(path))
            self.assertEqual((len(games), len(errors), n), (1, 1, 2))
            m.load_mras(Fake(), "/x", m.MraCache(path))
            files["a.mra"] = (MRA, (10, 2))          # touched
            files["c.mra"] = (ring_mra(), (7, 1))    # new
            del files["b.mra"]                      # deleted
            games, _, errors, n = m.load_mras(Fake(), "/x", m.MraCache(path))
            self.assertEqual(Fake.fetched, [["a.mra", "b.mra"], ["a.mra", "c.mra"]])
            self.assertEqual((sorted(g.mra for g in games), errors, n), (["a.mra", "c.mra"], [], 2))
            self.assertEqual(games[0].reqs, parse_mra(MRA, "a.mra").reqs)


class BuildPathSafetyTest(unittest.TestCase):
    def test_build_path_inside_collection_rejected(self):
        import mister_rom_audit as m
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "roms"))
            cfg = os.path.join(d, "c.yaml")
            with open(cfg, "w") as f:
                f.write(f"mister: {{host: x}}\nroms: {{path: {d}/roms, build_path: {d}/roms/built}}\n")
            with self.assertRaises(SystemExit) as cm:
                m.main(["-c", cfg, "-n"])
            self.assertIn("build_path", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
