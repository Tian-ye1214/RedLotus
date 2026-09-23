---
version: "2.0.0"
name: skill-template
description: "OpenClaw Skill template generator. Create skill scaffolds, validate structure, enhance SKILL.md, generate command frameworks, tips, publish checklists."
author: BytesAgain
homepage: https://bytesagain.com
source: https://github.com/bytesagain/ai-skills
---

# skill-template

Multi-purpose utility tool for managing entries, searching data, and exporting records. A general-purpose CLI toolkit with init, add/remove, list, search, and export capabilities — ideal as a starting point for building your own skill or as a standalone data management tool.

## Commands

| Command | Description |
|---------|-------------|
| `skill-template run <input>` | Execute the main function with the given input |
| `skill-template config` | Show configuration file path (`config.json` location) |
| `skill-template status` | Show current status (reports "ready" when operational) |
| `skill-template init` | Initialize the data directory and prepare for use |
| `skill-template list` | List all entries in the data log |
| `skill-template add <entry>` | Add a new dated entry to the data log |
| `skill-template remove <entry>` | Remove a specified entry |
| `skill-template search <term>` | Search the data log for a term (case-insensitive) |
| `skill-template export` | Export all data from the data log to stdout |
| `skill-template info` | Show version number and data directory path |
| `skill-template help` | Show help message with all available commands |
| `skill-template version` | Show version number |

## How It Works

`skill-template` manages a simple text-based data log (`data.log`) in its data directory. You can add entries (automatically date-stamped), list them, search through them, and export the full contents. Every command is also recorded in a separate `history.log` for auditing.

### Workflow

1. **Initialize**: Run `skill-template init` to set up the data directory
2. **Add entries**: Use `skill-template add` to log items with automatic date stamps
3. **Review**: Use `skill-template list` to see all entries or `skill-template search` to find specific ones
4. **Export**: Run `skill-template export` to output all data for backup or processing

## Data Storage

All data is stored in `$SKILL_TEMPLATE_DIR` or defaults to `~/.local/share/skill-template/`. The directory contains:

- `data.log` — main data file where entries are stored (date-stamped lines)
- `history.log` — timestamped log of all commands executed
- `config.json` — configuration file path (referenced by `config` command)

The tool automatically creates the data directory on first run. You can override the storage location by setting the `SKILL_TEMPLATE_DIR` environment variable.

## Requirements

- **Shell**: Bash 4+
- **No external dependencies** — uses only standard Unix utilities (`date`, `grep`, `wc`, `cat`)
- **Works on**: Linux, macOS, any POSIX-compatible system

## When to Use

1. **Quick data logging** — Use `skill-template add "meeting notes: discussed Q2 targets"` to capture timestamped entries without opening an editor
2. **Building a new skill** — Fork this as a starting template with a working CLI framework (case dispatch, logging, data management) already in place
3. **Simple searchable notebook** — Add notes throughout the day, then use `skill-template search "keyword"` to find them later
4. **Data export for processing** — Run `skill-template export` to pipe all entries to another tool or save as a backup file
5. **Learning bash CLI patterns** — Study the script as an example of a well-structured bash tool with case-based dispatch, logging, and data management

## Examples

```bash
# Initialize the data directory
skill-template init

# Add some entries
skill-template add "Project Alpha: milestone 1 complete"
skill-template add "Bug fix: resolved login timeout issue"
skill-template add "Meeting: sprint review with team"

# List all entries
skill-template list

# Search for project-related entries
skill-template search "Project"

# Check the tool status
skill-template status

# View version and data location
skill-template info

# Show configuration path
skill-template config

# Export all data to a file
skill-template export > backup.txt

# Run a custom function
skill-template run "my-task"
```

## Configuration

Set the `SKILL_TEMPLATE_DIR` environment variable to change the data directory:

```bash
export SKILL_TEMPLATE_DIR="$HOME/my-skill-data"
```

Default location: `~/.local/share/skill-template/`

## Output

All command output goes to stdout in plain text. The `list` and `export` commands output the raw content of `data.log`, making them easy to pipe into other tools:

```bash
skill-template list | grep "2024-03"
skill-template export | wc -l
```

---
Powered by BytesAgain | bytesagain.com | hello@bytesagain.com
