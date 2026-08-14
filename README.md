# mcp-server-bm25-code-search

AI コーディングエージェント（VS Code, Cursor, GitHub Copilot, ChatGPT & Codex, Kiro, Hermes Agent, OpenClaw, Grok Bot, NanoClaw 等）におけるファイル検索を高速化・低トークン化するための、**Agent Plugins 規格準拠の SQLite FTS5 ローカル BM25 コード検索プラグイン & MCP サーバ**です。

---

## ✨ 特徴

- 📦 **外部依存ゼロ (Python 標準ライブラリのみ)**  
  `sqlite3` (FTS5) および標準ライブラリのみで構築されており、`pip install` などのサードパーティ依存パッケージなしで即座に動作します。

- 🧩 **Agent Plugins (v1.0.0) 規格準拠**  
  [Agent Plugins](https://agent-plugins.org/compatible-clients) に準拠し、公式対応クライアント（VS Code, Cursor, GitHub Copilot, ChatGPT & Codex, Kiro, Hermes Agent, OpenClaw, Grok Bot, NanoClaw）でディレクトリを指定するだけで、MCP サーバ（`mcp.json`）と検索ガイドスキル（`skills/`）をゼロコンフィグで一発認識・即時導入可能。

- 🔤 **コード識別子 & 日本語ハイブリッド対応**  
  `getUserProfile` (camelCase) や `session_token` (snake_case) のサブワード分割に加え、日本語技術文書の CJK 2-gram（バイグラム）トークナイズを Python 側で事前処理。FTS5 インデックスと検索クエリの両方に自動適用されます。

- 📁 **ファイルパスブースト (3.0x)**  
  FTS5 の `bm25(code_fts, 3.0, 1.0)` 列重み付けにより、ファイルパスとの一致を本文の一致より 3.0 倍優遇。探したいファイルへ少ない検索回数で到達できます。

- ⚡ **高速増分更新 & Git Worktree 非干渉**  
  `git ls-files` による `.gitignore` 完全準拠のファイル収集と、`git diff` / HEAD ハッシュトラッキングによる高速増分更新（通常編集時 0.1〜0.5秒）。インデックス `.bm25_index.db` は Worktree ローカルに配置され `.gitignore` で自動除外されます。

- 🔌 **MCP 2026-07-28 ＆ Hermes 標準対応**  
  - **MCP ネイティブ**: 2026-07-28 仕様準拠のステートレス stdio JSON-RPC サーバ。プロンプトキャッシュ効率を高める決定論的ツールソートを実装。
  - **Hermes Agent**: MCP 非対応環境向けに薄い Function Calling アダプタ層 (`hermes_adapter.py`) も標準同梱。

- 🛡️ **コンテキスト溢れ防止 & フォールバック**  
  出力文字制限 (`--max-bytes`) は UTF-8 のマルチバイト文字境界を保護して安全に切り詰め。検索結果 0 件時は grep/glob への切り替えを促す構造化フォールバックメッセージを返却します。

---

## 📁 モジュール構成

```text
mcp-server-bm25-code-search/
├── plugin.json            # Agent Plugins v1.0.0 マニフェスト
├── mcp.json               # Agent Plugins v1.0.0 MCP 設定
├── skills/                # Agent Skills (エージェント向け検索プロンプト・指針)
│   └── bm25-search/
│       └── SKILL.md
├── bm25_search/
│   ├── db.py              # SQLite FTS5 v2 スキーマ (chunks / code_fts / triggers)
│   ├── tokenizer.py       # 事前トークナイザ (camelCase / snake_case / CJK 2-gram)
│   ├── indexer.py         # インデクサ (git ls-files, 80/20 チャンキング, 増分更新)
│   ├── search.py          # 検索エンジン & CLI インターフェース
│   ├── mcp_server.py      # MCP 2026-07-28 ステートレス stdio サーバ
│   └── hermes_adapter.py  # Hermes Agent 向け Function Calling アダプタ
├── bin/
│   └── cli.js             # Node.js 向け CLI / npx 起動ラッパー
├── docs/
│   ├── specification.md   # 詳細仕様書
│   └── plans/             # 設計ドキュメント
└── tests/                 # pytest テストスイート
```

---

## 🚀 使い方

### 1. Agent Plugins としての導入 (推奨・ゼロコンフィグ)

[Agent Plugins 公式対応クライアント](https://agent-plugins.org/compatible-clients)（**VS Code, Cursor, GitHub Copilot, ChatGPT & Codex, Kiro, Hermes Agent, OpenClaw, Grok Bot, NanoClaw**）では、本リポジトリのディレクトリを指定またはプラグインとして読み込むだけで、**MCP サーバ（`mcp.json`）と検索ガイドスキル（`skills/`）が同時に自動認識**されます。

各クライアントの公式セットアップ手順:
- **VS Code**: [Agent Plugins in VS Code](https://code.visualstudio.com/docs/agent-customization/agent-plugins)
- **Cursor**: [Cursor Plugins](https://cursor.com/docs/plugins)
- **GitHub Copilot**: [Copilot Agent Plugins](https://docs.github.com/en/copilot/concepts/agents/about-plugins)
- **ChatGPT & Codex**: [OpenAI Plugin Developers](https://developers.openai.com/plugins)
- **Kiro**: [Kiro Powers](https://kiro.dev/docs/powers/)
- **Hermes Agent**: [Hermes Portable Plugins](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins#portable-agent-plugins-v1-packages)
- **OpenClaw**: [OpenClaw Plugin Bundles](https://docs.openclaw.ai/plugins/bundles)
- **Grok Bot**: [Grok Bot Automations](https://docs.x.ai/grok-bot/skills-routines-and-automations)
- **NanoClaw**: [NanoClaw Templates](https://github.com/nanocoai/nanoclaw/blob/main/docs/templates.md)

### 2. CLI での検索実行

```bash
python bm25_search/search.py "<検索クエリ>" --top-k 5 --format markdown --max-bytes 4000
```

**主なオプション:**
- `<query>`: 検索クエリ（日本語、camelCase、snake_case 対応）
- `--top-k`: 返す検索結果の上限件数（デフォルト: `5`）
- `--format`: 出力形式 `markdown` または `json`（デフォルト: `markdown`）
- `--max-bytes`: 最大出力バイト数。マルチバイト文字を安全に維持して切詰（デフォルト: `4000`）
- `--mode`: クエリトークンの結合モード `OR` または `AND`（デフォルト: `OR`）
- `--db`: 使用する SQLite インデックス DB パス（デフォルト: `.bm25_index.db`）

### 3. 個別 MCP サーバとしての起動（uvx / npx / 手動設定）

プロジェクトごとに Stdio ＋ 自動インデックス構築で動かすため、`uvx` または `npx` で即座に起動できます。
引数未指定の場合、MCP サーバが起動されたプロジェクト（カレントディレクトリ）のコードベースを自動検出・増分インデックス（`.bm25_index.db`）の作成・同期を行います。

#### ① `uvx` (uv / Python) を使う場合
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

#### ② `npx` (Node.js / npm) を使う場合
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

#### ③ ローカル Python での直接指定
```json
{
  "mcpServers": {
    "bm25-code-search": {
      "command": "python",
      "args": [
        "D:/path/to/mcp-server-bm25-code-search/bm25_search/mcp_server.py",
        "--stdio"
      ],
      "alwaysAllow": [
        "search"
      ]
    }
  }
}
```


#### 💡 AI エージェントに `grep` 連打を抑止し BM25 検索を優先させる設定 (`AGENTS.md` / `CLAUDE.md`)

AI エージェントが `grep` を何度もリトライしてトークンやコンテキストを無駄に消費するのを防ぐため、利用するプロジェクトの `AGENTS.md` や `CLAUDE.md`（またはシステムプロンプト）に以下の指示を追記することを推奨します。

```markdown
## コード検索の指示方針
- コードベースの機能調査やコード探索を行う際は、最初に MCP ツール `search` (BM25 Code Search) を優先して使用してください。
- **Claude Code での呼び出し手順**: Claude Code では MCP ツールが Deferred Tool となるため、初回呼び出し前に必ず `ToolSearch` (`select:mcp__bm25-code-search__search`) でスキーマをロードしてから `mcp__bm25-code-search__search` を実行してください。
- `search` で結果が得られない場合、または特定のシンボル名の完全一致を直接検索する場合にのみ `grep_search` や `glob` を使用してください。
```

### 4. Claude Code での設定方法（Agent Plugins 非準拠のため個別設定が必要）

[Claude Code](https://docs.claude.com/en/docs/claude-code) は [Agent Plugins](https://agent-plugins.org/) 規格に準拠していないため、リポジトリを指定するだけの自動認識（`plugin.json` / `mcp.json` のゼロコンフィグ読み込み）は行われません。代わりに、Claude Code 標準の MCP サーバ登録機能を使って個別に設定してください。

> **💡 Claude Code 利用時のポイント (Deferred Tool):**  
> Claude Code はコンテキスト節約のため MCP ツールを「Deferred Tool（遅延ロード）」として扱います。初回の検索実行前に内部ツール `ToolSearch` (`select:mcp__bm25-code-search__search`) でツールのスキーマをロードしてから検索が実行されます（上記の `CLAUDE.md` 指示やスキル `.claude/skills/bm25-search/SKILL.md` を配置しておくと確実に実行されます）。

#### 方法 A: `claude mcp add` CLI コマンド（推奨）

プロジェクト直下で以下のいずれかを実行します（`--scope project` を付けるとプロジェクト直下に `.mcp.json` が生成され、git commit してチームで共有できます。`--scope user` にすると全プロジェクト共通のユーザー設定として登録されます）。

```bash
# uvx (uv / Python) を使う場合
claude mcp add bm25-code-search --scope project -- uvx mcp-server-bm25-code-search

# npx (Node.js / npm) を使う場合
claude mcp add bm25-code-search --scope project -- npx -y mcp-server-bm25-code-search

# ローカル Python を直接指定する場合（環境変数 -e も指定可能）
claude mcp add bm25-code-search --scope project -e PYTHONUTF8=1 -- python D:/path/to/mcp-server-bm25-code-search/bm25_search/mcp_server.py --stdio
```

登録後は `claude mcp list` または Claude Code セッション内の `/mcp` コマンドで認識状況を確認できます。

#### 方法 B: `.mcp.json` を直接作成

プロジェクトルートに `.mcp.json` を作成しても同様に登録できます（内容は上記「個別 MCP サーバとしての起動」節の JSON と同一形式です）。

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

`.mcp.json` はプロジェクトルートに置いて git commit することで、チームメンバー間で設定を共有できます（初回読み込み時に Claude Code から承認確認が入ります）。

### 5. Hermes Agent アダプタの使用

MCP 非対応の Hermes Agent からは、`bm25_search.hermes_adapter` モジュールを利用します。

```python
from bm25_search.hermes_adapter import hermes_function_schema, run_hermes_tool

# Hermes 用 Tool Schema の取得
schema = hermes_function_schema()

# Hermes からの Function Call 実行
response = run_hermes_tool({
    "name": "bm25_search",
    "arguments": {
        "query": "getUserProfile",
        "top_k": 5
    }
})
```

---

## 🧪 テストの実行

`pytest` を使ってユニットテストおよび統合テストを実行できます。

```bash
pytest tests/
```

---

## 📄 ドキュメント

- [仕様書 (docs/specification.md)](file:///d:/vagrant/harnesses/mcp-server-bm25-code-search/docs/specification.md)
- [設計仕様書 (docs/plans/bm25-multi-agent-search-skill-design.md)](file:///d:/vagrant/harnesses/mcp-server-bm25-code-search/docs/plans/bm25-multi-agent-search-skill-design.md)

---

## 📚 参考文献・関連リンク

- **Agent Plugins 規格**: [Agent Plugins Specification (agentplugins/agent-plugins-spec)](https://github.com/agentplugins/agent-plugins-spec) / [agent-plugins.org](https://agent-plugins.org/)
- **Agent Skills 規格**: [Agent Skills Specification](https://agentskills.io/specification)
- **論文**: Wang et al., *"BM25 Wins at Scale: Evaluating Agentic Search over Enterprise Corpora"* (2026)  
  [https://arxiv.org/abs/2607.26497](https://arxiv.org/abs/2607.26497)
- **解説記事**: 須藤英寿（株式会社ナレッジセンス）, *"BM25を使用してCodexのトークンの消費を30%抑える"* (Zenn, 2026)  
  [https://zenn.dev/knowledgesense/articles/9e55a3bb67729c](https://zenn.dev/knowledgesense/articles/9e55a3bb67729c)

---

## ⚖️ ライセンス

本プロジェクトは [MIT License](LICENSE) の下で公開されています。

