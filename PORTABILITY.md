# Moving SkyN3t to a new machine

This is a fully self-contained Python project with a few external-CLI dependencies.
Three paths to portability, in order of recommended:

## Path A — Git push/pull (recommended)

If your code lives in git (which it does):

```bash
# Old machine: push everything
cd /path/to/jarvis
git remote add origin <your-remote-url>
git push -u origin main
git push --all origin   # includes the skyn3t/auto/* branches

# New machine
git clone <your-remote-url> jarvis && cd jarvis
./scripts/setup-new-machine.sh
```

## Path B — Snapshot (if no git remote)

```bash
# Old machine
./scripts/snapshot.sh
# produces skyn3t-snapshot-YYYY-MM-DD-HHMM.tar.gz in /tmp/

# Move the tarball to the new machine, then:
mkdir -p ~/jarvis && cd ~/jarvis
./restore-snapshot.sh /path/to/skyn3t-snapshot-*.tar.gz
./scripts/setup-new-machine.sh
```

## Path C — Manual

If you can't use git or a tarball, copy the project tree by hand and skip the
generated/regeneratable directories:

1. Copy the entire `jarvis/` directory **except**:
   - `__pycache__/` (any depth)
   - `*.pyc`, `*.log`
   - `.venv/`, `node_modules/`
   - `data/vector_db/` (regenerates on first ingest)
   - `data/embedding_cache/` (regenerates)
   - `logs/`
   - `.env` (sensitive — copy `.env.example` instead and re-fill)
   - `/tmp_*` scratch files
2. Install **Python 3.10+** and **git** on the new machine.
3. Install the external CLIs you need (see table below) and log in to each.
4. From the project root run:
   ```bash
   python3 -m pip install --user -e .
   skyn3t init
   cp .env.example .env       # if you didn't bring .env over
   ```
5. Verify with `skyn3t status` and `pytest tests/`.

## What `setup-new-machine.sh` does

1. Verify Python 3.10+ is on PATH (errors if missing)
2. Verify `git` is on PATH
3. Run `pip install -e .` to install the package + deps
4. Run `skyn3t init` to create data/, logs/, vector DB
5. Probe each CLI subscription:
   - `claude --version` (Claude Pro/Max)
   - `kimi --version` (Kimi)
   - `copilot --version` (GitHub Copilot)
6. For any CLI missing, print install instructions
7. If `.env` doesn't exist, copy `.env.example` to `.env` (so the user can edit)
8. Print a summary of what's working / what needs setup

## What does NOT transfer

- **Subscription auth** — each CLI stores its login in `~/.claude/`, `~/.kimi/`, `~/.config/gh/`, etc. You'll need to re-login on the new machine.
- **HuggingFace cached models** (~80MB embedding model) — auto-redownloads first run.
- **`data/vector_db/`** — large ChromaDB files. CAN copy if you want the RAG corpus preserved, but it'll regenerate on next ingest.

## Required external CLIs

| CLI | What for | Install | Login |
|-----|----------|---------|-------|
| claude | Claude Pro/Max subscription | npm: `npm i -g @anthropic-ai/claude-code` | `claude login` |
| copilot | GitHub Copilot CLI | gh ext: `gh extension install github/gh-copilot` (or as configured) | `gh auth login` |
| kimi | Moonshot Kimi CLI | per Kimi docs | `kimi` (interactive auth) |
| python3 | Runtime | brew/system | — |
| git | Version control | brew/system | — |

## .env

The `.env` file is gitignored (and should be). Contains:
- `SECRET_KEY` (random)
- `SKYN3T_MASTER_KEY` (for encrypting at-rest secrets)
- Optional `OPENROUTER_API_KEY`, `ANTHROPIC_API_KEY` (only used if no CLI is available)

For a new install: copy `.env.example` → `.env`, generate a fresh `SECRET_KEY` (the script can do this).

## Data files worth keeping

- `data/agent_overrides.json` — your tuned routing per agent
- `data/seeds.yaml`, `data/llm_docs_seeds.yaml` — RAG seed lists
- `projects/` — every Studio project's artifacts
- `.git/` — full history including all `skyn3t/auto/*` branches

## Verifying after move

```bash
skyn3t status   # all 16 agents listed?
pytest tests/   # 91 passing?
skyn3t start    # server boots?
# in another terminal:
skyn3t          # REPL launches?
```
