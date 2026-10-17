# T0: セッション取得方式とfixtureの調査結果

状態: **T0完了、T1以降の製品機能は未実装**。
対象: このWindows環境のClaude Code、Codex、agy（旧・IDE・CLIの3保存領域）、Cursor。
設計: [セッション検索設計書](../../plans/session-recall-design.md)。

## 1. 結論

4製品とも、ローカル履歴から会話を取得する経路を確認した。
グローバルに置かれている各製品の元ログを入力として読み、現在のプロジェクトに属する記録だけを `<root>/.bm25_sessions.db` へ取り込む。
元ログの場所と、本機能のDB配置は独立している。

| 製品・形式 | 採用する入力 | 本文 | project | 日時 | tool | T0で確定した制限 |
| --- | --- | --- | --- | --- | --- | --- |
| Claude Code / jsonl-v2 | projects配下JSONL | user/assistantのtext | 各recordのcwd | timestamp | tool_use / tool_result | user envelopeにはtool結果もある。内部推論・API requestの複製は除外 |
| Codex / rollout-v1 | sessions配下JSONL | response_itemのmessage | 最初のsession_meta.cwd | record.timestamp | custom_tool_call / output | event_msgも同じ発言を含む。全イベントを連結しない |
| agy旧 / trajectory-v0 | SQLite＋transcript JSONL | SQLiteの既知stepを復号 | trajectory metadata | metadataのTimestamp | call IDとtyped tool / transcript | transcriptは切り詰めあり。未知stepはpartial |
| agy IDE / trajectory-v0 | 同上 | 同上 | 同上 | 同上 | 同上 | IDE独自toolを含む。全tool payloadの復号は対象範囲外 |
| agy CLI / trajectory-v0 | 同上 | 同上 | 同上 | 同上 | 同じschema、当該3DB sampleではtool実行なし | CLIのtool call自体は別の最新transcript sampleで観測。SQLによる相関の実測は旧/IDE |
| Cursor / storage-v1 | workspace/global SQLiteの結合 | inline conversationまたは外部bubble | workspace metadataとのjoin | bubble.createdAt等、なければ不明 | toolFormerData | workspaceとの対応が失われた会話・NULL値・時刻欠落を診断 |

ここでの「確認」は形式・取得経路の確認であり、完成済みアダプターや4クライアントE2Eの成功を意味しない。
対応する製品版・形式を絞ってT2/T3を実装し、未知形式をpartial/unsupportedとして報告する。

## 2. 調査方法と証拠

- `probe_local.py`: 標準ライブラリだけで、既知のローカル保存領域を明示指定の `--local` で調査。
- `t0-observations.json`: 同スクリプトが出力した件数・event種別・writer version・参照整合の観測値。
- `capabilities.json`: 製品別フィールド対応・取得範囲・取得しないものの機械可読一覧。
- `build_fixtures.py`: 観測した構造を用い、本文・パス・IDをすべて合成値に置き換えたfixtureの生成器。
- `tests/sessions/fixtures`: JSONL、SQL、workspace metadata。SQLのBLOBも合成したprotobufであり、実DBのコピーではない。
- `tests/sessions/test_t0_fixtures.py`: 出典位置、ID相関、切り詰めの補完、未知形式・壊れたprotobufの拒否などを検証。

JSONLは各保存領域の更新時刻が新しい3ファイル、各最大5,000行を構造調査した。
agy SQLiteはWALがない履歴から新しい3DBずつを選び、ファイルが読み取り中に変わらないこととquick_checkを確認した。
transcript sampleとDB sampleは別々の「新しい3件」であり、同じ3セッションとは限らない。
Cursorはglobal DBの対象レコード群と、ローカルworkspace DBの対象キーを調べた。
これは全履歴・全旧バージョンを網羅した互換性試験ではない。

### 2.1 インストール版とログwriter版は別

| 対象 | 確認した値 | 解釈 |
| --- | --- | --- |
| Claude CLI `--version` | 2.1.296 | 今回呼び出したCLI |
| Claude JSONLのversion | 2.1.300 | 調査sampleのwriter。CLI版と一致すると仮定しない |
| Codex CLI `--version` | 0.162.1 | 今回呼び出したCLI |
| Codex session_meta.cli_version | 0.162.0-alpha.17.2 | 調査sampleのwriter |
| agy CLI `--version` | 1.4.0 | 今回呼び出したCLI |
| agy IDE package.json | 1.107.0 | インストールされたeditor packageの版。会話schemaや製品全体の版と同一視しない |
| agy extension package.json | 0.2.0 | descriptorを調査したextension |
| Cursor package/product.json | 3.20.10 | 調査したSQLiteと同じローカル環境の製品版 |

