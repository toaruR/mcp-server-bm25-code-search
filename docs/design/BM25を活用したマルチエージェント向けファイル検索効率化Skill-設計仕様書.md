# 設計: BM25を活用したマルチエージェント向けファイル検索効率化Skill 設計仕様書

# BM25を活用したマルチエージェント向けファイル検索効率化Skill 設計仕様書

本ドキュメントは、Claude Code, Codex, Antigravity, hermes の4つのエージェントにおけるファイル検索処理を最適化し、トークン消費量を削減するための「コード探索向けBM25 Search Skill」の設計仕様および設計煮詰め（イテレーション）のプロセスをまとめたものです。

---

## 1. 適切なゴール (Goal)

> **ゴール設定**
> **Claude Code, Codex, Antigravity, hermes の4大コーディングエージェントにおいて、コード構造（CamelCase/snake_case）および日本語技術文書に最適化したローカルBM25検索エンジンを提供し、従来のGlob/Grep主体の探索と比較して、検索セッション全体のトークン消費量を30%以上削減しつつ、ファイル到達精度と検索レスポンス速度を向上させる汎用Skill設計を確立する。**

---

## 2. テストコード以外の採点要件（4要件）と採点ルーブリック

テストコード（自動テスト）の成否以外に、本Skill設計の実現性・運用性・効果を検証・評価するための採点要件を以下の4点として定めます。

| 採点要件 | 評価内容 | 満点（5点）の条件 |
| :--- | :--- | :--- |
| **要件1: マルチエージェント標準接続性 (Multi-Agent Interoperability)** | Claude Code, Codex, Antigravity, hermes の4環境に対し、検索・索引ロジックを単一コードベースで共有しつつ接続可能か | 外部重厚ライブラリ非依存（標準ライブラリ+SQLite3ベース）。コア（索引/検索）は単一Pythonコードベースとし、MCPネイティブ対応の3エージェント（Claude Code/Codex/Antigravity）は共通の `mcp_config.json` で追加設定なしに接続、MCP非対応の hermes には薄いアダプタ層（Function Schema変換）のみを追加する |
| **要件2: コード＆日本語ハイブリッド精度 (Tokenization & Ranking Precision)** | 英語識別子（Camel/snake）と日本語文書（2-gram）、ファイルパスブーストにより必要な情報をTop-Kへ抽出できるか | サブワード分割・2-gram・パス重み付けアルゴリズムが明文化されており、1回の検索で意図する行・チャンクへ最小限のトークン数で到達可能 |
| **要件3: インデックス性能・増分更新＆Worktree非干渉 (Indexing & Worktree Sync)** | 大規模コードベースでの構築速度、変更追従性、ブランチ/Worktree並列環境での堅牢性 | 7,000〜10,000ファイル規模を数十秒以内でインデックス化し、Git HEAD追従による増分更新、および `.gitignore` 範囲内での Worktreeローカル非干渉設計が確立されている |
| **要件4: コンテキスト溢れ防止＆フォールバック (Context Budget & Graceful Fallback)** | LLMコンテキストの過剰消費防止と未ヒット時のフォールバック設計 | レスポンスサイズ上限（Max Tokens/Chars）制御と、ヒットゼロ時の標準grep/globへのフォールバック誘導プロンプトが組み込まれている |

---

## 3. 採点の合格基準 (Pass Criteria)

1. **総合スコア**: 各評価要件（各5点、計20点満点）において、**全項目4点以上** かつ **合計16点以上** を獲得すること。
2. **設計の完結性**: 各エージェント（Claude Code / Codex / Antigravity / hermes）向けの具体的呼び出し仕様・DB構造・Git/Worktree適応ロジックが一切の曖昧さなく定義されていること。
3. **煮詰めプロセスの完遂**: ブランチ切替・並列Worktree・パフォーマンス特性を含む全懸念を解消した最終仕様へ到達していること。

---

## 4. 設計仕様の詳細

