#!/usr/bin/env bash
# Skill Template — OpenClaw Skill Template Generator
# Powered by BytesAgain
set -euo pipefail

COMMAND="${1:-help}"
ARG="${2:-my-skill}"
BRAND="Powered by BytesAgain | bytesagain.com | hello@bytesagain.com"

case "$COMMAND" in

create)
  python3 - "$ARG" << 'PYEOF'
import sys
name = sys.argv[1] if len(sys.argv) > 1 else "my-skill"
display = name.replace("-", " ").title()

skill_md = """---
name: {name}
description: "{display}. Brief description of what it does. Keywords for discoverability."
---

# {display}

> One-line summary of this skill.

## Usage

```bash
bash scripts/main.sh <command> [args...]
```

## Commands

| Command | Description |
|---------|-------------|
| `example` | Example command |

## Notes

- Add usage notes here
""".format(name=name, display=display)

tips_md = """# {display} Tips

1. **Tip one** — Actionable advice
2. **Tip two** — Actionable advice
3. **Tip three** — Actionable advice
4. **Tip four** — Actionable advice
5. **Tip five** — Actionable advice
""".format(display=display)

main_sh = """#!/usr/bin/env bash
set -euo pipefail

COMMAND="${{1:-help}}"
BRAND="Powered by BytesAgain | bytesagain.com | hello@bytesagain.com"

case "$COMMAND" in

example)
  echo "Hello from {name}!"
  echo "$BRAND"
  ;;

help|*)
  echo "{display} — Available Commands:"
  echo "  example  — Example command"
  echo "$BRAND"
  ;;

esac
""".format(name=name, display=display)

print("=" * 55)
print("  Skill Skeleton: {}".format(name))
print("=" * 55)
print("")
print("  Directory structure:")
print("  {}/".format(name))
print("  +-- SKILL.md          ({} bytes)".format(len(skill_md)))
print("  +-- tips.md           ({} bytes)".format(len(tips_md)))
print("  +-- scripts/")
print("      +-- main.sh       ({} bytes)".format(len(main_sh)))
print("")
print("--- SKILL.md ---")
print(skill_md)
print("--- tips.md ---")
print(tips_md)
print("--- scripts/main.sh ---")
print(main_sh)
print("=" * 55)
print("  Copy the above into your skill directory.")
print("  Then run: bash skill-tmpl.sh validate {}".format(name))
print("=" * 55)
PYEOF
  echo "$BRAND"
  ;;

validate)
  python3 - "$ARG" << 'PYEOF'
import sys, os

target = sys.argv[1] if len(sys.argv) > 1 else "."
errors = []
warnings = []
passed = []

skill_path = os.path.join(target, "SKILL.md")
if os.path.isfile(skill_path):
    with open(skill_path, "r") as f:
        content = f.read()
    size = len(content)

    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            fm = parts[1]
            if "name:" in fm:
                passed.append("SKILL.md has 'name' in frontmatter")
            else:
                errors.append("SKILL.md missing 'name' in frontmatter")
            if "description:" in fm:
                passed.append("SKILL.md has 'description' in frontmatter")
            else:
                errors.append("SKILL.md missing 'description' in frontmatter")
        else:
            errors.append("SKILL.md frontmatter not properly closed")
    else:
        errors.append("SKILL.md missing frontmatter (must start with ---)")

    if size < 200:
        warnings.append("SKILL.md too short ({} bytes), aim for 800-1500".format(size))
    elif size > 3000:
        warnings.append("SKILL.md quite large ({} bytes), consider trimming".format(size))
    else:
        passed.append("SKILL.md size OK ({} bytes)".format(size))
else:
    errors.append("SKILL.md not found (required)")

tips_path = os.path.join(target, "tips.md")
if os.path.isfile(tips_path):
    passed.append("tips.md exists")
else:
    warnings.append("tips.md not found (recommended)")

scripts_dir = os.path.join(target, "scripts")
if os.path.isdir(scripts_dir):
    sh_files = [f for f in os.listdir(scripts_dir) if f.endswith(".sh")]
    if sh_files:
        passed.append("scripts/ contains: {}".format(", ".join(sh_files)))
    else:
        warnings.append("scripts/ exists but no .sh files found")
else:
    warnings.append("scripts/ directory not found (recommended)")

