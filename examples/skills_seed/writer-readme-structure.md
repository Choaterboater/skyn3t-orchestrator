---
name: writer-readme-structure
tags: [writer, readme, docs, runnable]
success_count: 1
failure_count: 0
last_used_at: 1778516000.0
source: hand-curated:seed-skill
created_at: 1778516000.0
---

# README structure for a runnable program

A README for a runnable program has exactly one job: get the reader
from cloned-repo to working-program in the shortest possible path.
Marketing copy, philosophy, and screenshots all come AFTER the
runnable section.

## The structure (in order)

```markdown
# <Project name>

<One sentence: what this is, who it's for.>

## Quickstart

\`\`\`
git clone <repo>
cd <repo>
<install command>
<run command>
\`\`\`

Visit http://localhost:<port>.

## Configuration

<Env vars table. Required vars first, then optional. Each with a one-line
description and example value. Group by service if multiple integrations.>

| Variable | Required | Example | Description |
|---|---|---|---|
| `SONARR_API_KEY` | yes | `1a2b3c...` | Sonarr API key from Settings → General → Security |

## What it does

<3-5 bullet points or one short paragraph. Concrete features, not vision.>

## Architecture (optional)

<Only if non-obvious. Skip for single-process apps.>

## Development

<\`npm run dev\`, \`pytest\`, \`make test\` — whatever the local dev loop is.>

## Deploy

<Docker compose, env file, target host — one path that works, not
five options.>

## Troubleshooting

<Two or three real failure modes the reader is likely to hit, with fixes.
Example: "If you see `ECONNREFUSED 127.0.0.1:8989`, Sonarr isn't running
or `SONARR_URL` is wrong.">
```

## Anti-patterns to avoid

- ❌ Logo / hero / badges before the Quickstart. The reader didn't come to admire.
- ❌ "Why we built this" section anywhere in the README. Goes on the website if at all.
- ❌ Listing every env var in alphabetical order. Group by importance + by service.
- ❌ Code blocks without language tags. `\`\`\`bash`, not `\`\`\``.
- ❌ `## Contributing` boilerplate before the program even runs.
- ❌ Outdated quickstart from three commits ago. Test the quickstart on every release.

## Tone

Direct, second-person, present tense. "Set `SONARR_URL`" not "You will
need to set `SONARR_URL`". Avoid "simply", "just", "easy" — they're
defensive about the fact that it might not be.

## When the program has integrations

Each integration gets a subsection under Configuration explaining where
to get the credentials. Don't assume the reader knows.

Example:
```
### Sonarr

1. In Sonarr: Settings → General → Security → API Key (top of page).
2. Copy the value into `SONARR_API_KEY` in your `.env`.
3. If Sonarr runs on a different host than the dashboard, set `SONARR_URL`
   to its full URL (default: `http://localhost:8989`).
```
