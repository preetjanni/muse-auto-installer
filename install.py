#!/usr/bin/env python3
"""muse-auto-installer — koi bhi GitHub repo ek command se install karo.

Usage:
    python3 install.py owner/repo
    python3 install.py https://github.com/owner/repo
    python3 install.py owner/repo --dry-run      # sirf bataye, kuch na kare
    python3 install.py owner/repo --update       # pehle se installed ho to update
    python3 install.py owner/repo --force        # dobara clone + vetting blocks ignore

Kaam:
  1. Repo ko shallow clone karta hai (pehle temp dir me — kuch execute nahi hota).
  2. Safety vetting: README + licence padhta hai, suspicious install hooks /
     postinstall scripts / curl|bash jaise patterns ho to rukta hai.
  3. Project type auto-detect karta hai (npm / pip / cargo / go / bundler)
     aur sahi install command chalata hai.
  4. Verify karta hai — entry point maujood hai, code compile hota hai.
  5. Registry me darj karta hai: ~/workspace/installed/registry.json

Blind install kabhi nahi — vetting fail ho to bina --force aage nahi badhta.
Sirf Python 3 stdlib use hoti hai. Koi secret kahin save nahi hota.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

VERSION = "1.0.0"
DEFAULT_ROOT = Path.home() / "workspace" / "installed"
GITHUB_API = "https://api.github.com/repos/{owner}/{repo}"
TIMEOUT_CLONE = 180
TIMEOUT_INSTALL = 600

# ---------------------------------------------------------------------------
# Vetting patterns
# ---------------------------------------------------------------------------
# BLOCK: ye mile to install ruk jata hai (bina --force aage nahi badhta).
BLOCK_PATTERNS = [
    (r"curl\s+.*\|\s*(ba)?sh", "curl | bash — download karke turant execute"),
    (r"wget\s+.*\|\s*(ba)?sh", "wget | sh — download karke turant execute"),
    (r"curl\s+.*\|\s*python", "curl | python — download karke turant execute"),
    (r"rm\s+-rf\s+(/|~|\\$HOME)\b", "rm -rf / ya $HOME — poora system delete"),
    (r"eval\s*\(\s*\$\(", "eval $(...) — hidden command execute"),
    (r"base64\s+(-d|--decode)[^|]*\|\s*(ba)?sh", "base64 decode | sh — chhupa code"),
    (r"chmod\s+\+x.*(curl|wget)", "download karke executable banana"),
]

# WARNING: ye mile to warning dikhata hai, par install rukta nahi.
WARNING_PATTERNS = [
    (r"subprocess\.(run|Popen|call)", "subprocess use — external command chalata hai"),
    (r"os\.system\s*\(", "os.system use — shell command chalata hai"),
    (r"socket\.|urllib\.request\.urlopen|requests\.(get|post)",
     "network call — internet se kuch mangata hai"),
    (r"eval\s*\(", "eval() use — dynamic code execute"),
    (r"exec\s*\(", "exec() use — dynamic code execute"),
]

# package.json me ye scripts install time par khud chal padti hain.
NPM_HOOK_SCRIPTS = ("preinstall", "install", "postinstall", "prepare")


def log(msg, verbose=False, always=False):
    if always or verbose:
        print(msg)


# ---------------------------------------------------------------------------
# Repo spec parsing
# ---------------------------------------------------------------------------
def parse_spec(spec):
    """'owner/repo', URL ya git@ spec se (owner, repo, clone_url) nikalo."""
    spec = spec.strip().rstrip("/")
    m = re.match(
        r"^(?:https?://github\.com/|git@github\.com:)"
        r"(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+?)(?:\.git)?$",
        spec)
    if m:
        owner, repo = m.group("owner"), m.group("repo")
    else:
        m = re.match(r"^(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)$", spec)
        if not m:
            raise ValueError(
                "galat repo spec: '%s' — 'owner/repo' ya GitHub URL do" % spec)
        owner, repo = m.group("owner"), m.group("repo")
    return owner, repo, "https://github.com/%s/%s.git" % (owner, repo)


# ---------------------------------------------------------------------------
# GitHub meta (optional — network na ho to warning dekar aage badho)
# ---------------------------------------------------------------------------
def fetch_meta(owner, repo, verbose=False):
    url = GITHUB_API.format(owner=owner, repo=repo)
    req = urllib.request.Request(url, headers={"User-Agent": "muse-auto-installer"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise RuntimeError("repo nahi mila: %s/%s (404 — naam check karo)" % (owner, repo))
        if e.code in (403, 429):
            # Rate limit (shared IP par common hai) — meta optional hai,
            # isliye warning dekar bina meta ke aage badho.
            log("  [warn] GitHub API rate limit (%s) — bina meta ke aage badh rahe hain"
                % e.code, verbose=verbose)
            return {}
        raise RuntimeError("GitHub API error %s — %s" % (e.code, e.reason))
    except Exception as e:
        log("  [warn] GitHub API nahi pahuncha (%s) — bina meta ke aage badh rahe hain"
            % e, verbose=verbose)
        return {}


# ---------------------------------------------------------------------------
# Clone
# ---------------------------------------------------------------------------
def run_cmd(cmd, cwd=None, timeout=120, verbose=False):
    log("  $ %s" % " ".join(cmd), verbose=verbose)
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                           timeout=timeout)
    except subprocess.TimeoutExpired:
        return -1, "", "command timeout (%ds): %s" % (timeout, " ".join(cmd))
    return r.returncode, r.stdout, r.stderr


def clone_repo(clone_url, dest, verbose=False):
    rc, out, err = run_cmd(
        ["git", "clone", "--depth", "1", "--single-branch", clone_url, str(dest)],
        timeout=TIMEOUT_CLONE, verbose=verbose)
    if rc != 0:
        tail = (err or out or "").strip().splitlines()[-3:]
        raise RuntimeError("git clone fail: %s" % " | ".join(tail))
    rc, out, _ = run_cmd(["git", "-C", str(dest), "rev-parse", "HEAD"],
                         verbose=verbose)
    return out.strip() if rc == 0 else ""


# ---------------------------------------------------------------------------
# Safety vetting — clone ke baad, kuch bhi chalane se PEHLE
# ---------------------------------------------------------------------------
def scan_text(text):
    findings = []
    for pat, what in BLOCK_PATTERNS:
        if re.search(pat, text):
            findings.append(("BLOCK", what))
    for pat, what in WARNING_PATTERNS:
        if re.search(pat, text):
            findings.append(("WARN", what))
    return findings


def vet_tree(tree: Path, verbose=False):
    """Repo ke files scan karo. Returns (findings, info dict)."""
    findings = []  # list of (level, file, detail)
    info = {"readme": False, "license": False, "readme_install_hint": ""}

    for p in tree.rglob("*"):
        if p.is_dir():
            if p.name in (".git", "__pycache__", "node_modules", ".venv"):
                continue
            continue
        rel = str(p.relative_to(tree))
        if rel.startswith(".git/"):
            continue
        name = p.name.lower()
        if name in ("readme.md", "readme.txt", "readme"):
            info["readme"] = True
        if name in ("license", "license.md", "license.txt", "licence",
                    "licence.md", "copying"):
            info["license"] = True
        # .git/hooks me kuch custom hook to nahi (sample files chhodkar)
        if rel.startswith(".git/hooks/") and not p.name.endswith(".sample"):
            findings.append(("WARN", rel, "custom git hook maujood hai"))
        if p.suffix in (".py", ".js", ".ts", ".sh", ".rb", ".pl") \
                or name in ("package.json", "setup.py", "setup.cfg",
                            "pyproject.toml", "cargo.toml", "gemfile",
                            "install.sh", "setup.sh"):
            try:
                text = p.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for level, detail in scan_text(text):
                findings.append((level, rel, detail))

    # package.json ke install hooks alag se
    pkg = tree / "package.json"
    if pkg.is_file():
        try:
            data = json.loads(pkg.read_text(encoding="utf-8"))
            hooks = [s for s in NPM_HOOK_SCRIPTS
                     if isinstance(data.get("scripts"), dict) and s in data["scripts"]]
            for h in hooks:
                findings.append(("WARN", "package.json",
                                 "npm '%s' script install par khud chalegi" % h))
        except (json.JSONDecodeError, OSError):
            findings.append(("WARN", "package.json", "package.json parse nahi hua"))

    # README se install hint nikalo (sirf dikhane ke liye)
    for cand in ("README.md", "README.txt", "README"):
        rp = tree / cand
        if rp.is_file():
            text = rp.read_text(encoding="utf-8", errors="ignore")
            m = re.search(r"(?im)^#{1,3}\s*(install\w*|setup|getting started)\s*$",
                          text)
            if m:
                snippet = text[m.start():m.start() + 400].strip().splitlines()[:8]
                info["readme_install_hint"] = "\n".join(snippet)
            break

    if not info["readme"]:
        findings.append(("WARN", "-", "README nahi mila"))
    if not info["license"]:
        findings.append(("WARN", "-", "LICENSE file nahi mila"))
    return findings, info


def vet_report(findings, verbose=False):
    blocks = [f for f in findings if f[0] == "BLOCK"]
    warns = [f for f in findings if f[0] == "WARN"]
    for level, f, detail in findings:
        print("  [%s] %s — %s" % (level, f, detail))
    print("  Vetting: %d BLOCK, %d WARN" % (len(blocks), len(warns)))
    return blocks, warns


# ---------------------------------------------------------------------------
# Project type detection
# ---------------------------------------------------------------------------
def detect_type(tree: Path):
    if (tree / "package.json").is_file():
        return "node"
    if (tree / "Cargo.toml").is_file():
        return "rust"
    if (tree / "go.mod").is_file():
        return "go"
    if (tree / "Gemfile").is_file():
        return "ruby"
    if (tree / "composer.json").is_file():
        return "php"
    if (tree / "requirements.txt").is_file() or (tree / "pyproject.toml").is_file():
        return "python"
    setup_py = tree / "setup.py"
    if setup_py.is_file():
        text = setup_py.read_text(encoding="utf-8", errors="ignore")
        if "setuptools" in text or re.search(r"(?m)^setup\s*\(", text):
            return "python"
        return "python-scripts"
    if list(tree.glob("*.py")):
        return "python-scripts"
    if list(tree.glob("*.js")) or list(tree.glob("*.ts")):
        return "node-scripts"
    return "generic"


# ---------------------------------------------------------------------------
# Install steps
# ---------------------------------------------------------------------------
def install_deps(tree: Path, ptype, allow_scripts, verbose=False):
    """Deps install karo. Returns (ok, notes list)."""
    notes = []
    if ptype == "node":
        if not shutil.which("npm"):
            return False, ["npm nahi mila — Node.js install karo, deps skip"]
        cmd = ["npm", "ci"] if (tree / "package-lock.json").is_file() \
            else ["npm", "install"]
        if not allow_scripts:
            cmd.append("--ignore-scripts")
            notes.append("npm install --ignore-scripts (hooks band — safety)")
        rc, out, err = run_cmd(cmd, cwd=str(tree), timeout=TIMEOUT_INSTALL,
                               verbose=verbose)
        if rc != 0:
            return False, ["npm install fail: %s" % (err or out).strip().splitlines()[-1:]]
        notes.append("npm deps install ho gayi")
        return True, notes

    if ptype == "python":
        venv = tree / ".venv"
        rc, _, err = run_cmd([sys.executable, "-m", "venv", str(venv)],
                             cwd=str(tree), timeout=120, verbose=verbose)
        if rc != 0:
            return False, ["venv banana fail: %s" % err.strip().splitlines()[-1:]]
        pip = str(venv / "bin" / "pip")
        req = tree / "requirements.txt"
        if req.is_file():
            rc, _, err = run_cmd([pip, "install", "-r", str(req)],
                                 cwd=str(tree), timeout=TIMEOUT_INSTALL,
                                 verbose=verbose)
            if rc != 0:
                return False, ["pip install -r requirements.txt fail: %s"
                               % err.strip().splitlines()[-1:]]
            notes.append("requirements.txt install ho gayi")
        else:
            rc, _, err = run_cmd([pip, "install", "-e", "."],
                                 cwd=str(tree), timeout=TIMEOUT_INSTALL,
                                 verbose=verbose)
            if rc != 0:
                return False, ["pip install -e . fail: %s"
                               % err.strip().splitlines()[-1:]]
            notes.append("package editable install ho gaya")
        return True, notes

    if ptype == "rust":
        if not shutil.which("cargo"):
            return False, ["cargo nahi mila — Rust install karo, build skip"]
        rc, _, err = run_cmd(["cargo", "build"], cwd=str(tree),
                             timeout=TIMEOUT_INSTALL, verbose=verbose)
        return (rc == 0, ["cargo build %s" % ("ok" if rc == 0 else "fail: " + err.strip().splitlines()[-1:])])

    if ptype == "go":
        if not shutil.which("go"):
            return False, ["go nahi mila — Go install karo, build skip"]
        rc, _, err = run_cmd(["go", "build", "./..."], cwd=str(tree),
                             timeout=TIMEOUT_INSTALL, verbose=verbose)
        return (rc == 0, ["go build %s" % ("ok" if rc == 0 else "fail: " + err.strip().splitlines()[-1:])])

    if ptype == "ruby":
        if not shutil.which("bundle"):
            return False, ["bundler nahi mila — bundle install skip"]
        rc, _, err = run_cmd(["bundle", "install"], cwd=str(tree),
                             timeout=TIMEOUT_INSTALL, verbose=verbose)
        return (rc == 0, ["bundle install %s" % ("ok" if rc == 0 else "fail")])

    if ptype == "php":
        if not shutil.which("composer"):
            return False, ["composer nahi mila — install skip"]
        rc, _, err = run_cmd(["composer", "install"], cwd=str(tree),
                             timeout=TIMEOUT_INSTALL, verbose=verbose)
        return (rc == 0, ["composer install %s" % ("ok" if rc == 0 else "fail")])

    notes.append("koi deps nahi — sirf files (scripts/docs repo)")
    return True, notes


# ---------------------------------------------------------------------------
# Verification (smoke test — code execute nahi karta, sirf check karta hai)
# ---------------------------------------------------------------------------
ENTRY_FILES = ("main.py", "app.py", "cli.py", "__main__.py", "index.js",
               "index.ts", "src/index.js", "src/index.ts", "src/main.py",
               "handler.py", "install.py", "setup.py")


def verify_tree(tree: Path, ptype, verbose=False):
    checks = []
    ok = True
    entry = None
    for name in ENTRY_FILES:
        if (tree / name).is_file():
            entry = tree / name
            break
    if entry is None:
        for p in sorted(tree.rglob("*.py"))[:1]:
            if ".venv" not in str(p) and "__pycache__" not in str(p):
                entry = p
    if entry is None:
        for p in sorted(tree.rglob("*.js"))[:1]:
            if "node_modules" not in str(p):
                entry = p
    if entry is None:
        checks.append("koi entry file nahi mili")
        return False, checks, None

    checks.append("entry point: %s" % entry.relative_to(tree))
    if entry.suffix == ".py":
        rc, _, err = run_cmd([sys.executable, "-m", "py_compile", str(entry)],
                             verbose=verbose)
        if rc == 0:
            checks.append("py_compile ok — %s" % entry.name)
        else:
            ok = False
            checks.append("py_compile FAIL — %s: %s" % (entry.name, err.strip().splitlines()[-1:]))
    elif entry.suffix in (".js", ".ts") and shutil.which("node"):
        rc, _, err = run_cmd(["node", "--check", str(entry)], verbose=verbose)
        checks.append("node --check %s" % ("ok" if rc == 0 else "FAIL"))
        ok = ok and rc == 0
    return ok, checks, str(entry.relative_to(tree))


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
def registry_path(root: Path):
    return root / "registry.json"


def load_registry(root: Path):
    p = registry_path(root)
    if p.is_file():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("installed"), list):
                return data
        except json.JSONDecodeError:
            pass
    return {"installed": []}


def save_registry(root: Path, data):
    root.mkdir(parents=True, exist_ok=True)
    tmp = registry_path(root).with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    tmp.replace(registry_path(root))


def registry_upsert(root: Path, record):
    data = load_registry(root)
    data["installed"] = [r for r in data["installed"]
                         if r.get("repo") != record["repo"]
                         or r.get("owner") != record["owner"]]
    data["installed"].append(record)
    save_registry(root, data)


# ---------------------------------------------------------------------------
# Main install flow
# ---------------------------------------------------------------------------
def do_install(owner, repo, clone_url, args):
    root = Path(args.root).expanduser()
    dest = Path(args.dest).expanduser() if args.dest \
        else root / repo
    print("== muse-auto-installer v%s ==" % VERSION)
    print("Repo: %s/%s" % (owner, repo))
    print("Jagah: %s" % dest)
    if args.dry_run:
        print("Mode: DRY-RUN — kuch install nahi hoga, sirf plan batayenge")

    # 1. Meta (optional)
    meta = {}
    try:
        meta = fetch_meta(owner, repo, verbose=args.verbose)
    except RuntimeError as e:
        print("ERROR: %s" % e)
        return 3
    if meta:
        print("Description: %s" % (meta.get("description") or "-"))
        print("Default branch: %s | Licence: %s"
              % (meta.get("default_branch"),
                 (meta.get("license") or {}).get("spdx_id") or "-"))

    # 2. Pehle se installed?
    if dest.exists() and not args.update and not args.force and not args.dry_run:
        print("ERROR: %s pehle se maujood hai — --update ya --force do" % dest)
        return 1
    if dest.exists() and args.update and not args.dry_run:
        print("Update mode: purana install naya clone se badla jayega")

    # 3. Temp clone + vetting (kuch execute nahi hota)
    tmp = Path(tempfile.mkdtemp(prefix="muse-install-"))
    try:
        print("== Clone ho raha hai (temp) ==")
        commit = clone_repo(clone_url, tmp, verbose=args.verbose)
        print("  Clone ok, commit: %s" % (commit[:12] if commit else "?"))

        print("== Safety vetting ==")
        findings, info = vet_tree(tmp, verbose=args.verbose)
        blocks, warns = vet_report(findings, verbose=args.verbose)
        print("  README: %s | LICENSE: %s"
              % ("hai" if info["readme"] else "nahi",
                 "hai" if info["license"] else "nahi"))
        if info["readme_install_hint"]:
            print("  README install hint:\n%s" % info["readme_install_hint"])
        if blocks and not args.force:
            print("BLOCKED: %d dangerous pattern mile — install ruka. "
                  "Dekh lo, phir --force se chalao." % len(blocks))
            return 2
        if blocks and args.force:
            print("  --force: BLOCK patterns ignore karke aage badh rahe hain")

        # 4. Type detect
        ptype = detect_type(tmp)
        print("== Project type: %s ==" % ptype)

        if args.dry_run:
            print("== DRY-RUN plan ==")
            print("  1. Clone -> %s" % dest)
            print("  2. Install step: %s" % install_plan_text(ptype, args.allow_scripts))
            print("  3. Verify: entry point + compile check")
            print("  4. Registry: %s" % registry_path(root))
            print("DRY-RUN khatam — kuch badla nahi.")
            return 0

        # 5. Temp -> final jagah
        if dest.exists():
            shutil.rmtree(dest)
        root.mkdir(parents=True, exist_ok=True)
        shutil.move(str(tmp), str(dest))
        tmp = None  # ab dest ki zimmedari

        # 6. Deps install
        print("== Install ho raha hai ==")
        ok, notes = install_deps(dest, ptype, args.allow_scripts,
                                 verbose=args.verbose)
        for n in notes:
            print("  - %s" % n)
        if not ok:
            print("ERROR: install step fail — repo files maujood hain, deps nahi lagi")
            registry_upsert(root, make_record(owner, repo, clone_url, dest,
                                              ptype, commit, False, notes))
            return 1

        # 7. Verify
        verified = True
        checks = []
        entry = None
        if not args.no_verify:
            print("== Verify (smoke test) ==")
            verified, checks, entry = verify_tree(dest, ptype, verbose=args.verbose)
            for c in checks:
                print("  - %s" % c)
        else:
            checks.append("verify skip (--no-verify)")

        # 8. Registry
        registry_upsert(root, make_record(owner, repo, clone_url, dest, ptype,
                                          commit, verified, notes + checks))
        print()
        if verified:
            print("INSTALL OK: %s/%s -> %s (type: %s, verified)"
                  % (owner, repo, dest, ptype))
        else:
            print("INSTALL ADHURA: files aa gayi, par verify FAIL — upar dekho")
            return 1
        return 0
    finally:
        if tmp is not None and tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)


def install_plan_text(ptype, allow_scripts):
    plans = {
        "node": "npm %s (ignore-scripts: %s)"
                % ("ci/install", "nahi" if allow_scripts else "haan"),
        "python": "venv banao + pip install (-r requirements.txt ya -e .)",
        "python-scripts": "koi deps nahi — sirf py_compile check",
        "node-scripts": "koi deps nahi — sirf node --check",
        "rust": "cargo build",
        "go": "go build ./...",
        "ruby": "bundle install",
        "php": "composer install",
        "generic": "koi deps nahi — sirf files",
    }
    return plans.get(ptype, "koi deps nahi")


def make_record(owner, repo, clone_url, dest, ptype, commit, verified, notes):
    return {
        "owner": owner,
        "repo": repo,
        "url": clone_url,
        "path": str(dest),
        "type": ptype,
        "commit": commit,
        "verified": verified,
        "installed_at": datetime.now(timezone.utc).isoformat(),
        "notes": notes,
    }


def main(argv=None):
    p = argparse.ArgumentParser(
        description="muse-auto-installer — koi bhi GitHub repo ek command se install karo")
    p.add_argument("repo", help="owner/repo ya GitHub URL")
    p.add_argument("--dest", help="install ki jagah (default: ~/workspace/installed/<repo>)")
    p.add_argument("--root", default=str(DEFAULT_ROOT),
                   help="install root (default: ~/workspace/installed)")
    p.add_argument("--dry-run", action="store_true",
                   help="sirf bataye kya karega, kuch install na kare")
    p.add_argument("--force", action="store_true",
                   help="pehle se installed ho to dobara clone; vetting BLOCK ignore kare")
    p.add_argument("--update", action="store_true",
                   help="pehle se installed repo ko naye clone se update kare")
    p.add_argument("--allow-scripts", action="store_true",
                   help="npm install hooks chalne de (default: --ignore-scripts)")
    p.add_argument("--no-verify", action="store_true",
                   help="smoke test skip kare")
    p.add_argument("--verbose", action="store_true", help="zyada detail dikhaye")
    p.add_argument("--version", action="version", version="muse-auto-installer " + VERSION)
    args = p.parse_args(argv)

    try:
        owner, repo, clone_url = parse_spec(args.repo)
    except ValueError as e:
        print("ERROR: %s" % e)
        return 3
    return do_install(owner, repo, clone_url, args)


if __name__ == "__main__":
    sys.exit(main())
