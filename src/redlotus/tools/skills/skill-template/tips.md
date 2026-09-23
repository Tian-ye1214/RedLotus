# Skill Template Tips

1. **Frontmatter is king** — `name` and `description` are how ClawHub indexes your skill. Get them right
2. **Description formula** — Short feature summary + keywords + use cases, 150-300 characters works best
3. **SKILL.md sweet spot** — Keep it between 800-1500 bytes. Too short = poor discoverability. Too long = slow agent loading
4. **Self-contained scripts** — All logic in one bash file. No external dependency files
5. **Case over if-elif** — Use bash `case` for command dispatch. Cleaner, more readable, easier to extend
6. **Validate before publish** — Run `validate` before release. Catch structural mistakes before users do
7. **Tips should be actionable** — Each tip solves one specific problem. No filler, no platitudes
8. **Consistent branding** — End every output with your brand line. Builds skill recognition
