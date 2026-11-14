#!/usr/bin/env python3
"""指定した年に受け取る配当金(税引前・税引後)を支払月ごとに見積もるCLIツール。

楽天証券の取引履歴CSV(tradehistory_raw/)から各権利確定日の保有株数を復元し、
IRBANKの配当ページにある中間・期末等の1株配当を掛けて集計する。

考え方:
- 「その年に受け取る配当」= 権利確定日が前年10月末〜当年9月末の配当。支払は権利確定の
  約3か月後とみなして支払月を見積もる(9月権利の中間配当は実際には11月下旬〜12月上旬)。
- 権利確定日は月末とみなし、「受渡日 ≦ 権利確定日」の買付・売付・入庫・出庫を積み上げて
  保有株数を求める。株式分割・株式移転は入庫/出庫の行として出るため、分割前の株数や、
  売却済み・移転前の旧銘柄の配当も自動で拾える。
- IRBANKに当期の配当予想が無い(証券会社など予想非開示の銘柄)、配当表が取れない
  (優先株・新設の持株会社)ものは金額に含めず「要確認」に出す。TDnetの決定開示等で確認して足す。

取引履歴CSVの置き場所:
- 既定ではデメテル(自宅NAS)の share1/rakuten_stocks/ から最新の tradehistory(JP)_*.csv を
  smbclient で tradehistory_raw/ にコピーしてから使う。取れなければローカルの最新を使う。
- holdings_raw/ には置かない(holdings_loader.py が最新の資産残高CSVと誤認するため)。

使い方:
    python3 scripts/dividend_income.py                 # 今年分を月別に集計
    python3 scripts/dividend_income.py --year 2027
    python3 scripts/dividend_income.py --detail        # 銘柄ごとの明細も表示
    python3 scripts/dividend_income.py --csv /tmp/dividends.csv
    python3 scripts/dividend_income.py --no-fetch      # NASから取らずローカルの取引履歴を使う
"""
import argparse
import calendar
import csv
import re
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

import requests

from holdings_loader import HOLDINGS_RAW_DIR, load_held_positions
from irbank_dividend import fetch_dividend_table

REPO_ROOT = Path(__file__).resolve().parent.parent
TRADE_DIR = REPO_ROOT / "tradehistory_raw"
NAS_SHARE = "//192.168.1.3/share1"
NAS_DIR = "rakuten_stocks"
TAX_RATE = 0.20315
PAY_LAG_MONTHS = 3
IRBANK_INTERVAL_SEC = 1.0
# 決算月から何か月前が権利確定月か
PERIOD_OFFSETS = {"1Q": 9, "中間": 6, "3Q": 3, "期末": 0}
_TRADE_NAME_RE = re.compile(r"tradehistory\(JP\)_\d{8}\.csv")
_SIGNS = {"買付": 1, "入庫": 1, "売付": -1, "出庫": -1}


def to_index(year: int, month: int) -> int:
    return year * 12 + month - 1


def from_index(idx: int) -> tuple:
    year, m0 = divmod(idx, 12)
    return year, m0 + 1


def month_end(idx: int) -> date:
    year, month = from_index(idx)
    return date(year, month, calendar.monthrange(year, month)[1])


def fetch_from_nas():
    """NAS上の最新の取引履歴CSVを tradehistory_raw/ にコピーし、そのパスを返す。失敗時はNone。"""
    try:
        listing = subprocess.run(
            ["smbclient", "-U", "%", NAS_SHARE, "-c", f"cd {NAS_DIR}; ls"],
            capture_output=True, text=True, timeout=60, check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError) as e:
        print(f"警告: NASの取引履歴一覧を取得できませんでした({e})", file=sys.stderr)
        return None
    names = sorted(set(_TRADE_NAME_RE.findall(listing)))
    if not names:
        print(f"警告: NASの{NAS_DIR}/に取引履歴CSVがありません", file=sys.stderr)
        return None
    dest = TRADE_DIR / names[-1]
    if dest.exists():
        return dest
    TRADE_DIR.mkdir(exist_ok=True)
    # 一時ファイル名で受けてからrenameする。本来の名前に直接getすると、途中で失敗・タイムアウト
    # したときに壊れたCSVが残り、上の exists() で以後ずっと使われてしまうため(issue #163)。
    # 一時ファイル名は「.」始まりで「.csv」で終わらないので、latest_local() のglobにも引っかからない。
    part = TRADE_DIR / f".{names[-1]}.part"
    try:
        subprocess.run(
            ["smbclient", "-U", "%", NAS_SHARE, "-c", f'cd {NAS_DIR}; get "{names[-1]}" "{part}"'],
            capture_output=True, text=True, timeout=120, check=True,
        )
        if part.stat().st_size == 0:
            raise OSError("取得したファイルが空です")
        part.replace(dest)
    except (OSError, subprocess.SubprocessError) as e:
        part.unlink(missing_ok=True)
        print(f"警告: NASから{names[-1]}を取得できませんでした({e})", file=sys.stderr)
        return None
    return dest


