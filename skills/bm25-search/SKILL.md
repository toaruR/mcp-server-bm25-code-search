---
name: bm25-search
description: Best practices and guidelines for using the BM25 Code Search MCP tool to explore codebases efficiently with minimal token consumption.
---

# BM25 Code Search Guidelines

When exploring the codebase, finding function implementations, or investigating features, use the `search` (BM25 Code Search) MCP tool.

## Agent-Specific Execution (Claude Code)

Claude Code treats MCP tools as deferred tools. If the schema is not yet loaded in the current session:
1. **Load schema**: Run `ToolSearch` with query `select:mcp__bm25-code-search__search`.
2. **Execute search**: Call `mcp__bm25-code-search__search` with natural language keywords.

*(Other agents like Cursor, Codex, and Antigravity can call the tool directly without ToolSearch).*

## When to Use BM25 Search

- **Feature exploration**: Searching for where a specific feature, API, or logic is implemented (e.g., "user authentication", "webhook retry handling").
- **Symbol & identifier lookup**: Searching for camelCase (`getUserProfile`), PascalCase (`UserAuthManager`), or snake_case (`session_token`) names.
- **Multilingual documents**: Searching Japanese or CJK technical comments and documentation.

## Search Strategy

1. **Search first**: Prioritize the `search` tool over brute-force `grep` or `glob` to retrieve the most relevant code chunks within strict token limits.
2. **Path boosting**: File path matches are boosted 3.0x over body content, making it fast to locate files by name or directory structure.
3. **Query formulation**: Pass descriptive keywords (e.g., `"user profile authentication"` or `"process_payment error"`) rather than complex regex.
4. **Fallback**: If `search` returns 0 results or when exact substring/regex matches across all lines are strictly required, fall back to `grep_search` or `glob`.
