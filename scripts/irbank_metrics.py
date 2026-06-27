#!/usr/bin/env python3
"""IRBANKから指定銘柄の株式指標（PER・PBR・ROE・EPS・BPS・配当利回り等）を取得するCLIツール。

ステップ1（企業規模・割安度・資本効率性・財務健全性）とステップ6（自社株比率）に使用。
同一ページから一括取得するため、別々にWebFetchするより大幅にコンテキストを節約できる。

使い方:
    python3 irbank_metrics.py <銘柄コード or Eコード>
    python3 irbank_metrics.py 9233
    python3 irbank_metrics.py E04275
"""
import argparse
import re
import sys
import requests

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"


def strip_tags(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s).strip()


def decode_entities(s: str) -> str:
    return (s.replace("&thinsp;", " ").replace("&amp;", "&")
              .replace("&lt;", "<").replace("&gt;", ">")
              .replace("&nbsp;", " ").replace("&#039;", "'"))


def clean_text(s: str) -> str:
    return decode_entities(strip_tags(s)).strip()


def parse_float(s: str):
    s = re.sub(r"[倍円%株/]|円/株|億.*|万.*", "", s).replace(",", "").strip()
    if not s or s == "-":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def resolve_ecode(code: str) -> str:
    """数字コードを E##### 形式に解決する。既に E-code なら変換しない。"""
    if re.match(r'^E\d+$', code, re.I):
        return code.upper()
    url = f"https://irbank.net/{code}"
    resp = requests.get(url, headers={"User-Agent": UA}, timeout=15, allow_redirects=True)
    resp.raise_for_status()
    m = re.search(r'href="/(E\d{5})', resp.text)
    if m:
        return m.group(1)
    raise ValueError(f"Eコードが解決できませんでした（コード: {code}）")


def extract_section(html: str, section_id: str, end_pattern: str = r'(?=<section>|</main>)') -> str:
    m = re.search(rf'id="{section_id}"(.*?){end_pattern}', html, re.S)
    return m.group(1) if m else ""


def parse_dl_pairs(section: str) -> dict:
    """<dl class="gdl">内のdt/ddペアを {dt_text: dd_span_text} 辞書に変換する。"""
    pairs = {}
    for dt_raw, dd_raw in re.findall(
        r'<dt[^>]*>(.*?)</dt>\s*<dd[^>]*>(.*?)</dd>', section, re.S
    ):
        dt_text = clean_text(dt_raw).replace(" ", "").strip()
        span_m = re.search(r'<span class="text">(.*?)</span>', dd_raw, re.S)
        dd_text = clean_text(span_m.group(1)) if span_m else clean_text(dd_raw)
        if dt_text:
            pairs[dt_text] = dd_text
    return pairs


