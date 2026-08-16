#!/usr/bin/env python3
"""楽天証券の資産残高CSV(holdings_raw/)から保有銘柄を読み込むユーティリティ。

ファイル形式の詳細はpersonal_memoリポジトリの
design_doc/stock-earnings-notify-holdings.md参照。
"""
import csv
import re
from pathlib import Path

HOLDINGS_RAW_DIR = Path(__file__).resolve().parent.parent / "holdings_raw"
_CODE_RE = re.compile(r"^\d{4}$")


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
            # 4桁数字にならないため、このバリデーションだけで自然に弾ける。
            if _CODE_RE.fullmatch(code):
                held[code] = name
    return held


if __name__ == "__main__":
    import json
    print(json.dumps(load_held_stocks(), ensure_ascii=False, indent=2))
