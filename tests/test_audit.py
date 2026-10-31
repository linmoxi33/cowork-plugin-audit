#!/usr/bin/env python3
"""Tests for cowork-plugin-audit. Stdlib only: python3 -m unittest discover -s tests -v"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
spec = importlib.util.spec_from_file_location("cpa", os.path.join(ROOT, "cowork_plugin_audit.py"))
cpa = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cpa)


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cpa-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def make(self, plugins, marketplace=None, extra=None):
        mkt = marketplace if marketplace is not None else {"name": "m", "plugins": plugins}
        write(os.path.join(self.tmp, ".claude-plugin", "marketplace.json"), json.dumps(mkt))
        for p in plugins:
            src = p.get("source")
            if not isinstance(src, str):
                continue
            d = os.path.join(self.tmp, src.lstrip("./"))
            write(os.path.join(d, ".claude-plugin", "plugin.json"), json.dumps(p.get("plugin_json", {})))
            for rel, body in (p.get("files") or {}).items():
                write(os.path.join(d, rel), body)
        for rel, body in (extra or {}).items():
            write(os.path.join(self.tmp, rel), body)
        return self.tmp

    def rules(self, root=None):
        rep, err = cpa.audit(root or self.tmp)
        self.assertIsNone(err)
        return [f["rule"] for f in rep["findings"]], rep


class TestUnit(Base):
    def test_frontmatter_parsed(self):
        fm, _ = cpa.parse_frontmatter("---\nname: x\ndescription: hi\n---\nbody\n")
        self.assertEqual(fm, {"name": "x", "description": "hi"})

    def test_frontmatter_absent(self):
        self.assertEqual(cpa.parse_frontmatter("# no fm\n")[0], {})

    def test_frontmatter_unterminated(self):
        self.assertEqual(cpa.parse_frontmatter("---\nname: x\n")[0], {})

    def test_load_json_invalid(self):
        p = os.path.join(self.tmp, "bad.json")
        write(p, "{not json")
        data, err = cpa.load_json(p)
        self.assertIsNone(data)
        self.assertTrue(err)

    def test_is_installed_boundaries(self):
        plug = "/r/alpha"
        self.assertTrue(cpa.is_installed(plug, "/r/alpha/skills/a/SKILL.md"))
        self.assertTrue(cpa.is_installed(plug, "/r/alpha/.mcp.json"))
        self.assertFalse(cpa.is_installed(plug, "/r/alpha/CONNECTORS.md"))
        self.assertFalse(cpa.is_installed(plug, "/r/README.md"))

    def test_summary_counts_severities(self):
        rep = {"findings": [{"severity": "error", "rule": "X"}, {"severity": "info", "rule": "Y"}]}
        s = cpa._summary(rep["findings"], 1, 0, 0)
        self.assertEqual(s["by_severity"]["error"], 1)
        self.assertEqual(s["by_severity"]["info"], 1)
        self.assertEqual(s["by_rule"], {"X": 1, "Y": 1})

    def test_render_text_ok(self):
        txt = cpa.render_text({"root": "r", "summary": cpa._summary([], 0, 0, 0), "findings": []})
        self.assertIn("OK: no findings", txt)


class TestBehaviour(Base):
    def test_clean_marketplace_passes(self):
        self.make([{"name": "alpha", "source": "./alpha",
                    "plugin_json": {"name": "alpha", "version": "1.0.0"},
                    "files": {"skills/a/SKILL.md": "---\nname: a\ndescription: d\n---\n# a\n"}}])
        self.assertEqual(cpa.main([self.tmp]), 0)

    def test_install_boundary_link_is_error(self):
        self.make([{"name": "alpha", "source": "./alpha",
                    "plugin_json": {"name": "alpha", "version": "1.0.0"},
                    "files": {"CONNECTORS.md": "# c\n",
                              "skills/a/SKILL.md": "---\nname: a\ndescription: d\n---\n"
                                                   "[c](../../CONNECTORS.md)\n"}}])
        rules, rep = self.rules()
        self.assertIn("L001", rules)
        self.assertEqual(rep["summary"]["by_severity"]["error"], 1)
        self.assertEqual(cpa.main([self.tmp]), 1)

    def test_dangling_link_is_error(self):
        self.make([{"name": "alpha", "source": "./alpha",
                    "plugin_json": {"name": "alpha", "version": "1.0.0"},
                    "files": {"skills/a/SKILL.md": "---\nname: a\ndescription: d\n---\n"
                                                   "[x](./gone.md)\n"}}])
        self.assertIn("L002", self.rules()[0])

    def test_duplicate_skill_name_across_plugins(self):
        files = {"skills/dup/SKILL.md": "---\nname: dup\ndescription: d\n---\n# d\n"}
        self.make([{"name": "alpha", "source": "./alpha",
                    "plugin_json": {"name": "alpha", "version": "1"}, "files": files},
                   {"name": "beta", "source": "./beta",
                    "plugin_json": {"name": "beta", "version": "1"}, "files": files}])
        self.assertIn("S005", self.rules()[0])

    def test_marketplace_name_drift(self):
        self.make([{"name": "alpha", "source": "./alpha",
                    "plugin_json": {"name": "alpha-renamed", "version": "1"},
                    "files": {"skills/a/SKILL.md": "---\nname: a\ndescription: d\n---\n# a\n"}}])
        self.assertIn("M005", self.rules()[0])

    def test_orphan_plugin_dir_flagged(self):
        self.make([{"name": "alpha", "source": "./alpha",
                    "plugin_json": {"name": "alpha", "version": "1"},
                    "files": {"skills/a/SKILL.md": "---\nname: a\ndescription: d\n---\n# a\n"}}],
                  extra={"gamma/.claude-plugin/plugin.json": json.dumps({"name": "gamma", "version": "1"})})
        self.assertIn("M007", self.rules()[0])

    def test_missing_source_path_flagged(self):
        write(os.path.join(self.tmp, ".claude-plugin", "marketplace.json"),
              json.dumps({"name": "m", "plugins": [{"name": "alpha", "source": "./nope"}]}))
        self.assertIn("M002", self.rules()[0])

    def test_missing_version_warns(self):
        self.make([{"name": "alpha", "source": "./alpha", "plugin_json": {"name": "alpha"},
                    "files": {"skills/a/SKILL.md": "---\nname: a\ndescription: d\n---\n# a\n"}}])
        self.assertIn("M008", self.rules()[0])

    def test_unpinned_remote_source_warns(self):
        self.make([{"name": "remote", "source": {"source": "git-subdir", "url": "https://x/y.git", "path": "s"}}])
        self.assertIn("M010", self.rules()[0])

    def test_placeholder_is_info_only(self):
        self.make([{"name": "alpha", "source": "./alpha",
                    "plugin_json": {"name": "alpha", "version": "1"},
                    "files": {"skills/a/SKILL.md": "---\nname: a\ndescription: d\n---\n~~some tool\n"}}])
        rules, rep = self.rules()
        self.assertIn("P001", rules)
        self.assertEqual(rep["summary"]["by_severity"]["warning"], 0)
        self.assertEqual(cpa.main([self.tmp]), 0)

    def test_json_output_is_parseable(self):
        self.make([{"name": "alpha", "source": "./alpha", "plugin_json": {"name": "alpha", "version": "1"},
                    "files": {"skills/a/SKILL.md": "---\nname: a\ndescription: d\n---\n# a\n"}}])
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cpa.main([self.tmp, "--json"])
        payload = json.loads(buf.getvalue())
        self.assertEqual(rc, 0)
        self.assertIn("findings", payload)
        self.assertIn("by_rule", payload["summary"])

    def test_usage_error_exit2_without_marketplace(self):
        self.assertEqual(cpa.main([self.tmp]), 2)

    def test_usage_error_exit2_when_not_a_directory(self):
        self.assertEqual(cpa.main([os.path.join(self.tmp, "missing")]), 2)

    def test_remote_source_is_not_audited_locally(self):
        self.make([{"name": "remote", "source": {"source": "git-subdir", "url": "u", "sha": "abc"}}])
        rules, rep = self.rules()
        self.assertEqual(rep["summary"]["plugins_audited"], 0)
        self.assertNotIn("L001", rules)


if __name__ == "__main__":
    unittest.main()