### 4.1 アーキテクチャとモジュール構成
```
+-----------------------------------------------------------------------+
|                    Universal Agent Interface                          |
|  [Claude Code]    [Codex CLI]     [Antigravity]       [Hermes Agent]  |
|  (mcp.json)      (mcp.json)      (mcp_config.json)   (mcp_config.json)|
+-----------------------------------------------------------------------+
                                   |
                                   v  (Model Context Protocol / stdio - 2026-07-28 Spec Compliant)
+-----------------------------------------------------------------------+
|                    bm25_mcp_server Core Server                        |
|   - Stateless RPC & server/discover support                           |
|   - Output resultType: "complete"                                     |
|   - Deterministic Tool Sorting & Prompt Cache Friendly                |
+-----------------------------------------------------------------------+
       |                                                    |
       v                                                    v
+-----------------------+                         +---------------------+
| Tokenizer & Indexer   |                         |  Search & Snippet   |
| - Camel/snake split   |                         |  - Top-K Chunking   |
| - Japanese 2-gram     |                         |  - Relative Path    |
| - 80行/20行 overlap   |                         |  - JSON/MD Format   |
| - Git HEAD Sync       |                         |  - Max Output Bytes |
+-----------------------+                         +---------------------+
       |                                                    |
       +-------------------------+--------------------------+
                                 |
                                 v
                +---------------------------------+
                | SQLite FTS5 Database            |
                | (.bm25_index.db - Worktree Local)|
                | - code_fts (FTS5)               |
                | - chunks (Snippet info)         |
                | - file_metadata (mtime/hash/HEAD|
                +---------------------------------+
```

### 4.2 具体的な SQLite DB スキーマ定義（v2: 実装検証済み）

> **v1からの変更点**: `filepath UNINDEXED` を廃止（パス重み付けが機能しない矛盾を解消）、`code_fts` を `chunks` の External Content Table として rowid で1:1連結（チャンク粒度の曖昧さを解消）、同期用トリガーを追加。実際に SQLite (3.37, stdlib) 上で作成・検索・更新・削除まで動作確認済み。

```sql
-- チャンク本体（正規データ。80行チャンク単位で1行）
CREATE TABLE chunks (
    chunk_id INTEGER PRIMARY KEY AUTOINCREMENT,
    filepath TEXT NOT NULL,      -- ワークツリールートからの相対パス
    start_line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    raw_snippet TEXT NOT NULL,   -- 元のテキスト（エージェントへ返す表示用）
    index_text TEXT NOT NULL     -- 事前トークナイズ済みテキスト（検索用、4.3参照）
);

-- FTS5 External Content Table: rowid = chunks.chunk_id で1:1連結し、
-- どのチャンク（start_line/end_line）がヒットしたか一意に辿れる
CREATE VIRTUAL TABLE code_fts USING fts5(
    filepath,
    index_text,
    content='chunks',
    content_rowid='chunk_id',
    tokenize='unicode61 remove_diacritics 2'
);

-- chunks の変更を code_fts へ反映する同期トリガー（External Content Tableの定石）
CREATE TRIGGER chunks_ai AFTER INSERT ON chunks BEGIN
  INSERT INTO code_fts(rowid, filepath, index_text) VALUES (new.chunk_id, new.filepath, new.index_text);
END;
CREATE TRIGGER chunks_ad AFTER DELETE ON chunks BEGIN
  INSERT INTO code_fts(code_fts, rowid, filepath, index_text) VALUES ('delete', old.chunk_id, old.filepath, old.index_text);
END;
CREATE TRIGGER chunks_au AFTER UPDATE ON chunks BEGIN
  INSERT INTO code_fts(code_fts, rowid, filepath, index_text) VALUES ('delete', old.chunk_id, old.filepath, old.index_text);
  INSERT INTO code_fts(rowid, filepath, index_text) VALUES (new.chunk_id, new.filepath, new.index_text);
END;

-- 増分更新 & Git HEAD トラッキング用メタデータ管理テーブル
CREATE TABLE file_metadata (
    filepath TEXT PRIMARY KEY,
    mtime REAL NOT NULL,
    file_hash TEXT NOT NULL
);

CREATE TABLE repo_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
); -- key='last_commit_hash', value='<git_hash>'
```

ファイル変更時の更新フローは「該当 `filepath` の既存 `chunks` 行を DELETE → 再チャンク化して INSERT」のみで良い。トリガーが `code_fts` 側の追従を自動で行うため、アプリ側で FTS の整合性を個別に管理する必要はない。

