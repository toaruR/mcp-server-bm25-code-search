# セッション検索 T1以降の実装検証記録

検証日: 2026-10-17。対応方式の根拠は [T0調査](t0-report.md)、利用方法は [導入手順](../../session-recall.md)。

## 到達点

プロジェクト直下の `.bm25_sessions.db` を、Claude Code・Codex・agy・Cursorで共有する実装を追加した。
既存コード検索とはDBを分け、`--sessions` で明示的に有効化する。アクセス制御は通常のOS権限に委ねる。
このリポジトリの実利用DB・各クライアントの設定は作成・変更していない。

- T1: 設定、root照合、alias、worktree別DB、WAL、原文と派生FTS、backup・forget・migrationを実装・検証。
- T2: Claude Code / Codexの本文・tool・出典・fork/sidechain識別を実装・検証。
- T3: agyの旧版/IDE/CLIとCursorの既知形式を実装し、合成fixtureとローカル実ログで検証。未知形式・帰属欠落等はpartial/unsupportedとして残す。
- T4: 明示sync、変更検出、generation/revision競合制御、非同期run、途中終了回復、アーカイブ保持を実装・検証。
- T5: BM25、数値・日本語・識別子、project/source/agent/date制約、重複抑制、anchor、ページング、UTF-8出力上限を実装・検証。
- T6: MCP/CLI・共通skill・接続手順を実装。4種類のclient_agent設定によるstdio接続は確認済み。**4製品それぞれの画面からの呼び出し確認は未実施。**
- T7: 自動試験・性能測定・実ログの取り込み→検索→原文参照を実施。上記の製品画面での受入確認を残すため、リリース条件の全達成とはしない。
- T8: 任意の知見DB/FTS・出典固定・record/search/read・履歴・根拠失効・forgetを実装・検証。`--knowledge` 追加時だけMCPツールを公開する。

## 自動検証

`python -m pytest -q --tb=short --basetemp <isolated-temp>`: **85 passed**。
既存48件、T0 fixture14件、追加23件。通常のコード検索のカタログ互換性も含む。

追加検証には、2 writer、artifact更新競合、size/mtimeを維持した本文変更、FTS更新失敗時のrollback、SQLite transaction中のプロセス終了、run再開時のinterrupted判定、未知schemaを壊さない停止を含む。
長い日本語・絵文字、anchorの保持/失効、削除後の再取り込み抑止、source許可範囲、別root/linked worktree分離、日時の欠落も検証した。
Claude subagentでは親と同じsessionIdが使われる実例を確認し、agentIdで子の識別子を分ける回帰テストを追加した。

stdio試験は実際に `mcp_server.py` を起動し、initialize→tools/list→sync開始/状態取得→search→readを通す。
これは各client_agent値でのプロトコル試験であり、Claude Code・Codex・agy・Cursorの製品UIを操作した試験ではない。
共通skillはskill-creatorのvalidatorで検証済み。

## 実ログでの確認

[native-results.json](native-results.json) は会話本文・パス・session IDを含まない集計結果。
明示的に選んだ単一projectのログを、検証専用DBへ取り込んで検索・原文参照した。
Claude、Codex、agy legacy、agy IDE、agy CLI、Cursorの6ケースが通過した。
partialは本文の一部・日時・未対応tool等に欠落がある状態として保持しており、完全取得を意味しない。

agyはSQLiteのproject metadataを帰属判定に使い、transcriptだけからprojectを推測しない。
Cursorはworkspace/globalの関連付けを更新時にも確認し、孤立したcomposerや欠落bubbleを診断する。
入力SQLiteは読み取り専用。通常のread-only接続が使えない場合のimmutable fallbackは、WAL/journalがなく前後のhashが一致する安定した履歴ファイルだけに限定した。

実ログの検証DBにはprivateな本文が含まれるため成果物に含めず、検証後に一時領域ごと削除する。
研究スクリプト [native_smoke.py](native_smoke.py) は `--local` と新規workdirを明示した場合だけ動く。

## 検索品質と性能

再現スクリプト: [benchmarks/session_recall.py](../../../benchmarks/session_recall.py)。
入力は合成データ10万メッセージ、100セッション、約3.3万chunk。共通語の多いdistractorに、日本語・英語・識別子・エラー番号を含む30問の正解を配置した。
正解定義は [retrieval_cases.json](../../../tests/sessions/fixtures/retrieval_cases.json)、数値・環境・メモリ・DB/WALサイズは [benchmark-results.json](benchmark-results.json) に記録する。

測定では初回import、100件追記の差分sync、warm検索/参照のp95、Hit@5、該当セッションの全文を渡す場合に対するsearch+readのUTF-8バイト削減率を集計する。
warm値は最初の反復を除外し、4反復を採用。メモリはデータ生成を含むプロセス全体のpeak working set（Windows）で、syncだけの使用量とは区別する。
合成30問の結果を、実案件全般の検索品質やcold状態の速度の保証とはしない。

最終測定（Windows AMD64 / Python 3.13.1 / SQLite 3.45.3）:

- Hit@5: 30/30、検索p95: 169ms、原文参照p95: 16ms。
- 初回取り込み: 66.3秒、100件追記: 0.845秒。
- search+readの出力削減率中央値: 98.4%。
- 追記後100,100メッセージ、33,372chunk、DB: 330,944,512バイト、終了時WAL: 0バイト。
- peak working set: 40,157,184バイト。WAL値は終了時の残存量であり、sync中の最大量ではない。

## 設計の具体化・制限

- DDLは設計書の論理モデルを具体化したもの。session schema v1、知見機能の初回利用時にbackupを取ってv2へ移行する。未知の将来versionは拒否する。
- checkpointはartifactのfingerprintとgeneration、session revisionで管理する。JSONLの永続byte offsetによる末尾だけのparseは未実装。変更したartifactは再parseし、変わったmessage/chunkだけを更新する。
- sourceは明示登録。起動・検索でグローバル履歴を自動取り込みしない。無効化や入力元消失後も、既存の本文は保持する。
- 重複統合は同一本文を対象とする。似た別結論の消失を避けるため、近似重複の自動統合は見送った。
- 日時不明のmessageは無条件検索では参照できるが、日時filterがある場合は除外して診断する。
- redaction変更は保存済み本文・FTS・知見と根拠にも適用する。古いbackupは対象外。保存上限を下げた場合、消失済みsourceのtool本文を自動で再切り詰めする機能はない。
- Claude/Codexの思考・system/developer指示、画像等の本文ではないblockを除外する。Codexの検出できた親継承範囲は検索chunkから除外する。
- agyは既知のstep_format=0、Cursorは確認済みのworkspace/global構造が対象。製品全バージョン・cloud履歴・公式export全形式への対応は保証しない。
- 知見のacceptedはtestedを意味しない。testedは対応するtool callとnative成功結果を要求し、現時点で情報を保持するのはagy RUN_COMMAND。claimの意味的な正しさを自動証明するものではない。
- 入力ログの読み取り・DB backup以外の外部アプリ設定変更はしない。Windows/WSLをまたぐライブDB共有、ネットワークDB利用は初期対象外。

## 残る受入確認

各製品で同一project rootを指定し、導入手順のMCP登録と共通skill導入を行って再接続する。
tools/listで3ツールを確認後、session_syncの完了状態、既知の語でのsession_search、anchorによるsession_readを画面から実行する。
agent=selfが当該製品に解決し、別projectの本文を返さないことも確認する。
4製品すべての実操作が通るまで、設計書15.1の「4クライアント」を満たしたとは扱わない。
