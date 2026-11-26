# muse-auto-installer

**Koi bhi GitHub repo — ek command se install.**

```bash
python3 install.py owner/repo
# ya wrapper se (PATH me daalne ke baad):
muse-install owner/repo
```

Installer repo ko clone karta hai, **pehle safety vetting** karta hai,
project type auto-detect karke sahi install command chalata hai, verify
karta hai, aur registry me darj karta hai. Blind install kabhi nahi.

## Ek-command usage

```bash
# Install (default jagah: ~/workspace/installed/<repo>/)
muse-install preetjanni/preet-pb8-auto-reply

# Poora GitHub URL bhi chalega
muse-install https://github.com/vercel-labs/skills

# Sirf plan dekho — kuch install mat karo
muse-install preetjanni/preet-pb8-auto-reply --dry-run

# Pehle se installed ho to update karo
muse-install preetjanni/preet-pb8-auto-reply --update

# Dobara clone karo (purana hata kar)
muse-install preetjanni/preet-pb8-auto-reply --force

# Kisi aur jagah install karo
muse-install owner/repo --dest ~/workspace/my-tools/repo
```

PATH me daalne ke liye:

```bash
export PATH="$HOME/workspace/muse-auto-installer:$PATH"
```

## Kya-kya karta hai

1. **Clone** — shallow clone (`--depth 1`) pehle ek temp dir me.
   Clone ke time kuch execute nahi hota.
2. **Safety vetting** — install se PEHLE:
   - README + LICENSE padhta hai (na ho to warning).
   - Dangerous patterns scan karta hai: `curl | bash`, `wget | sh`,
     `rm -rf /`, hidden `eval`, base64-decode-pipe.
   - `package.json` ke install hooks (`preinstall` / `postinstall` /
     `prepare`) ho to warning deta hai.
   - **BLOCK mile to install ruk jata hai** — aage sirf `--force` se.
3. **Auto-detect project type** aur sahi install chalata hai:

   | Signal | Type | Install step |
   |---|---|---|
   | `package.json` | node | `npm ci` / `npm install` (`--ignore-scripts` default) |
   | `requirements.txt` / `pyproject.toml` | python | venv + `pip install` |
   | `setup.py` (setuptools) | python | venv + `pip install -e .` |
   | sirf `.py` files | python-scripts | koi deps nahi, sirf verify |
   | `Cargo.toml` | rust | `cargo build` |
   | `go.mod` | go | `go build ./...` |
   | `Gemfile` | ruby | `bundle install` |
   | `composer.json` | php | `composer install` |
   | kuch nahi | generic | sirf files, sirf verify |

   npm ke liye default `--ignore-scripts` hai — install hooks tabhi
   chalte hain jab `--allow-scripts` do.
4. **Verify (smoke test)** — entry point dhoondhta hai
   (`main.py`, `app.py`, `index.js`, `handler.py` …), Python ka
   `py_compile` ya Node ka `node --check` chalata hai. Fail ho to
   saaf error deta hai, chupchaap "ho gaya" nahi kehta.
5. **Registry** — har install `~/workspace/installed/registry.json`
   me darj hota hai: owner/repo, URL, path, type, commit, verified,
   date.

## Options

| Option | Kaam |
|---|---|
| `--dry-run` | Sirf bataye kya karega — clone temp me karke vet + detect karega, kuch install nahi karega |
| `--force` | Dobara clone kare; vetting BLOCK ko ignore kare |
| `--update` | Pehle se installed repo ko naye clone se update kare |
| `--dest PATH` | Install ki jagah badlo |
| `--root PATH` | Install root badlo (default `~/workspace/installed`) |
| `--allow-scripts` | npm install hooks chalne do |
| `--no-verify` | Smoke test skip karo |
| `--verbose` | Zyada detail dikhayo |
| `--version` | Version dikhayo |

Exit codes: `0` ok · `1` install/verify fail · `2` vetting BLOCK ·
`3` galat usage (jaise galat repo naam).

## Safety rules (standing)

- Blind install kabhi nahi — vetting ke bina koi install command nahi.
- Install hooks default band hain (`--ignore-scripts`).
- Koi secret, token ya credential kahin save nahi hota.
- Suspicious repo mile to ruk jata hai aur saaf batata hai kyun ruka.
- Sirf public repos — private repo ke liye alag auth chahiye (abhi supported nahi).

## Examples

```bash
# Chhota Python scripts repo
muse-install preetjanni/preet-pb8-auto-reply

# Node tool
muse-install vercel-labs/skills --dry-run

# Update
muse-install preetjanni/preet-pb8-auto-reply --update
```

## Tests

```bash
python3 -m unittest discover -s tests -v
```

Sirf stdlib use hoti hai — Python 3.8+ chahiye, koi extra package nahi.

## License

MIT — `LICENSE` dekho.
