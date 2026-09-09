# mcp-server-bm25-code-search

**English** | [日本語](README.ja.md)

Fast, low-token BM25 local code search plugin & MCP server backed by SQLite FTS5 for AI coding agents (VS Code, Cursor, GitHub Copilot, ChatGPT & Codex, Kiro, Hermes Agent, OpenClaw, Grok Bot, NanoClaw, etc.), fully compliant with the **Agent Plugins** specification.

---

## ✨ Features

- 📦 **Zero External Dependencies (Python Standard Library Only)**  
  Built entirely on `sqlite3` (FTS5) and the Python standard library. Runs out-of-the-box without requiring third-party package installations (`pip install`).

- 🧩 **Agent Plugins (v1.0.0) Compliant**  
  Conforms to the [Agent Plugins](https://agent-plugins.org/compatible-clients) standard. In supported clients (VS Code, Cursor, GitHub Copilot, ChatGPT & Codex, Kiro, Hermes Agent, OpenClaw, Grok Bot, NanoClaw), simply pointing to this repository automatically discovers and loads both the MCP server (`mcp.json`) and the search guidance skill (`skills/`) with zero configuration.

- 🔤 **Code Identifier & Japanese/CJK Hybrid Tokenization**  
  Pre-processes code identifiers with subword splitting for `getUserProfile` (camelCase) and `session_token` (snake_case), combined with Python-side CJK 2-gram (bigram) tokenization for technical documentation and comments. Automatically applied to both FTS5 indexing and search queries.

- 📁 **File Path Boost (3.0x)**  
  Leverages FTS5 column weighting `bm25(code_fts, 3.0, 1.0)` to weight file path matches 3.0x higher than file content matches, pinpointing target files in fewer search iterations.

- ⚡ **Fast Incremental Indexing & Git Worktree Isolation**  
  Strict `.gitignore` compliance using `git ls-files` with ultra-fast incremental updates (0.1–0.5s during standard editing) via `git diff` / HEAD hash tracking. The index database `.bm25_index.db` is stored locally within the worktree and automatically ignored by `.gitignore`.

- 🔌 **MCP 2026-07-28 & Hermes Native Support**  
  - **MCP Native**: Stateless stdio JSON-RPC server adhering to the MCP 2026-07-28 specification, with deterministic tool sorting for prompt cache optimization.
  - **Hermes Agent**: Includes a lightweight Function Calling adapter layer (`hermes_adapter.py`) for environments without native MCP support.

- 🛡️ **Context Overflow Protection & Fallback Guidance**  
  Safe byte-length truncation (`--max-bytes`) preserving UTF-8 multi-byte character boundaries. Returns structured fallback messages prompting agents to switch to `grep`/`glob` when zero results are found.

---

## 📁 Directory Structure

```text
mcp-server-bm25-code-search/
├── plugin.json            # Agent Plugins v1.0.0 manifest
├── mcp.json               # Agent Plugins v1.0.0 MCP configuration
├── skills/                # Agent Skills (agent search guidelines & prompt)
│   └── bm25-search/
│       └── SKILL.md
├── bm25_search/
│   ├── db.py              # SQLite FTS5 v2 schema (chunks / code_fts / triggers)
│   ├── tokenizer.py       # Pre-tokenizer (camelCase / snake_case / CJK 2-gram)
│   ├── indexer.py         # Indexer (git ls-files, 80/20 chunking, incremental sync)
│   ├── search.py          # Search engine & CLI interface
│   ├── mcp_server.py      # MCP 2026-07-28 stateless stdio server
│   └── hermes_adapter.py  # Function Calling adapter for Hermes Agent
├── bin/
│   └── cli.js             # Node.js CLI / npx runner wrapper
├── docs/
│   ├── specification.md   # Detailed specification
│   └── plans/             # Design documents
└── tests/                 # pytest test suite
```

---

## 🚀 Getting Started

### 1. Installation via Agent Plugins (Recommended / Zero-Config)

In [Agent Plugins compatible clients](https://agent-plugins.org/compatible-clients) (**VS Code, Cursor, GitHub Copilot, ChatGPT & Codex, Kiro, Hermes Agent, OpenClaw, Grok Bot, NanoClaw**), simply adding this repository directory or loading it as a plugin automatically recognizes **both the MCP server (`mcp.json`) and the search guidance skill (`skills/`)**:

Official setup documentation for supported clients:
- **VS Code**: [Agent Plugins in VS Code](https://code.visualstudio.com/docs/agent-customization/agent-plugins)
- **Cursor**: [Cursor Plugins](https://cursor.com/docs/plugins)
- **GitHub Copilot**: [Copilot Agent Plugins](https://docs.github.com/en/copilot/concepts/agents/about-plugins)
- **ChatGPT & Codex**: [OpenAI Plugin Developers](https://developers.openai.com/plugins)
- **Kiro**: [Kiro Powers](https://kiro.dev/docs/powers/)
- **Hermes Agent**: [Hermes Portable Plugins](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins#portable-agent-plugins-v1-packages)
- **OpenClaw**: [OpenClaw Plugin Bundles](https://docs.openclaw.ai/plugins/bundles)
- **Grok Bot**: [Grok Bot Automations](https://docs.x.ai/grok-bot/skills-routines-and-automations)
- **NanoClaw**: [NanoClaw Templates](https://github.com/nanocoai/nanoclaw/blob/main/docs/templates.md)

### 2. Running Search via CLI

```bash
python bm25_search/search.py "<search query>" --top-k 5 --format markdown --max-bytes 4000
```

**Key Options:**
- `<query>`: Search query (supports Japanese, CJK, camelCase, snake_case)
- `--top-k`: Maximum number of search results to return (default: `5`)
- `--format`: Output format, `markdown` or `json` (default: `markdown`)
- `--max-bytes`: Maximum output bytes; safely truncated preserving multibyte characters (default: `4000`)
- `--mode`: Token conjunction mode, `OR` or `AND` (default: `OR`)
- `--db`: Path to SQLite index DB file (default: `.bm25_index.db`)

### 3. Running as a Standalone MCP Server (uvx / npx / Manual)

The server communicates via Stdio and automatically indexes the project codebase. You can launch it instantly with `uvx` or `npx`.
If arguments are omitted, the server automatically detects the current working directory as the project root and synchronizes the incremental index (`.bm25_index.db`).

#### CLI Options
| Option | Description | Default |
|---|---|---|
| `--root`, `-r` | Target project root directory to index and search | `.` (current directory) |
| `--db` | Path to SQLite FTS5 index DB file | `<root>/.bm25_index.db` |
| `--no-auto-sync` | Disable automatic index synchronization on tool calls | Disabled (auto-sync active) |
| `--stdio` | Run stdio JSON-RPC transport loop | Enabled |

> **💡 Automatic Project Root Detection with `--db`:**  
> When specifying `--db <path>` (e.g. `--db /path/to/project/.bm25_index.db`) without an explicit `--root`, **the parent directory of the DB file is automatically detected as the project root**.  
> This allows global or shared agent configurations to easily target specific projects while maintaining seamless Auto Sync and search functionality (specifying `--root` explicitly will take precedence).

#### ① Using `uvx` (uv / Python)
```json
{
  "mcpServers": {
    "bm25-code-search": {
      "command": "uvx",
      "args": ["mcp-server-bm25-code-search"],
      "alwaysAllow": ["search"]
    }
  }
}
```

#### ② Using `npx` (Node.js / npm)
```json
{
  "mcpServers": {
    "bm25-code-search": {
      "command": "npx",
      "args": ["-y", "mcp-server-bm25-code-search"],
      "alwaysAllow": ["search"]
    }
  }
}
```

#### ③ Using Local Python Directly
```json
{
  "mcpServers": {
    "bm25-code-search": {
      "command": "python",
      "args": [
        "/path/to/mcp-server-bm25-code-search/bm25_search/mcp_server.py",
        "--stdio"
      ],
      "alwaysAllow": [
        "search"
      ]
    }
  }
}
```

#### ④ Specifying a Target Project DB via `--db`
```json
{
  "mcpServers": {
    "bm25-code-search": {
      "command": "uvx",
      "args": [
        "mcp-server-bm25-code-search",
        "--db",
        "/path/to/my-project/.bm25_index.db"
      ],
      "alwaysAllow": ["search"]
    }
  }
}
```
*Note: The parent directory `/path/to/my-project` is automatically recognized as the project root for indexing and synchronization.*

#### 💡 Agent Instruction Guideline (`AGENTS.md` / `CLAUDE.md`)

To prevent AI agents from repeatedly spamming `grep` and wasting context tokens, adding the following instruction to your project's `AGENTS.md`, `CLAUDE.md`, or system prompt is strongly recommended:

```markdown
## Code Search Policy
- When exploring code or investigating features across the codebase, always prioritize the MCP tool `search` (BM25 Code Search) first.
- Only fall back to `grep_search` or `glob` if `search` returns zero results or when exact literal matches for a specific symbol are required.
```

### 4. Setup in Claude Code (Manual Configuration)

Because [Claude Code](https://docs.claude.com/en/docs/claude-code) does not natively support the [Agent Plugins](https://agent-plugins.org/) standard, automatic manifest discovery (`plugin.json` / `mcp.json`) is not available. Configure it using Claude Code's MCP server registration:

> **💡 Note on Deferred Tools in Claude Code:**  
> Claude Code treats MCP tools as "Deferred Tools" to conserve context. The agent will load the tool schema before execution. Adding the instruction above to `CLAUDE.md` or placing the skill in `.claude/skills/bm25-search/SKILL.md` ensures consistent tool selection.

#### Method A: `claude mcp add` CLI Command (Recommended)

Run one of the following commands in your project root (`--scope project` generates a `.mcp.json` file to commit to git; `--scope user` registers it globally):

```bash
# Using uvx (uv / Python)
claude mcp add bm25-code-search --scope project -- uvx mcp-server-bm25-code-search

# Using npx (Node.js / npm)
claude mcp add bm25-code-search --scope project -- npx -y mcp-server-bm25-code-search

# Using local Python directly
claude mcp add bm25-code-search --scope project -e PYTHONUTF8=1 -- python /path/to/mcp-server-bm25-code-search/bm25_search/mcp_server.py --stdio
```

Verify the configuration with `claude mcp list` or the `/mcp` command inside a Claude Code session.

#### Method B: Direct `.mcp.json` File

Place a `.mcp.json` file in your project root (identical schema to the MCP configuration above):

```json
{
  "mcpServers": {
    "bm25-code-search": {
      "command": "uvx",
      "args": ["mcp-server-bm25-code-search"]
    }
  }
}
```

### 5. Using Hermes Agent Adapter

For Hermes Agent environments without MCP support, use the `bm25_search.hermes_adapter` module:

```python
from bm25_search.hermes_adapter import hermes_function_schema, run_hermes_tool

# Get Hermes Tool Schema
schema = hermes_function_schema()

# Execute Function Call from Hermes
response = run_hermes_tool({
    "name": "bm25_search",
    "arguments": {
        "query": "getUserProfile",
        "top_k": 5
    }
})
```

---

## 🧪 Running Tests

Run the unit and integration test suite using `pytest`:

```bash
pytest tests/
```

---

## 📄 Documentation

- [Specification (docs/specification.md)](docs/specification.md)
- [Design Specification (docs/plans/bm25-multi-agent-search-skill-design.md)](docs/plans/bm25-multi-agent-search-skill-design.md)

---

## 📚 References & Links

- **Agent Plugins Specification**: [Agent Plugins Specification (agentplugins/agent-plugins-spec)](https://github.com/agentplugins/agent-plugins-spec) / [agent-plugins.org](https://agent-plugins.org/)
- **Agent Skills Specification**: [Agent Skills Specification](https://agentskills.io/specification)
- **Paper**: Wang et al., *"BM25 Wins at Scale: Evaluating Agentic Search over Enterprise Corpora"* (2026)  
  [https://arxiv.org/abs/2607.26497](https://arxiv.org/abs/2607.26497)
- **Article**: Hidetoshi Sudo (KnowledgeSense, Inc.), *"Using BM25 to reduce Codex token consumption by 30%"* (Zenn, 2026)  
  [https://zenn.dev/knowledgesense/articles/9e55a3bb67729c](https://zenn.dev/knowledgesense/articles/9e55a3bb67729c)

---

## ⚖️ License

This project is licensed under the [MIT License](LICENSE).
