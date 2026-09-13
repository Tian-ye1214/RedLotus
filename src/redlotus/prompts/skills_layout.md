## Skills Directory Description

- Skills are loaded from **two roots**: shipped baseline skills inside the RedLotus package, plus a **writable overlay** at `{skills_root_path}`. New installs go to the overlay path shown above.

- **Stored by Skill directory:** Each **subfolder** under a skills root corresponds to one Skill. The folder name can differ from the `name` field in `SKILL.md` YAML; the system registers by `name`.

- Each Skill directory must contain **`SKILL.md`**: YAML frontmatter (`name`, `description`, …) followed by instructions.

- Reference materials (`references/`, `rules/`, scripts, etc.) live in the same Skill directory and can be loaded with `load_skill_resource()`.

- The catalog below is a **session snapshot**. Use `list_available_skills()` or `refresh_skills()` to discover changes without rewriting the session's system instructions.

- **Progressive loading:** choose by name and description, read `get_skill_instructions(skill_name)`, then request only the needed files through `load_skill_resource(skill_name, resource_name)`. Run provided scripts with `execute_skill_script`; do not load every Skill's full contents in advance.
