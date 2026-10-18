import functools
import hashlib
import http.server
import io
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import unittest
import zipfile
import xml.etree.ElementTree as ET

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import kodirepo as kr  # noqa: E402

ADDON_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<addon id="{id}" name="Test" version="{version}" provider-name="me">
    <requires><import addon="xbmc.python" version="3.0.1"/></requires>
    <extension point="xbmc.python.pluginsource" library="addon.py"><provides>video</provides></extension>
    <extension point="xbmc.addon.metadata">
        <summary lang="en_GB">Test (unofficial)</summary>
        <description lang="en_GB">Unofficial test add-on.</description>
        <license>GPL-3.0-or-later</license>
        <source>https://example.invalid/src</source>
        <assets><icon>resources/icon.png</icon></assets>
    </extension>
</addon>
"""


def make_source(base, addon_id="plugin.video.test", version="1.0.0", xml=None, extra=None):
    src = tempfile.mkdtemp(dir=base)
    with open(os.path.join(src, "addon.xml"), "w", encoding="utf-8") as fh:
        fh.write(xml if xml is not None else ADDON_XML.format(id=addon_id, version=version))
    with open(os.path.join(src, "addon.py"), "w") as fh:
        fh.write("print('hi')\n")
    os.makedirs(os.path.join(src, "resources", "lib"))
    with open(os.path.join(src, "resources", "icon.png"), "wb") as fh:
        fh.write(b"\x89PNG fake")
    with open(os.path.join(src, "resources", "lib", "mod.py"), "w") as fh:
        fh.write("X = 1\n")
    for rel, data in (extra or {}).items():
        path = os.path.join(src, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(data)
    return src


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.site = os.path.join(self.tmp, "site")


class VersionTest(unittest.TestCase):
    def test_parse_and_pad(self):
        self.assertEqual(kr.parse_version("1.2"), kr.parse_version("1.2.0"))
        self.assertLess(kr.parse_version("0.9.0"), kr.parse_version("0.10.0"))  # numeric, not lexical
        self.assertEqual(kr.newest(["0.9.0", "0.10.0", "0.6.4"]), "0.10.0")

    def test_invalid(self):
        for bad in ("", "1", "v1.0.0", "1.0.0-beta", "1.0.0.0.0", "a.b", "1..2"):
            with self.assertRaises(kr.ValidationError, msg=bad):
                kr.parse_version(bad)


class ValidateTest(unittest.TestCase):
    def check(self, xml, expected="plugin.video.test", tag=None):
        return kr.validate_addon_xml(xml.encode("utf-8"), expected, tag)

    def test_valid(self):
        info = self.check(ADDON_XML.format(id="plugin.video.test", version="1.2.3"))
        self.assertEqual((info.id, info.version, info.libraries), ("plugin.video.test", "1.2.3", ["addon.py"]))

    def test_id_mismatch(self):
        with self.assertRaisesRegex(kr.ValidationError, "does not match the expected id"):
            self.check(ADDON_XML.format(id="plugin.video.other", version="1.0.0"))

    def test_bad_version(self):
        with self.assertRaisesRegex(kr.ValidationError, "invalid version"):
            self.check(ADDON_XML.format(id="plugin.video.test", version="1.0.0-rc1"))

    def test_tag_must_match_version(self):
        xml = ADDON_XML.format(id="plugin.video.test", version="1.2.3")
        self.check(xml, tag="v1.2.3")
        self.check(xml, tag="1.2.3")
        with self.assertRaisesRegex(kr.ValidationError, "release tag"):
            self.check(xml, tag="v1.2.4")

    def test_not_xml_and_wrong_root(self):
        with self.assertRaises(kr.ValidationError):
            self.check("<addon")
        with self.assertRaisesRegex(kr.ValidationError, "root element"):
            self.check("<foo/>")

    def test_required_metadata_reported_together(self):
        xml = '<addon id="plugin.video.test" name="T" version="1.0.0" provider-name="me"><requires>' \
              '<import addon="xbmc.python" version="3.0.1"/></requires>' \
              '<extension point="xbmc.addon.metadata"><summary>x</summary></extension></addon>'
        with self.assertRaises(kr.ValidationError) as cm:
            self.check(xml)
        self.assertIn("<license>", str(cm.exception))
        self.assertIn("<description>", str(cm.exception))
        self.assertIn("unofficial", str(cm.exception))

    def test_hungarian_unofficial_notice_accepted(self):
        xml = ADDON_XML.format(id="plugin.video.test", version="1.0.0") \
            .replace("Test (unofficial)", "Teszt (nem hivatalos)").replace("Unofficial test add-on.", "Teszt.")
        self.check(xml)

    def test_plugin_needs_xbmc_python(self):
        xml = ADDON_XML.format(id="plugin.video.test", version="1.0.0").replace(
            '<import addon="xbmc.python" version="3.0.1"/>', "")
        with self.assertRaisesRegex(kr.ValidationError, "xbmc.python"):
            self.check(xml)

    def test_missing_optional_fields_are_warnings(self):
        xml = ADDON_XML.format(id="plugin.video.test", version="1.0.0")
        info = self.check(xml)
        self.assertTrue(any("website" in w for w in info.warnings))


class PackageTest(Base):
    def test_layout_and_determinism(self):
        src = make_source(self.tmp)
        info, _ = kr.load_source(src, "plugin.video.test")
        a, b = os.path.join(self.tmp, "a.zip"), os.path.join(self.tmp, "b.zip")
        kr.build_zip(src, info, a)
        kr.build_zip(src, info, b)
        with open(a, "rb") as fa, open(b, "rb") as fb:
            self.assertEqual(fa.read(), fb.read())
        names = zipfile.ZipFile(a).namelist()
        self.assertEqual(names, sorted(names))
        self.assertTrue(all(n.startswith("plugin.video.test/") for n in names))
        self.assertIn("plugin.video.test/addon.xml", names)
        self.assertIn("plugin.video.test/resources/lib/mod.py", names)
        with open(a, "rb") as fh:
            self.assertEqual(kr.inspect_zip(fh, "plugin.video.test")[:2], ("plugin.video.test", "1.0.0"))

    def test_excludes_tests_docs_and_caches(self):
        src = make_source(self.tmp, extra={"tests/t.py": b"x", "docs/d.md": b"x", "README.md": b"x",
                                           "resources/lib/__pycache__/m.pyc": b"x", "resources/.DS_Store": b"x",
                                           "LICENSE": b"GPL"})
        info, _ = kr.load_source(src, "plugin.video.test")
        files = kr.collect_files(src, info.libraries)
        self.assertEqual(files, ["LICENSE", "addon.py", "addon.xml", "resources/icon.png", "resources/lib/mod.py"])

    def test_refuses_secret_like_files(self):
        for bad in ("resources/lib/_local_key.py", "resources/.env", "resources/x.pem", "resources/a.har",
                    "resources/my.secret.json"):
            src = make_source(self.tmp, extra={bad: b"s"})
            info, _ = kr.load_source(src, "plugin.video.test")
            with self.assertRaises(kr.PackageError, msg=bad):
                kr.collect_files(src, info.libraries)

    def test_refuses_symlinks(self):
        src = make_source(self.tmp)
        os.symlink("/etc/hostname", os.path.join(src, "resources", "link"))
        info, _ = kr.load_source(src, "plugin.video.test")
        with self.assertRaisesRegex(kr.PackageError, "symbolic link"):
            kr.collect_files(src, info.libraries)

    def test_missing_referenced_file(self):
        src = make_source(self.tmp)
        os.remove(os.path.join(src, "resources", "icon.png"))
        with self.assertRaisesRegex(kr.ValidationError, "missing"):
            kr.load_source(src, "plugin.video.test")

    def test_path_traversal_in_assets_rejected(self):
        xml = ADDON_XML.format(id="plugin.video.test", version="1.0.0").replace(
            "resources/icon.png", "../outside.png")
        src = make_source(self.tmp, xml=xml)
        with self.assertRaisesRegex(kr.ValidationError, "escapes"):
            kr.load_source(src, "plugin.video.test")

    def test_inspect_zip_rejects_bad_layouts(self):
        def zipped(entries):
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as zf:
                for name, data in entries.items():
                    zf.writestr(name, data)
            buf.seek(0)
            return buf
        good_xml = ADDON_XML.format(id="plugin.video.test", version="1.0.0")
        with self.assertRaisesRegex(kr.PackageError, "one top-level"):
            kr.inspect_zip(zipped({"addon.xml": good_xml, "x/y": "1"}))
        with self.assertRaisesRegex(kr.PackageError, "does not match add-on id"):
            kr.inspect_zip(zipped({"other/addon.xml": good_xml}), "plugin.video.test")
        with self.assertRaisesRegex(kr.PackageError, "no plugin.video.test/addon.xml"):
            kr.inspect_zip(zipped({"plugin.video.test/readme": "x"}))
        with self.assertRaisesRegex(kr.PackageError, "does not match the folder"):
            kr.inspect_zip(zipped({"plugin.video.wrong/addon.xml": good_xml}))
        with self.assertRaisesRegex(kr.PackageError, "unsafe"):
            kr.inspect_zip(zipped({"plugin.video.test/../evil": "x", "plugin.video.test/addon.xml": good_xml}))


class StageTest(Base):
    def stage(self, src, addon_id="plugin.video.test", **kw):
        return kr.stage(src, addon_id, self.site, **kw)

    def read(self, rel):
        with open(os.path.join(self.site, rel), "rb") as fh:
            return fh.read()

    def test_first_publish_creates_complete_site(self):
        res = self.stage(make_source(self.tmp))
        self.assertEqual(res["status"], "added")
        index = self.read("addons.xml")
        self.assertEqual(hashlib.md5(index).hexdigest(), self.read("addons.xml.md5").decode())
        self.assertFalse(self.read("addons.xml.md5").endswith(b"\n"))
        self.assertTrue(index.startswith(b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<addons>'))
        self.assertEqual(index.count(b"<?xml"), 1)  # per-add-on declarations stripped
        root = ET.fromstring(index)
        self.assertEqual([(a.get("id"), a.get("version")) for a in root], [("plugin.video.test", "1.0.0")])
        self.assertTrue(os.path.isfile(os.path.join(self.site, "plugin.video.test", "plugin.video.test-1.0.0.zip")))
        self.assertTrue(os.path.isfile(os.path.join(self.site, "plugin.video.test", "resources", "icon.png")))
        self.assertEqual(kr.verify_site(self.site), [])

    def test_rerun_is_idempotent_and_byte_stable(self):
        src = make_source(self.tmp)
        self.stage(src)
        before = {r: self.read(r) for r in ("addons.xml", "addons.xml.md5", "index.html")}
        res = self.stage(src)
        self.assertEqual(res["status"], "unchanged")
        self.assertEqual(res["changed"], [])
        self.assertEqual(before, {r: self.read(r) for r in before})

    def test_new_version_keeps_old_zip_and_updates_index(self):
        self.stage(make_source(self.tmp, version="0.9.0"))
        self.stage(make_source(self.tmp, version="0.10.0"))
        d = os.path.join(self.site, "plugin.video.test")
        self.assertEqual(sorted(f for f in os.listdir(d) if f.endswith(".zip")),
                         ["plugin.video.test-0.10.0.zip", "plugin.video.test-0.9.0.zip"])
        self.assertEqual(ET.fromstring(self.read("addons.xml"))[0].get("version"), "0.10.0")
        self.assertEqual(kr.verify_site(self.site), [])

    def test_same_version_different_content_is_refused(self):
        self.stage(make_source(self.tmp))
        snapshot = self.read("addons.xml")
        changed = make_source(self.tmp, extra={"resources/lib/mod.py": b"X = 2\n"})
        with self.assertRaisesRegex(kr.ConflictError, "already published with different content"):
            self.stage(changed)
        self.assertEqual(self.read("addons.xml"), snapshot)

    def test_equivalent_version_spelling_is_refused(self):
        self.stage(make_source(self.tmp, version="1.0.0"))
        with self.assertRaises(kr.ConflictError):
            self.stage(make_source(self.tmp, version="1.0"))

    def test_lower_version_is_refused(self):
        self.stage(make_source(self.tmp, version="1.1.0"))
        with self.assertRaisesRegex(kr.ConflictError, "not newer"):
            self.stage(make_source(self.tmp, version="1.0.9"))

    def test_failure_leaves_site_untouched(self):
        self.stage(make_source(self.tmp, version="1.0.0"))
        snap = {r: self.read(r) for r in ("addons.xml", "addons.xml.md5")}
        bad = make_source(self.tmp, version="1.0.1", extra={"resources/_local_key.py": b"k"})
        with self.assertRaises(kr.PackageError):
            self.stage(bad)
        self.assertEqual(snap, {r: self.read(r) for r in snap})
        self.assertFalse(os.path.exists(os.path.join(self.site, "plugin.video.test",
                                                     "plugin.video.test-1.0.1.zip")))

    def test_wrong_id_and_tag_are_refused(self):
        src = make_source(self.tmp)
        with self.assertRaises(kr.ValidationError):
            self.stage(src, addon_id="plugin.video.expected")
        with self.assertRaises(kr.ValidationError):
            self.stage(src, tag="v9.9.9")
        self.assertFalse(os.path.exists(self.site))

    def test_two_addons_sorted_in_index(self):
        self.stage(make_source(self.tmp, addon_id="plugin.video.zeta"), "plugin.video.zeta")
        self.stage(make_source(self.tmp, addon_id="plugin.video.alpha"), "plugin.video.alpha")
        ids = [a.get("id") for a in ET.fromstring(self.read("addons.xml"))]
        self.assertEqual(ids, ["plugin.video.alpha", "plugin.video.zeta"])

    def test_empty_site_cannot_be_indexed(self):
        os.makedirs(self.site)
        with self.assertRaises(kr.RepoError):
            kr.generate_index(self.site)


class VerifyTest(Base):
    def setUp(self):
        super().setUp()
        kr.stage(make_source(self.tmp), "plugin.video.test", self.site)

    def test_detects_md5_mismatch(self):
        with open(os.path.join(self.site, "addons.xml.md5"), "w") as fh:
            fh.write("0" * 32)
        self.assertTrue(any("md5" in p for p in kr.verify_site(self.site)))

    def test_detects_edited_index(self):
        path = os.path.join(self.site, "addons.xml")
        with open(path, "ab") as fh:
            fh.write(b"\n")
        self.assertTrue(any("md5" in p for p in kr.verify_site(self.site)))

    def test_detects_missing_zip(self):
        os.remove(os.path.join(self.site, "plugin.video.test", "plugin.video.test-1.0.0.zip"))
        self.assertTrue(kr.verify_site(self.site))

    def test_detects_zip_with_other_version(self):
        d = os.path.join(self.site, "plugin.video.test")
        kr.build_zip(make_source(self.tmp, version="2.0.0"), kr.load_source(
            make_source(self.tmp, version="2.0.0"), "plugin.video.test")[0],
            os.path.join(d, "plugin.video.test-1.0.0.zip"))
        self.assertTrue(any("version" in p for p in kr.verify_site(self.site)))

    def test_detects_index_entry_diverging_from_zip(self):
        path = os.path.join(self.site, "addons.xml")
        with open(path, "rb") as fh:
            data = fh.read().replace(b'name="Test"', b'name="Changed"')
        with open(path, "wb") as fh:
            fh.write(data)
        with open(path + ".md5", "w") as fh:
            fh.write(hashlib.md5(data).hexdigest())
        self.assertTrue(any("differs" in p for p in kr.verify_site(self.site)))

    def test_cli_exit_codes(self):
        self.assertEqual(kr.main(["verify", "--site", self.site]), 0)
        os.remove(os.path.join(self.site, "addons.xml.md5"))
        self.assertEqual(kr.main(["verify", "--site", self.site]), 1)


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


class VerifyUrlTest(Base):
    def serve(self):
        handler = functools.partial(_Quiet, directory=self.site)
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        return "http://127.0.0.1:%d" % server.server_address[1]

    def test_live_site_verifies_and_breakage_is_detected(self):
        kr.stage(make_source(self.tmp), "plugin.video.test", self.site)
        base = self.serve()
        self.assertEqual(kr.verify_url(base), [])
        os.remove(os.path.join(self.site, "plugin.video.test", "plugin.video.test-1.0.0.zip"))
        self.assertTrue(kr.verify_url(base))


class ConfigAndRepoAddonTest(unittest.TestCase):
    def test_source_info(self):
        self.assertEqual(kr.source_info("plugin.video.filmio")["repo"], "ceto/plugin.video.filmio")
        self.assertEqual(kr.source_info("repository.ceto")["path"], "repository.ceto")
        with self.assertRaises(kr.RepoError):
            kr.source_info("plugin.video.unknown")

    def test_repository_addon_is_valid_and_points_at_pages(self):
        src = os.path.join(ROOT, "repository.ceto")
        info, data = kr.load_source(src, "repository.ceto")
        root = ET.fromstring(data)
        ext = root.find("./extension[@point='xbmc.addon.repository']")
        d = ext.find("dir")
        base = "https://cetorepo.szabogabi.com"
        self.assertEqual(d.findtext("info"), base + "/addons.xml")
        self.assertEqual(d.findtext("checksum"), base + "/addons.xml.md5")
        self.assertEqual(d.find("datadir").get("zip"), "true")
        self.assertEqual(d.findtext("datadir"), base)

    def test_repository_zip_is_installable_layout(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        site = os.path.join(tmp, "site")
        src = os.path.join(ROOT, "repository.ceto")
        version = kr.load_source(src, "repository.ceto")[0].version
        res = kr.stage(src, "repository.ceto", site)
        expected = "repository.ceto/repository.ceto-%s.zip" % version
        self.assertEqual(res["zip"], expected)
        with open(os.path.join(site, res["zip"]), "rb") as fh:
            self.assertEqual(kr.inspect_zip(fh, "repository.ceto")[:2], ("repository.ceto", version))
        with open(os.path.join(site, "index.html"), encoding="utf-8") as fh:
            self.assertIn(expected, fh.read())

    def test_workflows_are_valid_yaml_and_never_embed_secrets(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML not installed")
        wf = os.path.join(ROOT, ".github", "workflows")
        for name in sorted(os.listdir(wf)):
            with open(os.path.join(wf, name), encoding="utf-8") as fh:
                text = fh.read()
            doc = yaml.safe_load(text)
            self.assertIn("jobs", doc, name)
            self.assertNotRegex(text, r"gh[pousr]_[A-Za-z0-9]{20,}", name)

    def test_no_untrusted_expression_in_run_blocks(self):
        # client_payload / inputs must reach shell only through env:, never inline in run:
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML not installed")
        with open(os.path.join(ROOT, ".github", "workflows", "release.yml"), encoding="utf-8") as fh:
            doc = yaml.safe_load(fh)
        for job in doc["jobs"].values():
            for step in job["steps"]:
                run = step.get("run", "")
                self.assertNotIn("client_payload", run)
                self.assertNotIn("github.event.inputs", run)
                self.assertNotIn("inputs.", run)

    def test_step_output_references_are_written_by_that_step(self):
        # an expression naming the wrong step silently evaluates to an empty string
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML not installed")
        wf = os.path.join(ROOT, ".github", "workflows")
        ref = re.compile(r"steps\.([A-Za-z0-9_-]+)\.outputs\.([A-Za-z0-9_-]+)")

        def strings(node):
            if isinstance(node, dict):
                for value in node.values():
                    yield from strings(value)
            elif isinstance(node, list):
                for value in node:
                    yield from strings(value)
            elif isinstance(node, str):
                yield node

        for name in sorted(os.listdir(wf)):
            with open(os.path.join(wf, name), encoding="utf-8") as fh:
                doc = yaml.safe_load(fh)
            for job_name, job in doc["jobs"].items():
                steps = {s["id"]: s for s in job.get("steps", []) if "id" in s}
                for text in strings(job):
                    for step_id, output in ref.findall(text):
                        label = "%s/%s: steps.%s.outputs.%s" % (name, job_name, step_id, output)
                        self.assertIn(step_id, steps, label)
                        step = steps[step_id]
                        if "run" in step:
                            self.assertRegex(step["run"], r"\b%s=" % re.escape(output), label)


if __name__ == "__main__":
    unittest.main()
