# Seed — 条件付き変更パッケージの実験

Small, content-addressed version-control prototype with conditional, atomic change packages.
Python standard library only. Experimental. MIT licensed.

[![Tests](https://github.com/marvelousu/seed-vcs/actions/workflows/test.yml/badge.svg)](https://github.com/marvelousu/seed-vcs/actions/workflows/test.yml)

「そのファイルがまだこの内容なら、これに置き換えていい」で共同開発を組み立てる。
Python 3.12 の標準ライブラリだけで動く、小さなバージョン管理の核です。

## まず試す

Python 3.12以上が必要です。追加パッケージのインストールは不要です。

```sh
git clone https://github.com/marvelousu/seed-vcs.git
cd seed-vcs
python demo.py
python -m unittest -v test_seed.py
```

デモは独立変更の取り込み、同じファイルの競合、参照先の変化、未申告の依存で実行が壊れる例、
同じ提出の再試行を順に実行します。DBは `artifacts/tmp` の専用一時ディレクトリに作成し、終了時に削除します。

## Gitが強い理由と、Scionが変えた前提

Gitの核は、内容からIDを作るオブジェクトの保存、ファイル構成のスナップショット、
親を参照するコミット、それを指す名前です。変更のない内容は共有されます。
手元で履歴を調べ、コミットでき、中央サービスが使えない間も作業できます。
分岐と取り込みを組み合わせれば、独立した組織や保守担当者の間でも開発を進められます。
その核の上に、CLI・ホスティング・レビュー・CIとの接続が積み重なっています。
新しいVCSの採用では、その周辺まで移し替える負担が生じます。

技術の一次資料：[Gitの基本モデル](https://git-scm.com/book/en/v2/Getting-Started-What-is-Git%3F)、
[オブジェクト](https://git-scm.com/book/en/v2/Git-Internals-Git-Objects)、
[分散ワークフロー](https://git-scm.com/book/en/v2/Distributed-Git-Distributed-Workflows)。

AnthropicのScionは、常時オンラインのエージェントとジョブを前提に、
コミット・履歴検索・マージなどの仕事をサーバーへ寄せます。
変更ファイルの記録と検索用の派生データを分け、必要なファイルだけを取得し、
不変の内容を階層的にキャッシュする設計です。
狙いは、巨大モノレポでクライアントが繰り返す転送と計算の削減です。
一般用途のGit代替を目指すものとは説明されていません。
[Anthropicの設計説明](https://www.anthropic.com/engineering/redesigning-source-control)

Gitにも[partial clone](https://git-scm.com/docs/partial-clone)があります。
Gitが必ず全内容をダウンロードする、ブランチが高価、独立変更を統合できない、という前提で比較しません。

## こうすればいいのでは

作業者が提出する単位を、**前提条件＋置換内容**にする。
main全体が進んだかどうかを、取り込み可否の条件にはしない。
書き換えるファイルと、判断のために参照したファイルの内容を照合する。

```text
proposal = {
  base: 作業を始めた履歴ID,
  expected: { path: 前提とした内容のhash または null },
  writes:   { path: 新しい内容のhash または null }
}

land(proposal):
  トランザクション開始
  すでに取り込み済みなら、その結果を返す
  expected の全パスについて current[path] == expected[path] を照合
  全部一致したら、現在の状態へ writes を一括適用
  ファイルとディレクトリの名前衝突を確認
  親＝現在のHEAD、提案＝proposal ID の履歴を追加
  HEADを更新してコミット
  途中で失敗したら、すべてロールバック
```

`null` は「そのパスのファイルがない」です。空ファイルのhashとは別です。
追加・変更・削除を同じ規則で表せます。renameは削除＋追加を一つの提案にします。
書込み先の前提は必須で、自動的にbaseから記録します。
追加の参照先は `reads` / `--read` で明示します。

たとえば同じbaseからAが `a.py`、Bが `b.py` を変更する。
Aを取り込んでも `b.py` が前提のままなら、Bもそのまま取り込めます。
AとBが同じファイルを変えれば、後着を拒否します。
BがAの変更対象を参照先に申告していれば、別ファイルへの書込みでも拒否します。

これは内容アドレス方式と楽観的並行制御の組合せです。
新しいマージアルゴリズムの発明ではありません。
Gitもrebaseやcherry-pickで独立変更を統合できます。
実験の価値は、提出のIDを固定したまま、小さな条件付き操作として取り込めるかを確かめることです。
Gitとの入出力アダプターは将来の採用経路の候補です。現在の形式にGit互換性はありません。
変更のIDをコミットのIDから分ける先例もあります。
[GerritのChange-Id](https://gerrit-review.googlesource.com/Documentation/user-changeid.html)は、
rebaseやcherry-pick後もレビューとの関連を保つために使われています。

## 動かす

PowerShellでCLIを使って独自のファイルを扱う例です。`example.db` が既にある場合、initは上書きしません。

```powershell
python seed.py --db example.db init
$base = python seed.py --db example.db head
$change = python seed.py --db example.db propose --base $base --set hello.txt=README.md --author human --message hello
python seed.py --db example.db show $change
python seed.py --db example.db land $change
python seed.py --db example.db ls
python seed.py --db example.db cat hello.txt
python seed.py --db example.db log
```

`cat` / `ls` に `--rev <履歴ID>` を付けると過去の状態を取得できます。
`--delete <パス>` が削除、`--read <パス>` が追加の前提です。
`--set` の右辺は手元のファイル、左辺はリポジトリ内の論理パスです。
同じ左辺の重複は拒否します。取り込み成功は終了コード0、条件競合は2、その他のエラーは1です。

正本は一つのSQLite DBです。内容は種類付きSHA-256で識別し、提案はcanonical JSONで保存します。
現在のパス表と、変更したパスだけの履歴表を持ちます。
通常の着地で全リポジトリのスナップショットをコピーしません。
履歴IDは親と提案IDから作ります。独立変更を逆順に取り込んだ場合、最終ファイル状態は同じでも履歴IDは異なります。

## 保証する範囲

- 取り込み時に、申告済みの各ファイル内容が前提と一致すること。
- 一つの提案の書込み・履歴・HEADが一括で反映されること。
- 再提出で同じ提案を二度取り込まないこと。
- 別プロセスの着地をSQLiteのトランザクションで直列化し、変更を失わないこと。
- 過去の内容を、削除の記録と空ファイルを区別して取得できること。

参照先は自動捕捉しません。未申告の依存、ディレクトリ列挙に後から増えたファイル、
外部サービス、実行環境、プログラムの意味は照合の対象外です。
取り込み可否はCI成功やレビュー承認を表しません。
動作の確認は、実際にリリースする版に対するテストやレビューで別途行う必要があります。

同じファイルの異なる行への変更も保守的に競合として扱います。
内容がA→B→Aと戻った場合は条件一致です。途中で変更されなかったという保証ではありません。
no-op編集は書込みと暗黙条件から除去します。条件として残したいパスは明示的にreadsへ入れます。
実効書込みのない提案は拒否します。

論理パスは大小文字を区別する相対POSIX形式です。ファイル `pkg` と `pkg/config` は共存させません。
ファイル内容だけを扱い、実行属性・symlink・submodule・Windowsへ書き出せる名前の制約は扱いません。
作業ディレクトリへのcheckout、ネットワーク同期、認証、レビュー基盤もありません。
作者名は自己申告のメタデータです。内容hashは本人確認や承認を証明しません。

SQLiteのwriterは一つです。lock-freeでも、Scion規模の水平分散でもありません。
大規模性能、プロセス強制終了・停電からの復旧、長期運用の耐久性は未検証です。
バックアップにはSQLiteのbackup API等で一貫したコピーを取る必要があります。
書込み中のDBファイルだけをコピーする方法はWALの内容を取りこぼす可能性があります。
これは設計を評価するための試作です。

## 検証とライセンス

22件のテストで独立変更、競合時の全件拒否、明示的な参照条件、複数プロセスによる同時取り込み、
再提出、過去の削除と再作成、途中の保存失敗によるロールバックを確認します。
GitHub ActionsでWindowsとLinuxのPython 3.12を使い、テストとデモを実行します。

[MIT License](LICENSE)。
