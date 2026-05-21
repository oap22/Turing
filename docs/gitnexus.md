# GitNexus integration

[GitNexus](https://github.com/abhigyanpatwari/GitNexus) is a client-side code
knowledge-graph engine with an MCP server. It gives Claude Code / Cursor structural
awareness of this codebase — execution flows, blast-radius (`impact`), and pre-commit
risk (`detect_changes`).

## One-time setup

```bash
npm install -g gitnexus                              # requires Node LTS
GITNEXUS_SKIP_OPTIONAL_GRAMMARS=1 gitnexus analyze   # builds the .gitnexus/ index
cp .mcp.json.example .mcp.json                       # register the MCP server (project-scoped)
```

Restart Claude Code afterwards so it picks up `.mcp.json`.

- `.gitnexus/` (the index) and `.mcp.json` are git-ignored — each developer builds locally.
- `gitnexus analyze` also maintains a `<!-- gitnexus:start -->` block in `CLAUDE.md` and an
  `AGENTS.md`; both are committed so the team shares the same guidance.

## Day-to-day

```bash
gitnexus analyze                 # re-index after pulling changes (incremental)
gitnexus query "<concept>"       # find execution flows
gitnexus impact "<symbol>" --direction upstream   # what breaks if I change this
gitnexus detect-changes          # map git diff to affected flows + risk level
gitnexus cypher "<query>"        # raw graph queries
gitnexus status                  # index freshness
```

With `.mcp.json` active, the same capabilities are available in-session as MCP tools
(`impact`, `context`, `detect_changes`, `query`, `cypher`, `rename`).

## Security review

GitNexus was used to map the agent tool-execution surface for the
[2026-05-21 security review](security/gitnexus-vulnerability-report-2026-05-21.md).
Note GitNexus is a structural navigation tool, not a CVE scanner — it locates risky
code paths; the judgement is still manual.
