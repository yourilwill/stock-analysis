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
#
# 自己修復・多重起動防止（personal_memo issue #8、
# design_doc/stock-analysis-watch-self-healing.md）:
# - systemdのwatchdog機構（WatchdogSec）向けに、外側の待機ループ・デバウンス
#   待ちループ・ビルド中いずれのフェーズでも一定間隔で`systemd-notify WATCHDOG=1`
#   を送る。systemd側で`stock_analysis_watch_notify_enabled: true`にするまでは
#   Type=simpleのままなのでこのpingは単に無視される（systemd-notifyはNOTIFY_SOCKET
#   未設定なら何もしないため、Type=notify化前でも安全に呼べる）。
# - 起動直後の初回ビルド後に`systemd-notify --ready`を送る。Type=notifyの
#   ユニットはこれが来るまで「起動中」扱いのままになるため必須。
# - `flock`で多重起動を防止する（issue #8で原因不明の重複プロセスが観測された
#   ため、原因不問の防御策として導入）。
set -euo pipefail
cd "$(dirname "$0")/.."

SRC="analysis_results"
OUT_DIR="${OUT_DIR:-analysis_results/_site}"
DEBOUNCE_SEC=5
EVENTS="close_write,create,delete,move"
EXCLUDE='/_site/'
SLACK_LIB="/usr/local/lib/hestia-health/slack_notify.sh"
LOCK_FILE="/run/stock-analysis-site-watch.lock"
WATCHDOG_POLL_SEC=30
BUILD_HEARTBEAT_SEC=15
HEARTBEAT_LOG_SEC=3600

# 多重起動防止: ロックを取得できなければ既に別インスタンスが動いているとみなし、
# 何もせず即終了する（/runはtmpfsのため再起動時に自動でクリアされる）。
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  echo "[$(date '+%F %T')] 既に別インスタンスが起動中のため終了します(ロック: $LOCK_FILE)" >&2
  exit 1
fi

send_watchdog_ping() {
  command -v systemd-notify >/dev/null 2>&1 && systemd-notify WATCHDOG=1 2>/dev/null || true
}

notify_ready() {
  command -v systemd-notify >/dev/null 2>&1 && systemd-notify --ready 2>/dev/null || true
}

LAST_BUILD_AT="未実施"
LAST_HEARTBEAT_LOG_AT=$(date +%s)

# watchdog用のpingとは別に、もっと粗い間隔で「稼働中・直近ビルド時刻」を
# journalctlへ出す。無変更が続く沈黙と、実は死んでいる沈黙を区別できるように
# するための観測性向上（design_doc参照）。
maybe_log_heartbeat() {
  local now
  now=$(date +%s)
  if (( now - LAST_HEARTBEAT_LOG_AT >= HEARTBEAT_LOG_SEC )); then
    echo "[$(date '+%F %T')] 稼働中(直近ビルド: ${LAST_BUILD_AT})"
    LAST_HEARTBEAT_LOG_AT=$now
  fi
}

notify_build_failure() {
  if [ -f "$SLACK_LIB" ]; then
    # shellcheck source=/dev/null
    source "$SLACK_LIB"
    notify_slack "うぅ…ごめんね、キミ…株分析サイトのビルドに失敗しちゃったよ…次の変更が来たらまた試すね。" || true
  fi
}

run_build() {
  LAST_BUILD_AT="$(date '+%F %T')"
  if ! python3 scripts/build_static_site.py --out "$OUT_DIR"; then
    echo "[$(date '+%F %T')] ビルド失敗" >&2
    notify_build_failure
    return 1
  fi
}

# run_buildの所要時間はビルド対象データ量次第で伸びうるため、WatchdogSecを
# ビルド時間に依存させない設計にする（design_doc参照）。ビルド中はバックグラウンド
# で短間隔にWATCHDOG=1を送り続け、ビルドが終わったら止める。
run_build_with_heartbeat() {
  local hb_pid=""
  if command -v systemd-notify >/dev/null 2>&1; then
    (
      while true; do
        sleep "$BUILD_HEARTBEAT_SEC"
        systemd-notify WATCHDOG=1 2>/dev/null || true
      done
    ) &
    hb_pid=$!
  fi

  local result=0
  run_build || result=$?

  if [ -n "$hb_pid" ]; then
    kill "$hb_pid" 2>/dev/null || true
    wait "$hb_pid" 2>/dev/null || true
  fi
  return "$result"
}

echo "[$(date '+%F %T')] 監視開始: $SRC/**/*.html (除外: _site/, 出力先: $OUT_DIR)"

# 起動直後は次のHTML更新イベントを待たず、まず一度ビルドしておく
# （再起動直後に公開ディレクトリが空/古いままになるのを防ぐため）
run_build_with_heartbeat || true

# 初回ビルドの成否によらず、ここでready通知する。プロセス自体は既に監視を
# 開始できる状態にあり、Type=notifyのユニットはこれが来るまで「起動中」の
# ままになってしまうため。
notify_ready

while true; do
  # *.html関連のイベントが来るまで待つ（raw_data_*.mdの更新は無視）。
  # タイムアウト付きにして、イベントが長時間来なくても定期的にwatchdog pingと
  # ハートビートログを出せるようにしている（design_doc参照。タイムアウト無しだと
  # 健全な待機中に一度もpingが送れず、watchdogに誤って再起動させられてしまう）。
  while true; do
    if changed=$(inotifywait -r -t "$WATCHDOG_POLL_SEC" -e "$EVENTS" --exclude "$EXCLUDE" --format '%f' "$SRC" 2>/dev/null); then
      send_watchdog_ping
      maybe_log_heartbeat
      [[ "$changed" == *.html ]] && break
      continue
    else
      # $?はここで即座に取る(if/fiの間にelseを挟まないと、if文自体の終了コード
      # (=0)を拾ってしまいinotifywaitの本当の終了コードが失われるため)。
      status=$?
      send_watchdog_ping
      maybe_log_heartbeat
      if [ "$status" -eq 2 ]; then
        # -tのタイムアウト(イベント無し)。エラーではないので黙って次の周回へ。
        continue
      fi
      echo "[$(date '+%F %T')] inotifywait失敗、1秒後に再試行" >&2
      sleep 1
    fi
  done
  # 静穏化待ち: DEBOUNCE_SEC秒間イベントが来なくなるまでループ。
  # こちらも1周がDEBOUNCE_SEC(短い)ごとなので、そのついでにwatchdog pingを送る。
  while inotifywait -r -t "$DEBOUNCE_SEC" -e "$EVENTS" --exclude "$EXCLUDE" "$SRC" >/dev/null 2>&1; do
    send_watchdog_ping
  done
  send_watchdog_ping
  echo "[$(date '+%F %T')] 変更検知、ビルド実行"
  run_build_with_heartbeat || true
done
