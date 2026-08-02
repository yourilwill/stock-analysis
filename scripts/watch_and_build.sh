#!/usr/bin/env bash
# analysis_results/ 配下のHTMLレポート生成・更新を検知して build_static_site.py を
# 自動実行する。
#
# 監視トリガーは *.html のみに絞っている。raw_data_*.md は1回の銘柄分析中に
# 13ステップ分Editツールで何度も追記されるため、これをトリガーに含めると
# 分析1回につき何度もフルビルドが走って負荷になる。Web上で見たいのは
# 最終成果物のHTMLレポートなので、その生成・更新だけを起点にする
# （このinotify-toolsのバージョンは --include と --exclude を同時指定できないため、
#  拡張子の判定はスクリプト側で行っている）。
# イベントも modify ではなく close_write ベースにし、書き込み途中のファイルを
# 読みに行くことを避ける。
set -euo pipefail
cd "$(dirname "$0")/.."

SRC="analysis_results"
DEBOUNCE_SEC=5
EVENTS="close_write,create,delete,move"
EXCLUDE='/_site/'

echo "[$(date '+%F %T')] 監視開始: $SRC/**/*.html (除外: _site/)"

while true; do
  # *.html関連のイベントが来るまで待つ（raw_data_*.mdの更新は無視）
  while true; do
    changed=$(inotifywait -r -e "$EVENTS" --exclude "$EXCLUDE" --format '%f' "$SRC" 2>/dev/null)
    [[ "$changed" == *.html ]] && break
  done
  # 静穏化待ち: DEBOUNCE_SEC秒間イベントが来なくなるまでループ
  while inotifywait -r -t "$DEBOUNCE_SEC" -e "$EVENTS" --exclude "$EXCLUDE" "$SRC" >/dev/null 2>&1; do
    :
  done
  echo "[$(date '+%F %T')] 変更検知、ビルド実行"
  python3 scripts/build_static_site.py
done
