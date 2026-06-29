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
import html as html_mod
import re
import sys
import requests

from irbank_utils import fetch_with_retry

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
    resp = fetch_with_retry(f"https://irbank.net/{code}", allow_redirects=True)
    m = re.search(r'href="/(E\d{5})', resp.text)
    if m:
        return m.group(1)
    raise ValueError(f"Eコードが解決できませんでした（コード: {code}）")


def fetch_cf_summary(ecode: str):
    url = f"https://irbank.net/{ecode}/cf"
    resp = fetch_with_retry(url)
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
    """最新期の詳細CF計算書を取得する。

    Returns:
        fin_data: 財務活動CF区分の行ラベル→最新期値（順序保持dict）
        unit: 単位文字列（"千円" or "百万円"）
        url: 取得元URL
    """
    url = f"https://irbank.net/{ecode}/{doc_id}/cf"
    resp = fetch_with_retry(url)

    # 単位を caption から取得（「千円」か「百万円」）
    cap_m = re.search(r"<caption[^>]*>.*?（(千円|百万円)）", resp.text, re.S)
    unit = cap_m.group(1) if cap_m else "千円"

    t_m = re.search(r"<table[^>]*>(.*?)</table>", resp.text, re.S)
    if not t_m:
        return {}, unit, url

    def clean(cell_html: str) -> str:
        text = re.sub(r"<[^>]+>", "", cell_html)
        text = html_mod.unescape(text)
        return text.replace("　", "").replace(",", "").replace("－", "0").strip()

    fin_data = {}
    in_fin = False
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", t_m.group(1), re.S):
        cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S)
        vals = [clean(c) for c in cells]
        if not vals:
            continue
        label = vals[0]
        # セクション切り替え検出
        if "財務活動によるキャッシュ・フロー" in label:
            in_fin = True
        elif any(kw in label for kw in ["営業活動", "投資活動", "現金及び現金同等物"]):
            in_fin = False
        if in_fin and len(vals) >= 2:
            fin_data[label] = vals[-1]  # 最後の列 = 最新期

    return fin_data, unit, url


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

    # 最新期の財務CF詳細（財務活動CF全行）
    latest = rows[-1]
    print()
    try:
        fin_data, unit, detail_url = fetch_cf_detail(ecode, latest["doc_id"])
        print(f"【最新期({latest['label']})財務CF内訳・{unit}】")

        def to_m(raw: str, unit: str) -> str:
            """値を百万円換算して文字列で返す。千円単位なら÷1000、百万円単位はそのまま。"""
            raw = raw.replace("△", "-").replace("▲", "-").replace("−", "-")
            try:
                val = int(float(raw))
            except (ValueError, TypeError):
                return raw
            if unit == "千円":
                return f"{round(val / 1000):,}百万円"
            return f"{val:,}百万円"

        # 財務活動CF区分のうち「合計」行と空値を除いた全項目を出力
        skip = {"財務活動によるキャッシュ・フロー"}
        for key, raw in fin_data.items():
            if key in skip:
                continue
            val_str = to_m(raw, unit)
            # 空・ゼロ・変換不能は「-」表示
            if not raw or raw in ("0",):
                val_str = "-"
            print(f"  {key:<30}: {val_str}")

        print(f"  {'財務CF合計':<30}: {fmt(latest['financial_cf'])}百万円")
        print(f"出典: {summary_url}")
        print(f"      {detail_url}")

    except requests.RequestException as e:
        print(f"  財務CF詳細の取得に失敗しました: {e}", file=sys.stderr)
        print(f"出典: {summary_url}")


if __name__ == "__main__":
    main()
