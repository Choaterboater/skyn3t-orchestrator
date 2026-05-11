# Seed skills

Hand-curated starting skill set for fresh checkouts.

`data/skills/` is gitignored (runtime state — grows per machine).
These are the skills SkyN3t starts with before the auto-promotion
loop has run.

## Install

```bash
cp examples/skills_seed/*.md data/skills/
```

After that, builds will pick them up via `BaseAgent.load_skills_for_prompt()`.

## What's here

| File | Tag(s) | Used by |
|---|---|---|
| `designer-dashboard-density.md` | designer, dashboard, ui-pattern | Designer |
| `designer-machine-room-palette.md` | designer, palette, dark-mode | Designer |
| `architect-credential-proxy-pattern.md` | architect, system-design, integration | Architect |
| `codeagent-react-polling-pattern.md` | code_agent, react, polling | CodeAgent |
| `codeagent-websocket-reconnect.md` | code_agent, react, websocket | CodeAgent |
| `codeagent-error-empty-loading-states.md` | code_agent, react, ux | CodeAgent |
| `writer-readme-structure.md` | writer, readme, docs | Writer |
| `reviewer-cross-model-critique.md` | reviewer, critique, quality | Reviewer |

## How agents reach them

```python
# Inside any BaseAgent subclass:
skills_block = self.load_skills_for_prompt(
    tags=["designer", "palette", "dashboard"],
    limit=3,
)
prompt = base_prompt + skills_block
```

`load_skills_for_prompt()` returns a fully-formatted system-prompt
suffix (or `""` if no matches). Safe to append unconditionally.

## Adding more

Two paths:

1. **Hand-curate**: drop a new `.md` into `data/skills/` with the
   frontmatter format the existing files use. Tag it appropriately.
2. **Let the system learn**: ResearchAgent auto-promotes integration
   specs to skills tagged `integration-spec-{service}` after a
   successful research stage. Subsequent builds recall those.
