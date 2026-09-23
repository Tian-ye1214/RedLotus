## Skills Directory Description

- Skills are loaded from **two roots**: shipped baseline skills inside the RedLotus package, plus a **writable overlay** at `{skills_root_path}`. New installs go to the overlay path shown above.

- **Stored by Skill directory:** Each **subfolder** under a skills root corresponds to one Skill. The folder name can differ from the `name` field in `SKILL.md` YAML; the system registers by `name`.

- Each Skill directory must contain **`SKILL.md`**: YAML frontmatter (`name`, `description`, …) followed by instructions.

- Reference materials (`references/`, `rules/`, scripts, etc.) live in the same Skill directory and can be loaded with `load_skill_resource()`.

- The skill list is **rescanned at the start of each user turn** and when an agent calls `refresh_skills`. The "Available Skills" section below reflects the latest scan.