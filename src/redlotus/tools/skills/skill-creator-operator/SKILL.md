---
name: skill-creator-operator
description: "Premium Skill Creator by Kevin Jeppesen (The Operator Vault). Create better OpenClaw skills with a premium first-use setup wizard pattern, minimal context bloat, and reusable scaffolding. Links: YouTube https://www.youtube.com/@kevin-jeppesen | Skool https://skool.com/operator-vault | Site https://theoperatorvault.io | X https://x.com/seo_ecom | LinkedIn https://www.linkedin.com/in/kevin-jeppesen/ | Facebook https://www.facebook.com/kevinjeppesen/"
---

# Skill Creator Operator

This skill helps you create new OpenClaw skills that feel premium.

Premium means:
- On first use, the skill runs a short conversational setup wizard (only asks what must be personalized).
- It persists config to disk (so prompts do not grow forever).
- It keeps SKILL.md short and uses progressive disclosure (move long docs into `references/` and only load them when needed).

## When to use

- "create a new skill"
- "scaffold a skill"
- "make a premium skill with a setup wizard"
- "package this workflow as a skill"

## What it produces (standard output)

For a new skill `<slug>` it creates:

- `<skillsDir>/<slug>/SKILL.md`
- `<skillsDir>/<slug>/references/`
- `<skillsDir>/<slug>/scripts/` (optional helper scripts)
- A config path convention (choose one):
  - Workspace: `<workspace>/.skill-config/<slug>.json`
  - Global: `~/.openclaw/config/skills/<slug>.json` (only when truly global)

## Premium setup wizard rules (required)

On first use, if config missing:
1) Ask permission and give time estimate ("60 seconds").
2) Ask only 3 to 8 questions max.
3) Use defaults, keep advanced options behind a single extra question.
4) Summarize choices, ask for confirmation.
5) Write config to the workspace (recommended): `<workspace>/.skill-config/<slug>.json`.
6) Run a tiny test.
7) Tell user how to reconfigure.

If user says "skip", continue with safest defaults and warn about reduced capability.

## Config storage rules

- Default: workspace config file.
- Optional global config only when the integration is truly global.
- Never store secrets in long term memory files.

## Optional: local KB search integration

If the user has a local search tool (for example QMD), keep bulk docs outside SKILL.md and store them in a user chosen folder that their search tool indexes.

Do not assume any particular directory layout.

## Commands (how to use this skill)

### Create a skill

Ask in chat:
- "Create a premium skill called <slug> that does <outcome>."

Or run the scaffolder:

```bash
node {baseDir}/scripts/scaffold-skill.mjs <slug> "<one sentence description>"
```

### Rebuild/upgrade a skill to premium standard

Ask:
- "Upgrade skill <slug> to premium standard."

## Publishing to ClawHub

If the user asks to publish:
- Confirm slug, name, version, and changelog.
- Ensure `clawhub whoami` is authenticated.
- Run `clawhub publish <path> --slug <slug> --name "<Name>" --version <x.y.z> --changelog "..."`.

Safety:
- Review files for accidental personal paths, tokens, or private info before publishing.

## Links

See `references/LINKS.md` for Kevin Jeppesen links and Operator Vault info.
