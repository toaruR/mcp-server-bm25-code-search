<!-- spec-doc:last-reviewed-commit=b516b1f2eebb91d53b422434b0eac3885a65056f reviewed-at=2026-08-12 -->

# コード探索向け BM25 検索 Skill 仕様書

本ドキュメントは、Claude Code, Codex, Antigravity, hermes などのマルチエージェント環境において、コード構造および日本語技術文書に最適化されたローカル BM25 検索エンジンの仕様を記述したものです。

---

## 1. 全体アーキテクチャ

単一の Python コードベース（標準ライブラリ `sqlite3` ベース）により、外部依存ライブラリなしで動作する検索エンジンおよびインデクサを提供します。

- **コア検索エンジン (`bm25_search/`)**: データベースアクセス、事前トークナイズ、インデックス作成・更新、検索実行を担うモジュール群。
- **MCP サーバ (`bm25_search/mcp_server.py`)**: MCP 2026-07-28 仕様に完全準拠したステートレスな stdio JSON-RPC サーバ。
- **パッケージ化・ランチャー (`pyproject.toml` / `package.json` / `bin/cli.js`)**: `uvx` および `npx` によるワンライナー自動起動とプロジェクトごとの Stdio 接続をサポート。
- **Hermes アダプタ (`bm25_search/hermes_adapter.py`)**: MCP 非対応の Hermes Agent 向け Function Calling スキーマ変換および CLI 呼び出しアダプタ。


---

## 2. データベーススキーマ (`bm25_search/db.py`)

SQLite FTS5 の External Content Table パターン（v2 スキーマ）を採用し、データの一貫性と高速な全文検索を実現します。

### 2.1 テーブル構造

- **`chunks`**: ソースオブトゥルース（正規データ）。チャンク単位で格納。
  - `chunk_id` (`INTEGER PRIMARY KEY AUTOINCREMENT`)
  - `filepath` (`TEXT NOT NULL`): ワークツリールートからの相対パス
  - `start_line` (`INTEGER NOT NULL`): 開始行番号 (1-based, inclusive)
  - `end_line` (`INTEGER NOT NULL`): 終了行番号 (1-based, inclusive)
  - `raw_snippet` (`TEXT NOT NULL`): 元のコード・テキスト（エージェント返却用）
  - `index_text` (`TEXT NOT NULL`): 事前トークナイズ済みテキスト（FTS5 検索専用）
- **`code_fts`**: FTS5 Virtual Table (External Content Table)。
  - `filepath`, `index_text` をインデックス化。
  - `content='chunks'`, `content_rowid='chunk_id'`, `tokenize='unicode61 remove_diacritics 2'`
- **`file_metadata`**: 増分更新・差分同期用のファイル管理テーブル。
  - `filepath` (`PRIMARY KEY`), `mtime` (`REAL`), `file_hash` (`TEXT`, SHA-256)
- **`repo_state`**: 状態管理（リポジトリの HEAD ハッシュ等）キー・バリューテーブル。
  - `key` (`PRIMARY KEY`), `value` (`TEXT`)

### 2.2 同期トリガー
- `chunks_ai`, `chunks_ad`, `chunks_au`: `chunks` テーブルに対する INSERT / DELETE / UPDATE 操作時に、`code_fts` インデックスを自動同期。

---

## 3. トークナイズパイプライン (`bm25_search/tokenizer.py`)

標準 `sqlite3` ではカスタム FTS5 トークナイザが登録できないため、インデックス挿入前および検索クエリ実行時に Python 側で事前トークナイズ処理を行います。

### 3.1 事前トークナイズ仕様 (`pre_tokenize`)
- **ASCII 識別子**: camelCase（`getUserProfile` -> `getuserprofile`, `get`, `user`, `profile`）および snake_case（`session_token` -> `session_token`, `session`, `token`）を元の単語とサブワードトークンに分割し、空白区切りで併記。
- **CJK / 日本語**: 連続する CJK・仮名文字を 2-gram（文字バイグラム）に分解（例: 「有効期限」 -> 「有効 効期 期限」）。1文字の場合はそのまま維持。

### 3.2 クエリ側トークナイズ (`pre_tokenize_query`)
- 検索クエリにも同一の `pre_tokenize` を適用。
- 分割された各トークンをダブルクォートで囲み、既定では `OR` 演算子で結合（例: `"有効" OR "効期" OR "期限"`）。

---

## 4. インデクサ & ファイル管理 (`bm25_search/indexer.py`)

### 4.1 ファイル収集・拡張子フィルタ
- `git ls-files --cached --others --exclude-standard` を使用して候補ファイルを収集（`.gitignore` の否定・ネスト規則に100%準拠）。非 Git ディレクトリではディレクトリウォークへフォールバック。
- 対象拡張子: `.ts`, `.tsx`, `.js`, `.jsx`, `.mjs`, `.cjs`, `.vue`, `.svelte`, `.astro`, `.html`, `.htm`, `.hbs`, `.ejs`, `.twig`, `.php`, `.phtml`, `.blade.php`, `.css`, `.scss`, `.sass`, `.less`, `.styl`, `.py`, `.go`, `.java`, `.rs`, `.c`, `.cpp`, `.h`, `.md`, `.json`, `.yaml`, `.toml`, `.sql` の 34 種類。

