# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## リポジトリの目的

日本株（Japanese stock）の個別銘柄について、Claude Codeが13ステップの定性・定量分析を行い、HTMLレポートを出力するためのリポジトリ。分析ロジックは `.claude/skills/jp-stock-analysis/SKILL.md` にSkillとして定義されている。

## ディレクトリ構成

- `.claude/skills/jp-stock-analysis/` — 銘柄分析Skill本体（`SKILL.md`）とHTMLレポートのテンプレート（`templates/report.html`）。
- `scripts/` — IRBANK（https://irbank.net/）から指標・財務データを取得するPython CLIツール群。WebFetchより大幅にコンテキストを節約するため、Skillの各ステップから呼び出される。
- `analysis_results/` — 分析結果の出力先（`<証券コード>_<会社名>/` ごとにraw_data・HTMLレポートを格納）。**gitignore対象のためリポジトリには含まれず、マシンごとにローカルへ蓄積される。**

## 開発環境

- Python 3.10以上、`requests` ライブラリが必要（`pip install -r requirements.txt`）。
- `scripts/` 配下は単体のCLIツールとして `python3 scripts/irbank_xxx.py <銘柄コード or Eコード>` の形で実行する。ビルド・lint・テストの仕組みは無い。

## 使い方

具体的な銘柄について「〇〇を分析して」のように指示すると、`jp-stock-analysis` Skillが起動し13ステップの分析を行う。分析ロジックの詳細・各ステップの担当範囲・サブエージェントへの並列委譲方針はSKILL.mdを参照。分析作業のやり方が固まったら、再利用できる形でSkillを更新していく運用とする。

## 権限設定について

`.claude/settings.local.json`（WebFetch許可ドメイン、git操作の許可等）は**gitignore対象でマシンごとに独立している**。新しい環境で使い始める場合、分析対象企業の公式サイトドメインへのWebFetch許可などが初回に都度求められる。

## 複数マシンでの運用について

このリポジトリは複数のマシン（自宅PC、hestia＝ラズパイ上のClaude Code等）から`git clone`して同じSkill（`jp-stock-analysis`）で分析作業を行う運用を想定している。
- `analysis_results/`はgitignore対象のため、リポジトリをcloneしただけでは他マシンの過去の分析実績は付いてこない。必要な場合は`rsync`等で保有者が手動同期する（自動同期の仕組みは無い）。ある時点で急に大量のファイルが増えている場合は、この手動同期によるもの。
- 各マシンの分析結果はそれぞれのマシンにローカルに蓄積されていく想定で、どこか一箇所に集約する運用にはなっていない（今後変わる可能性はある）。
- コード（`scripts/`・`.claude/skills/`）側の変更はこのリポジトリ経由で共有されるため、あるマシンで分析手順を改善したら、コミット・pushして他マシンにも`git pull`で反映するのが望ましい。

## ドキュメント作成方針

README・CLAUDE.mdなど、このリポジトリ内のドキュメントは基本的に日本語で作成する。