def fetch_metrics(code: str):
    ecode = resolve_ecode(code)
    url = f"https://irbank.net/{ecode}"
    resp = requests.get(url, headers={"User-Agent": UA}, timeout=20, allow_redirects=True)
    resp.raise_for_status()
    html = resp.text
    final_url = resp.url

    # h1 内の <span> から社名を抽出（"9233 <span>アジア航測</span>" 形式）
    h1_m = re.search(r'<h1>\d+\s*<span>([^<]+)</span>', html)
    if h1_m:
        company = h1_m.group(1).strip()
    else:
        og_m = re.search(r'<meta property="og:title" content="([^|（\(]+)', html)
        company = og_m.group(1).strip() if og_m else code

    stock_section = extract_section(html, "c_Stock", r'(?=<section>|</main>)')
    if not stock_section:
        raise ValueError(f"株式指標セクションが見つかりません: {url}")

    pairs = parse_dl_pairs(stock_section)

    # 日付付きの終値を検出（"終値+x.xx%"形式）
    close_price = None
    close_date = None
    for dt, val in pairs.items():
        if dt.startswith("終値"):
            close_price = val
        if "終値" in dt or "時" in dt:
            date_m = re.search(r'(\d+/\d+)', stock_section[:500])
            if date_m and close_date is None:
                close_date = date_m.group(1)

    # 発行済み株式数を株主セクションから取得
    shareholder_section = extract_section(html, "c_Shareholder", r'(?=id="c_Link"|</main>)')
    shares_issued = None
    shares_m = re.search(r'発行済み?株式総数</dt>\s*<dd[^>]*>\s*([0-9,]+株)', shareholder_section, re.S)
    if shares_m:
        shares_issued = shares_m.group(1)

    shareholder_date = None
    date_m2 = re.search(r'<h2 class="hdl">([^<]+)</h2>', shareholder_section)
    if date_m2:
        shareholder_date = date_m2.group(1).strip()

    return {
        "company": company,
        "code": code,
        "url": final_url,
        "close_price": close_price,
        "close_date": close_date,
        "mktcap": pairs.get("時価総額", "-"),
        "per_actual": pairs.get("PER（連）", "-"),
        "per_forecast": pairs.get("PER（連）予", "-"),
        "pbr": pairs.get("PBR（連）", "-"),
        "div_yield": pairs.get("配当利回り 予", pairs.get("配当利回り", "-")),
        "roe_actual": pairs.get("ROE（連）", "-"),
        "roe_forecast": pairs.get("ROE（連）予", "-"),
        "eps_actual": pairs.get("EPS（連）", "-"),
        "eps_forecast": pairs.get("EPS（連）予", "-"),
        "bps": pairs.get("BPS（連）", "-"),
        "equity_ratio": pairs.get("株主資本比率（連）", "-"),
        "shares_issued": shares_issued or "-",
        "shareholder_date": shareholder_date or "-",
    }


def calc_div_payout(div_yield_str: str, eps_str: str, per_str: str) -> str:
    """配当性向を EPS と 1株配当（div_yield括弧内）から計算する。"""
    m = re.search(r'\((\d+(?:\.\d+)?)\)', div_yield_str)
    if not m:
        return "-"
    div_per_share = float(m.group(1))
    eps_val = parse_float(eps_str)
    if eps_val and eps_val > 0:
        return f"{div_per_share / eps_val * 100:.1f}%"
    return "-"


def main():
    parser = argparse.ArgumentParser(description="IRBANKから株式指標を取得（ステップ1・6用）")
    parser.add_argument("code", help="銘柄コード(例: 9233) または Eコード(例: E04275)")
    args = parser.parse_args()

    try:
        m = fetch_metrics(args.code)
    except (ValueError, requests.RequestException) as e:
        print(f"エラー: {e}", file=sys.stderr)
        sys.exit(1)

    # 1株配当を配当利回り文字列から分離
    div_str = m["div_yield"]
    div_m = re.search(r'([\d.]+)%\s*\((\d+(?:\.\d+)?)\)', div_str)
    yield_str = f"{div_m.group(1)}%" if div_m else div_str
    div_per_share = f"{div_m.group(2)}円" if div_m else "-"

    payout = calc_div_payout(div_str, m["eps_forecast"] or m["eps_actual"], m["per_forecast"])

    print(f"{m['company']}({args.code}) 株式指標")
    if m["close_date"]:
        print(f"取得日: {m['close_date']}")
    print("-" * 50)
    print(f"時価総額         : {m['mktcap']}")
    print(f"株価（終値）     : {m['close_price']} 円")
    print(f"PER（実績）      : {m['per_actual']}")
    print(f"PER（予想）      : {m['per_forecast']}")
    print(f"PBR              : {m['pbr']}")
    print(f"配当利回り（予） : {yield_str} / 1株配当: {div_per_share}")
    print(f"ROE（実績）      : {m['roe_actual']}")
    print(f"ROE（予想）      : {m['roe_forecast']}")
    print(f"EPS（実績）      : {m['eps_actual']}")
    print(f"EPS（予想）      : {m['eps_forecast']}")
    print(f"BPS              : {m['bps']}")
    print(f"株主資本比率     : {m['equity_ratio']}")
    print(f"配当性向（計算） : {payout}  ※EPS予想÷1株配当")
    print(f"発行済株式総数   : {m['shares_issued']}  ({m['shareholder_date']}時点)")
    print(f"出典: {m['url']}")


if __name__ == "__main__":
    main()