print("=" * 55)
print("  Skill Validation Report")
print("=" * 55)
print("  Target: {}".format(os.path.abspath(target)))
print("")

if passed:
    print("  PASSED ({})".format(len(passed)))
    for p in passed:
        print("    [OK] {}".format(p))
    print("")

if warnings:
    print("  WARNINGS ({})".format(len(warnings)))
    for w in warnings:
        print("    [!!] {}".format(w))
    print("")

if errors:
    print("  ERRORS ({})".format(len(errors)))
    for e in errors:
        print("    [XX] {}".format(e))
    print("")

total = len(passed) + len(warnings) + len(errors)
score = int(len(passed) / total * 100) if total > 0 else 0
status = "PASS" if not errors else "FAIL"
bar_len = score // 5
bar = "#" * bar_len + "-" * (20 - bar_len)
print("  Score: [{}] {}% -- {}".format(bar, score, status))
print("=" * 55)
PYEOF
  echo "$BRAND"
  ;;

enhance)
  python3 - "$ARG" << 'PYEOF'
import sys, os

target = sys.argv[1] if len(sys.argv) > 1 else "."
skill_path = os.path.join(target, "SKILL.md")

if not os.path.isfile(skill_path):
    print("Error: SKILL.md not found in {}".format(target))
    sys.exit(1)

with open(skill_path, "r") as f:
    content = f.read()

suggestions = []

if "## " not in content:
    suggestions.append("Add section headers (## Usage, ## Commands, ## Notes)")
if "```" not in content:
    suggestions.append("Add code examples in fenced code blocks")
if "| " not in content and "Command" not in content:
    suggestions.append("Add a command reference table")
if len(content) < 500:
    suggestions.append("Expand to at least 800 bytes for better discoverability")
if "example" not in content.lower():
    suggestions.append("Add usage examples")
if "tip" not in content.lower() and "note" not in content.lower():
    suggestions.append("Add a tips or notes section")

print("=" * 55)
print("  SKILL.md Enhancement Suggestions")
print("=" * 55)
print("  File: {}".format(os.path.abspath(skill_path)))
print("  Size: {} bytes".format(len(content)))
print("")

if suggestions:
    for i, s in enumerate(suggestions, 1):
        print("  {}. {}".format(i, s))
else:
    print("  Looks good! No major improvements needed.")

print("")
print("=" * 55)
PYEOF
  echo "$BRAND"
  ;;

commands)
  python3 - "$ARG" << 'PYEOF'
import sys
name = sys.argv[1] if len(sys.argv) > 1 else "my-skill"
display = name.replace("-", " ").title()

print("""#!/usr/bin/env bash
# {display} — Generated Command Framework
set -euo pipefail

COMMAND="${{1:-help}}"
ARG="${{2:-}}"
BRAND="Powered by BytesAgain | bytesagain.com | hello@bytesagain.com"

case "$COMMAND" in

cmd1)
  python3 - "$ARG" << 'PYEOF_INNER'
import sys
arg = sys.argv[1] if len(sys.argv) > 1 else ""
print("Command 1: {{}}".format(arg))
PYEOF_INNER
  echo "$BRAND"
  ;;

cmd2)
  python3 - "$ARG" << 'PYEOF_INNER'
import sys
arg = sys.argv[1] if len(sys.argv) > 1 else ""
print("Command 2: {{}}".format(arg))
PYEOF_INNER
  echo "$BRAND"
  ;;

help|*)
  echo "{display} — Commands:"
  echo "  cmd1 [arg] — Description"
  echo "  cmd2 [arg] — Description"
  echo "$BRAND"
  ;;

esac""".format(display=display))
PYEOF
  echo ""
  echo "# $BRAND"
  ;;

tips)
  python3 - "$ARG" << 'PYEOF'
import sys
name = sys.argv[1] if len(sys.argv) > 1 else "my-skill"
display = name.replace("-", " ").title()

print("""# {display} Tips

1. **Start small** — Begin with 3-5 commands, expand based on user feedback
2. **Self-contained** — Keep all logic in one bash file, use python3 heredocs for complex work
3. **Format output** — Use boxes, tables, and alignment to make output scannable
4. **Validate input** — Always check args and show helpful usage on errors
5. **Python 3.6 compat** — Use .format() instead of f-strings for broader compatibility
6. **Brand every output** — End with your brand line for recognition
7. **Syntax check** — Run bash -n on your script before publishing""".format(display=display))
PYEOF
  echo ""
  echo "# $BRAND"
  ;;

