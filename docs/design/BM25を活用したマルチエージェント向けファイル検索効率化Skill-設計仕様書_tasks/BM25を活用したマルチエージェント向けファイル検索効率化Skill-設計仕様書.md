# タスク分解（decompose 出力）

要求: BM25を活用したマルチエージェント向けファイル検索効率化Skill 設計仕様書

タスク数: 6

## 1. task_db_schema

- 目標: SQLite DBの初期化、chunksテーブル、code_fts (FTS5 external content table)、自動同期トリガー（chunks_ai, chunks_ad, chunks_au）、file_metadata、repo_stateテーブルの作成および基礎操作を実装する。
- 依存: （なし）
- 触ってよい範囲: bm25_search/db.py, tests/test_db.py
- 受入基準 (3):
  - `pytest` tests/test_db.py -k test_schema_initialization (expect_exit=0)
  - `pytest` tests/test_db.py -k test_fts_triggers_sync (expect_exit=0)
  - `pytest` tests/test_db.py -k test_metadata_tables (expect_exit=0)
- 採点基準 (rubric, 合格ライン: 80点):
  - acceptance のテストファイル・アサーションを一切変更していない (配点: 30)
  - touch_allow の範囲外のファイルに一切変更を加えていない (配点: 20)
  - chunks テーブル更新時に code_fts トリガーが正常動作し、FTSインデックスの整合性が保たれている (配点: 25)
  - 外部重厚ライブラリに依存せず、Python標準ライブラリの sqlite3 モジュールのみで構築されている (配点: 25)

## 2. task_tokenizer

- 目標: コード識別子（CamelCase, snake_case）のサブワード分割、日本語文書の 2-gram 化を行う事前トークナイズ処理 (pre_tokenize) と、クエリ用の OR 結合トークン生成処理 (pre_tokenize_query) を実装する。
- 依存: （なし）
- 触ってよい範囲: bm25_search/tokenizer.py, tests/test_tokenizer.py
- 受入基準 (3):
  - `pytest` tests/test_tokenizer.py -k test_camel_case_and_snake_case_splitting (expect_exit=0)
  - `pytest` tests/test_tokenizer.py -k test_cjk_2gram_tokenization (expect_exit=0)
  - `pytest` tests/test_tokenizer.py -k test_query_pre_tokenization_or_join (expect_exit=0)
- 採点基準 (rubric, 合格ライン: 80点):
  - acceptance のテストファイル・アサーションを一切変更していない (配点: 30)
  - touch_allow の範囲外のファイルに一切変更を加えていない (配点: 20)
  - CamelCase, snake_case, 日本語2-gramが混在した文字列でも正確にトークン分割が行われる (配点: 25)
  - クエリ事前処理で生成される OR 結合トークン列の構文が FTS5 MATCH 条件として正常に機能する (配点: 25)

## 3. task_indexer

- 目標: git ls-files を用いた対象ファイル収集と拡張子フィルタリング、80行/20行オーバーラップチャンキング、git diff と HEAD トラッキングによる増分更新、Worktree コピー（ウォームスタート）処理を実装する。
- 依存: task_db_schema, task_tokenizer
- 触ってよい範囲: bm25_search/indexer.py, tests/test_indexer.py
- 受入基準 (4):
  - `pytest` tests/test_indexer.py -k test_git_file_collection_and_extension_filtering (expect_exit=0)
  - `pytest` tests/test_indexer.py -k test_80_20_chunking_overlap (expect_exit=0)
  - `pytest` tests/test_indexer.py -k test_incremental_indexing_with_git_diff (expect_exit=0)
  - `pytest` tests/test_indexer.py -k test_worktree_warmstart_copy (expect_exit=0)
