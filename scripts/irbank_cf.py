#!/usr/bin/env python3
"""IRBANKから指定銘柄のキャッシュフロー推移と最新期の財務CF内訳を取得するCLIツール。

ステップ6（自社株買い実績・総還元性向）に使用。CF推移（年次）と最新期の
配当金支払額・自己株式取得額を一括取得するため、有報PDFの参照が不要になる。

使い方:
    python3 irbank_cf.py <銘柄コード or Eコード>
    python3 irbank_cf.py 2169
    python3 irbank_cf.py E05726
"""
import argparse
import re
import sys
import requests

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"


def strip_tags(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s).strip()


def parse_num(s: str):
    s = strip_tags(s).replace(",", "").replace("△", "-").replace("▲", "-").replace("－", "0").replace("−", "-").strip()
    if not s or s == "-":
        return None
    try:
        return int(float(s))
    except ValueError:
        return None


def fmt(v) -> str:
    if v is None:
        return "-"
    return f"{v:,}"


def resolve_ecode(code: str) -> str:
    if re.match(r"^E\d+$", code, re.I):
        return code.upper()
    resp = requests.get(f"https://irbank.net/{code}", headers={"User-Agent": UA}, timeout=15, allow_redirects=True)
    resp.raise_for_status()
    m = re.search(r'href="/(E\d{5})', resp.text)
    if m:
        return m.group(1)
    raise ValueError(f"Eコードが解決できませんでした（コード: {code}）")


def fetch_cf_summary(ecode: str):
    url = f"https://irbank.net/{ecode}/cf"
    resp = requests.get(url, headers={"User-Agent": UA}, timeout=20)
    resp.raise_for_status()
    html = resp.text

    name_m = re.search(r'<meta property="og:title" content="([^（\(]+)', html)
    company = name_m.group(1).strip() if name_m else ecode

    tbody = re.search(r"<tbody>(.*?)</tbody>", html, re.S)
    if not tbody:
        raise ValueError(f"CFテーブルが見つかりません: {url}")
    tbody_html = tbody.group(1)

    # 年ラベル: rowspan="2" のTDから「2025年12月期」を抽出
    year_labels = []
    for ycell in re.findall(r"<td[^>]+rowspan[^>]+>(.*?)</td>", tbody_html, re.S):
        clean = re.sub(r"<[^>]+>", "", ycell).strip()
        m = re.search(r"(\d{4})年\D*?(\d{1,2})月期", clean)
        if m:
            year_labels.append(f"{m.group(1)}/{int(m.group(2)):02d}")

    # 通期行: IRBANKのHTMLは <tr> 外にある裸のTD群で構成されるため、末尾の </tr> を区切りに使う
    annual_rows = re.findall(
        r'href="(S\w+)/cf">通期</a></td>(.*?)</tr>',
        tbody_html, re.S
    )

    rows = []
    for i, (doc_id, cells_html) in enumerate(annual_rows):
        label = year_labels[i] if i < len(year_labels) else f"期{i + 1}"
        vals_raw = re.findall(r"<td[^>]*>([^<]*)</td>", cells_html)
        nums = [parse_num(v) for v in vals_raw[:6]]
        while len(nums) < 6:
            nums.append(None)
        rows.append({
            "label": label,
            "doc_id": doc_id,
            "operating_cf": nums[0],
            "investing_cf": nums[1],
            "financial_cf": nums[2],
            "free_cf": nums[3],
            "capex": nums[4],
            "cash": nums[5],
        })

    return company, rows, url


def fetch_cf_detail(ecode: str, doc_id: str):
    """最新期の詳細CF計算書を取得し、行ラベル→最新期値の辞書を返す。"""
    url = f"https://irbank.net/{ecode}/{doc_id}/cf"
    resp = requests.get(url, headers={"User-Agent": UA}, timeout=20)
    resp.raise_for_status()

    t_m = re.search(r"<table[^>]*>(.*?)</table>", resp.text, re.S)
    if not t_m:
        return {}, url

    data = {}
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", t_m.group(1), re.S):
        cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S)
        vals = [re.sub(r"<[^>]+>", "", c).replace("　", "").replace(",", "").replace("－", "0").strip() for c in cells]
        if len(vals) >= 2:
            data[vals[0]] = vals[-1]  # 最後の列 = 最新期

    return data, url


def main():
    parser = argparse.ArgumentParser(description="IRBANKからCF推移・最新期財務CF内訳を取得（ステップ6用）")
    parser.add_argument("code", help="銘柄コード(例: 2169) または Eコード(例: E05726)")
    args = parser.parse_args()

    try:
        ecode = resolve_ecode(args.code)
        company, rows, summary_url = fetch_cf_summary(ecode)
    except (ValueError, requests.RequestException) as e:
        print(f"エラー: {e}", file=sys.stderr)
        sys.exit(1)

    if not rows:
        print(f"エラー: {args.code} のCFデータが取得できませんでした", file=sys.stderr)
        sys.exit(1)

    # CF推移サマリー
    print(f"{company}({args.code}) キャッシュフロー推移・百万円")
    print(f"{'年度':<10} {'営業CF':>8} {'投資CF':>8} {'財務CF':>8} {'フリーCF':>9} {'設備投資':>9} {'現金等':>8}")
    print("-" * 68)
    for r in rows:
        print(
            f"{r['label']:<10} {fmt(r['operating_cf']):>8} {fmt(r['investing_cf']):>8} "
            f"{fmt(r['financial_cf']):>8} {fmt(r['free_cf']):>9} {fmt(r['capex']):>9} {fmt(r['cash']):>8}"
        )

    # 最新期の財務CF詳細（配当金支払・自己株式取得）
    latest = rows[-1]
    print()
    print(f"【最新期({latest['label']})財務CF内訳・千円】")
    try:
        detail, detail_url = fetch_cf_detail(ecode, latest["doc_id"])

        key_items = [
            ("配当金の支払額", "配当金の支払額"),
            ("自己株式の取得による支出", "自己株式の取得による支出"),
        ]
        for display, key in key_items:
            raw = detail.get(key, "-")
            try:
                val_k = int(float(raw))
                val_m = round(val_k / 1000)
                print(f"  {display:<28}: {val_k:>12,}千円 (≒{val_m:>6,}百万円)")
            except (ValueError, TypeError):
                print(f"  {display:<28}: {raw}")

        print(f"  財務CF合計（百万円）          : {fmt(latest['financial_cf']):>8}百万円")
        print(f"出典: {summary_url}")
        print(f"      {detail_url}")

    except requests.RequestException as e:
        print(f"  財務CF詳細の取得に失敗しました: {e}", file=sys.stderr)
        print(f"出典: {summary_url}")


if __name__ == "__main__":
    main()