CLIのversion照会にはCodexの一時PATH操作、Cursorのchdirに関するsandbox警告があった。
値はログmetadata・インストール済みpackageの情報とも区別して記録し、警告を起動成功や書き込み権限の証明には使わない。

## 3. Claude Code

入力候補: `~/.claude/projects/**/*.jsonl`。
ファイル配置だけで所属プロジェクトを決めず、実recordの `cwd` とproject aliasを照合する。

| 用途 | フィールド |
| --- | --- |
| セッション | `sessionId` |
| 発言・系譜 | `uuid`, `parentUuid`, `isSidechain` |
| 人の発言 | `type=user`, `message.content` がstringまたはtext block |
| 応答 | `type=assistant`, `message.content[].type=text`, `.text` |
| tool call | assistantの `tool_use.{id,name,input}` |
| tool result | user envelope内の `tool_result.{tool_use_id,content,is_error}` |
| 日時・project | `timestamp`, `cwd` |

`type=user` をすべて人の発言として扱うのは誤り。
tool_resultはrole=toolとして正規化し、`tool_use_id` で対応させる。
thinking/signature、system、attachment、api-request/API blob、進捗・履歴snapshotは検索本文に混ぜない。
未対応blockは取り込み件数ゼロの成功にせず、block種別付きのpartial診断を出す。

source locator: JSONL行、uuid、content block index。
partialな末尾行は再sync待ちとし、正常な前半を壊さない。

## 4. Codex

入力候補: `~/.codex/sessions/**/*.jsonl`。
先頭の `session_meta.payload.id/session_id` を当該ファイルのIDとして固定する。
forkの継承部分で後から現れる親のsession_metaで上書きしない。

| 用途 | フィールド |
| --- | --- |
| project | 最初の `session_meta.payload.cwd`、補助情報にturn_context |
| 発言 | `type=response_item`, `payload.type=message`, `role=user/assistant` |
| 本文 | `payload.content[].text`、typed input_text/output_text |
| tool call | `payload.type=custom_tool_call`, `call_id`, `name`, `input` |
| tool result | `payload.type=custom_tool_call_output`, `call_id`, `output` |
| 日時 | recordの `timestamp` |

`output` にtext blockの配列を持つ実例を確認した。string専用の処理にしない。
`reasoning/encrypted_content`、world_state、developer/systemの常設指示を検索本文にしない。

`event_msg.item_completed` にもUserMessage/AgentMessageがある。
追加調査sampleでは46件中32件のitem IDがresponse_itemのIDと一致し、すべてのイベントが同じIDになるわけではなかった。
そのため、response_itemを本文の正規streamとし、event_msgを丸ごと追加して二重取り込みしない。
本文streamがない旧形式へのfallbackは、別fixtureと明示した形式検出が必要。

最新3sampleで確認したtool形式はcustom_tool_call系。
旧function_call/function_call_outputの全派生を同じ対応版として保証しない。
source locator: JSONL行、payload.id、call_id、content block index。

## 5. agy: SQLiteとtranscriptを組み合わせる

### 5.1 取得元と位置付け

保存領域は以下を別sourceとして登録する。

- 旧: `~/.gemini/antigravity/`
- IDE: `~/.gemini/antigravity-ide/`
- CLI: `~/.gemini/antigravity-cli/`

