#!/usr/bin/env node
/*
Scaffold a new OpenClaw skill folder using the Premium Skill Creator blueprint.

Usage:
  node scaffold-skill.mjs <slug> "<one sentence description>" [outDir]

Defaults:
  outDir = ./skills (relative to current working directory)

Notes:
- Creates a minimal SKILL.md, references/ directory, and scripts/setup.mjs stub.
- Does not publish.
*/

import fs from "node:fs";
import path from "node:path";

const [,, slug, desc, outDirArg] = process.argv;
if (!slug || !desc) {
  console.error("Usage: node scaffold-skill.mjs <slug> \"<description>\" [outDir]");
  process.exit(2);
}

if (!/^[a-z0-9-]+$/.test(slug)) {
  console.error("Slug must be lowercase letters, numbers, and hyphens only.");
  process.exit(2);
}

const outDir = outDirArg || path.resolve(process.cwd(), "skills");
const skillDir = path.join(outDir, slug);
const refsDir = path.join(skillDir, "references");
const scriptsDir = path.join(skillDir, "scripts");

fs.mkdirSync(refsDir, { recursive: true });
fs.mkdirSync(scriptsDir, { recursive: true });

const skillMd = `---\nname: ${slug}\ndescription: ${JSON.stringify(desc)}\n---\n\n# ${slug}\n\n## What this does\n\n- ${desc}\n\n## First run setup (premium wizard)\n\nIf this skill is not configured yet, run the setup wizard before doing real work.\n\n- Config file (workspace): <workspace>/.skill-config/${slug}.json\n\nWizard rules:\n- Ask only what is necessary to personalize the skill.\n- Keep it to 3 to 8 questions.\n- Show a short summary and ask for confirmation.\n- Persist answers to the config file.\n- Run a tiny test.\n\n## Reconfigure\n\nIf the user says: setup, configure, reconfigure, change settings then re-run the wizard.\n\n## References\n\nSee references/ for examples and deeper docs.\n`;

fs.writeFileSync(path.join(skillDir, "SKILL.md"), skillMd, "utf8");

const setupStub = `#!/usr/bin/env node\n/*\nOptional CLI setup helper.\nThis is a fallback for users who prefer terminal over chat.\nWrite config to: <workspace>/.skill-config/${slug}.json\n*/\n\nconsole.log("This skill is configured via chat by default. If you need CLI setup, implement prompts here.");\n`;
fs.writeFileSync(path.join(scriptsDir, "setup.mjs"), setupStub, "utf8");

fs.writeFileSync(path.join(refsDir, "README.md"), `# ${slug} references\n\nPut longer docs and examples here.\n`, "utf8");

console.log(JSON.stringify({ ok: true, skillDir }, null, 2));
