# セッション検索（Session Recall）の実用性評価レポート

- **調査日**: 2026-10-17
- **評価対象リポジトリ**: `D:\vagrant\harnesses\mcp-server-bm25-code-search`
- **対象DB**: リポジトリ直下 `.bm25_sessions.db`（13 sessions / 946 messages / 962 chunks）
- **評価手法**: PowerShell CLI (`bm25_search.sessions.cli`) による検索および原文参照

---

## 1. 評価概要と目的

本調査は、Claude Code、Codex、Antigravity（agy）、Cursorの過去の会話履歴をSQLite FTS5（BM25）で検索・参照する機能（Session Recall）について、**「実際の開発業務で役に立つか」** を実測データに基づき判定することを目的とする。

### 評価条件
1. 開発者が実際に後から知りたくなる質問（設計理由、エラー対処、他エージェントの作業内容など）を8問作成。
2. 少なくとも2問は、既存の記録（`git log`、`docs/`、`CLAUDE.md`）にはなく、**セッション検索にしか答えがない内容**とする。
3. 少なくとも2問は、**Codex や Antigravity（agy）での作業内容**について聞く（エージェント間引き継ぎの評価）。
4. 各質問について最大2通りのクエリを試行し、上位5件を分類（正解を含む / 関連はあるが不十分 / ノイズ）。
5. 正解候補は `read` コマンドで原文を確認し、回答が得られているか検証。既存記録（git log / docs / CLAUDE.md）との重複有無を確認。

---

## 2. 質問一覧と測定結果

### Q1. セッションDBの保存先選定における議論経緯（Codex / 設計意図）

- **質問**: セッションDBの保存先について、当初提案された「ユーザーや権限プロファイルごとの分離」をやめて「プロジェクトルート直下（`<root>/.bm25_sessions.db`）」に置くことにした議論の経緯・理由は何か？
- **背景**: `docs/session-recall.md` や `docs/plans/session-recall-design.md` には最終仕様として「プロジェクトルートに置く」「OS権限に委ねる」とあるが、当初の検討から変更された理由を把握したい。
- **対象エージェント**: Codex (`01a14509-f049-7d31-97a3-b0258c8032ef`)

#### クエリ試行
- **クエリ1**: `セッション DB プロジェクトルート 保存先`
  1. **関連はあるが不十分** (`s_f9ded886...` - grep実行結果ログ。設計書内の「方針（2026-10-16改訂）」記述がヒットしているが、議論の経緯そのものは含まれない)
  2. **ノイズ: ツール呼び出し・差分コード** (`s_f9ded886...` - `apply_patch` の差分文字列)
  3. **ノイズ: 他セッションのコミット時diff** (`s_82f23075...` - README.mdの更新diff)
  4. **ノイズ: システム自動記録** (`s_72715b4c...` - Codex agent history の評価判定)
  5. **ノイズ: 他セッションのコミット時diff** (`s_82f23075...` - CLAUDE.md のハマりポイント追記diff)
- **クエリ2（言い換え）**: `DBプロファイル 過剰設計 プロジェクトルート`
  1. **正解を含む** (`s_f9ded886...` - アシスタント「今回はその分離は過剰設計で、既存と同じくプロジェクトルートに置く方が素直です...」)
  2. **正解を含む** (`s_72715b4c...` - Codex agent history内に引用された同一メッセージ)
  3. **関連はあるが不十分** (`s_f9ded886...` - 設計書の記述修正に関するアシスタント発話)
  4. **ノイズ: システム自動記録** (`s_72715b4c...` - Codex agent history の評価判定)
  5. **ノイズ: 他の話題** (`s_37daaec0...` - README英語化時のgit log出力)

#### 原文確認（`read`）
- **session_id**: `s_f9ded88679c69d71e0a0ce7a05d848a939178045f940ad83454d4e641cff1060`
- **anchor**: `eyJ2IjoxLCJzaWQiOiJzX2Y5ZGVkODg2NzljNjlkNzFlMGEwY2U3YTA1ZDg0OGE5MzkxNzgwNDVmOTQwYWQ4MzQ1NGQ0ZTY0MWNmZjEwNjAiLCJtaWQiOiJtXzA5NzIyMTUzZTFiNTVkODNlNzhkMmZiZTc2OGZjMWQ1MGI1YWMyNWE3YTk3MGI3ZWFjYTNlYWZlN2EzNjBjMTMiLCJoYXNoIjoiZWM3N2I1Y2MwZTVlZGYxM2YzOTdmMzliYTU0ZmRkMjI0MWNjZWVjNmY0ZGMwMzU1NGVhOTU3ZDUzY2IzZWIyMSIsInN0YXJ0IjowLCJlbmQiOjI4NX0`
- **確認できた回答**: ユーザーが「『異なるOSユーザー、組織、権限、機密区分では別のDBプロファイルに分離する』これはやり過ぎで、既存のbm25検索のdbはプロジェクトルートにあるので、プロジェクトルートで良いのでは？どう思う。忖度抜きで」と指摘。アシスタントが「未要求のプロファイル分離やworktree共有まで先回りして複雑にしすぎた過剰設計であった」と認め、プロジェクトルート直下配置に合意した経緯が確認できた。
- **既存の記録で足りるか**: **セッション検索にしか答えがない**（`docs/` には決定後の仕様文しかなく、ユーザーの指摘やアシスタントの反省・意思決定の理由はセッションのみに存在）。