その下の `conversations/<id>.db` と `brain/<id>/.system_generated/logs/transcript.jsonl` を対応させる。
同じ名前の別製品・非公式CLIのディレクトリを混同しない。
[公式Hooks文書](https://www.antigravity.google/docs/hooks)にもこのtranscriptパスと、hook入力のconversationId/workspacePathsが記載されている。

transcriptには `step_index,type,source,status,created_at,content,tool_calls,truncated_fields` などがある。
最新3sampleで旧120件、IDE49件、CLI3件にtruncated_fieldsを観測した（snapshot時点の詳細は観測JSON）。
transcriptだけを保存して「完全な原文をアーカイブした」とは説明できない。
また、本文からファイルパスを拾うだけでprojectを決めない。

CLI cacheの `conversation_metadata.json` は `conversations.<id>.summary.WorkspaceURIs` を持つが、cacheを唯一のproject根拠にしない。
IDEの `antigravityUnifiedStateSync.trajectorySummaries` も独自encodingなので、SQLite trajectory metadataの取得を優先する。

### 5.2 SQLiteの形式

3領域で共通して以下の表を観測した。

- `trajectory_meta(trajectory_id,cascade_id,trajectory_type,source)`
- `steps(idx,step_type,status,has_subtrajectory,metadata,...,step_payload,step_format)`
- `trajectory_metadata_blob(id,data)`
- `gen_metadata,executor_metadata,parent_references,battle_mode_infos`

`gen_metadata`やmodel_usageは使用量であり、会話本文の代わりにしない。
既存session-tokensスクリプトはこれらの理解には使えるが、本文取得済みの証明にはならない。

`step_payload` は既知sampleではstep全体を包むprotobuf、`step_format=0`。
インストール済みIDE extension内のFileDescriptorProtoを既設google.protobufで調査し、本文型・metadata型のfield名と番号を確認した。
外部ライブラリはこの調査に使用しただけで、probeとfixture検証にprotobufの実行時依存は追加していない。

| フィールド | 確認した番号・意味 |
| --- | --- |
| wrapper | 1=step_type、4=status、5=metadata、19=user input、20=planner response、28=run command |
| trajectory metadata | 1=workspaces、workspaces内1=workspace_folder_absolute_uri、7=workspace_uris |
| step metadata | 1=created_at Timestamp、4=tool_call |
| user input | 2=user_response、1=旧query。items(3)は別のtyped表現 |
| planner response | 1=response、8=modified_response、3=thinking（除外）、7=tool_calls |
| tool call | 1=id。nameのfield2はtranscriptのnameとの一致を191callで確認 |
| run command | step_type=21、23=command_line、2=cwd、6=exit_code、21=combined_output |
| RunCommandOutput | 1=full、2=truncated、3=num_lines_above |

wrapperのtypeとsteps.step_type、およびwrapper内metadataとsteps.metadataの一致をsampleで確認した。
本文型・metadata型のfield名はdescriptorに基づき、wrapperの19/20/28等の位置は実レコードとの相関で確認した既知形式の契約として扱う。
planner側tool call IDとresult metadata側IDも、追加調査した旧90/90件、IDE191/191件で一致した。
全数値・既知fieldの検出に加えて、未知step_formatや型不一致は拒否する。
field番号を推測で他形式に適用しない。

### 5.3 取得範囲と限界

- user/assistantの既知scalar本文はDBから取得し、transcriptに短縮版があってもそれを原文として上書きしない。
- user入力のitems/scopes/mediaのみで表現されたものは、対応するdecoderを実装するまでpartial。
- plannerに本文がなくtool_callsだけがあるのは正常。内部推論をassistant本文の代用にしない。
- RUN_COMMANDは既知payloadからcommand、exit code、full outputを取得可能。
- その他のtool型はtranscriptを補助として使い、truncated_fieldsやDB側未対応を結果に示す。
- tool call IDがないtranscriptだけのケースでは、順序だけで関連を確定しない。
- source locatorはDBのsteps.idxとfield path。transcriptとの結合キーはstep_index。
- cacheやhookを新設しなくても履歴を読める。hookのworkspacePathsは将来の新規session補助に使えるが、T0でhook設定は変更していない。

T3はこの既知subsetをdecoderとして実装し、全protobuf payloadの万能復号を実装条件にはしない。
必要な会話本文が取り出せないsessionはcompleteness=partialとし、対応表に取得範囲を掲載する。

## 6. Cursor

### 6.1 正しいprojectへの帰属

- `%APPDATA%/Cursor/User/workspaceStorage/<workspaceId>/workspace.json` のfolder/workspace/configurationからworkspaceを識別する。
- 同フォルダの `state.vscdb` → ItemTableの `composer.composerData` → `allComposers[].composerId` が旧経路。
- globalStorageの `state.vscdb` → `composerHeaders.workspaceId` とworkspaceStorageのIDを結ぶのが新経路。
- `.code-workspace` 等の複数rootを含む設定は、実際のworkspace定義を解決する。単一rootに確定できなければ要明示指定。

観測した27headerのうちworkspaceStorageのディレクトリに一致したのは15件だった。
存在しないworkspaceの会話を、添付ファイルのパスや検索サーバーのcwdから勝手に現在projectへ割り当てない。
旧経路では10workspaceに `allComposers` があり、現行headerだけでは見えない履歴を補完できる。

### 6.2 本文の取得

global DBの `cursorDiskKV` から、次の両形式を読む。

1. `composerData:<composerId>` の `conversation[]` に本文がinlineである形式。
2. `fullConversationHeadersOnly[]` のbubbleIdを使い、`bubbleId:<composerId>:<bubbleId>` を取得する形式。

観測時点ではcomposer 165row（NULL5）、inlineあり3、外部headerあり81、外部bubble参照2,879件に未解決参照なし。
bubble row 2,963件中83件はNULLだった。
NULL・欠損参照はtombstone/欠損として扱い、本文が空の成功recordにしない。

`type=1` はhuman、`type=2` はAI。インストール済み製品コードのMessageType enumにも1=HUMAN/2=AIがあり、本文レコードの構造と照合した。
`text` を本文とし、`isThought` やallThinkingBlocksは除外する。
`toolFormerData` のtoolCallId/name/rawArgs/params/result/statusを別のtool情報として保持する。
params/resultが常にJSONとは限らないため、無条件のJSON decodeでデータを失わない。

### 6.3 日時と限界

createdAtを持つbubbleは調査時点で138件にとどまった。
timingInfoは値の単位を検証した場合だけevent時刻の補助とする。
composerHeadersのcreatedAt/lastUpdatedAtはセッションの時刻であり、全bubbleの発生時刻として複製しない。
時刻不明はNULLのままにし、期間検索での扱いを設計書に従って診断する。

source locator: global DB、cursorDiskKVのcomposite key、native bubble ID、フィールド位置。
Cloud Agentのリモート履歴と公式Markdown exportは今回検証しておらず、初期取得経路に含めない。
[旧公式History文書](https://docs.cursor.com/en/agent/chat/history)の説明だけで現行SQLiteの内部schemaを保証しない。

## 7. 読み取り専用とfixtureの扱い

SQLiteはmode=ro + query_only + read transactionを基本とし、書き込み可能接続へのfallbackは設けない。
今回のsandboxでは、読み取り専用でもSQLiteがSHMを扱えず一部agy DBを開けなかった。
そのため調査probeだけは、WALなし・読み取り中に変更なし・quick_check成功を条件にimmutableで履歴DBを読んだ。
**この調査用経路を稼働中のWAL付きDBに流用しない。**
[SQLiteのimmutableの契約](https://www.sqlite.org/uri.html#uriimmutable)は、呼び出し側がファイル不変を保証する前提。
T3では通常のro transactionまたは整合したSQLite snapshotを使い、読めなければunavailableを返す。

fixtureはすべて合成値。実際の会話本文、ログのUUID、アカウントID、ユーザー名、ローカルprojectパスをコピーしない。
元DBやWALのコピーはリポジトリに作成していない。
protobuf fixtureはfield番号を保ち、値を合成してSQLのhex literalとして保存した。
probe出力は件数・固定enum・版番号だけで、本文・title・tool引数・project URIの値を出力しない。

## 8. 再現手順とT1への引き渡し

```powershell
# ローカル履歴の構造を再確認（会話本文は出力しない）
python docs/research/session-recall/probe_local.py --local --samples 3 --output docs/research/session-recall/t0-observations.json
# nativeログを入力に使わず、合成fixtureを再生成
python docs/research/session-recall/build_fixtures.py
# native製品への接続なしで形式の契約を検証
python -m pytest tests/sessions/test_t0_fixtures.py -q
```

T1には、root固定のDB・source登録・native locator・partial診断を実装するための情報を渡せる。
T2/T3ではここで確定した既知形式を正式アダプターにし、T0の研究用witnessを完成済みimporterとして公開しない。
4製品の取り込み・検索・原文参照の実機確認はT6/T7に残る。

## 9. 検証結果

- T0の形式・fixture検証: **14 passed**。
- リポジトリ全体: **62 passed**（22.76秒）。
- Python 3.9の文法互換性をASTで確認。実際のテスト実行環境のPython/SQLite版は観測JSONに記録。
- JSONの構文、Markdownのコードブロック・ローカルリンク、空白、既知credential形式の混入を検査。
- fixture内のSQL BLOBも検査し、パスが合成root以外を参照しないことを確認。

全体テストの初回実行はsandbox既定の一時ディレクトリ権限により41 passed / 21 errorsだった。
同じテストを、作業領域内の専用一時ディレクトリをTEMP/TMPとpytest basetempに指定して再実行し、全62件が成功した。
一時環境変数はそのシェルプロセスだけに設定し、ユーザー/システム設定や既存テストコードは変更していない。