def local_trade_files() -> list:
    """tradehistory_raw/ の取引履歴CSV(楽天のファイル名のまま置かれたもの)を古い順に返す。"""
    if not TRADE_DIR.is_dir():
        return []
    return sorted(p for p in TRADE_DIR.glob("tradehistory*.csv") if _TRADE_NAME_RE.fullmatch(p.name))


def latest_local():
    files = local_trade_files()
    return files[-1] if files else None


def parse_date(s: str) -> date:
    y, m, d = map(int, s.strip().split("/"))
    return date(y, m, d)


# 複数の取引履歴CSVで同じ取引かどうかを見分ける列
_TRADE_KEY_COLS = ("約定日", "受渡日", "銘柄コード", "口座区分", "取引区分", "売買区分", "数量［株］", "単価［円］", "受渡金額［円］")


def _read_trade_rows(path: Path) -> list:
    with path.open(encoding="cp932", newline="") as f:
        return list(csv.DictReader(f))


def merged_trade_rows(paths: list) -> list:
    """複数の取引履歴CSVの行を合わせ、重複を除いて返す。

    楽天からダウンロードする期間はファイルごとに違ってよい(期間を絞った新しいCSVを置いても、
    以前に取り込んだCSVにしか無い古い取引は残る)。同じ取引は複数のCSVに出てくるので、
    キー列が同じ行は「各CSVでの出現回数の最大値」だけ数える。こうすると、同じ日に同じ銘柄を
    同じ数量・単価で2回取引した場合(1つのCSVに同じ行が2行ある)も、2件のまま残せる。
    """
    best = {}
    for path in paths:
        counts = {}
        for row in _read_trade_rows(path):
            key = tuple((row.get(c) or "").strip() for c in _TRADE_KEY_COLS)
            counts.setdefault(key, []).append(row)
        for key, rows in counts.items():
            if len(rows) > len(best.get(key, [])):
                best[key] = rows
    return [row for rows in best.values() for row in rows]


def load_trades(path=None, paths=None) -> list:
    """取引履歴を読み込む。paths を渡すと、それらのCSVを合わせて重複を除いたものを使う。"""
    rows = merged_trade_rows(paths) if paths else _read_trade_rows(path)
    trades = []
    for row in rows:
        kind = row["売買区分"].strip()
        if kind not in _SIGNS:
            print(f"警告: 未対応の売買区分のため無視します: {row['約定日']} {row['銘柄コード']} {kind}",
                  file=sys.stderr)
            continue
        trades.append({
            "trade_date": parse_date(row["約定日"]),
            "settle_date": parse_date(row["受渡日"]),
            "code": row["銘柄コード"].strip(),
            "name": row["銘柄名"].strip(),
            "qty": _SIGNS[kind] * int(row["数量［株］"].replace(",", "")),
        })
    return trades


def holdings_at(trades: list, day: date, key: str = "settle_date") -> dict:
    held = {}
    for t in trades:
        if t[key] <= day:
            held[t["code"]] = held.get(t["code"], 0) + t["qty"]
    return {code: qty for code, qty in held.items() if qty}


def date_in_name(path: Path):
    m = re.search(r"_(\d{4})(\d{2})(\d{2})", path.name)
    return date(*map(int, m.groups())) if m else None


