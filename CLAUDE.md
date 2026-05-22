# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Build & Development Commands

```bash
# Setup
python3.11 -m venv .venv && source .venv/bin/activate
pip install hatchling && pip install -e ".[dev]"

# Run
python -m turing                          # Start the bot (needs .env)
TURING_ENV=development python -m turing   # Explicit dev mode

# Test
pytest tests/ -v                          # Full suite (301 tests)
pytest tests/test_agent/ -v               # Single module
pytest tests/test_tools/test_shell.py::TestDenylist::test_rm_rf_root -v  # Single test
pytest tests/ -m "not slow"               # Skip slow tests
pytest --cov=turing tests/                # With coverage

# Lint & Format
ruff check src/ tests/                    # Lint
ruff format src/ tests/                   # Format
mypy src/                                 # Type check

# Docker multi-node simulation
docker compose up -d                      # 4 nodes + Ollama
docker compose logs -f turing-node-1      # Follow primary node
```

## Architecture

**Turing** is an autonomous AI assistant running on Raspberry Pis, communicating via Discord, with a hybrid LLM brain (local Ollama + cloud Claude API).

### Core Loop: Perceive → Think → Act → Remember

The agent loop in `agent/core.py` processes each message through:

1. **Perceive**: `build_context()` runs 4 parallel queries via `memory/retriever.py` — recent messages, semantic vector search, user preferences, and fact search
2. **Think**: LLM called via `llm/router.py` with dynamic system prompt (node identity, user prefs, tool definitions, mesh peers, plugin additions)
3. **Act**: If LLM returns `tool_calls`, the executor runs safety gate → tool execution → audit log, then feeds results back to LLM (up to 10 iterations)
4. **Remember**: Store conversation, optionally extract patterns via LLM every N interactions

### LLM Routing (`llm/router.py`)

In `auto` mode: tools present → cloud; otherwise classifier decides simple (local) vs complex (cloud). Local failures automatically fall back to cloud. Config `llm_routing_mode` can force `cloud`/`local`.

### Tool Safety Pipeline

`ToolRegistry.execute()` → `SafetyGate.check()` → execute or deny. Three outcomes: APPROVED (auto-execute), DENIED (blocked by deny-list patterns), NEEDS_CONFIRMATION (sends Discord button UI, waits 60s). All actions audit-logged to SQLite.

### Key Conventions

- **All config via env vars** with `TURING_` prefix (Pydantic Settings). See `.env.example`.
- **Async everywhere** — aiosqlite, async LLM clients, asyncio.create_subprocess_shell for tools
- **Lazy imports in `__main__.py`** to avoid circular dependencies during the 9-step init sequence
- **structlog** for all logging — JSON in production, colored console in dev. Node name bound globally.
- **Tool risk levels**: LOW (read-only), MEDIUM (network/writes), HIGH (process management/destructive). HIGH requires admin or confirmation.
- **Tests use in-memory SQLite** (`:memory:`) and mock LLM providers returning canned `LLMResponse` objects. Config fixtures use `_env_file=None` to avoid loading real `.env`.

### Module Dependencies (initialization order)

```
Config → Logging → MemoryStore → EmbeddingModel → VectorStore → MemoryRetriever
                → ClaudeProvider + OllamaProvider → LLMRouter
                → ToolRegistry (register built-in tools)
                → PluginLoader → PluginRegistry (register plugin tools)
                → SafetyGate
                → MeshNode + PeerDiscovery (if mesh_enabled)
                → Agent + Executor
                → TuringBot (receives agent, mesh_node, memory_store)
```

### Multi-Node Architecture

Docker Compose simulates 4 Pi nodes on a shared network. Node 1 (pi-alpha) has Discord token + Anthropic key. Nodes 2-4 are `local_only` workers. Mesh peer presence rides on the shared NATS bus (subjects `mesh.presence.heartbeat` / `mesh.presence.leave`); see ADR-0008. Plugins are loaded from `plugins/` directory via `manifest.json`.

## Agent skills

### Issue tracker

Issues live in GitHub Issues at `oap22/Turing`, managed via the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

Canonical label vocabulary (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`). See `docs/agents/triage-labels.md`.

### Domain docs

Single-context layout: `CONTEXT.md` and `docs/adr/` at the repo root. See `docs/agents/domain.md`.

<!-- gitnexus:start -->
# GitNexus — Code Intelligence

This project is indexed by GitNexus as **Turing** (7905 symbols, 14952 relationships, 102 execution flows). Use the GitNexus MCP tools to understand code, assess impact, and navigate safely.

> If any GitNexus tool warns the index is stale, run `npx gitnexus analyze` in terminal first.

## Always Do

- **MUST run impact analysis before editing any symbol.** Before modifying a function, class, or method, run `gitnexus_impact({target: "symbolName", direction: "upstream"})` and report the blast radius (direct callers, affected processes, risk level) to the user.
- **MUST run `gitnexus_detect_changes()` before committing** to verify your changes only affect expected symbols and execution flows.
- **MUST warn the user** if impact analysis returns HIGH or CRITICAL risk before proceeding with edits.
- When exploring unfamiliar code, use `gitnexus_query({query: "concept"})` to find execution flows instead of grepping. It returns process-grouped results ranked by relevance.
- When you need full context on a specific symbol — callers, callees, which execution flows it participates in — use `gitnexus_context({name: "symbolName"})`.

## Never Do

- NEVER edit a function, class, or method without first running `gitnexus_impact` on it.
- NEVER ignore HIGH or CRITICAL risk warnings from impact analysis.
- NEVER rename symbols with find-and-replace — use `gitnexus_rename` which understands the call graph.
- NEVER commit changes without running `gitnexus_detect_changes()` to check affected scope.

## Resources

| Resource | Use for |
|----------|---------|
| `gitnexus://repo/Turing/context` | Codebase overview, check index freshness |
| `gitnexus://repo/Turing/clusters` | All functional areas |
| `gitnexus://repo/Turing/processes` | All execution flows |
| `gitnexus://repo/Turing/process/{name}` | Step-by-step execution trace |

## CLI

| Task | Read this skill file |
|------|---------------------|
| Understand architecture / "How does X work?" | `.claude/skills/gitnexus/gitnexus-exploring/SKILL.md` |
| Blast radius / "What breaks if I change X?" | `.claude/skills/gitnexus/gitnexus-impact-analysis/SKILL.md` |
| Trace bugs / "Why is X failing?" | `.claude/skills/gitnexus/gitnexus-debugging/SKILL.md` |
| Rename / extract / split / refactor | `.claude/skills/gitnexus/gitnexus-refactoring/SKILL.md` |
| Tools, resources, schema reference | `.claude/skills/gitnexus/gitnexus-guide/SKILL.md` |
| Index, status, clean, wiki CLI commands | `.claude/skills/gitnexus/gitnexus-cli/SKILL.md` |

<!-- gitnexus:end -->
