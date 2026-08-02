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
#
# ビルド失敗・inotifywait失敗のいずれも、この監視ループ自体は落とさない
# （落ちると次のHTML更新まで誰も気づけないため）。ビルド失敗時は
# /usr/local/lib/hestia-health/slack_notify.sh がある環境（hestia本体）
# ではSlack通知する。
set -euo pipefail
cd "$(dirname "$0")/.."

SRC="analysis_results"
OUT_DIR="${OUT_DIR:-analysis_results/_site}"
DEBOUNCE_SEC=5
EVENTS="close_write,create,delete,move"
EXCLUDE='/_site/'
SLACK_LIB="/usr/local/lib/hestia-health/slack_notify.sh"

notify_build_failure() {
  if [ -f "$SLACK_LIB" ]; then
    # shellcheck source=/dev/null
    source "$SLACK_LIB"
    notify_slack "うぅ…ごめんね、キミ…株分析サイトのビルドに失敗しちゃったよ…次の変更が来たらまた試すね。" || true
  fi
}

run_build() {
  if ! python3 scripts/build_static_site.py --out "$OUT_DIR"; then
    echo "[$(date '+%F %T')] ビルド失敗" >&2
    notify_build_failure
    return 1
  fi
}

echo "[$(date '+%F %T')] 監視開始: $SRC/**/*.html (除外: _site/, 出力先: $OUT_DIR)"

# 起動直後は次のHTML更新イベントを待たず、まず一度ビルドしておく
# （再起動直後に公開ディレクトリが空/古いままになるのを防ぐため）
run_build || true

while true; do
  # *.html関連のイベントが来るまで待つ（raw_data_*.mdの更新は無視）
  while true; do
    if ! changed=$(inotifywait -r -e "$EVENTS" --exclude "$EXCLUDE" --format '%f' "$SRC" 2>/dev/null); then
      echo "[$(date '+%F %T')] inotifywait失敗、1秒後に再試行" >&2
      sleep 1
      continue
    fi
    [[ "$changed" == *.html ]] && break
  done
  # 静穏化待ち: DEBOUNCE_SEC秒間イベントが来なくなるまでループ
  while inotifywait -r -t "$DEBOUNCE_SEC" -e "$EVENTS" --exclude "$EXCLUDE" "$SRC" >/dev/null 2>&1; do
    :
  done
  echo "[$(date '+%F %T')] 変更検知、ビルド実行"
  run_build || true
done
