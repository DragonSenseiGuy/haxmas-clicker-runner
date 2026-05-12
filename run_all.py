#!/usr/bin/env python3.13
"""
Cookie-Clicker bot competition runner.

Reads projects.tsv, clones each repo, installs its requirements into a
per-project venv, runs the bot for RUN_SECONDS under Xvfb, captures stdout,
parses the highest cookie count it printed, and ranks them.

Single-threaded. One bot at a time.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Optional

ROOT          = Path(__file__).resolve().parent
PROJECTS_TSV  = ROOT / "projects.tsv"
WORKSPACE     = ROOT / "workspace"
LOGS          = ROOT / "logs"
RESULTS_JSON  = ROOT / "results.json"
LEADERBOARD   = ROOT / "leaderboard.txt"

RUN_SECONDS   = 60
CLONE_TIMEOUT = 120
PIP_TIMEOUT   = 300
KILL_GRACE    = 8

# Python that should run each bot. Must be 3.13 per the rules.
PY = shutil.which("python3.13") or sys.executable

# Common files we treat as the entry point, in priority order.
ENTRY_CANDIDATES = [
    "main.py", "index.py", "bot.py", "run.py", "app.py",
    "cookie_clicker.py", "cookieclicker.py", "clicker.py",
    "cookie.py", "cookies.py", "auto_cookie.py", "autocookie.py",
    "cookie_bot.py", "cookiebot.py", "selenium_bot.py",
]

# Patterns we use to pull cookie counts out of the bot's stdout.
COOKIE_PATTERNS = [
    re.compile(r"cookies?\s*[:=]\s*([\d,\.]+)", re.I),
    re.compile(r"([\d,\.]+)\s*cookies?\b", re.I),
    re.compile(r"total[^:=\d]*[:=]\s*([\d,\.]+)", re.I),
    re.compile(r"score\s*[:=]\s*([\d,\.]+)", re.I),
]


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

@dataclass
class Result:
    idx: str
    repo: str
    entry: Optional[str] = None
    cookies: Optional[int] = None
    cps: Optional[float] = None
    duration: Optional[float] = None
    error: Optional[str] = None
    log_file: Optional[str] = None
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Parsing projects.tsv
# ---------------------------------------------------------------------------

def parse_projects() -> list[tuple[str, str]]:
    """projects.tsv is one project per line. Each line is whitespace/tab
    separated URLs; we use the first one as the repo. Index is the 1-based
    line number."""
    rows: list[tuple[str, str]] = []
    for n, line in enumerate(PROJECTS_TSV.read_text().splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        first = line.split()[0]
        rows.append((str(n), first))
    return rows


def normalize_repo_url(url: str) -> Optional[str]:
    if not url:
        return None
    if "github.com" not in url:
        return None
    # Repair URLs that lost their scheme during parsing or in the TSV.
    if url.startswith("//"):
        url = "https:" + url
    elif url.startswith("github.com"):
        url = "https://" + url
    # Strip /blob/... and /tree/... refs to get the repo root.
    for marker in ("/blob/", "/tree/", "/raw/", "/releases", "#"):
        if marker in url:
            url = url.split(marker)[0]
    url = url.rstrip("/").rstrip("#")
    if not url.endswith(".git"):
        url = url + ".git"
    return url


# ---------------------------------------------------------------------------
# Per-project helpers
# ---------------------------------------------------------------------------

def make_venv(project_dir: Path) -> Path:
    venv = project_dir / ".venv"
    subprocess.run([PY, "-m", "venv", str(venv)], check=True, capture_output=True)
    pip = venv / "bin" / "pip"
    subprocess.run(
        [str(pip), "install", "--quiet", "--upgrade", "pip", "wheel"],
        check=False, capture_output=True, timeout=PIP_TIMEOUT,
    )
    # Always make sure selenium is available (most bots use it).
    subprocess.run(
        [str(pip), "install", "--quiet", "selenium"],
        check=False, capture_output=True, timeout=PIP_TIMEOUT,
    )
    return venv


def install_requirements(project_dir: Path, venv: Path, notes: list[str]) -> None:
    pip = venv / "bin" / "pip"
    for req in project_dir.rglob("requirements*.txt"):
        if ".venv" in req.parts:
            continue
        notes.append(f"pip install -r {req.relative_to(project_dir)}")
        subprocess.run(
            [str(pip), "install", "--quiet", "-r", str(req)],
            capture_output=True, timeout=PIP_TIMEOUT,
        )

    pyproject = project_dir / "pyproject.toml"
    if pyproject.exists():
        notes.append("pip install . (pyproject.toml)")
        subprocess.run(
            [str(pip), "install", "--quiet", str(project_dir)],
            capture_output=True, timeout=PIP_TIMEOUT,
        )


def find_entry(project_dir: Path) -> Optional[Path]:
    # Prefer files in the repo root, then by name.
    py_files = [p for p in project_dir.rglob("*.py") if ".venv" not in p.parts]
    if not py_files:
        return None

    # 1) named candidates closest to the root.
    by_depth = sorted(py_files, key=lambda p: len(p.relative_to(project_dir).parts))
    for name in ENTRY_CANDIDATES:
        for p in by_depth:
            if p.name.lower() == name:
                return p

    # 2) any python file containing "selenium", closest to root.
    for p in by_depth:
        try:
            txt = p.read_text(errors="ignore").lower()
        except Exception:
            continue
        if "selenium" in txt or "webdriver" in txt:
            return p

    # 3) just the shallowest .py file.
    return by_depth[0]


# ---------------------------------------------------------------------------
# Running a bot
# ---------------------------------------------------------------------------

def run_bot(idx: str, repo_url_raw: str) -> Result:
    LOGS.mkdir(exist_ok=True)
    res = Result(idx=idx, repo=repo_url_raw)

    repo = normalize_repo_url(repo_url_raw)
    if not repo:
        res.error = f"unsupported repo url: {repo_url_raw!r}"
        return res
    res.repo = repo

    project_dir = WORKSPACE / f"proj-{idx}"
    if project_dir.exists():
        shutil.rmtree(project_dir)

    print(f"\n=== [{idx}] {repo} ===")
    print(f"[{idx}] cloning…")
    r = subprocess.run(
        ["git", "clone", "--depth=1", "--recurse-submodules", repo, str(project_dir)],
        capture_output=True, text=True, timeout=CLONE_TIMEOUT,
    )
    if r.returncode != 0:
        res.error = "clone failed: " + r.stderr.strip().splitlines()[-1:][0] if r.stderr else "clone failed"
        return res

    print(f"[{idx}] creating venv + installing deps…")
    try:
        venv = make_venv(project_dir)
        install_requirements(project_dir, venv, res.notes)
    except subprocess.TimeoutExpired:
        res.error = "dependency install timed out"
        return res
    except Exception as e:
        res.error = f"venv/deps failed: {e}"
        return res

    entry = find_entry(project_dir)
    if not entry:
        res.error = "no python entry point found"
        return res
    res.entry = str(entry.relative_to(project_dir))
    print(f"[{idx}] running {res.entry} for {RUN_SECONDS}s…")

    log_path = LOGS / f"proj-{idx}.log"
    res.log_file = str(log_path.relative_to(ROOT))

    env = os.environ.copy()
    # Headless display via xvfb-run if available; selenium bots usually need a display.
    env.setdefault("PYTHONUNBUFFERED", "1")
    env.setdefault("HOME", str(project_dir))

    py = str(venv / "bin" / "python")
    cmd: list[str]
    if shutil.which("xvfb-run"):
        cmd = ["xvfb-run", "-a", "--server-args=-screen 0 1280x1024x24", py, "-u", entry.name]
    else:
        cmd = [py, "-u", entry.name]

    start = time.monotonic()
    with log_path.open("wb") as logf:
        # Use a new process group so we can kill the bot + any child (browser) it spawned.
        proc = subprocess.Popen(
            cmd,
            cwd=str(entry.parent),
            stdout=logf,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=env,
            preexec_fn=os.setsid,
        )

        try:
            while time.monotonic() - start < RUN_SECONDS:
                if proc.poll() is not None:
                    break
                time.sleep(0.25)
        finally:
            duration = time.monotonic() - start
            res.duration = round(duration, 2)
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=KILL_GRACE)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait(timeout=5)
            # Kill any leftover chrome/chromedriver this bot spawned.
            subprocess.run(["pkill", "-9", "-f", "chromedriver"], capture_output=True)
            subprocess.run(["pkill", "-9", "-f", "chrome"], capture_output=True)

    output = log_path.read_text(errors="ignore")
    cookies = parse_cookies(output)
    res.cookies = cookies
    if cookies is not None and res.duration:
        res.cps = round(cookies / min(res.duration, RUN_SECONDS), 2)
    print(f"[{idx}] -> cookies={cookies}  cps={res.cps}")
    return res


def parse_cookies(output: str) -> Optional[int]:
    """Return the largest cookie count we can find in the bot's stdout."""
    best: Optional[int] = None
    for line in output.splitlines():
        for pat in COOKIE_PATTERNS:
            for m in pat.finditer(line):
                raw = m.group(1).replace(",", "").replace("_", "")
                # Treat trailing ".0" as int; reject obvious floats with non-zero fraction.
                try:
                    val = int(float(raw))
                except ValueError:
                    continue
                if val < 0 or val > 10**12:
                    continue
                if best is None or val > best:
                    best = val
    return best


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def write_leaderboard(results: list[Result]) -> None:
    ranked = sorted(
        [r for r in results if r.cookies is not None],
        key=lambda r: r.cookies or 0,
        reverse=True,
    )
    lines = ["rank  idx  cookies        cps        repo"]
    for i, r in enumerate(ranked, 1):
        lines.append(f"{i:>4}  {r.idx:>3}  {r.cookies:>13,}  {r.cps or 0:>9}  {r.repo}")
    failed = [r for r in results if r.cookies is None]
    if failed:
        lines.append("")
        lines.append("DNF:")
        for r in failed:
            lines.append(f"  [{r.idx}] {r.repo}  -- {r.error or 'no cookie count parsed'}")
    LEADERBOARD.write_text("\n".join(lines) + "\n")
    print("\n" + "\n".join(lines))


def main() -> int:
    if not PROJECTS_TSV.exists():
        print(f"missing {PROJECTS_TSV}", file=sys.stderr)
        return 1
    WORKSPACE.mkdir(exist_ok=True)
    LOGS.mkdir(exist_ok=True)

    projects = parse_projects()
    print(f"loaded {len(projects)} projects, python={PY}")

    results: list[Result] = []
    for idx, repo in projects:
        try:
            res = run_bot(idx, repo)
        except Exception as e:
            res = Result(idx=idx, repo=repo, error=f"runner crashed: {e}")
        results.append(res)
        # Persist after every bot so we don't lose progress.
        RESULTS_JSON.write_text(
            json.dumps([asdict(r) for r in results], indent=2)
        )
        write_leaderboard(results)

    return 0


if __name__ == "__main__":
    sys.exit(main())