---

### Q2. コード検索指示方針でHook/Skill化を避けた理由（agy / 意思決定）

- **質問**: CLAUDE.md / AGENTS.md のコード検索方針で、Hookによる強制やSkill化ではなく「3行の極小プロンプトルール」に留めた理由は何か？
- **背景**: 検索手順をSkill化したりHookで強制するアプローチも考えられるが、なぜ現在のシンプルなプロンプト形式が選ばれたのか。
- **対象エージェント**: agy (`eefa5656-217d-4345-96d1-d7e4a5bee61b`)

#### クエリ試行
- **クエリ1**: `hook skill コード検索 トークン削減`
  1. **正解を含む** (`s_40d0bc9e...` - アシスタント「目的が『トークン消費の削減』であれば、本末転倒（指示文やSkillの読み込み自体でトークンを食うこと）を避けるのが最優先ですね！...」)
  2. **関連はあるが不十分** (`s_a38f65c5...` - トークン削減効果のローカル計測手法の説明)
  3. **正解を含む** (`s_40d0bc9e...` - ユーザー「トークンを削減するのが目的なので、本末転倒は避けたい」)
  4. **ノイズ: ツール実行結果** (`s_834288f4...` - `view_file` で閲覧した設計書の中身)
  5. **ノイズ: ツール呼び出し** (`s_37daaec0...` - `replace_file_content` の引数JSON)

#### 原文確認（`read`）
- **session_id**: `s_40d0bc9e3933461d03a327b6cc865bbe85cb735106a4393d87f5d95200572871`
- **anchor**: `eyJ2IjoxLCJzaWQiOiJzXzQwZDBiYzllMzkzMzQ2MWQwM2EzMjdiNmNjODY1YmJlODVjYjczNTEwNmE0MzkzZDg3ZjVkOTUyMDA1NzI4NzEiLCJtaWQiOiJtXzhmNDIzNjJjMjRkNDdlOTBlY2Q0MDUyMDBhNmZjMzIzMmJjZmJiZGI3ZDEzMmQ0ZjU1YzRlNmRkNzRkOTE3YjUiLCJoYXNoIjoiNzQxMmY1NTgxNDJjOGQwMWEyYTMwZTgzMzYzYmZkMjI0YWQ3YWJjNDYzZThiMWJlNWQ1YmE5YWE2Yzg2YjhjNyIsInN0YXJ0IjowLCJlbmQiOjEyMTV9`
- **確認できた回答**: Skill化するとSkillの読み込み自体で毎回500〜1,000トークン消費してしまうため、トークン削減という本来の目的において本末転倒になる。一方、3行のルールであれば約80トークンで済み、かつAIによる不要なファイル丸ごと読み（数千トークン）を確実に阻止できるため、最も費用対効果が高いと結論づけられた。
- **既存の記録で足りるか**: **セッション検索にしか答えがない**（CLAUDE.md には指示ルールのみが書かれており、SkillやHookを却下したトークン効率の議論は一切残っていない）。

---

### Q3. commit 786a260 適用時のローカルPC環境への影響確認（Claude Code / 影響調査）

- **質問**: commit 786a260（スクリプト起動時の indexer import 修正）を適用した際、ローカルの `.mcp.json` や環境への影響についてどのような結論になったか？
- **背景**: スクリプト起動時の `sys.path` 修正が、ローカルで動いているMCPクライアントや設定に悪影響を及ぼさないか事前に検討された内容を知りたい。
- **対象エージェント**: Claude Code (`8750900b-6ebd-43e2-a834-e131a69dfd72`)