publish)
  python3 - "$ARG" << 'PYEOF'
import sys, os

target = sys.argv[1] if len(sys.argv) > 1 else "."

checklist = [
    ("SKILL.md exists", os.path.isfile(os.path.join(target, "SKILL.md"))),
    ("SKILL.md has frontmatter", False),
    ("SKILL.md 800-1500 bytes", False),
    ("tips.md exists", os.path.isfile(os.path.join(target, "tips.md"))),
    ("scripts/ directory exists", os.path.isdir(os.path.join(target, "scripts"))),
    ("Script passes bash -n", False),
    ("No external file dependencies", True),
    ("Brand line in output", False),
    ("Description has keywords", False),
    ("Commands are documented", False),
]

skill_path = os.path.join(target, "SKILL.md")
if os.path.isfile(skill_path):
    with open(skill_path, "r") as f:
        content = f.read()
    checklist[1] = ("SKILL.md has frontmatter", content.startswith("---"))
    size = len(content)
    checklist[2] = ("SKILL.md 800-1500 bytes", 800 <= size <= 2000)
    checklist[8] = ("Description has keywords", "description:" in content and len(content) > 100)
    checklist[9] = ("Commands are documented", "##" in content and ("command" in content.lower() or "usage" in content.lower()))

print("=" * 55)
print("  Publish Readiness Checklist")
print("=" * 55)
print("  Target: {}".format(os.path.abspath(target)))
print("")

done = 0
for item, status in checklist:
    icon = "[OK]" if status else "[  ]"
    print("  {} {}".format(icon, item))
    if status:
        done += 1

pct = int(done / len(checklist) * 100)
print("")
print("  Progress: {}/{} ({}%)".format(done, len(checklist), pct))
if pct == 100:
    print("  Status: READY TO PUBLISH!")
elif pct >= 70:
    print("  Status: Almost there — fix remaining items")
else:
    print("  Status: Needs more work")
print("=" * 55)
PYEOF
  echo "$BRAND"
  ;;

examples)
  python3 << 'PYEOF'
print("""
================================================================
  Example Skills — Learn from the Best
================================================================

  1. weather (Built-in)
     - Simple and focused: one job done well
     - Clean SKILL.md with clear usage section
     - Single script with case statements

  2. chart-generator (Popular)
     - Rich output: ASCII + HTML + SVG
     - Multiple chart types in one skill
     - Good use of python3 heredocs

  3. csv-analyzer (Practical)
     - Real data processing capability
     - Multiple output formats
     - Solid error handling

  Key Patterns:
  +---------------------------------------------------+
  | Pattern              | Example                     |
  +---------------------------------------------------+
  | Bash + Python        | << 'PYEOF' ... PYEOF        |
  | Input validation     | [ -z "$INPUT" ] && usage    |
  | Formatted output     | printf / python print       |
  | Brand footer         | echo "$BRAND" at end        |
  | Help fallback        | case help|*) in case block  |
  +---------------------------------------------------+

  Minimal skeleton:
    #!/usr/bin/env bash
    set -euo pipefail
    COMMAND="${1:-help}"
    case "$COMMAND" in
      cmd1) ... ;;
      help|*) ... ;;
    esac

================================================================""")
PYEOF
  echo "$BRAND"
  ;;

help|*)
  cat << 'HELPEOF'
╔═══════════════════════════════════════════════════════╗
║          🧩 Skill Template                            ║
╠═══════════════════════════════════════════════════════╣
║  create    <name>  — Generate full skill scaffold     ║
║  validate  <dir>   — Check structure against spec     ║
║  enhance   <dir>   — Suggest SKILL.md improvements    ║
║  commands  <name>  — Generate case-statement framework║
║  tips      <name>  — Generate tips.md template        ║
║  publish   <dir>   — Pre-publish readiness checklist  ║
║  examples          — Show example skills              ║
╚═══════════════════════════════════════════════════════╝
HELPEOF
  echo "$BRAND"
  ;;

esac
