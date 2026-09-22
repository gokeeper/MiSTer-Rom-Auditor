import io
import os
import sys
import unittest
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from mister_rom_audit import (HBMAME, MAME, LocalZip, Resolver, parse_mra,  # noqa: E402
                              read_zip_contents)

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


if __name__ == "__main__":
    unittest.main()
