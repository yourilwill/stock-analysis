#!/usr/bin/env python3
"""受取配当金の実績(楽天証券の配当金一覧CSV)と見積もり(dividend_income.py)をまとめるツール。

配当金ページ(dividends.html、build_static_site.py が生成)のデータを用意する。

データの優先順位:
1. 実績: 楽天証券からダウンロードした配当金・分配金一覧 dividends_raw/dividendlist_YYYYMMDD.csv
   (gitignore対象。デメテルの rakuten_stocks/ に置くと、平日夕方の取り込み処理でコピーされる想定)。
   ファイル名の日付までに入金された分を「実績」とする。
2. 推定: CSVの日付より後の月は、dividend_income.py の見積もり(取引履歴＋IRBANKの1株配当)で埋める。
   支払月が今月より前なら「推定」(入金済みのはずだが未確認)、今月以降なら「見込み」。
   CSVの日付と同じ月は、実績にまだ出ていない銘柄の分だけ見積もりを足す。
   見積もりは国内株のみ。米国株は実績CSVだけを参考として扱う。

見積もりは IRBANK に約100回アクセスするので、サイトのビルド時ではなく、月1回
`python3 scripts/dividend_history.py estimate` で market_watch/dividend_estimates/YYYY.json に保存しておく。

使い方:
    python3 scripts/dividend_history.py estimate              # 今年分の見積もりを保存
    python3 scripts/dividend_history.py estimate --year 2027
    python3 scripts/dividend_history.py show                  # 実績＋見積もりの月別集計を表示
"""
import argparse
import csv
import json
import re
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ACTUAL_DIR = REPO_ROOT / "dividends_raw"
ESTIMATE_DIR = REPO_ROOT / "market_watch" / "dividend_estimates"
TAX_RATE = 0.20315
_ACTUAL_NAME_RE = re.compile(r"dividendlist_(\d{8})\.csv")


def _num(s: str) -> float:
    s = s.replace(",", "").strip()
    return float(s) if s not in ("", "-") else 0.0


def latest_actual_csv(actual_dir: Path = ACTUAL_DIR):
    if not actual_dir.is_dir():
        return None
    files = sorted(p for p in actual_dir.glob("dividendlist_*.csv") if _ACTUAL_NAME_RE.fullmatch(p.name))
    return files[-1] if files else None


def load_actual(path: Path):
    """(CSVの日付, 実績のリスト)。金額は円(米国株はCSVの為替レートで換算)。"""
    as_of = datetime.strptime(_ACTUAL_NAME_RE.fullmatch(path.name).group(1), "%Y%m%d").date()
    rows = list(csv.reader(path.read_text(encoding="cp932").splitlines()))
    header_idx = next(i for i, r in enumerate(rows) if r and r[0].startswith("入金日"))
    cols = {h: i for i, h in enumerate(rows[header_idx])}

    def col(r, prefix):
        # 「銘柄」と「銘柄コード」のように前方一致が重なる列があるため、完全一致を優先する
        idx = cols.get(prefix)
        if idx is None:
            idx = next(i for h, i in cols.items() if h.startswith(prefix))
        return r[idx] if idx < len(r) else ""

    entries = []
    for r in rows[header_idx + 1:]:
        if len(r) < len(cols) or not re.fullmatch(r"\d{4}/\d{2}/\d{2}", r[0]):
            continue
        paid = datetime.strptime(r[0], "%Y/%m/%d").date()
        rate = 1.0 if col(r, "受取通貨") == "円" else _num(col(r, "為替レート"))
        entries.append({
            "date": paid.isoformat(),
            "ym": paid.strftime("%Y-%m"),
            "market": "国内株式" if col(r, "商品") == "国内株式" else col(r, "商品"),
            "code": col(r, "銘柄コード"),
            "name": col(r, "銘柄"),
            "dps": col(r, "単価"),
            "qty": _num(col(r, "数量")),
            "gross": round(_num(col(r, "配当金・分配金・利金合計")) * rate),
            "net": round(_num(col(r, "受取金額")) * rate),
        })
    return as_of, entries


def load_estimates(estimate_dir: Path = ESTIMATE_DIR) -> list:
    entries = []
    if not estimate_dir.is_dir():
        return entries
    for path in sorted(estimate_dir.glob("[0-9][0-9][0-9][0-9].json")):
        try:
            entries += json.loads(path.read_text(encoding="utf-8"))["entries"]
        except (OSError, json.JSONDecodeError, KeyError):
            continue
    return entries


