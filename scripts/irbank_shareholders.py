#!/usr/bin/env python3
"""IRBANKから指定銘柄の大株主リスト・株主構成を取得するCLIツール。

ステップ7（株主構成）に使用。WebFetchを不要にしコンテキスト消費を削減する。

使い方:
    python3 irbank_shareholders.py <銘柄コード or Eコード>
    python3 irbank_shareholders.py 9233
    python3 irbank_shareholders.py E04275
"""
import argparse
import re
import sys
import requests

from irbank_utils import fetch_with_retry

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"


def strip_tags(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s).strip()


def decode_entities(s: str) -> str:
    return (s.replace("&thinsp;", " ").replace("&amp;", "&")
              .replace("&lt;", "<").replace("&gt;", ">")
              .replace("&nbsp;", " ").replace("&#039;", "'"))


def clean_text(s: str) -> str:
    return decode_entities(strip_tags(s)).strip()


def resolve_ecode(code: str) -> str:
    """数字コードを E##### 形式に解決する。既に E-code なら変換しない。"""
    if re.match(r'^E\d+$', code, re.I):
        return code.upper()
    url = f"https://irbank.net/{code}"
    resp = fetch_with_retry(url, allow_redirects=True)
    m = re.search(r'href="/(E\d{5})', resp.text)
    if m:
        return m.group(1)
    raise ValueError(f"Eコードが解決できませんでした（コード: {code}）")


def fetch_shareholders(code: str):
    ecode = resolve_ecode(code)
    url = f"https://irbank.net/{ecode}"
    resp = fetch_with_retry(url, allow_redirects=True)
    html = resp.text
    final_url = resp.url

    h1_m = re.search(r'<h1>\d+\s*<span>([^<]+)</span>', html)
    if h1_m:
        company = h1_m.group(1).strip()
    else:
        og_m = re.search(r'<meta property="og:title" content="([^|（\(]+)', html)
        company = og_m.group(1).strip() if og_m else code

    # c_Shareholder セクションを抽出
    sh_m = re.search(r'id="c_Shareholder"(.*?)(?=id="c_Link"|</main>)', html, re.S)
    if not sh_m:
        raise ValueError(f"株主セクションが見つかりません: {url}")
    sh_section = sh_m.group(1)

    # 基準日
    date_m = re.search(r'<h2 class="hdl">([^<]+)</h2>', sh_section)
    record_date = date_m.group(1).strip() if date_m else "-"

    # 株主数・発行済み株式総数
    shareholders_count = None
    shares_issued = None
    for dt_raw, dd_raw in re.findall(r'<dt>(.*?)</dt>\s*<dd[^>]*>(.*?)</dd>', sh_section, re.S):
        dt = clean_text(dt_raw)
        dd = clean_text(dd_raw)
        if "株主数" in dt:
            shareholders_count = dd
        elif "発行済み" in dt and "株式" in dt:
            shares_issued = dd

    # 株主構成（金融機関・法人等の分類）
    owner_section_m = re.search(r'株主構成(.*?)(?=大株主|$)', sh_section, re.S)
    ownership_breakdown = []
    if owner_section_m:
        for dt_raw, dd_raw in re.findall(
            r'<dt>(.*?)</dt>\s*<dd[^>]*>.*?<span class="text">([\d.]+%)</span>',
            owner_section_m.group(1), re.S
        ):
            cat = clean_text(dt_raw)
            pct = dd_raw.strip()
            if cat:
                ownership_breakdown.append((cat, pct))

    # 大株主リスト（id="c_holder" 直下の dl.gdl を対象にする）
    major_shareholders = []
    holder_dl_m = re.search(r'id="c_holder".*?<dl class="gdl[^"]*">(.*?)</dl>', sh_section, re.S)
    if holder_dl_m:
        for dt_raw, dd_raw in re.findall(
            r'<dt[^>]*>(.*?)</dt>\s*<dd[^>]*>.*?<span class="text">([\d.]+%)</span>',
            holder_dl_m.group(1), re.S
        ):
            name = clean_text(dt_raw)
            pct = dd_raw.strip()
            if name:
                major_shareholders.append((name, pct))

    return {
        "company": company,
        "code": code,
        "url": final_url,
        "record_date": record_date,
        "shareholders_count": shareholders_count or "-",
        "shares_issued": shares_issued or "-",
        "ownership_breakdown": ownership_breakdown,
        "major_shareholders": major_shareholders,
    }


def main():
    parser = argparse.ArgumentParser(description="IRBANKから大株主リストを取得（ステップ7用）")
    parser.add_argument("code", help="銘柄コード(例: 9233) または Eコード(例: E04275)")
    args = parser.parse_args()

    try:
        r = fetch_shareholders(args.code)
    except (ValueError, requests.RequestException) as e:
        print(f"エラー: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"{r['company']}({args.code}) 株主情報 （{r['record_date']}時点）")
    print(f"株主数: {r['shareholders_count']} / 発行済み株式総数: {r['shares_issued']}")
    print()

    if r["ownership_breakdown"]:
        print("【株主構成】")
        for cat, pct in r["ownership_breakdown"]:
            print(f"  {cat:<20} {pct}")
        print()

    if r["major_shareholders"]:
        print("【大株主】")
        print(f"  {'順位':<4} {'株主名':<30} {'保有比率':>8}")
        print("  " + "-" * 46)
        for i, (name, pct) in enumerate(r["major_shareholders"], 1):
            print(f"  {i:<4} {name:<30} {pct:>8}")
    else:
        print("大株主データが取得できませんでした")

    # 支配株主・アクティビスト判定ヒント
    print()
    top_holders = r["major_shareholders"][:3] if r["major_shareholders"] else []
    total_top = sum(float(p.rstrip("%")) for _, p in top_holders)
    if total_top >= 40:
        print(f"※ 上位3株主の合計保有比率: {total_top:.2f}%（支配株主的立場の確認を推奨）")

    print(f"出典: {r['url']}")


if __name__ == "__main__":
    main()
