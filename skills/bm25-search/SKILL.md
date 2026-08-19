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
4. **Fallback**: See the [Re-Search Loop](#re-search-loop) section below for the full retry/fallback sequence, or when exact substring/regex matches across all lines are strictly required, fall back to `grep_search` or `glob` immediately.

## Multi-Query Fan-Out (`queries`)

When a single query risks missing relevant chunks due to vocabulary mismatch (e.g. the code may say "auth" while you searched "login"), pass paraphrased alternatives in the optional `queries` array (up to 5) alongside `query`. The server searches every variant, merges them into one ranked list via Reciprocal Rank Fusion (RRF), and returns a single response — this avoids spending a full extra round trip per rephrasing.

```json
{"query": "authenticate_user", "queries": ["session_token", "login handler"]}
```

Chunks that matched multiple queries are ranked higher and carry a `matched_queries` list, so you can see which of your phrasings actually converged on the same code.

## Reading the `confidence` Field

Every response includes a `confidence` object (`label`: `none` / `single` / `dominant` / `close_contest`) summarizing how clearly the top result stands out from the runner-up:

- **`dominant`**: the top result is a clearly stronger match (or, for `queries` fan-out, was hit by more query variants) — safe to trust it as the primary answer.
- **`close_contest`**: several candidates are comparably relevant — inspect more than just the top result before concluding.
- **`single`**: only one candidate was returned — there's nothing to compare against, so verify it independently if the match looks weak.

This is a hint, not a verdict — you (the agent) still decide whether to accept the result, inspect more candidates, re-search with different keywords, or fall back to `grep_search`/`glob`.

## Reading the `low_confidence_hint` Field

When `results` is non-empty but either the hit count is thin (well under `top_k`) or the top result's raw score is weak, the response also carries a non-null `low_confidence_hint` string recommending a re-search with a synonym or a different granularity keyword. `null` means neither signal fired — no action needed on this basis alone.

## Re-Search Loop

Follow this concrete loop instead of accepting the first response at face value or re-searching indefinitely:

1. **Initial search**: run `search` with your best-guess keywords.
2. **Inspect the result**: check `confidence.label`, `low_confidence_hint`, and how many results came back relative to `top_k`.
3. **Re-search if thin**: if `confidence.label` is `close_contest`/`single`, or `low_confidence_hint` is non-null, rephrase with a synonym or a broader/narrower granularity keyword and search again — **up to 2 additional rounds**. Prefer bundling paraphrases into one call via the `queries` fan-out (see above) over issuing several separate round trips.
4. **Fall back**: if it's still thin/empty after those rounds, or `status` is `zero_match` at any point, switch to `grep_search`/`glob` rather than continuing to re-search with BM25.