def check_against_snapshot(trades: list, trade_path: Path) -> None:
    """取引履歴から復元した株数を最新の資産残高CSVと突き合わせ、ズレを警告する。"""
    files = sorted(HOLDINGS_RAW_DIR.glob("*.csv")) if HOLDINGS_RAW_DIR.is_dir() else []
    snap_date = date_in_name(files[-1]) if files else None
    if snap_date is None:
        return
    trade_date = date_in_name(trade_path)
    if trade_date is None or trade_date < snap_date:
        print(f"警告: 取引履歴が資産残高CSV({snap_date})より古いため、突き合わせを省略します", file=sys.stderr)
        return
    # 資産残高CSVは約定ベースの株数なので、約定日で積み上げて比べる
    rebuilt = holdings_at(trades, snap_date, key="trade_date")
    snapshot = {code: int(p["quantity"]) for code, p in load_held_positions().items()}
    for code in sorted(set(rebuilt) | set(snapshot)):
        if rebuilt.get(code, 0) != snapshot.get(code, 0):
            print(f"警告: {code} の株数が資産残高CSV({snap_date})と一致しません"
                  f"(取引履歴 {rebuilt.get(code, 0)}株 / CSV {snapshot.get(code, 0)}株)", file=sys.stderr)


def latest_value(rows: list, period: str):
    """表の後ろ(実績→修正→予想の順に新しい)から、その時期の値が入っている最初の行を返す。"""
    for row in reversed(rows):
        value = row["periods"].get(period)
        if value is not None:
            return value, row["status"]
    return None, None


def estimate(year: int, trades: list):
    first, last = to_index(year - 1, 10), to_index(year, 9)
    held_by_month = {idx: holdings_at(trades, month_end(idx)) for idx in range(first, last + 1)}
    names = {t["code"]: t["name"] for t in trades}
    codes = sorted({code for held in held_by_month.values() for code in held})

    entries, unresolved = [], []
    for i, code in enumerate(codes):
        if i:
            time.sleep(IRBANK_INTERVAL_SEC)
        print(f"IRBANK取得中 ({i + 1}/{len(codes)}): {code} {names[code]}", file=sys.stderr)
        try:
            _, groups, _ = fetch_dividend_table(code)
        except (ValueError, requests.RequestException) as e:
            held_months = [idx for idx in held_by_month if code in held_by_month[idx]]
            unresolved.append((code, names[code], f"IRBANKの配当表を取得できません({e})", held_months))
            continue

        groups = [g for g in groups if g["month"]]
        seen_fy = set()
        prev = None
        for g in groups:
            fy_idx = to_index(g["year"], g["month"])
            seen_fy.add(fy_idx)
            for period, offset in PERIOD_OFFSETS.items():
                rec_idx = fy_idx - offset
                if not first <= rec_idx <= last or period not in g["rows"][0]["periods"]:
                    continue
                qty = held_by_month[rec_idx].get(code)
                if not qty:
                    continue
                value, status = latest_value(g["rows"], period)
                if value is None:
                    # 前期に配当があった時期が空欄なら「未定」とみなす
                    prev_value = latest_value(prev["rows"], period)[0] if prev else None
                    if prev_value:
                        unresolved.append((code, names[code],
                                           f"{g['year']}/{g['month']:02d}期の{period}配当が未定(前期{prev_value}円)",
                                           [rec_idx]))
                    continue
                if value > 0:
                    entries.append({
                        "code": code, "name": names[code], "period": period,
                        "record": rec_idx, "pay": rec_idx + PAY_LAG_MONTHS,
                        "qty": qty, "dps": value, "status": status, "amount": qty * value,
                    })
            prev = g

        # IRBANKにまだ載っていない期(予想非開示など)のうち、前期の支払時期に当たる権利日を要確認にする
        if not groups:
            continue
        last_g = groups[-1]
        last_fy = to_index(last_g["year"], last_g["month"])
        for fy_idx in range(last_fy + 12, last + 12 + 1, 12):
            for period, offset in PERIOD_OFFSETS.items():
                rec_idx = fy_idx - offset
                prev_value = latest_value(last_g["rows"], period)[0] if period in last_g["rows"][0]["periods"] else None
                if first <= rec_idx <= last and prev_value and held_by_month[rec_idx].get(code):
                    fy_year, fy_month = from_index(fy_idx)
                    unresolved.append((code, names[code],
                                       f"IRBANKに{fy_year}/{fy_month:02d}期の配当予想なし"
                                       f"({period}、前期{prev_value}円)", [rec_idx]))
    return entries, unresolved