### 4.2 チャンキング戦略
- 80行固定ブロック / 20行オーバーラップ（ストライド 60行）。
- `start_line` / `end_line` は 1-based かつ inclusive。
- **境界スナップ（2-A、既定で有効）**: 理想境界（80行目）が関数・クラスの途中に来る場合、直後最大10行以内に空行があれば、そこまでチャンク終端を前方にのみ延長する（[bm25-agent-reasoning-gap-improvement-plan.md](plans/bm25-agent-reasoning-gap-improvement-plan.md) 施策 2-A）。空行に依存する言語非依存のヒューリスティックであり、対応言語判定・構文解析は行わない。近傍に空行が無い場合（JSON/CSS/圧縮コード等）は従来通りの固定長カットにフォールバックする。前方拡張のみのため次チャンクとのカバレッジの穴は発生しない。`chunk_lines(..., snap_boundaries=False)` で無効化可能。

### 4.3 増分更新 & HEAD 追従
- `repo_state` に記録された前回コミットハッシュと現在の `HEAD` を比較。
- ブランチ切り替えやコミット進捗時、`git diff --name-status <old> <new>` により削除ファイル（`D`）のパージおよび変更・追加ファイル（`A`/`M`/`R`）の再インデックスを瞬時に実行。
- `file_metadata` の mtime および SHA-256 ファイルハッシュ比較による未コミット変更の自動追従。

### 4.4 Git Worktree 対応 & ウォームスタート
- インデックスファイル `.bm25_index.db` は Worktree ローカル直下に配置し、`.gitignore` で除外。
- 新規 Worktree 作成時、親 Worktree の `.bm25_index.db` をコピーし `git diff` 差分同期を行うウォームスタート機能を提供。

---

## 5. 検索エンジン & CLI (`bm25_search/search.py`)

### 5.1 BM25 スコアリング & パスブースト
- FTS5 `bm25(code_fts, 3.0, 1.0)` を使用し、`filepath` への一致を `index_text`（本文）より 3.0 倍重み付け。
- **同一ファイル内チャンクの多様化（1-A、既定で有効）**: `search()` は既定で `top_k` の3倍（最低20件）のプールを取得したうえで、同一 `filepath` かつ `start_line` の差が60行未満（オーバーラップのストライドと同値）のチャンクをオーバーラップ由来の重複とみなし、クラスタ内でスコア最良の1件のみを残す（[bm25-agent-reasoning-gap-improvement-plan.md](plans/bm25-agent-reasoning-gap-improvement-plan.md) 施策 1-A）。別ファイルや同一ファイル内でも離れた位置のチャンクは間引かれない。`search(..., diversify=False)` で無効化可能（BM25 スコア自体の計算は変更しない）。

### 5.2 出力フォーマット & コンテキスト保護
- CLI: `python search.py "<query>" --top-k 5 --format [markdown|json] --max-bytes 4000 --mode [OR|AND] --queries <言い換えクエリ...>`
- `--max-bytes`: 指定バイト数制限時、UTF-8 のマルチバイト文字境界を壊さない安全な文字単位の切り詰めを実施。
- **Zero-Match フォールバック**: 検索ヒットが 0 件の場合、以下の固定メッセージを含む構造化 JSON/レスポンスを返却し、エージェントへ grep/glob への切り替えを促進:
  `BM25 match zero. Recommended Fallback: Use standard grep_search or glob to locate exact symbol definitions.`

### 5.3 複数クエリファンアウト & 比較材料（`search_multi` / `confidence`）

[bm25-agent-reasoning-gap-improvement-plan.md](plans/bm25-agent-reasoning-gap-improvement-plan.md) の施策 2-B / 1-B に対応。

- **`queries`（複数クエリファンアウト）**: `query` に加え、言い換えクエリを最大5件まで `queries` として渡すと、`search_multi()` が各クエリを個別に（`top_k` を広げたプールで）検索し、`(filepath, start_line, end_line)` をキーに Reciprocal Rank Fusion（`1 / (60 + rank)` を積算、標準的な RRF 定数 k=60）で統合して 1 回のレスポンスで返す。複数クエリでヒットしたチャンクは `fusion_score` が加算され上位に来る。各結果には `matched_queries`（ヒットしたクエリ一覧）が付与される。
  - `queries` を渡さない場合（または結果として単一クエリに正規化された場合）は `search()` と完全に同一の結果を返す（後方互換）。
