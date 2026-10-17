# プロジェクト単位のセッション検索

Claude Code、Codex、Antigravity（agy）、Cursorのローカル履歴を、必要なときに検索・参照する機能。
追加のLLMや常駐サービスは不要。既存のコード検索と同じサーバーから、明示的に有効化して使う。

## 保存先

| ファイル | 用途 |
| --- | --- |
| `<root>/.bm25_index.db` | 既存のコード検索 |
| `<root>/.bm25_sessions.db` | 会話本文・出典・FTS索引・任意の知見 |
| `<root>/.bm25_sessions.json` | 登録した入力元、project alias、除外、redaction |

同じrootの4エージェントでセッションDBを共有する。linked worktreeはそれぞれ別DB。
DB本体、`-wal`、`-shm`、ローカル設定、マイグレーションbackupはGit管理対象外。
ユーザー間のアクセス制御はOS権限に委ねる。
リポジトリを削除すると中のDBも失われるため、保持する履歴は先にbackupする。

## 入力元の登録

MCPサーバー（`--sessions`）は、入力元が1件も登録されていない間、実在する既定保存領域を自動で入力元として使う。
プロジェクトごとの登録は不要で、`session_sync` を呼べば取り込まれる。`--no-auto-sources` で無効化できる。

| source_id | agent | 既定パス |
| --- | --- | --- |
| `claude-local` | claude_code | `$CLAUDE_CONFIG_DIR/projects`（未設定時 `~/.claude/projects`） |
| `codex-local` | codex | `$CODEX_HOME/sessions`（未設定時 `~/.codex/sessions`） |
| `agy-cli` / `agy-ide` / `agy-legacy` | agy | `~/.gemini/antigravity-cli` / `-ide` / `~/.gemini/antigravity` |
| `cursor-local` | cursor | Windows `%APPDATA%\Cursor\User`、macOS `~/Library/Application Support/Cursor/User`、他 `~/.config/Cursor/User` |

既定パスはコード内固定で、ツール引数からパスは受け付けない。`allowed_source_ids` には上表のIDを指定できる。
1件でも明示登録すると既定は使わず、登録分だけが対象になる。CLIは既定を使わないため、CLIで同期する場合は登録が必要。

以下はPowerShellでの明示登録の例。使う製品だけを登録し、rootはセッションを蓄積したいプロジェクトに指定する。
CLIの実行には、このパッケージをインストールするか、サーバーリポジトリをPythonの検索パスに置く。

```powershell
$sessionProject = '<project-root>'
python -m bm25_search.sessions.cli --root $sessionProject source add claude-local --agent claude_code --path (Join-Path $env:USERPROFILE '.claude\projects')
python -m bm25_search.sessions.cli --root $sessionProject source add codex-local --agent codex --path (Join-Path $env:USERPROFILE '.codex\sessions')
python -m bm25_search.sessions.cli --root $sessionProject source add agy-cli --agent agy --variant cli --path (Join-Path $env:USERPROFILE '.gemini\antigravity-cli')
python -m bm25_search.sessions.cli --root $sessionProject source add agy-ide --agent agy --variant ide --path (Join-Path $env:USERPROFILE '.gemini\antigravity-ide')
python -m bm25_search.sessions.cli --root $sessionProject source add cursor-local --agent cursor --path (Join-Path $env:APPDATA 'Cursor\User')
python -m bm25_search.sessions.cli --root $sessionProject sync
```

旧agyの保存領域は `.gemini/antigravity`、variantは `legacy` として別sourceに登録できる。
入力元は製品のグローバル保存領域でも、本DBに保存するのは現在projectの記録だけ。
複数workspaceが別projectにまたがる記録や帰属不明ログは診断付きで除外する。
元ログのパスを本文から推測して現在projectに割り当てない。

## 検索と原文参照

```powershell
python -m bm25_search.sessions.cli --root $sessionProject search '差分取り込み generation'
python -m bm25_search.sessions.cli --root $sessionProject --client-agent codex search '前回の設定' --agent self
python -m bm25_search.sessions.cli --root $sessionProject read '<session-id>' --anchor '<anchor>'
python -m bm25_search.sessions.cli --root $sessionProject read '<session-id>' --cursor '<next_cursor>'
python -m bm25_search.sessions.cli --root $sessionProject status --json
```

検索は既定で全エージェント。`--agent codex` などで絞り込み、`self` は `--client-agent` で解決する。
`since/until` はRFC3339（timezone必須）、sinceを含みuntilを含まない。
過去の原文は現在の指示ではなく根拠。抜粋だけで断定せず、後続の訂正と現在のコード・環境を確認する。
0件は「履歴が存在しない」という意味ではなく、入力元・範囲・最終sync・partial診断も確認する。
原文参照はDBだけを読むので、元ログが削除されても取り込み済みの範囲は残る。

## MCP接続

各クライアントのMCP登録で、同じproject rootとそのクライアント名を明示する。
既存登録を変更しない場合は、セッション用の別サーバー名で登録できる。
例: command=`python`、argsは以下（`cwd` は対象project root）。

