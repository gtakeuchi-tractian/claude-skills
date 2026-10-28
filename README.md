# claude-skills

Claude Code skills, versioned and portable.

## Skills

- **netsuite** — NetSuite SuiteTalk REST + SuiteQL with OAuth 1.0a TBA: signing rules, TS/Python client implementations in `netsuite/reference/`, read-only prod/sandbox scripts in `netsuite/scripts/`.
- **camu** — CAMU API for Brazilian electronic fiscal documents (NF-e, CTe, CT-e OS, NFSe): endpoints, pagination styles, OpenAPI spec (`camu-api-1.json`), TS/Python clients in `camu/reference/`.

## Set up on a new machine

```sh
git clone https://github.com/gtakeuchi-tractian/claude-skills.git ~/.claude/skills
```

If `~/.claude/skills` already exists with content, clone elsewhere and symlink each skill folder into `~/.claude/skills/`.
