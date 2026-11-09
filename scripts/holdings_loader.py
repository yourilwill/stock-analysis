#!/usr/bin/env python3
"""楽天証券の資産残高CSV(holdings_raw/)から保有銘柄を読み込むユーティリティ。

ファイル形式の詳細はpersonal_memoリポジトリの
design_doc/stock-earnings-notify-holdings.md参照。
"""
import csv
import re
from pathlib import Path

HOLDINGS_RAW_DIR = Path(__file__).resolve().parent.parent / "holdings_raw"
# 通常の4桁数字(7203)に加え、英字入りの新形式コード(545A・130A等)と
# 優先株などの5桁コード(25935)も銘柄コードとして受け付ける。
_CODE_RE = re.compile(r"^(?:\d{4,5}|\d{3}[A-Z])$")


def load_held_stocks() -> dict:
    """{証券コード: 銘柄名} を返す。

    holdings_raw/にCSVが1件も無い場合は空dictを返す(呼び出し側は
    保有銘柄0件として扱う)。複数ファイルがある場合はファイル名の
    タイムスタンプ部分(assetbalance(JP)_YYYYMMDD_HHMMSS.csv)が
    昇順ソートでそのまま新しさの判定に使えるため、末尾(最新)を選ぶ。
    """
    if not HOLDINGS_RAW_DIR.is_dir():
        return {}
    csv_files = sorted(HOLDINGS_RAW_DIR.glob("*.csv"))
    if not csv_files:
        return {}
    latest = csv_files[-1]

    held = {}
    with latest.open(encoding="cp932", newline="") as f:
        for row in csv.reader(f):
            if len(row) < 2:
                continue
            code, name = row[0].strip(), row[1].strip()
            # サマリー行・口座区分見出し行・合計行は銘柄コード列が
            # 上記の形式にならないため、このバリデーションだけで自然に弾ける。
            if _CODE_RE.fullmatch(code):
                held[code] = name
    return held


def _to_float(s: str):
    s = s.replace(",", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def load_held_positions() -> dict:
    """{証券コード: {"name", "quantity", "avg_cost"}} を返す(簿価利回りの計算用)。

    「銘柄コード」で始まる見出し行から列位置を決める。同じ銘柄が複数の口座区分に
    あれば数量を合算し、平均取得価額は数量の加重平均にする。
    """
    if not HOLDINGS_RAW_DIR.is_dir():
        return {}
    csv_files = sorted(HOLDINGS_RAW_DIR.glob("*.csv"))
    if not csv_files:
        return {}

    positions = {}
    cols = None
    with csv_files[-1].open(encoding="cp932", newline="") as f:
        for row in csv.reader(f):
            if row and row[0].strip() == "銘柄コード":
                cols = {h.strip(): i for i, h in enumerate(row)}
                continue
            if cols is None or len(row) < 2 or not _CODE_RE.fullmatch(row[0].strip()):
                continue
            qty_col = next((i for h, i in cols.items() if h.startswith("保有数量")), None)
            cost_col = next((i for h, i in cols.items() if h.startswith("平均取得価額")), None)
            qty = _to_float(row[qty_col]) if qty_col is not None else None
            cost = _to_float(row[cost_col]) if cost_col is not None else None
            if not qty or cost is None:
                continue
            code = row[0].strip()
            p = positions.setdefault(code, {"name": row[1].strip(), "quantity": 0.0, "avg_cost": 0.0})
            total_cost = p["quantity"] * p["avg_cost"] + qty * cost
            p["quantity"] += qty
            p["avg_cost"] = total_cost / p["quantity"]
    return positions


if __name__ == "__main__":
    import json
    print(json.dumps(load_held_stocks(), ensure_ascii=False, indent=2))