#### クエリ試行
- **クエリ1**: `786a260 mcp.json indexer import`
  1. **ノイズ: ツール呼び出し** (`s_89bf2e31...` - Bash `git show 786a260:bm25_search/mcp_server.py ...` ※ただし `read` で直後の回答に到達可能)
  2. **ノイズ: ツール呼び出し** (`s_89bf2e31...` - Bash `git show 786a260 --stat ...`)
  3. **ノイズ: ツール呼び出し** (`s_283b4ce2...` - `replace_file_content` の引数JSON)
  4. **ノイズ: ツール実行結果** (`s_283b4ce2...` - `replace_file_content` の実行結果)
  5. **ノイズ: ツール実行結果** (`s_f9ded886...` - スクリプト実行結果ログ)
- **クエリ2（言い換え）**: `786a260 ローカル 問題なし 影響`
  1. **正解を含む** (`s_89bf2e31...` - アシスタント「問題なし。ローカルの [.mcp.json](.mcp.json) の場合: スクリプトは `D:/vagrant/harnesses/simple/...` で `sys.path` に入るパスは `PYTHONPATH` と同じ。今の挙動は何も変わらない...」)
  2. **正解を含む** (`s_89bf2e31...` - ユーザー「これを適用しても、ローカルＰＣでの利用に問題は発生しない？」)
  3. **ノイズ: 他の話題** (`s_37daaec0...` - README多言語化のコミットメッセージ案)
  4. **ノイズ: ツール呼び出し** (`s_834288f4...` - `write_to_file` README.md)
  5. **ノイズ: 他の話題** (`s_82f23075...` - smart-commit メッセージ案)

#### 原文確認（`read`）
- **session_id**: `s_89bf2e31caf3525f12fae99f5602926095a2626e28bc3a1cefc0c21208e7d43c`
- **anchor**: `eyJ2IjoxLCJzaWQiOiJzXzg5YmYyZTMxY2FmMzUyNWYxMmZhZTk5ZjU2MDI5MjYwOTVhMjYyNmUyOGJjM2ExY2VmYzBjMjEyMDhlN2Q0M2MiLCJtaWQiOiJtXzMyNjc0NzAyYmMxMTcwMmMzNTYwOTQzODNkNTEyNWM4YWI2YmMxZDExNDQ3OGUzNzBiMThiY2JmMmZiOTcwNGMiLCJoYXNoIjoiNzVkNTA3MGY1MWNkNGU0MmM4YWMxNjc1Yzc2MjNlOTVhZjEyZWYyNzJmMzgxOGYyYzJhNmM2MzdkN2QxOTlkZSIsInN0YXJ0IjowLCJlbmQiOjcwM30`
- **確認できた回答**: 「問題なし」。修正によって追加されるパスは `.mcp.json` に設定されている `PYTHONPATH` と同等であるため既存の挙動は変わらない。ただし「MCPサーバーは `simple/` 側から起動されているため、本リポジトリに適用するだけでは動作は変わらず、`simple/` 側にも反映が必要」という実務上の重大な注意点が示されていた。
- **既存の記録で足りるか**: **セッション検索にしか答えがない**（コミット 786a260 のログには不具合修正の概要しかなく、ローカル固有の `.mcp.json` への影響や `simple/` コピー側への反映要件は記録されていない）。

---

### Q4. MCPサーバー起動時の initialize エラー対処（agy / エラー解決）

- **質問**: MCPサーバー起動時や接続時に `Method not found: 'initialize'` エラーが出た原因と対処法は何か？
- **背景**: サーバー起動時に発生したハンドシェイクエラーの原因と、どのように修正されたかを確認したい。
- **対象エージェント**: agy (`073edd8c-7209-424a-a7d1-e6aea23bb540`)

#### クエリ試行
- **クエリ1**: `initialize Method not found エラー`
  1. **ノイズ: ツール呼び出し** (`s_a38f65c5...` - `replace_file_content` CLAUDE.mdへのハマりポイント追記)
  2. **ノイズ: ツール実行結果** (`s_a38f65c5...` - `replace_file_content` のdiff出力)
  3. **ノイズ: ツール実行結果** (`s_283b4ce2...` - `view_file` CLAUDE.md)
  4. **ノイズ: ツール呼び出し** (`s_82f23075...` - Bash JSON-RPC 疎通確認スクリプト)
  5. **関連はあるが不十分** (`s_283b4ce2...` - 別エラー `context deadline exceeded` の原因解説)
