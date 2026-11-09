#!/usr/bin/env python3
"""押し目判定に使う「目安の配当利回り」(過去5年平均配当利回り)を管理するCLIツール。

対象は analysis_results/ の分析済み銘柄と holdings_raw/ の保有銘柄の和集合。
結果は market_watch/yield_targets.json(gitignore対象、マシンごとにローカル)に保存し、
scripts/market_digest.py がこれを読んで当日の利回りと比較する。

目安値の決め方(source):
- raw_data: 分析済み銘柄は raw_data.md のステップ5に記載された過去5年平均配当利回りを優先する。
  分割銘柄の補正など、分析時に人が確認した値のため。
- irbank:   raw_data に記載が無い銘柄(保有のみ・記載漏れ)は IRBANK の配当ページから
  irbank_dividend.get_all_dividends() で算出する。株式分割で配当が未調整の年は
  利回りが歪むため、5年の中に外れ値があれば note に「要確認」を付ける。
- manual:   手で書き換えた値。build で上書きしない。
- excluded: 押し目判定から外した銘柄(優先株など、Yahoo!に会社予想配当が出ず判定できないもの)。
  build で上書きせず、--report-json の要対応にも出さない。解除は yield_targets.json から
  該当エントリを消して build --only <コード>。
- none:     目安値なし。理由を none_reason に入れる。
    no_dividend:  配当実績が0%(無配続き)。押し目判定の対象外で、対応は不要。
    no_history:   IRBANKに配当利回りの実績が1年も無い(新規上場など)。手動設定が必要。
    fetch_failed: IRBANKの取得・解析に失敗した。手動設定か、原因の確認が必要。

使い方:
    python3 scripts/yield_targets.py build             # 全銘柄を(再)構築(manualは維持)
    python3 scripts/yield_targets.py build --only 6328 # 指定銘柄だけ更新
    python3 scripts/yield_targets.py build --missing   # yield_targets.jsonに無い銘柄だけ作る
    python3 scripts/yield_targets.py build --missing --report-json /tmp/report.json
    python3 scripts/yield_targets.py set 6328 3.08 --note "2021年の分割混在値を除外"
    python3 scripts/yield_targets.py exclude 25935 --note "優先株。Yahoo!に予想配当が出ない"
    python3 scripts/yield_targets.py show
"""
import argparse
import json
import re
import sys
import time
from datetime import date
from pathlib import Path

import requests

from holdings_loader import load_held_stocks
from irbank_dividend import get_all_dividends

REPO_ROOT = Path(__file__).resolve().parent.parent
ANALYSIS_RESULTS_DIR = REPO_ROOT / "analysis_results"
TARGETS_PATH = REPO_ROOT / "market_watch" / "yield_targets.json"
IRBANK_INTERVAL_SEC = 2.0
# 5年の中に、他年の中央値からこの倍率以上ずれた年があれば分割・特別配当の疑いとして要確認にする。
OUTLIER_RATIO = 1.7

# 「5年平均」「過去5年（2022〜2026年度）の平均」「過去5年間の平均」等の表記ゆれを許容する。
_AVG_RE = re.compile(r"(?<!\d)5年[^%\n]{0,40}?平均[^%\n]{0,60}?(\d+(?:\.\d+)?)\s*[%％]")
_BOLD_PCT_RE = re.compile(r"\*\*[^*]*?(\d+(?:\.\d+)?)\s*[%％]\*\*")


def load_targets() -> dict:
    if not TARGETS_PATH.exists():
        return {}
    return json.loads(TARGETS_PATH.read_text(encoding="utf-8"))