- 採点基準 (rubric, 合格ライン: 80点):
  - acceptance のテストファイル・アサーションを一切変更していない (配点: 25)
  - touch_allow の範囲外のファイルに一切変更を加えていない (配点: 20)
  - .gitignore 規則および指定拡張子フィルタリングが git 公式と同等に機能している (配点: 20)
  - 80行固定ブロック / 20行オーバーラップの計算が正確で、余り行も適切に処理される (配点: 15)
  - ブランチ切り替え時に git diff に基づく削除・追加ファイルの増分更新が正しく実行される (配点: 20)

## 4. task_search

- 目標: SQLite FTS5 BM25 検索実行（filepath 列重み 3.0）、CLI インターフェース、Max Bytes 自動 Truncate、Zero-Match 時のフォールバックレスポンス生成を実装する。
- 依存: task_indexer
- 触ってよい範囲: bm25_search/search.py, tests/test_search.py
- 受入基準 (4):
  - `pytest` tests/test_search.py -k test_bm25_search_scoring_and_path_boost (expect_exit=0)
  - `pytest` tests/test_search.py -k test_max_bytes_truncation (expect_exit=0)
  - `pytest` tests/test_search.py -k test_zero_match_fallback_response (expect_exit=0)
  - `pytest` tests/test_search.py -k test_cli_output_formats (expect_exit=0)
- 採点基準 (rubric, 合格ライン: 80点):
  - acceptance のテストファイル・アサーションを一切変更していない (配点: 25)
  - touch_allow の範囲外のファイルに一切変更を加えていない (配点: 20)
  - 検索クエリに pre_tokenize_query が適用され、日本語クエリでも正常にヒットする (配点: 20)
  - max-bytes 超過時にマルチバイト文字を破損させず安全に出力が切り捨てられる (配点: 20)
  - ヒット数ゼロ時に仕様書通りの grep/glob フォールバック誘導メッセージを返却する (配点: 15)

## 5. task_mcp_server

- 目標: Model Context Protocol (stdio / 2026-07-28 Spec) に準拠した MCP サーバーを実装し、Stateless RPC、server/discover サポート、resultType: complete 出力、決定論的ツール整列機能を提供する。
- 依存: task_search
- 触ってよい範囲: bm25_search/mcp_server.py, tests/test_mcp_server.py
- 受入基準 (3):
  - `pytest` tests/test_mcp_server.py -k test_mcp_discover_and_tools_list (expect_exit=0)
  - `pytest` tests/test_mcp_server.py -k test_mcp_search_rpc_execution (expect_exit=0)
  - `pytest` tests/test_mcp_server.py -k test_mcp_output_result_type (expect_exit=0)
- 採点基準 (rubric, 合格ライン: 80点):
  - acceptance のテストファイル・アサーションを一切変更していない (配点: 30)
  - touch_allow の範囲外のファイルに一切変更を加えていない (配点: 20)
  - MCP 仕様に従った Stdio JSON-RPC リクエスト/レスポンスの処理が正常に行われる (配点: 25)
  - プロンプトキャッシュ効率化のためツール定義の deterministic sort（決定論的並び替え）が適用されている (配点: 25)

## 6. task_hermes_adapter

- 目標: MCP 非対応の Hermes Agent 向けに、Function Calling スキーマ変換および CLI/Stdio 呼び出しを仲介する薄いアダプター層を実装する。
- 依存: task_search
- 触ってよい範囲: bm25_search/hermes_adapter.py, tests/test_hermes_adapter.py
- 受入基準 (2):
  - `pytest` tests/test_hermes_adapter.py -k test_hermes_function_schema_conversion (expect_exit=0)
  - `pytest` tests/test_hermes_adapter.py -k test_hermes_cli_invocation_wrapping (expect_exit=0)
- 採点基準 (rubric, 合格ライン: 80点):
  - acceptance のテストファイル・アサーションを一切変更していない (配点: 30)
  - touch_allow の範囲外のファイルに一切変更を加えていない (配点: 20)
  - Hermes Agent の Function Call 形式を search.py のパラメータへ正しくマッピングできる (配点: 25)
  - アダプター経由の検索呼び出しおよび結果フォーマット変換において例外が発生しない (配点: 25)