- **クエリ2（言い換え）**: `Method not found initialize 原因`
  1. **正解を含む** (`s_a38f65c5...` - アシスタント「発生していたエラー `Method not found: 'initialize'` の原因と修正内容は以下の通りです...」)
  2. **関連はあるが不十分** (`s_a38f65c5...` - ユーザーによるエラー文字列の提示)
  3. **ノイズ: ツール実行結果** (`s_283b4ce2...` - `view_file` CLAUDE.md)
  4. **ノイズ: ツール呼び出し** (`s_82f23075...` - Bash JSON-RPC 疎通確認スクリプト)
  5. **ノイズ: ツール呼び出し** (`s_dec29ab7...` - Bash try_sessions.py 作成スクリプト)

#### 原文確認（`read`）
- **session_id**: `s_a38f65c59fb933751860c899b4f22f757496717493a9bc191242151389d39719`
- **anchor**: `eyJ2IjoxLCJzaWQiOiJzX2EzOGY2NWM1OWZiOTMzNzUxODYwYzg5OWI0ZjIyZjc1NzQ5NjcxNzQ5M2E5YmMxOTEyNDIxNTEzODlkMzk3MTkiLCJtaWQiOiJtXzEwZmY0N2ZiNzUzODFiN2U2YTBhYTU5NGM4ZGUxNmM1ZjE4NGVkOThkMjY0NGIwZjQxOGRiMDZiNjM3OGJkMDMiLCJoYXNoIjoiY2U1MmYyNWVlYzliMWI2MjBlMTI3MjhlYjA3ZjQ5ZWMwMzNhNGE1ZjgzY2Q5NWEyMDBjZjIzNjQ1NTczODJlYSIsInN0YXJ0IjowLCJlbmQiOjkyM30`
- **確認できた回答**: 多くのMCPクライアントは初期化ハンドシェイクとして `initialize` メソッドを送るが、本サーバーがステートレスMCP仕様に準拠して構築されていたため未定義だった。`initialize` / `initialized` / `ping` の互換ハンドラを `mcp_server.py` に追加して解決。
- **既存の記録で足りるか**: **既存の記録で足りる**（commit 46658a5 のコミットメッセージ「fix(mcp_server): MCP 2024-11-05 互換の initialize/ping ハンドラを追加」や当時のCLAUDE.mdに記載あり）。

---

### Q5. ツール実行時のSubmitボタン・承認ダイアログ抑制（agy / 設定ノウハウ）

- **質問**: Antigravity等のMCPクライアントで `search` ツール実行時に毎回出るSubmit/承認ダイアログを出なくする（自動承認する）にはどう設定すればよいか？
- **背景**: 検索が実行されるたびに確認ボタンを押す手間を省くための設定方法を確認したい。
- **対象エージェント**: agy (`073edd8c-7209-424a-a7d1-e6aea23bb540`)

#### クエリ試行
- **クエリ1**: `search Submit 承認 ボタン 自動許可 alwaysAllow`
  1. **正解を含む** (`s_a38f65c5...` - アシスタント「デフォルトでは、MCP クライアント（Antigravity IDE や Claude Code など）の安全機能により、MCP ツール（`search`）が呼び出されるたびに承認・実行確認ダイアログ（Submit / Allow ボタン）が表示される場合があります...」)
  2. **正解を含む** (`s_a38f65c5...` - アシスタント「画像をお見せいただきありがとうございます！結論から言うと、現在の状態だけでは承認ボタン（Submitボタン）が出る可能性があります...」)
  3. **ノイズ: ツール呼び出し** (`s_283b4ce2...` - `replace_file_content` README.md)
  4. **ノイズ: ツール実行結果** (`s_283b4ce2...` - `replace_file_content` の実行結果)
  5. **ノイズ: 他セッションのコミット時diff** (`s_82f23075...` - README.md の起動オプション説明)

#### 原文確認（`read`）
- **session_id**: `s_a38f65c59fb933751860c899b4f22f757496717493a9bc191242151389d39719`
- **anchor**: `eyJ2IjoxLCJzaWQiOiJzX2EzOGY2NWM1OWZiOTMzNzUxODYwYzg5OWI0ZjIyZjc1NzQ5NjcxNzQ5M2E5YmMxOTEyNDIxNTEzODlkMzk3MTkiLCJtaWQiOiJtX2UzYzVjZjdkZTc2OWMxNDgyYjNiNGQyZjFhNjY4MTI1ZWEzMWRmMTA4MjlhOTRlMTg3Mjc3M2ZiMTc3NTI2OTkiLCJoYXNoIjoiODVjODM4OWZlZTU1MmE5ZDMxMTNlYWQ5Nzk2YTNiZGY0YWI0YjVmOTk5OTkyNTYxODg0ZjlkMjJhZjRkYTVlYSIsInN0YXJ0IjowLCJlbmQiOjc2NH0`
- **確認できた回答**: MCP設定ファイル（`mcp_config.json`）のサーバー定義内に `"alwaysAllow": ["search"]`（または `"autoApprove"`）を追加することで、実行時の確認ダイアログをスキップ可能。
- **既存の記録で足りるか**: **既存の記録で足りる**（README.md の「自動承認 (`alwaysAllow`) の設定例」に記載あり）。

