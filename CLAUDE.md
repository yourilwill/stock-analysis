# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 現在の状態

このリポジトリは現時点では空です（ソースコード・README・ビルド設定はまだありません）。そのため、ビルド・lint・テストのコマンドや、説明すべきアーキテクチャもまだ存在しません。

## 進め方

このリポジトリは日本株（Japanese stock）の銘柄分析を目的としているが、現時点ではPythonなどの開発環境構築は行わない方針。

代わりに、ユーザーが具体的な銘柄について分析作業をその場で指示し、Claude Codeがその都度分析を実施する。分析作業が固まったら、再利用できる形で `Skill` として登録していく運用とする。

コード基盤（pip/requirements.txt、notebook構成など）が実際に必要になった時点で、このファイルを実際のコマンドとモジュール構成の説明で更新すること。プレースホルダーのまま放置しないこと。

## ドキュメント作成方針

README・CLAUDE.mdなど、このリポジトリ内のドキュメントは基本的に日本語で作成する。
