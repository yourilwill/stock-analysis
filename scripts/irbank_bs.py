#!/usr/bin/env python3
"""IRBANKから指定銘柄の財務推移（BS・自己資本比率等）を取得するCLIツール。

使い方:
    python3 irbank_bs.py <銘柄コード or Eコード>
    python3 irbank_bs.py 6817
    python3 irbank_bs.py E01971
    python3 irbank_bs.py 6817 --years 5
    python3 irbank_bs.py 2169 --detail   # ステップ10用: 最新期の詳細BS＋ネットキャッシュ計算
"""
import argparse
import re
import sys
import requests

from irbank_utils import fetch_with_retry

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"


def strip_tags(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s).strip()


def parse_number(s: str):
    s = strip_tags(s).replace(",", "").replace("△", "-").replace("▲", "-")
    if s in ("", "-"):
        return None
    try:
        return float(s) if "." in s else int(s)
    except ValueError:
        return None


def parse_percent(s: str):
    s = strip_tags(s).replace(",", "").rstrip("%")
    if s in ("", "-"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def fmt(v) -> str:
    if v is None:
        return "-"
    if isinstance(v, int):
        return f"{v:,}"
    return f"{v:,.1f}"


def resolve_ecode(code: str) -> str:
    if re.match(r"^E\d+$", code, re.I):
        return code.upper()
    resp = fetch_with_retry(f"https://irbank.net/{code}", allow_redirects=True)
    m = re.search(r'href="/(E\d{5})', resp.text)
    if m:
        return m.group(1)
    raise ValueError(f"Eコードが解決できませんでした（コード: {code}）")


def get_latest_annual_doc_id(ecode: str) -> str:
    """CFサマリーページから最新の有価証券報告書のdoc_idを取得する。"""
    url = f"https://irbank.net/{ecode}/cf"
    resp = fetch_with_retry(url)
    tbody = re.search(r"<tbody>(.*?)</tbody>", resp.text, re.S)
    if not tbody:
        raise ValueError(f"CFテーブルが見つかりません: {url}")
    annual_docs = re.findall(r'href="(S\w+)/cf">通期</a>', tbody.group(1))
    if not annual_docs:
        raise ValueError(f"通期報告書のdoc_idが見つかりません: {url}")
    return annual_docs[-1]  # 最新期


def fetch_bs_detail(ecode: str, doc_id: str):
    """最新期の詳細BS（ステップ10: ネットキャッシュ計算用）を取得する。

    Returns:
        data: 行ラベル→最新期値の辞書
        unit: 単位文字列（"千円" or "百万円"）
        url: 取得元URL
    """
    url = f"https://irbank.net/{ecode}/{doc_id}/bs"
    resp = fetch_with_retry(url)

    # 単位を caption から取得（「千円」か「百万円」）
    cap_m = re.search(r"<caption[^>]*>.*?（(千円|百万円)）", resp.text, re.S)
    unit = cap_m.group(1) if cap_m else "千円"

    t_m = re.search(r"<table[^>]*>(.*?)</table>", resp.text, re.S)
    if not t_m:
        raise ValueError(f"BSテーブルが見つかりません: {url}")

    data = {}
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", t_m.group(1), re.S):
        cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S)
        vals = [re.sub(r"<[^>]+>", "", c).replace("　", "").replace(",", "").replace("－", "0").strip() for c in cells]
        if len(vals) >= 2:
            label = vals[0]
            value = vals[-1]  # 最後の列 = 最新期
            # 空値はスキップし、値のある初出のラベルを優先
            if label not in data and value:
                data[label] = value

    return data, unit, url


def fetch_bs(code: str):
    url = f"https://irbank.net/{code}/bs"
    resp = fetch_with_retry(url, allow_redirects=True)
    html = resp.text

    name_m = re.search(r'<meta property="og:title" content="([^（\(]+)', html)
    company = name_m.group(1).strip() if name_m else code

    table_m = re.search(r'<table class="cs">(.*?)</table>', html, re.S)
    if not table_m:
        raise ValueError(f"BSテーブルが見つかりません: {url}")
    table = table_m.group(1)

    thead_m = re.search(r"<thead>(.*?)</thead>", table, re.S)
    if not thead_m:
        raise ValueError("BSテーブルのヘッダーが見つかりませんでした")
    headers = [strip_tags(h) for h in re.findall(r"<th[^>]*>(.*?)</th>", thead_m.group(), re.S)]
    # [年度, 総資産, 株主資本, 利益剰余金, 有利子負債, 現金等, 株主資本比率, 有利子負債比率, のれん]
    n_cols = len(headers)

    tbody_m = re.search(r"<tbody>(.*?)</tbody>", table, re.S)
    if not tbody_m:
        raise ValueError("BSテーブルのボディが見つかりませんでした")
    rows_html = re.findall(r"<tr[^>]*>(.*?)</tr>", tbody_m.group(), re.S)

    all_rows = []
    for row_html in rows_html:
        cells_raw = re.findall(r"<td([^>]*)>(.*?)</td>", row_html, re.S)
        if len(cells_raw) < n_cols:
            continue
        year_str = strip_tags(cells_raw[0][1])
        m = re.match(r"(\d{4})/(\d{1,2})", year_str)
        if not m:
            continue

        def get(idx):
            return cells_raw[idx][1] if idx < len(cells_raw) else ""

        all_rows.append({
            "label": year_str,
            "year": int(m.group(1)),
            "month": int(m.group(2)),
            "total_assets": parse_number(get(1)),
            "equity": parse_number(get(2)),
            "retained": parse_number(get(3)),
            "debt": parse_number(get(4)),
            "cash": parse_number(get(5)),
            "equity_ratio": parse_percent(get(6)),
            "debt_ratio": parse_percent(get(7)),
        })

    # 決算月を自動検出: 最頻出の月 = 本決算月
    if all_rows:
        from collections import Counter
        fiscal_month = Counter(r["month"] for r in all_rows).most_common(1)[0][0]
        results = [r for r in all_rows if r["month"] == fiscal_month]
    else:
        results = []

    return company, results, url


