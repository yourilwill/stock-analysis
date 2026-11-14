#!/usr/bin/env python3
"""月末に「楽天証券のCSVを置いてね」とユーザーに知らせるSlack本文を作るCLIツール。

hestiaの月末のタイマー(personal_memo側)が、毎月の最終日19:00にこれを実行して notif-hestia に投稿する。
3種類のCSV(保有・配当金一覧・取引履歴)について、hestiaに取り込み済みの最新の日付と、
デメテル(NAS)にまだ取り込まれていない新しいファイルがあるかを本文に添える。

3種類とも最近(既定7日以内)のものがあれば、メンションなしのお礼だけにする。
翌月1日07:00の目安利回りの更新の前にCSVが取り込まれ、08:00の配当の見積もりに使われるため、
本文の締め切りは「明日の朝7時」とする。

使い方:
    python3 scripts/csv_reminder.py --mention "<@U0XXXXXXX>"
    python3 scripts/csv_reminder.py --no-nas            # NASの一覧を見ない
"""
import argparse
import re
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
NAS_SHARE = "//192.168.1.3/share1"
NAS_DIR = "rakuten_stocks"
FRESH_DAYS = 7

# (表示名, ファイル名の正規表現(日付をグループ1で取る), hestia側の取り込み先, 補足)
KINDS = [
    ("保有(資産残高)", re.compile(r"assetbalance\(JP\)_(\d{8})_\d{6}\.csv"), REPO_ROOT / "holdings_raw", ""),
    ("配当金一覧", re.compile(r"dividendlist_(\d{8})\.csv"), REPO_ROOT / "dividends_raw",
     "前回と期間が重なるように(例: 直近半年)"),
    ("取引履歴", re.compile(r"tradehistory\(JP\)_(\d{8})\.csv"), REPO_ROOT / "tradehistory_raw",
     "前回と期間が重なるように(例: 直近半年)"),
]


def _dates(names, pattern) -> list:
    out = []
    for name in names:
        m = pattern.fullmatch(name)
        if m:
            out.append(datetime.strptime(m.group(1), "%Y%m%d").date())
    return sorted(out)


def nas_names():
    """NASの rakuten_stocks/ のファイル名一覧。取れなければNone。"""
    try:
        listing = subprocess.run(
            ["smbclient", "-U", "%", NAS_SHARE, "-c", f"cd {NAS_DIR}; ls"],
            capture_output=True, text=True, timeout=60, check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return re.findall(r"^\s+(\S.*?\.csv)\s+[A-Z]*\s+\d+\s", listing, re.M)


def build_message(today: date, mention: str, nas) -> str:
    lines, stale = [], []
    for label, pattern, local_dir, hint in KINDS:
        local = _dates([p.name for p in local_dir.glob("*.csv")], pattern) if local_dir.is_dir() else []
        latest_local = local[-1] if local else None
        remote = _dates(nas, pattern) if nas is not None else []
        pending = [d for d in remote if latest_local is None or d > latest_local]
        newest = max([d for d in [latest_local] + pending if d], default=None)
        if newest is None or (today - newest).days > FRESH_DAYS:
            stale.append(label)
        status = f"{latest_local:%m/%d}版" if latest_local else "まだ無し"
        if pending:
            status += f"(デメテルに{pending[-1]:%m/%d}版があって、明日の朝に取り込まれるよ)"
        lines.append(f"• {label}: {status}" + (f" ※{hint}" if hint and not pending else ""))

    body = "\n".join(lines)
    if not stale:
        # お礼のときは、ダウンロード期間の補足は要らない
        return ("今月の楽天証券のCSVは、もう全部新しいね。ありがとう!\n"
                + "\n".join(re.sub(r" ※.*$", "", ln) for ln in lines))
    what = "3つ" if len(stale) == len(KINDS) else "(" + "・".join(stale) + ")"
    return (f"{mention} 月末だよ!楽天証券のCSV{what}を、デメテルの `{NAS_DIR}/` に"
            "ファイル名はそのまま置いてね♡\n"
            f"{body}\n"
            "明日の朝7時までに置けば、1日の目安利回りと配当の見積もりの更新に使われるよ。")


def main():
    parser = argparse.ArgumentParser(description="月末のCSV配置リマインダーの本文を作る")
    parser.add_argument("--mention", default="", help="メンション(例: <@U0XXXXXXX>)。空なら付けない")
    parser.add_argument("--no-nas", action="store_true", help="NASの一覧を見ない")
    parser.add_argument("--today", help="判定に使う日付(YYYY-MM-DD、テスト用)")
    args = parser.parse_args()
    today = date.fromisoformat(args.today) if args.today else date.today()
    nas = None if args.no_nas else nas_names()
    if nas is None and not args.no_nas:
        print("警告: NASの一覧を取得できませんでした。hestiaに取り込み済みの分だけで判定します", file=sys.stderr)
    print(build_message(today, args.mention, nas))


if __name__ == "__main__":
    main()