### 4.3 トークナイズ & BM25スコアリング・チャンキング仕様（v2: 実装検証済み）
1. **ファイル収集と拡張子フィルタ**:
   - `git ls-files --cached --others --exclude-standard` を内部利用することで、複雑な `.gitignore`（否定パターン `!`, ネストされた `.gitignore`, 複合ワイルドカード等）をGit公式と同等に100%正確に処理。
   - 対象拡張子フィルタ: `.ts`, `.tsx`, `.js`, `.jsx`, `.mjs`, `.cjs`, `.vue`, `.svelte`, `.astro`, `.html`, `.htm`, `.hbs`, `.ejs`, `.twig`, `.php`, `.phtml`, `.blade.php`, `.css`, `.scss`, `.sass`, `.less`, `.styl`, `.py`, `.go`, `.java`, `.rs`, `.c`, `.cpp`, `.h`, `.md`, `.json`, `.yaml`, `.toml`, `.sql`
2. **固定チャンキング戦略**:
   - **80行固定ブロック / 20行オーバーラップ**。
3. **Python側での事前トークナイズ（重要な設計変更）**:
   - **なぜ必要か**: Python標準の `sqlite3` モジュールはCの拡張機構を介したFTS5カスタムトークナイザ登録ができない（要件1「外部重厚ライブラリ非依存」を満たすなら尚更）。そのため、CamelCase/snake_case分割・CJK 2-gram化は SQLite側ではなく **挿入前に Python側で完結させ、スペース区切りの `index_text` に変換してから標準の `unicode61` トークナイザに渡す**。
   - CamelCase (`getUserProfile` -> `getuserprofile`, `get`, `user`, `profile` を空白区切りで併記)
   - snake_case (`user_profile` -> `user_profile`, `user`, `profile`)
   - CJK 2-gram (「有効期限」 -> 「有効」「効期」「期限」を空白区切りで併記)
   - `raw_snippet` には変換前の元テキストを保持し、エージェントへの表示に使う。検索対象の `index_text` と表示用の `raw_snippet` を分離することで、トークナイズの都合が出力に漏れない。
4. **⚠️ 検証で判明した必須事項: クエリ側にも同じ事前トークナイズを適用する**:
   - 動作確認の過程で、生の日本語クエリ `有効期限` をそのまま `MATCH` に渡すと **ゼロヒットになるバグ**を確認した。理由は、インデックス側だけでなく `unicode61` は **クエリ文字列自体にも**適用され、区切り文字のないCJK4文字が1トークンとして扱われるため、2-gramインデックス（「有効」「効期」「期限」）とは一致しないため。
   - 対策として、検索時にユーザークエリへ同一の `pre_tokenize()` を適用し、得られたトークン列を `"有効" OR "効期" OR "期限"` のように結合してから `MATCH` へ渡す。AND結合は再現率が落ちすぎるため既定は **OR**、厳密一致が必要な場合のみ呼び出し側でANDやフレーズ検索へ切り替える。
5. **パス重み付け & BM25パラメータ（訂正）**:
   - `filepath` 列も `code_fts` に含めて検索対象化し、`bm25(code_fts, 3.0, 1.0)` で列重み（filepath=3.0, index_text=1.0）を付与。v1では `filepath UNINDEXED` としていたため、この重み付けは実際には機能しない矛盾があった。v2で解消。
   - **訂正**: SQLite FTS5 の `bm25()` は $k_1=1.2, b=0.75$ を内部固定値として使用しており、外部からパラメータ化はできない（v1の「BM25 Okapiパラメータとして設定」という記述は誤り）。設計として制御できるのは**列重みのみ**である。

### 4.4 インターフェースと出力制御 (`search.py`)
- **CLIオプション**:
  `python search.py "<query>" --top-k 5 --format [markdown|json] --max-bytes 4000`
- **コンテキスト安全制御**:
  - レスポンスは指定バイト数（デフォルト 4,000Bytes / 約1,000トークン）で自動 Truncate。
- **Zero-Match 時のフォールバック制御プロンプト**:
  ```json
  {
    "status": "zero_match",
    "message": "BM25 match zero. Recommended Fallback: Use standard grep_search or glob to locate exact symbol definitions."
  }
  ```

### 4.5 ブランチ切り替え & `git worktree` 適応設計
1. **Git リポジトリへの完全非干渉（.gitignore 設定）**:
   - `.bm25_index.db` はローカル一次キャッシュであり、`.gitignore` で完全除外。リポジトリやコミット対象へ含めない。
