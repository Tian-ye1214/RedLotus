# Skill Creator principles (inspired by chindden/skill-creator)

## Concise is key

The context window is a public good.
- Assume the model is already smart.
- Only include information that is not obvious and that materially improves outcomes.

## Degrees of freedom

- High freedom: heuristics and selection guidance.
- Medium freedom: pseudocode, parameterized scripts.
- Low freedom: exact steps and deterministic scripts.

## Progressive disclosure

Keep SKILL.md minimal.
Move:
- long examples,
- variant specific details,
- deep references,
into `references/` files and only load them when needed.

## Skill anatomy

Required:
- `SKILL.md`

Optional:
- `scripts/` deterministic helpers
- `references/` docs loaded on demand
- `assets/` templates, not meant for context

## Avoid clutter

Do not create extra top-level docs that an agent will never read.
Prefer one SKILL.md plus a small number of references files.
