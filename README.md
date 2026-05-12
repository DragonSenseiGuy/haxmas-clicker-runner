# haxmas-clicker-runner

Runner that grades Selenium cookie-clicker bots from a list of GitHub repos.

For each project in `projects.tsv` (one repo URL per line, not committed):

1. Shallow-clones the repo
2. Builds a per-project venv with Python 3.13
3. Installs `requirements*.txt` / `pyproject.toml`
4. Picks an entry point (`main.py`, `index.py`, …, else the first `selenium`-importing file)
5. Runs it under `xvfb-run` for 60 seconds, single-threaded
6. Kills the process group + any leftover Chrome
7. Parses the highest cookie count from stdout
8. Writes `results.json` and `leaderboard.txt` after every bot

## Setup (Ubuntu VM)

```bash
bash setup.sh
```

Installs Python 3.13 (deadsnakes), Chrome, Xvfb, and a master venv.

## Run

```bash
.venv/bin/python run_all.py
```

Per-bot logs land in `logs/proj-N.log`.

## projects.tsv

Not committed. One project per line; the first whitespace-separated token on
each line is treated as the repo URL.