2. **Worktree ローカル排他配置**:
   - DBは各 Worktree ディレクトリ直下に作成し、複数エージェント/サブエージェントの並行実行時における SQLite ファイルロック競合や検索混同を回避。
3. **相対パス記録によるコンテキスト保全**:
   - 全ファイルパスは Worktree ルートからの相対パス (`src/index.ts`) で登録・返却。
4. **ブランチ切り替え検知 & 高速差分同期 (`git diff` 連動)**:
   - `repo_state` に保存された前回の `last_commit_hash` と現在の `HEAD` を比較。
   - ブランチ切替を検知した場合、`git diff --name-status <old_hash> HEAD` を用い、削除ファイル（D）のパージと変更ファイル（A/M）の再インデックスを瞬時に実行。
5. **親 Worktree からのウォームスタート機能**:
   - 新規 Worktree 作成時、親 Worktree の `.bm25_index.db` が存在する場合はそれをコピーして差分同期を行うことで、初回ビルド待ち時間を1秒未満に削減。

### 4.6 インデックス構築のライフサイクルとパフォーマンス目標
- **自動実行タイミング**:
  - `search.py` 実行時にバックグラウンドで透過的に自動構築・差分更新を実施。エージェント側の追加コマンド実行は不要。
- **初回フルビルド（Full Build）目標値**:
  - 小〜中規模（~3,000ファイル）: **2 〜 5秒**
  - 標準大規模（7,500ファイル / 70MB）: **25 〜 27秒**
- **差分更新（Incremental Sync）目標値**:
  - 通常編集時: **0.1 〜 0.5秒**
  - ブランチ切替時 (`git diff` 連動): **1 〜 3秒**

---

## 5. 設計の煮詰め（イテレーション）プロセス

### 🔄 Iteration 1: 初回基本設計の自己評価 (12 / 20点 ➔ 不合格)
- マルチエージェント接続仕様および増分更新ロジックの具体化が不足。

### 🔄 Iteration 2: 各エージェントバインディングと安全制御の補強 (19 / 20点 ➔ 合格)
- MCP, CLI, SKILL.md, Hermes Function Schema へのアダプタ層の設計、および Max Bytes 制限とフォールバック案内を導入。

### 🔄 Iteration 3: 既存プランの強みの統合 (20 / 20点 ➔ 満点達成)
- SQLスキーマ定義、80/20チャンク、JSON/Markdown対応、拡張子フィルタを取り込み。

### 🔄 Iteration 4: ブランチ切替 & Worktree並列実行環境の完全適応 (20 / 20点 ➔ 決定版到達)
- **改善点1**: ブランチ切替（`git switch`）によるゴースト検出防止用 `repo_state` (HEADハッシュ管理) と `git diff` パージロジックを追加。
- **改善点2**: `git worktree` での並列Subagent安全動作のため、相対パス管理と Worktree ローカル配置（`.gitignore` 登録）を明確化。
- **改善点3**: 初回作成・差分更新の具体的ライフサイクルと所要時間目標（7,500ファイルで25~27秒 / 差分0.1~0.5秒）を仕様に統合。

---

### 🔄 Iteration 4 の最終採点（自己採点・未検証）

- **要件1 (マルチエージェント互換性)**: 5/5点 (全4エージェントのプロトコルと `JSON/Markdown` 形式対応)
- **要件2 (コード・日本語精度)**: 5/5点 (FTS5 SQL, $k_1, b$, 80/20チャンク, 2-gram, 相対パス完全定義)
- **要件3 (インデックス性能 & Worktree適合)**: 5/5点 (Git HEAD追従, `git diff` 差分同期, Worktree排他ローカル配置)
- **要件4 (コンテキスト制限 & フォールバック)**: 5/5点 (Max Bytes Truncate + Zero-Match Fallback Msg)

**Iteration 4 総得点**: 20 / 20点

> **注記**: この採点はルーブリックの作成者自身による自己採点であり、実装や動作検証を伴っていない。満点という結果自体を設計の完成度の根拠にはできない。Iteration 5で実際にSQLiteを動かして検証した。

---

### 🔄 Iteration 5: 実装検証による是正（2026-08-12）