def merged_monthly(today: date = None):
    """月別の国内株配当(実績＋推定・見込み)と、米国株等の実績、銘柄別の明細を返す。

    戻り値: dict
      domestic: {"YYYY-MM": {"gross", "net", "kind": "実績"|"一部推定"|"推定"|"見込み"}}
      foreign:  {"YYYY-MM": {"gross", "net"}}
      by_stock: {"YYYY": {(code, name): gross}}
      as_of: 実績CSVの日付(ISO) or None
    """
    today = today or date.today()
    csv_path = latest_actual_csv()
    as_of, actual = load_actual(csv_path) if csv_path else (None, [])
    estimates = load_estimates()
    this_month = today.strftime("%Y-%m")
    as_of_month = as_of.strftime("%Y-%m") if as_of else "0000-00"

    domestic = defaultdict(lambda: {"gross": 0, "net": 0, "kinds": set()})
    foreign = defaultdict(lambda: {"gross": 0, "net": 0})
    # 銘柄別はコードで集計する(実績CSVと見積もりで銘柄名の表記が違うことがあるため)。
    by_stock = defaultdict(lambda: defaultdict(int))
    names = {}
    received_in_month = defaultdict(set)

    for e in actual:
        if e["market"] == "国内株式":
            d = domestic[e["ym"]]
            d["gross"] += e["gross"]
            d["net"] += e["net"]
            d["kinds"].add("実績")
            received_in_month[e["ym"]].add(e["code"])
        else:
            foreign[e["ym"]]["gross"] += e["gross"]
            foreign[e["ym"]]["net"] += e["net"]
        by_stock[e["ym"][:4]][e["code"]] += e["gross"]
        names.setdefault(e["code"], e["name"])

    for e in estimates:
        ym = e["pay"]
        if ym < as_of_month:
            continue  # 実績CSVでカバー済みの月
        if ym == as_of_month and e["code"] in received_in_month[ym]:
            continue  # 同じ月にすでに実績がある銘柄
        kind = "推定" if ym < this_month else "見込み"
        d = domestic[ym]
        d["gross"] += e["gross"]
        d["net"] += round(e["gross"] * (1 - TAX_RATE))
        d["kinds"].add(kind)
        by_stock[ym[:4]][e["code"]] += e["gross"]
        names.setdefault(e["code"], e["name"])

    def kind_of(kinds):
        if kinds == {"実績"}:
            return "実績"
        if "実績" in kinds:
            return "一部見込み" if "見込み" in kinds else "一部推定"
        return "推定" if kinds == {"推定"} else "見込み"

    # 実績CSVでカバーされている期間に入金の無い月は、空欄ではなく0円の実績にする
    if actual:
        first_m = min(e["ym"] for e in actual)
        y, mo = int(first_m[:4]), int(first_m[5:])
        while f"{y:04d}-{mo:02d}" <= as_of_month:
            domestic[f"{y:04d}-{mo:02d}"]["kinds"].add("実績")
            y, mo = (y + 1, 1) if mo == 12 else (y, mo + 1)

    return {
        "domestic": {ym: {"gross": v["gross"], "net": v["net"], "kind": kind_of(v["kinds"])}
                     for ym, v in sorted(domestic.items())},
        "foreign": dict(sorted(foreign.items())),
        "by_stock": {y: {(c, names.get(c, c)): v for c, v in d.items()} for y, d in by_stock.items()},
        "as_of": as_of.isoformat() if as_of else None,
        "first_month": min([e["ym"] for e in actual], default=None),
    }


def cmd_estimate(year: int, no_fetch: bool) -> None:
    import dividend_income as di

    path = None if no_fetch else di.fetch_from_nas()
    path = path or di.latest_local()
    if path is None:
        sys.exit("取引履歴CSV(tradehistory_raw/)がありません")
    trades = di.load_trades(path)
    entries, unresolved = di.estimate(year, trades)
    ESTIMATE_DIR.mkdir(parents=True, exist_ok=True)
    out = {
        "year": year,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "trade_history": path.name,
        "entries": [
            {"pay": "%04d-%02d" % di.from_index(e["pay"]), "code": e["code"], "name": e["name"],
             "period": e["period"], "record": "%04d-%02d" % di.from_index(e["record"]),
             "qty": e["qty"], "dps": e["dps"], "status": e["status"], "gross": round(e["amount"])}
            for e in sorted(entries, key=lambda e: (e["pay"], -e["amount"]))
        ],
        "unresolved": [{"code": c, "name": n, "reason": r} for c, n, r, _ in unresolved],
    }
    target = ESTIMATE_DIR / f"{year}.json"
    tmp = ESTIMATE_DIR / f".{target.name}.part"
    tmp.write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    tmp.replace(target)
    print(f"保存しました: {target}({len(out['entries'])}件、要確認{len(out['unresolved'])}件)")


def cmd_show() -> None:
    m = merged_monthly()
    print(f"実績CSV: {m['as_of'] or 'なし'}")
    for ym, v in m["domestic"].items():
        print(f"{ym} {v['gross']:>10,} {v['net']:>10,} {v['kind']}")


def main():
    parser = argparse.ArgumentParser(description="受取配当金の実績と見積もりをまとめる")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_est = sub.add_parser("estimate", help="dividend_income.pyの見積もりを保存する(IRBANKに約100回アクセス)")
    p_est.add_argument("--year", type=int, default=date.today().year)
    p_est.add_argument("--no-fetch", action="store_true", help="NASから取引履歴を取らずローカルの最新を使う")
    sub.add_parser("show", help="実績＋見積もりの月別集計を表示する")
    args = parser.parse_args()
    if args.cmd == "estimate":
        cmd_estimate(args.year, args.no_fetch)
    else:
        cmd_show()


if __name__ == "__main__":
    main()