def main():
    parser = argparse.ArgumentParser(description="IRBANKから財務推移(BS)を取得")
    parser.add_argument("code", help="銘柄コード(例: 6817) または Eコード(例: E01971)")
    parser.add_argument("--years", type=int, default=None, help="直近N期分のみ表示 (省略時: 全件)")
    parser.add_argument("--detail", action="store_true", help="最新期の詳細BS＋ネットキャッシュ計算を追加出力（ステップ10用）")
    args = parser.parse_args()

    try:
        company, rows, url = fetch_bs(args.code)
    except (ValueError, requests.RequestException) as e:
        print(f"エラー: {e}", file=sys.stderr)
        sys.exit(1)

    if not rows:
        print(f"エラー: {company}({args.code}) の財務データが取得できませんでした", file=sys.stderr)
        sys.exit(1)

    display = rows[-args.years:] if args.years else rows

    print(f"{company}({args.code}) 財務推移・百万円")
    print(f"{'年度':<10} {'総資産':>10} {'株主資本':>10} {'利益剰余金':>10} {'有利子負債':>10} {'現金等':>10} {'自己資本比率':>12} {'有利子負債比率':>14}")
    print("-" * 94)
    for r in display:
        eq_r = f"{r['equity_ratio']:.1f}%" if r["equity_ratio"] is not None else "-"
        debt_r = f"{r['debt_ratio']:.1f}%" if r["debt_ratio"] is not None else "-"
        print(
            f"{r['label']:<10} {fmt(r['total_assets']):>10} {fmt(r['equity']):>10} "
            f"{fmt(r['retained']):>10} {fmt(r['debt']):>10} {fmt(r['cash']):>10} "
            f"{eq_r:>12} {debt_r:>14}"
        )
    print(f"出典: {url}")

    if not args.detail:
        return

    # --detail: 最新期の詳細BS＋ネットキャッシュ計算（ステップ10用）
    try:
        ecode = resolve_ecode(args.code)
        doc_id = get_latest_annual_doc_id(ecode)
        detail, unit, detail_url = fetch_bs_detail(ecode, doc_id)
    except (ValueError, requests.RequestException) as e:
        print(f"エラー: {e}", file=sys.stderr)
        return

    print()
    print(f"【最新期の詳細BS・{unit}（ネットキャッシュ計算用）】")

    def get_int(*keys: str):
        """優先順にキーを試し、最初に見つかった非空の値をintで返す（J-GAAP/IFRS両対応）。"""
        for key in keys:
            raw = detail.get(key, "")
            if not raw:
                continue
            raw = raw.replace("△", "-").replace("▲", "-").replace("−", "-")
            try:
                return int(float(raw))
            except (ValueError, TypeError):
                continue
        return None

    # J-GAAPのラベルを優先し、IFRSラベルをフォールバックとして列挙
    current_assets = get_int("流動資産計", "流動資産合計")
    inv_securities = get_int("投資有価証券")
    current_liab = get_int("流動負債計", "流動負債合計")
    fixed_liab = get_int("固定負債計", "非流動負債合計")
    total_liab = get_int("負債の部合計", "負債合計")
    equity_total = get_int("株主資本合計", "親会社の所有者に帰属する持分合計")
    net_assets = get_int("純資産の部合計", "資本合計", "持分合計")
    cash = get_int("現金及び預金", "現金及び現金同等物", "現金及び現金同等物（IFRS）")
    treasury = get_int("自己株式")

    def fk(v):
        return f"{v:,}" if v is not None else "-"

    print(f"  流動資産合計    : {fk(current_assets):>14}")
    print(f"  投資有価証券    : {fk(inv_securities):>14}")
    print(f"  負債合計        : {fk(total_liab):>14}")
    print(f"    うち流動負債  : {fk(current_liab):>14}")
    print(f"    うち固定負債  : {fk(fixed_liab):>14}")
    print(f"  株主資本合計    : {fk(equity_total):>14}")
    print(f"  純資産合計      : {fk(net_assets):>14}")
    print(f"  現金及び預金    : {fk(cash):>14}")
    print(f"  自己株式        : {fk(treasury):>14}")

    # ネットキャッシュ計算
    if current_assets is not None and total_liab is not None:
        inv_adj = round((inv_securities or 0) * 0.7)
        net_cash = current_assets + inv_adj - total_liab
        # 百万円換算（千円単位なら÷1000、百万円単位ならそのまま）
        net_cash_m = round(net_cash / 1000) if unit == "千円" else net_cash
        print()
        print("【ネットキャッシュ計算】")
        inv_str = f"({fk(inv_securities)}×70%={fk(inv_adj)})" if inv_securities else ""
        print(f"  ネットキャッシュ = 流動資産 + 投資有価証券×70% - 総負債")
        print(f"                   = {fk(current_assets)} + {fk(inv_adj)} {inv_str} - {fk(total_liab)}")
        if unit == "千円":
            print(f"                   = {fk(net_cash)}{unit} (≒{net_cash_m:,}百万円)")
        else:
            print(f"                   = {fk(net_cash)}百万円")

    print(f"出典: {detail_url}")


if __name__ == "__main__":
    main()