Iteration 4までの仕様を実際に SQLite (3.37, Python stdlib) 上に構築し、挿入・検索・更新・削除まで動作させたところ、自己採点では見つからなかった**設計と実装が矛盾する箇所**が4件見つかった。

| # | 問題 | 内容 | 対応 |
| :--- | :--- | :--- | :--- |
| 1 | `filepath UNINDEXED` とパス重み付けの矛盾 | UNINDEXED列は `bm25()` の重み付け・MATCH対象にならず、「パス一致に3.0倍」が機能しない | `filepath` を検索対象化し、External Content Table化 |
| 2 | チャンク粒度の未定義 | `code_fts` と `chunks` を結ぶキーがなく、ヒットしたチャンクの `start_line/end_line` を一意に辿れない | `code_fts` を `chunks` の External Content Table とし、rowid=`chunk_id` で1:1連結 |
| 3 | `unicode61` では CamelCase/snake_case/2-gram を分割できない | トークナイザの仕様上、区切り文字がない文字列は分割されない（日本語の連続文字は特に） | Python側で事前トークナイズし、空白区切り済みテキストを `index_text` に格納してから挿入 |
| 4 | **クエリ側の事前トークナイズ漏れ（検証で新規発見）** | 生の日本語クエリをそのまま `MATCH` に渡すとゼロヒットになることを実機確認。`unicode61`はクエリ文字列にも適用され、区切りのないCJK文字列は1トークン化されるため、2-gramインデックスと一致しない | 検索時にクエリへも同じ `pre_tokenize()` を適用し、トークンをOR結合してから `MATCH` に渡す |
| 5 | $k_1, b$ をパラメータとして設定可能と記述 | SQLite FTS5の `bm25()` は $k_1=1.2, b=0.75$ が内部固定値であり、外部からの変更はできない | 「列重みのみ制御可能」に記述を訂正 |
| 6 | 要件1「共通スキーマ・追加設定なし」と Iteration 2 の「Hermesアダプタ層」が矛盾 | MCP非対応エージェントには変換層が必須であり、"ゼロ設定"は正確ではない | 要件1の合格条件を「コアは単一コードベース、MCP非対応エージェントのみ薄いアダプタを追加」に修正 |

検証に使用したテストケース（`getUserProfile`のcamelCase分割、`session_token`のsnake_case分割、「有効期限」のCJK 2-gram、パス一致によるブースト、UPDATE/DELETEトリガーによるインデックス追従）は全て期待通りに動作することを確認した。4.2/4.3節は上記の修正を反映した v2 スキーマに更新済み。

---

### 🔄 Iteration 5 の採点（実装検証込み）

- **要件1 (マルチエージェント互換性)**: 4/5点 — コアの単一コードベース化は妥当だが、Hermes向けアダプタの具体的なFunction Schema変換仕様は未着手
- **要件2 (コード・日本語精度)**: 5/5点 — スキーマ・トークナイズパイプライン・クエリ側処理まで含め実機検証済み
- **要件3 (インデックス性能 & Worktree適合)**: 4/5点 — スキーマ・トリガーは検証済みだが、7,000〜10,000ファイル規模でのビルド時間目標値（25〜27秒等）は未計測のまま
- **要件4 (コンテキスト制限 & フォールバック)**: 4/5点 — 設計は妥当だが、バイト数とトークン数の対応（日本語はUTF-8で1文字3バイト程度）が粗く、CJK主体のファイルではMax Bytes設定の実効性を再検証すべき

**Iteration 5 総得点**: 17 / 20点（合格基準の16点はクリアするが、要件3・4は実装・計測が必要な"設計は固まったが未検証"の状態）

---

## 6. 結論

ブランチ切り替えや `git worktree` 環境での運用設計、および Iteration 5 でのスキーマ実装検証により、少なくとも SQL・トークナイズ・同期トリガーのレベルでは実際に動く設計であることを確認した。一方で、大規模リポジトリでのビルド時間・Max Bytes制御の実効性・Hermesアダプタの詳細は数値的な裏付けや実装がまだなく、次のステップは以下の3点である。

1. `search.py` / インデクサ本体をこの v2 スキーマで実装する
2. 7,000〜10,000ファイル規模の実リポジトリでビルド時間・差分更新時間を計測し、4.6節の目標値を実測値に置き換える
3. Hermesのfunction-calling仕様を確認し、アダプタ層の具体的な変換ルールを定義する