- **`confidence`（比較材料）**: レスポンス直下に、1位と2位の相対関係を示す `{"label", "top_score", "runner_up_score", "score_gap"}` を常時付与する。
  - `label` は `none`（0件）/ `single`（1件のみ）/ `dominant`（1位が明確に優勢）/ `close_contest`（僅差の候補が並ぶ）のいずれか。
  - `queries` ファンアウト時は `fusion_score` の比（＝複数クエリへの収束度）で、単一クエリ時は BM25 スコアの絶対値比で判定する（閾値 `DOMINANT_RATIO = 1.5`）。
  - サーバー側はこの値で何も判断・分岐しない。呼び出し元エージェントが「1位を信頼するか」「上位複数件を見るか」「再検索するか」を判断するための材料として渡すのみ。

### 5.4 低確信時フォールバックの拡充（`low_confidence_hint`）

[bm25-agent-reasoning-gap-improvement-plan.md](plans/bm25-agent-reasoning-gap-improvement-plan.md) の施策 1-C に対応。0件時の Zero-Match フォールバック（5.2）とは別に、**ヒットはしたが薄い／弱い**場合にも構造化ヒントを追加する。

- レスポンス JSON のトップレベルに `low_confidence_hint`（`string | null`）を常時付与する。
- 以下いずれかを満たすと非 `null`（同義語・別粒度キーワードでの再検索を推奨する固定文言）になる:
  - **ヒット件数が薄い**: 返却件数が `max(1, int(top_k * 0.4))` 未満（`top_k` が渡された場合のみ判定）。
  - **上位スコアが弱い**: 上位1件の生 BM25 スコアが `WEAK_SCORE_THRESHOLD = -1.0` より大きい（0に近い＝弱いマッチ）。
- `DOMINANT_RATIO` 同様、暫定的で調整可能なヒューリスティック定数である。

---

## 6. MCP サーバ (`bm25_search/mcp_server.py`) & パッケージ化

MCP (Model Context Protocol) 2026-07-28 仕様に準拠したステートレス RPC サーバ。

- **パッケージ化と自動起動 (`uvx` / `npx`)**:
  - `pyproject.toml`: PyPI / `uvx` 対応。`[project.scripts]` により `mcp-server-bm25-code-search` コマンドを提供。
  - `package.json` + `bin/cli.js`: Node.js / `npx` 対応。`npx -y mcp-server-bm25-code-search` 実行時に `uvx` やローカル Python プロセスをパイプ起動する Node.js Stdio ラッパーを提供。
- **プロジェクト自動検出 & 自動インデックス同期 (Auto Sync)**:
  - CLI 引数 `--root <DIR>` (既定: カレントディレクトリ `.`) によりプロジェクトルートを指定。
  - `--db <PATH>` 指定時は、その親ディレクトリをプロジェクトルートとして自動認識（`--root` 未指定時）。
  - サーバー起動時および `tools/call` の `search` 呼び出し時、暗黙のインデックスパス（`<root>/.bm25_index.db`）に対して `indexer.sync_index()` を自動実行。クライアント側での事前インデックス作成コマンド実行を不要化。
- **ステートレス RPC**: `initialize` / `initialized` ハンドシェイクおよび `Mcp-Session-Id` を廃止し、全リクエストが自己完結型。ただし、従来の 2024-11-05 仕様の MCP クライアントとの接続互換性を保つため、`initialize`, `initialized`, `ping` RPC ハンドラを具備。
- **`server/discover`**: プロトコルバージョン (`2026-07-28`)、サーバ機能 (`stateless: True`)、サーバ情報を応答。
- **`tools/list`**: ツール一覧（`search` ツール）を名前順でソートして返し、プロンプトキャッシュ効率を最大化。
- **`tools/call`**: 検索を実行し、全成功レスポンスに `resultType: "complete"` を付与して返却。
- **トランスポート**: stdio 上での改行区切り JSON-RPC 2.0 通信（単一リクエスト・バッチリクエスト・通知に対応）。


---

## 7. Hermes アダプタ (`bm25_search/hermes_adapter.py`)

MCP 非対応の Hermes Agent 向けアダプタ層。

- **Function Calling スキーマ**: `hermes_function_schema()` により OpenAI スタイルの Function Schema (`bm25_search`) を生成。
- **パラメータ変換 & CLI 呼び出し**: Hermes からの関数呼び出しを `search.py` 向け引数へ変換し、`subprocess` 経由で CLI を起動。
- **例外保護**: 引数不足、タイムアウト、subprocess 失敗、JSON 解凍失敗などすべてのエラーを構造化 `isError: True` レスポンスとしてキャッチし、呼び出し側へ例外を一切送出しない。

---

## 8. 参考文献 & ライセンス

- **論文**: Wang et al., *"BM25 Wins at Scale: Evaluating Agentic Search over Enterprise Corpora"* (2026) [arXiv:2607.26497](https://arxiv.org/abs/2607.26497)
- **解説記事**: 須藤英寿（株式会社ナレッジセンス）, *"BM25を使用してCodexのトークンの消費を30%抑える"* (Zenn, 2026) [https://zenn.dev/knowledgesense/articles/9e55a3bb67729c](https://zenn.dev/knowledgesense/articles/9e55a3bb67729c)
- **ライセンス**: MIT License ([LICENSE](file:///d:/vagrant/harnesses/mcp-server-bm25-code-search/LICENSE))

