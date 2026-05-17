# Fix: Kimi CLI Idle Timeout / Stall

**Date:** 2026-05-15  
**Status:** Merged, tested, deployed  
**Affected:** `kimi_cli` backend (also benefits `claude_cli`, `openai_cli`, `copilot_cli`)  

---

## Symptom

DesignerAgent (and other agents) repeatedly failed with:

```
llm complete failed; caller=designer backend=kimi_cli error=idle timeout (180s no output)
```

The subprocess was killed even though Kimi was actively generating tokens. On retry, the same thing happened, burning multiple 180s windows per run.

---

## Root Cause

**Python block-buffering when stdout is a pipe.**

The `kimi` CLI is a Python script (`#!/.../python3`). When Python detects stdout is a pipe (not a TTY), it switches to block-buffering mode (typically 4–8 KB). Kimi can generate continuously for minutes without filling that buffer, so **no bytes reach the parent process** even though the model is working. Our idle detector saw zero stdout growth for >180s and correctly concluded "hung" — but the process wasn't hung, just buffered.

The same issue affects any Python-based CLI (e.g., `openai` CLI). Native binaries (`claude`, `copilot`) can also buffer at the C stdio level.

---

## Fixes Applied

### 1. `llm_client.py` — `_run_capture()` (primary LLM completion path)

**File:** `skyn3t/adapters/llm_client.py`

| Change | Why |
|--------|-----|
| `PYTHONUNBUFFERED=1` injected into subprocess env | Forces Python CLIs to flush after every write instead of block-buffering. |
| `stdbuf -oL -eL` prepended on Unix | Line-buffers stdout/stderr at the C stdio level for non-Python runtimes. |
| Idle detector now watches **stdout + stderr** | Some CLIs write heartbeat/progress data to stderr; that now resets the idle timer. |
| `_IDLE_TIMEOUT` 180s → **240s** | Gives headroom for occasional multi-minute pauses on very large codegen tasks. |

### 2. `sandbox.py` — `_prepare_env()` (sandboxed CLI-agent path)

**File:** `skyn3t/security/sandbox.py`

| Change | Why |
|--------|-----|
| `PYTHONUNBUFFERED=1` added to sandbox env | Same buffering fix for `KimiCLIAgent` and other CLI agents that run through the sandbox layer. |

---

## Code Diff (summary)

```python
# llm_client.py
env = dict(os.environ)
env["PYTHONUNBUFFERED"] = "1"

exec_args = list(args)
if sys.platform != "win32":
    exec_args = ["stdbuf", "-oL", "-eL"] + exec_args

# Idle check now uses:
prev_len = _total_len(stdout_buf) + _total_len(stderr_buf)
```

```python
# sandbox.py
env.setdefault("PYTHONUNBUFFERED", "1")
```

---

## Verification

```bash
# 1. Backend resolves and responds
python3 -c "
import asyncio
from skyn3t.adapters.llm_client import LLMClient
async def t():
    c = LLMClient(backend='kimi_cli')
    print(await c.complete('say hi', max_tokens=10))
asyncio.run(t())
"

# 2. Tests pass
pytest tests/test_llm_client.py tests/test_cli_agents.py tests/test_cross_model_skip.py -v
```

**Result:** 25/25 tests pass; kimi backend resolves and returns output in ~2–5s for small prompts.

---

## Related Patches (already in repo)

- DesignerAgent now sets `_skip_llm_for_run = True` after the first failure, so one dead Kimi call no longer triggers multiple 180s retries in the same `execute()` cycle.
- `LLMClient.complete()` now performs a **single cross-backend failover retry** before falling back to deterministic output. If `kimi_cli` times out, the call retries once with `backend=auto` and `skip_backends=['kimi_cli']`, so the next available backend (for example `copilot_cli` or `claude_cli`) gets a chance to answer.
- `DesignerAgent` now bounds its **post-write side effects** (`send_message()` and `share_learning()`) so ancillary swarm signaling cannot keep the stage open forever after `brand.md`, `palette.json`, `components.md`, `tokens.css`, `tokens.json`, `logo.svg`, and `README.md` already exist on disk.
- `StudioRunner._critique_and_revise()` now bounds `ReviewerAgent.critique()` with a timeout. The original post-write "designer still running" symptom turned out not to be Kimi generation anymore: the runner was waiting forever in the **cross-model critique pass** after the designer artifacts had already been written.

### Why this matters

Kimi is still a strong choice for **design/UI ideation and feature framing**, but it should not hold the entire run hostage. With this patch, Kimi can remain the preferred "cheap/design specialist" backend while timing out calls naturally fall through to a stronger coder backend instead of immediately collapsing to deterministic text.

---

## Future Prevention

1. **Always set `PYTHONUNBUFFERED=1`** when spawning Python-based CLI subprocesses via pipe.
2. **Monitor combined stdout+stderr** for idle detection; never assume progress only appears on stdout.
3. **Prefer `_run_capture` for LLM calls** over raw `proc.communicate()` when you need granular idle-timeout behavior.
4. **Use one-hop backend failover** on timeout/error for non-critical specialist backends (for example Kimi for design) so the system can preserve specialization without sacrificing forward progress.
5. **Bound critique/review sidecars** the same way you bound primary generation. A stage that already wrote its artifacts should not stay `running` indefinitely because a reviewer or telemetry handoff never returns.