def save_targets(targets: dict) -> None:
    TARGETS_PATH.parent.mkdir(parents=True, exist_ok=True)
    ordered = dict(sorted(targets.items()))
    TARGETS_PATH.write_text(json.dumps(ordered, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def analyzed_stocks() -> dict:
    """{証券コード: (会社名, 最新raw_data.mdのPath or None)}"""
    result = {}
    if not ANALYSIS_RESULTS_DIR.is_dir():
        return result
    for d in sorted(ANALYSIS_RESULTS_DIR.iterdir()):
        if not d.is_dir() or d.name.startswith("_"):
            continue
        code, _, name = d.name.partition("_")
        mds = sorted(d.glob("raw_data_*.md"), key=lambda p: p.stat().st_mtime)
        result[code] = (name, mds[-1] if mds else None)
    return result


def avg_from_raw_data(path: Path):
    """raw_data.md から過去5年平均配当利回りの記載値を拾う。見つからなければNone。"""
    if path is None:
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        if "平均" not in line or "利回り" not in line:
            continue
        m = _AVG_RE.search(line)
        if not m:
            continue
        # 「スクリプト値3.38%は過大。**補正後は約3.08%**」のように補正値を太字で書く例があるため、
        # 同じ行に「%**」で閉じる太字の値があれば、その最初のものを優先する
        # (後ろの太字は「直近予想利回り: **3.27%**」のような別指標のことがある)。
        bold = _BOLD_PCT_RE.findall(line[m.start():])
        return float(bold[0]) if bold else float(m.group(1))
    return None


def avg_from_irbank(code: str):
    """(平均利回り, 対象年度リスト, 要確認メモ or None)。配当実績が無ければ平均はNone。"""
    result = get_all_dividends(code)
    avg = result["avg_yield_5y"]
    years = result["avg_yield_5y_years"]
    note = None
    yields = [r["yield"] for r in result["actual_rows"] if r["yield"] is not None][-5:]
    if len(yields) >= 3:
        for idx, (y, yr) in enumerate(zip(yields, years)):
            others = sorted(v for j, v in enumerate(yields) if j != idx)
            median = others[len(others) // 2]
            if median <= 0:
                continue
            if y <= 0 or y / median >= OUTLIER_RATIO or median / y >= OUTLIER_RATIO:
                note = f"要確認: {yr}年度の利回り{y}%が他年と大きく乖離(株価急変・分割・特別配当のいずれか)"
                break
    if avg is not None and len(years) < 5:
        note = (note + " / " if note else "") + f"実績{len(years)}年分のみ"
    return avg, years, note


# 目安値を作れず、ユーザーの対応が要る none_reason
UNRESOLVED_REASONS = ("no_history", "fetch_failed")


def build(only=None, missing=False, report_path=None) -> None:
    targets = load_targets()
    held = load_held_stocks()
    analyzed = analyzed_stocks()
    codes = sorted(set(held) | set(analyzed))
    if only:
        codes = [c for c in codes if c in only] + [c for c in only if c not in codes]
    if missing:
        # 月1回の全件更新を待たずに、新しく買った・分析した銘柄の目安値を作る用途。
        # 対象が0件ならIRBANKにはアクセスしない。
        codes = [c for c in codes if c not in targets]
    processed = []

    today = date.today().isoformat()
    for i, code in enumerate(codes):
        prev = targets.get(code, {})
        if prev.get("source") in ("manual", "excluded"):
            print(f"{code}: {prev['source']} のため維持 ({prev.get('target_yield')}%)")
            continue
        name = analyzed[code][0] if code in analyzed else held.get(code, prev.get("name", ""))
        raw_path = analyzed[code][1] if code in analyzed else None
        raw_avg = avg_from_raw_data(raw_path)

        if i > 0:
            time.sleep(IRBANK_INTERVAL_SEC)
        fetch_failed = False
        try:
            irbank_avg, years, note = avg_from_irbank(code)
        except (ValueError, requests.RequestException) as e:
            irbank_avg, years, note = None, [], f"IRBANK取得失敗: {e}"
            fetch_failed = True

        none_reason = None
        if raw_avg is not None:
            source, target = "raw_data", raw_avg
            if irbank_avg is not None and abs(raw_avg - irbank_avg) >= 0.3:
                note = f"raw_data {raw_avg}% と IRBANK {irbank_avg:.2f}% が乖離(raw_dataを採用)"
            elif irbank_avg is not None:
                note = None  # raw_dataで確認済みなので IRBANK 側の外れ値警告は出さない
        elif irbank_avg:  # 0%(無配続き)は対象外
            source, target = "irbank", round(irbank_avg, 2)
        else:
            source, target = "none", None
            # note は avg_from_irbank() の「要確認」等が残っている場合があるため、
            # 対応要否の判定には note の文言ではなく none_reason を使うこと。
            if fetch_failed:
                none_reason = "fetch_failed"
            elif irbank_avg is None:
                none_reason = "no_history"
                note = "IRBANKに配当利回りの実績が無い(新規上場など)。手動設定が必要"
            else:
                none_reason = "no_dividend"
                note = "配当実績なし(無配銘柄として対象外)"

        targets[code] = {
            "name": name,
            "target_yield": target,
            "source": source,
            "none_reason": none_reason,
            "raw_data_avg": raw_avg,
            "irbank_avg": round(irbank_avg, 2) if irbank_avg is not None else None,
            "irbank_years": years,
            "raw_data_file": raw_path.name if raw_path else None,
            "note": note,
            "updated": today,
        }
        print(f"{code} {name}: {target}% ({source}){' ※' + note if note else ''}", flush=True)
        save_targets(targets)  # 途中で落ちても進捗を残す
        processed.append(code)

    if report_path:
        write_report(report_path, targets, processed)


def write_report(path: str, targets: dict, processed: list) -> None:
    """今回の build で処理した銘柄のうち、目安値を作れず対応が要るものをJSONで書き出す。

    yield_targets.json には保有をやめた銘柄の古いエントリも残るため、
    通知の判定には json 全体ではなく、このレポート(今回の実行分)を使う。
    """
    unresolved = [
        {"code": c, "name": targets[c].get("name", ""), "none_reason": targets[c]["none_reason"],
         "note": targets[c].get("note")}
        for c in processed
        if targets[c].get("source") == "none" and targets[c].get("none_reason") in UNRESOLVED_REASONS
    ]
    report = {
        "date": date.today().isoformat(),
        "processed_count": len(processed),
        "unresolved": unresolved,
    }
    Path(path).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def set_manual(code: str, value: float, note) -> None:
    targets = load_targets()
    entry = targets.get(code, {"name": ""})
    entry.update({"target_yield": value, "source": "manual", "none_reason": None, "note": note,
                  "updated": date.today().isoformat()})
    targets[code] = entry
    save_targets(targets)
    print(f"{code} {entry.get('name', '')}: {value}% (manual)")


def exclude(code: str, note) -> None:
    targets = load_targets()
    entry = targets.get(code, {"name": ""})
    entry.update({"target_yield": None, "source": "excluded", "none_reason": None, "note": note,
                  "updated": date.today().isoformat()})
    targets[code] = entry
    save_targets(targets)
    print(f"{code} {entry.get('name', '')}: excluded")


def show() -> None:
    for code, e in load_targets().items():
        t = f"{e['target_yield']:.2f}%" if e.get("target_yield") is not None else "  -  "
        print(f"{code:>6} {e.get('name', ''):<24} {t:>7} {e.get('source', ''):<8} {e.get('note') or ''}")


def main():
    parser = argparse.ArgumentParser(description="目安の配当利回り(過去5年平均)を管理する")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_build = sub.add_parser("build", help="分析済み・保有銘柄の目安利回りを(再)構築する")
    target_group = p_build.add_mutually_exclusive_group()
    target_group.add_argument("--only", nargs="+", help="指定した銘柄コードだけ更新する")
    target_group.add_argument("--missing", action="store_true",
                              help="yield_targets.jsonにまだ無い保有・分析済み銘柄だけ作る")
    p_build.add_argument("--report-json", metavar="PATH",
                         help="今回処理した銘柄のうち、目安値を作れず対応が要るものをJSONで書き出す")
    p_set = sub.add_parser("set", help="目安利回りを手動で設定する(以後buildで上書きしない)")
    p_set.add_argument("code")
    p_set.add_argument("value", type=float)
    p_set.add_argument("--note")
    p_exclude = sub.add_parser("exclude", help="押し目判定から外す(以後buildで上書きしない)")
    p_exclude.add_argument("code")
    p_exclude.add_argument("--note")
    sub.add_parser("show", help="一覧を表示する")
    args = parser.parse_args()

    if args.cmd == "build":
        build(set(args.only) if args.only else None, args.missing, args.report_json)
    elif args.cmd == "set":
        set_manual(args.code, args.value, args.note)
    elif args.cmd == "exclude":
        exclude(args.code, args.note)
    else:
        show()


if __name__ == "__main__":
    sys.exit(main())