def ym(idx: int) -> str:
    y, m = from_index(idx)
    return f"{y}/{m:02d}"


def print_report(year: int, trade_path: Path, entries: list, unresolved: list, detail: bool) -> None:
    print(f"\n{year}年の受取配当金見込み(取引履歴: {trade_path.name}、税率{TAX_RATE * 100:.3f}%)")
    print(f"{'支払月':>6} {'税引前':>10} {'税引後':>10} {'件数':>4} {'うち予想':>10}")
    total = forecast_total = 0
    for month in range(1, 13):
        items = [e for e in entries if from_index(e["pay"]) == (year, month)]
        gross = sum(e["amount"] for e in items)
        forecast = sum(e["amount"] for e in items if e["status"] != "実績")
        total += gross
        forecast_total += forecast
        print(f"{month:>5}月 {gross:>10,.0f} {gross * (1 - TAX_RATE):>10,.0f} {len(items):>4} {forecast:>10,.0f}")
    print(f"{'合計':>5} {total:>10,.0f} {total * (1 - TAX_RATE):>10,.0f} {len(entries):>4} {forecast_total:>10,.0f}")
    print(f"※支払月は権利確定月+{PAY_LAG_MONTHS}か月の見込み。「うち予想」は実績確定前(予想・修正)の金額。")

    if detail:
        print(f"\n{'支払月':<7} {'コード':<6} {'銘柄':<16} {'区分':<3} {'権利':<7} {'株数':>5} {'1株':>7} {'区分':<3} {'税引前':>9}")
        for e in sorted(entries, key=lambda e: (e["pay"], -e["amount"])):
            print(f"{ym(e['pay']):<9} {e['code']:<7} {e['name'][:14]:<16} {e['period']:<3} {ym(e['record']):<8}"
                  f" {e['qty']:>5} {e['dps']:>7} {e['status']:<3} {e['amount']:>10,.0f}")

    if unresolved:
        print("\n要確認(金額に含めていません):")
        for code, name, reason, months in unresolved:
            span = ym(months[0]) if len(months) == 1 else f"{ym(months[0])}〜{ym(months[-1])}の保有分"
            print(f"  {code} {name}: {reason} 権利月 {span}")


def write_csv(path: str, entries: list) -> None:
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["支払月(見込)", "コード", "銘柄", "区分", "権利確定月", "権利日の株数", "1株配当",
                    "実績/予想", "税引前", "税引後"])
        for e in sorted(entries, key=lambda e: (e["pay"], -e["amount"])):
            w.writerow([ym(e["pay"]), e["code"], e["name"], e["period"], ym(e["record"]), e["qty"], e["dps"],
                        e["status"], round(e["amount"]), round(e["amount"] * (1 - TAX_RATE))])


def main():
    parser = argparse.ArgumentParser(description="指定した年に受け取る配当金を支払月ごとに見積もる")
    parser.add_argument("--year", type=int, default=date.today().year, help="対象の年(既定: 今年)")
    parser.add_argument("--detail", action="store_true", help="銘柄ごとの明細も表示する")
    parser.add_argument("--csv", metavar="PATH", help="明細をCSV(UTF-8)で保存する")
    parser.add_argument("--no-fetch", action="store_true", help="NASから取得せずローカルの取引履歴を使う")
    args = parser.parse_args()

    trade_path = (None if args.no_fetch else fetch_from_nas()) or latest_local()
    if trade_path is None:
        print(f"エラー: 取引履歴CSVがありません({TRADE_DIR}/ に tradehistory(JP)_*.csv を置いてください)",
              file=sys.stderr)
        sys.exit(1)

    # tradehistory_raw/ にある全ての取引履歴CSVを合わせて使う(期間を絞ったCSVでも過去分が消えない)
    trades = load_trades(paths=local_trade_files() or [trade_path])
    check_against_snapshot(trades, trade_path)
    entries, unresolved = estimate(args.year, trades)
    print_report(args.year, trade_path, entries, unresolved, args.detail)
    if args.csv:
        write_csv(args.csv, entries)
        print(f"\n明細を保存しました: {args.csv}")


if __name__ == "__main__":
    main()