```json
[
  "<server-repository>/bm25_search/mcp_server.py",
  "--stdio",
  "--sessions",
  "--root", "<project-root>",
  "--client-agent", "codex"
]
```

Claude Codeは `claude_code`、agyは `agy`、Cursorは `cursor` に置き換える。
パッケージから起動できる場合は `python -m bm25_search.mcp_server` でも同じフラグを使える。
`--sessions` なしでは従来の `search` のみで、ツール定義と動作を維持する。

| ツール | 用途 |
| --- | --- |
| `session_search` | BM25検索、出典付き抜粋とanchor |
| `session_read` | 周辺原文とページの続き |
| `session_sync` | 登録sourceの非同期取り込み、run_idで状態取得 |

```json
{"action":"start","agent":"all"}
```

上の結果のrun_idを使い `{"action":"status","run_id":"<run-id>"}` で取得する。
`accepted` は受付であり、完了ではない。完了状態はsucceeded/partial/failed/interrupted。
サーバー終了後のbackground job継続は保証しない。切断後は状態を確認して再syncする。
起動時や検索時に全履歴を自動importしない。
共通skillは `skills/session-recall/SKILL.md`。クライアントのskill導入とMCP接続は別々に確認する。

## 設定と保守

`source list|disable|enable` で取り込み元を管理する。disableは更新停止であり、アーカイブの削除ではない。
`project alias <old-root>` は同じrootの移動前パスを登録する用途で、別worktreeの共有機能ではない。
移動・復元したDBは `project relocate` でrootとの照合を明示的に更新する。

```powershell
python -m bm25_search.sessions.cli --root $sessionProject backup '<new-backup-file>'
python -m bm25_search.sessions.cli --root $sessionProject rebuild-index
python -m bm25_search.sessions.cli --root $sessionProject forget --session '<session-id>'
python -m bm25_search.sessions.cli --root $sessionProject forget --session '<session-id>' --apply
```

forgetの初回は件数表示だけ。applyすると本文・chunks・FTS・知見の根拠を削除し、再取り込み抑止を記録する。
再取り込みを明示的に許可する場合のみ `allow-reimport --session <id>` を使う。
論理削除で古いbackupまで消えるわけではなく、backupの消去は別操作。

redactionはローカル設定の `redaction.rules` にpattern/replacementを指定する。
変更後のsyncは既存アーカイブ・FTS・知見の根拠にも適用する。過去backupには自動適用しない。
入力元の探索は登録したrootだけ。`exclude_paths` はローカルパスのglobで、`max_artifact_bytes` の既定は64MiB。
tool outputは既定16,000文字を保存し2,000文字を索引化。上限外・元ログ側の切り詰めを復元済みとは扱わない。
`sync --verify` は共有ファイルのhashキャッシュを使わず再確認する。
未知のschema versionはmigration_requiredとして停止し、既存DBを作り直さない。

## 任意の知見機能

MCPに `--knowledge`（`--sessions` 必須）を追加すると `knowledge_search/read/record` も公開する。
初回利用時はbackupを作ってschema v2へ移行する。
単なる検索で知見を自動保存せず、ホストが原文を確認してrecordを明示する。

recordにはtitle、claim、generated_by、session_idとanchorを持つevidenceが必要。
conditions、procedure、applies_to（agent/os/versions）、status、verificationを添えられる。
既定はproposed/unverified。acceptedとtestedは独立している。
testedには実際の実行成功を表すnative tool結果と対応callが必要で、assistantの主張では昇格しない。
現在の既知adapterでその判定情報を持つのはagyのRUN_COMMAND結果で、その他はobserved/unverifiedとして記録する。
これはコマンド成功の根拠を要求する仕組みであり、その実行がclaimを論理的に証明するかはホストが確認する。

根拠が変更・削除された知見は既定検索から除外し、明示readでstale/missingを報告する。
別知見による置換はsupersedes_idで指定し、古い記録を履歴として残す。
CLIは `knowledge search/read`、`knowledge record --file <json-file>`。

## 対応範囲と検証

- Claude/Codex: T0で確認したJSONL envelope、typed textとtool。内部推論・常設指示を除外。
- agy: step_format=0の既知本文、project metadata、RUN_COMMAND。その他toolはtranscriptのpartial取得。
- Cursor: workspace/globalの関連付け、inline/external bubble。帰属・日時・参照の欠落を診断。
- 同一本文の重複はまとめるが、似ているだけの別結論を消さないため、曖昧な近似重複の統合は行わない。
- 入力ログは読み取り専用。WAL付きDBをimmutableで読まず、読み取れなければunavailableを報告する。
- Windows/WSLをまたぐ同じライブDBの同時利用、ネットワーク上のWAL DB、cloud履歴の取得、公式export全形式は初期対象外。

検証範囲・実測値・未完了の受入確認は [実装検証記録](research/session-recall/implementation-report.md) を参照。