---

### Q6. セッション検索機能設計時の参考記事（Codex / 参考資料）

- **質問**: セッション検索機能（session recall）の設計時に参考にされた外部のZenn記事やGitHubリポジトリは何だったか？
- **背景**: アーキテクチャ設計時にどのような外部実装や知見が参考にされたかを追跡したい。
- **対象エージェント**: Codex (`01a14509-f049-7d31-97a3-b0258c8032ef`)

#### クエリ試行
- **クエリ1**: `session recall 参考記事 Zenn`
  1. **ノイズ: ツール呼び出し** (`s_a38f65c5...` - 別件のZenn記事・arXiv論文追記コミットのコマンド)
  2. **ノイズ: 他セッションのアシスタント発話** (`s_a38f65c5...` - 別件のZenn記事追記完了報告)
  3. **ノイズ: ツール実行結果** (`s_283b4ce2...` - `view_file` README.md)
  4. **ノイズ: ツール実行結果** (`s_37daaec0...` - `view_file` README.md 内の参考文献節)
  5. **正解を含む** (`s_f9ded886...` - ユーザー「[https://zenn.dev/babarot/articles/claude-code-session-recall](https://zenn.dev/babarot/articles/claude-code-session-recall) を参考に...」)
- **クエリ2（言い換え）**: `babarot claude-code-session-recall`
  1. **ノイズ: ツール呼び出し** (`s_f9ded886...` - exec Get-Content CLAUDE.md / rg MEMORY.md)
  2. **正解を含む** (`s_f9ded886...` - ユーザー発話「https://zenn.dev/babarot/articles/claude-code-session-recall を参考に。そのエージェントの過去セッションをbm25検索して、知見を活用するツールを追加したい...」)
  3. **ノイズ: システム自動記録** (`s_d10210b9...` - Codex agent history の評価判定)
  4. **ノイズ: システム自動記録** (`s_72715b4c...` - Codex agent history の評価判定)
  5. **ノイズ: ツール呼び出し** (`s_82f23075...` - Bash ディレクトリ作成・sed置換コマンド)

#### 原文確認（`read`）
- **session_id**: `s_f9ded88679c69d71e0a0ce7a05d848a939178045f940ad83454d4e641cff1060`
- **anchor**: `eyJ2IjoxLCJzaWQiOiJzX2Y5ZGVkODg2NzljNjlkNzFlMGEwY2U3YTA1ZDg0OGE5MzkxNzgwNDVmOTQwYWQ4MzQ1NGQ0ZTY0MWNmZjEwNjAiLCJtaWQiOiJtXzg1MjFmMzZlNmMyYTMxYzA1NjUwOTZiZmNkZDM5ZTAyMDY5OGFmYWZhNzg5ZDhhNDQzNmMwMzhlOGQ3NDEyZDQiLCJoYXNoIjoiOWE5OTJkMmUxNjllMjI0YjVmY2E3NmUzMTI4YWI2ODNkYThkNzI5NjA4OGFlMTA0ZGY4M2I1NjI5ZTgxMmE3YyIsInN0YXJ0IjowLCJlbmQiOjI5M30`
- **確認できた回答**: babarot 氏の Zenn 記事 `https://zenn.dev/babarot/articles/claude-code-session-recall` が提示され、過去セッションのBM25検索・知見活用の着想元となったことが確認できた。
- **既存の記録で足りるか**: **既存の記録で足りる**（`docs/plans/session-recall-design.md` の参考資料節に当該URLおよび `claude-recall` リポジトリへの言及あり）。

---

### Q7. 未登録ソース時のsync挙動に関する仕様変遷（Claude Code / 仕様変更）

- **質問**: セッション検索の初期設計では、入力元（sources）が未登録の状態で `sync` を実行したときの挙動はどう設計されていたか？（自動検出だったか登録必須だったか）
- **背景**: 現行仕様では既定保存領域の実在チェックにより自動使用されるが、初期実装時の動作と仕様変更の経緯を確認したい。
- **対象エージェント**: Claude Code (`2dbf0e40-d341-4adb-94bf-dad6e71120d8`)

#### クエリ試行
- **クエリ1**: `sources 未登録 登録必須 sync`
  1. **ノイズ: ツール呼び出し** (`s_dec29ab7...` - Bash scratchpad/readme_ja.py 作成スクリプト)
  2. **関連はあるが不十分** (`s_dec29ab7...` - commit 7418591 のコミットコマンドログ)
  3. **関連はあるが不十分** (`s_82f23075...` - docs/session-recall.md の本文)
  4. **ノイズ: ツール呼び出し** (`s_82f23075...` - Edit README.ja.md)
  5. **ノイズ: ツール実行結果** (`s_d10210b9...` - Codex のシンボル一覧ログ)
- **クエリ2（言い換え）**: `source add 登録は必須 sources 初期値`
  1. **正解を含む** (`s_dec29ab7...` - アシスタント「ほぼ本当です。補足が2点あります。登録は必須: 読み込み元の初期値は `\"sources\": []`（config.py:14）で、自動検出はしません。設定はプロジェクトルートの `.bm25_sessions.json` に保存されるので、登録はプロジェクトごとに必要です...」)
  2. **ノイズ: ツール呼び出し** (`s_dec29ab7...` - Bash scratchpad/readme_ja.py 作成スクリプト)
  3. **関連はあるが不十分** (`s_82f23075...` - docs/session-recall.md の本文)
  4. **ノイズ: ツール実行結果** (`s_f9ded886...` - スクリプトエラー出力)
  5. **ノイズ: ツール実行結果** (`s_f9ded886...` - grep 出力)

#### 原文確認（`read`）
- **session_id**: `s_dec29ab781ecd0f46554c2cae4d220d2dcf4532d79496acb2c8dafcc4b580f37`
- **anchor**: `eyJ2IjoxLCJzaWQiOiJzX2RlYzI5YWI3ODFlY2QwZjQ2NTU0YzJjYWU0ZDIyMGQyZGNmNDUzMmQ3OTQ5NmFjYjJjOGRhZmNjNGI1ODBmMzciLCJtaWQiOiJtXzNjNjIwZTI3MGJmNjY1M2MyNTdlNDhkMDAxOTZkMjU1ZTlmMjQyYjI3YTgzYjgwOGYwZDkzYTc0MzlkY2UwZWYiLCJoYXNoIjoiOTZjZDYwM2ZiODk3OTFhYTYxZDgxZGEwZTY1NTNkZjMwYTA2ODQ1MWRhMjI0MmJlMDdhMDc0ZThmYjE1MTZmZCIsInN0YXJ0IjowLCJlbmQiOjUwN30`
- **確認できた回答**: 初期実装では自動検出は行わず、`"sources": []` が初期値で、プロジェクトごとに `source add` で明示登録しないと同期対象が0件になる仕様だった。直後にユーザーから「これを自動化できないかな？」と提案され、未登録時の既定ソース自動使用（commit 7418591）が導入された。
- **既存の記録で足りるか**: **既存の記録で足りる**（commit 7418591 のメッセージ「feat: セッション検索で未登録時に既定の入力元を自動使用」や `docs/session-recall.md` に記載あり）。

---

### Q8. トークン削減効果の費用ゼロ計測手法（agy / 計測ノウハウ）

- **質問**: BM25 Code Search によるトークン削減効果を、有料APIを呼ぶことなく低コスト（費用ゼロ）で計測するにはどうすればよいか？
- **背景**: プラグイン導入によるトークン節約効果を客観的に検証するために、過去にどのような検証が行われたかを知りたい。
- **対象エージェント**: agy (`073edd8c-7209-424a-a7d1-e6aea23bb540`)

#### クエリ試行
- **クエリ1**: `トークン削減 計測 費用ゼロ スクリプト`
  1. **正解を含む** (`s_a38f65c5...` - アシスタント「API呼出（有料）を一切使わず、ローカルの Python スクリプトでトークン数をカウント比較する方法（費用完全ゼロ）で計測できます。原理: パターンA(フル投入)とパターンB(BM25検索)...」)
  2. **関連はあるが不十分** (`s_40d0bc9e...` - トークン削減に特化した最少プロンプトの提案)
  3. **ノイズ: 他の話題** (`s_e59dbbbf...` - smart-commit によるスキーマ検証スクリプトコミット案)
  4. **ノイズ: ツール実行結果** (`s_e59dbbbf...` - git commit コマンド実行結果)
  5. **ノイズ: 他の話題** (`s_89bf2e31...` - commit 786a260 の影響確認)

#### 原文確認（`read`）
- **session_id**: `s_a38f65c59fb933751860c899b4f22f757496717493a9bc191242151389d39719`
- **anchor**: `eyJ2IjoxLCJzaWQiOiJzX2EzOGY2NWM1OWZiOTMzNzUxODYwYzg5OWI0ZjIyZjc1NzQ5NjcxNzQ5M2E5YmMxOTEyNDIxNTEzODlkMzk3MTkiLCJtaWQiOiJtXzhlNzUxYzI2NjAyMDJjZWY4YzlhMDVhYTM0ZDBlZGYzM2VjNWVhYTE0OWQ0ZmY2NjNjMjIxMGJlOTczYTkwNmMiLCJoYXNoIjoiZTg4NzUzMGJjN2U0MGVlMzU2Yjc1ZGY2OWMxZmUzYmZmOGMzY2VhYWQ2MTQxOTA4Y2Y1OGUzOGZjMTg1ZjJjMSIsInN0YXJ0IjowLCJlbmQiOjExNjV9`
- **確認できた回答**: リポジトリ全体の全ファイル・チャンク文字数（パターンA）と、BM25検索上位k件のチャンク文字数（パターンB）をローカルPythonスクリプトで計算・比較する手法が提示された（1 token ≒ 3.5〜4文字）。本リポジトリでの実測値として、全コード約63,276トークンに対し、BM25検索結果（top_k=5）が約3,300〜4,500トークンとなり、**92.78%〜94.74%の削減効果**が確認されたことが記録されている。
- **既存の記録で足りるか**: **セッション検索にしか答えがない**（この具体的なゼロコスト計測アプローチや93〜95%削減という実測ベンチマーク数値は、docsやコミットログには掲載されていない）。

---

## 3. 全体集計

### 1. 上位3件以内に正解が出た質問の数
- **8 / 8 問 (100%)**
  - 初回クエリで上位3件以内に正解: **3問** (Q2, Q5, Q8)
  - 言い換え（2回目のクエリ）で上位3件以内に正解: **5問** (Q1, Q3, Q4, Q6, Q7)
  - ※最大2回の試行により、全8問ですべて上位1位または2位に正解を引き当てることができた。

### 2. そのうち「既存の記録では答えが分からなかった」質問の数
- **4 / 8 問 (50%)**
  - **Q1**: セッションDB保存先選定における「過剰設計」指摘とプロファイル分離撤回の議論経緯
  - **Q2**: コード検索方針でHook/Skill化を避け「3行ルール」にした理由（Skill読み込みによるトークン消費の本末転倒防止）
  - **Q3**: commit 786a260 適用時のローカルPC環境・`.mcp.json` への影響確認と注意点（`simple/` 側への反映必要性）
  - **Q8**: トークン削減効果をローカルPythonスクリプトで費用ゼロ計測した手順・実測値

### 3. ノイズの種類ごとの件数
全13回の検索試行における上位5件（計65件）の分類内訳：

| 分類 | 件数 | 割合 | 主な具体例 |
|---|---|---|---|
| **正解を含む** | 15件 | 23.1% | ユーザーまたはアシスタントの直接的な回答メッセージ本文 |
| **関連はあるが不十分** | 8件 | 12.3% | grep結果ログ、ドキュメントの該当節、コミット直前ログなど |
| **ノイズ** | **42件** | **64.6%** | 以下に内訳を詳述 |

#### ノイズの内訳（計42件）
1. **ツール呼び出し・コマンド実行・差分コードの羅列**: **19件 (45.2%)**
   - `replace_file_content` の引数JSON、`Bash` のコマンド文字列、`apply_patch` の差分文字列など。検索クエリに含まれる英単語やファイル名が一致して上位に出てしまう。
2. **ツール実行結果・ログ出力**: **11件 (26.2%)**
   - `view_file` や `git show` の実行結果、テスト結果、スクリプト実行ログ。
3. **承認判定・ポリシー評価などのシステム自動記録**: **5件 (11.9%)**
   - Codex の `The following is the Codex agent history whose request action you are assessing...` や `{"outcome":"allow"}` など。
4. **他の話題・別コミット案**: **7件 (16.7%)**
   - READMEの多言語化や、別のZenn記事のコミットメッセージ案など別タスクの記録。

---

## 4. 結論と根拠

### **判定: B（ノイズ対策をすれば役立つ）**

### 根拠

#### 1. なぜ「C: 既存の記録で十分」ではないのか？
実務において最も知りたい「**なぜこの設計をやめたのか（過剰設計の撤回）**」「**なぜHookやSkill化を採用しなかったのか（トークン消費の本末転倒）**」「**特定コミットがローカルPCの別配置環境に与える影響**」「**検証スクリプトの比較原理と実測値**」といったコンテキストは、コミットメッセージや公式ドキュメント（`docs/`）、`CLAUDE.md` には一切残っておらず、セッション検索によってのみ発掘できた（8問中4問、50%）。
また、Codexで検討した設計経緯をAntigravityで参照したり、Claude Codeで行ったバグ調査をCodexから引き継ぐといった**エージェント間をまたいだ文脈の共有**において、セッション検索は極めて高い実務的価値を持っている。

#### 2. なぜ「A: このままで役立つ」ではないのか？
検索結果の上位5件のうち **約65%（42/65件）がノイズ** で占められており、特に初回クエリではツール呼び出しの引数JSONやコマンド文字列、Codexの承認判定ログが上位を独占する傾向が強かった。
適切な言い換えクエリ（会話に特有の単語を含めるなど）を行えば正解を引き当てられるものの、現状のままでは「開発者がパッと探して即座に回答を見つける」にはノイズが多すぎる。

---

## 5. 効果が大きい順のノイズ対策

実測結果に基づき、検索精度を大幅に向上させるための対策を効果が大きい順に提言する。

### 対策1: ツール呼び出し・実行結果（`kind in ('tool_call', 'tool_result')`）のBM25重み付け引き下げ、またはメッセージ種別によるフィルタ/ブースト 【効果: 極大】
- **課題**: ノイズ全体の **71.4%（30/42件）** がツール呼び出し（引数JSON、コマンド）およびその実行結果で占められている。
- **対策**:
  - `session_search` の引数に `--kind message`（人間とAIの対話メッセージのみ対象）を指定可能にする、または既定で `kind='message'` のスコアを優先ブーストする。
  - ツール引数（特に長いJSON文字列）のインデックス対象文字数を大幅に制限するか、別カラムに分離する。

### 対策2: システム自動記録（承認判定履歴やAGENTS.md挿入ブロック等）のインデックス除外 【効果: 大】
- **課題**: Codex の承認判定セッション（`01a14548...`, `01a14717...`）や、Codex agent history評価用のメタプロンプトが複数クエリで上位にヒットした（ノイズの11.9%）。
- **対策**:
  - Codex の `The following is the Codex agent history whose request action you are assessing...` や、`{"outcome":"allow"}` 等の判定のみのセッション、あるいはAGENTS.md自動挿入ブロックをインデックス対象から除外（または重み低減）する。

### 対策3: タイトル・本文の文字コード（Windows CP932 / UTF-8）デコード処理の堅牢化 【効果: 中〜大】
- **実測での発見**: `.bm25_sessions.db` 内の一部セッション（特にタイトルや初期プロンプト）において、日本語文字が `\ufffd`（REPLACEMENT CHARACTER）に化けて保存されているケースが確認された（例: `786a260 - fix(mcp): XNvgN...`）。
- **対策**: 取り込み（sync）時、Windows環境でのログ読み込みにおいて、UTF-8 だけでなく CP932 / MS932 へのフォールバックや、PowerShell経由のエンコーディング不整合を救済するデコードパイプラインを強化する。

### 対策4: タイトル生成時の自動挿入タグ（`<ide_opened_file>`, `<command-message>` など）の除去 【効果: 中】
- **課題**: Claude Code のセッションタイトルが `<ide_opened_file>The user opened the file...` や `<command-message>smart-commit</command-message>` のように自動挿入タグから生成され、検索時のタイトル一致ノイズになっている。
- **対策**: セッションタイトル抽出時に、IDE通知タグやスラッシュコマンドタグを除去し、人間が入力した実際のリクエスト文をタイトルとして採用する。

---

## 6. 推測と実測の分離

- **実測（事実）**:
  - 8問中8問すべてで、最大2回のクエリで上位1〜2位に正解を引き当てることができた。
  - 8問中4問（50%）は、コミットログ・docs・CLAUDE.md のいずれにも記録が存在せず、セッション検索のみから得られた。
  - 上位ヒット65件中42件（64.6%）がツール呼び出し・実行結果・システムログ・別話題のノイズであった。
  - 一部セッションのタイトルや本文に置換文字（`\ufffd`）が混入していることが確認された。
- **推測（評価者の見解）**:
  - ツール呼び出し（`tool_call` / `tool_result`）の重みを下げる、または会話メッセージ（`kind='message'`）を優先するだけで、初回クエリでの正解到達率は現在の37.5%（3/8）から75%以上へ劇的に改善すると考えられる。
  - 開発者が過去の意思決定や試行錯誤（ボツ案・過剰設計の撤回理由）を追跡するユースケースにおいて、本機能は不可欠なツールになり得る。
