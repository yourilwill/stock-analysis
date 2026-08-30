#!/usr/bin/env python3
"""夜間バッチ本体。積み残しキュー(analysis_results/queue_state.json)から、決算区分の構成による
動的上限(本決算を含めば2件、含まなければ3件)に従って当夜の処理対象を選び、
`claude -p`でjp-stock-analysisスキルの更新分析フローを無人実行する。

docs/design_doc/stock-analysis/nightly-batch.md参照。

使い方:
    python3 run_queued_analysis.py [--claude-bin PATH] [--model NAME] [--timeout-sec N]
"""
import argparse
import json
import re
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
QUEUE_STATE_PATH = REPO_ROOT / "analysis_results" / "queue_state.json"
MAX_ATTEMPTS = 2
MAX_SUMMARY_LEN = 150

# 2026-08-30実機検証(鈴木6785、本決算フル分析、21分・exit_code=0)で確認済みのallowedTools。
# design_doc「検証済み事項」参照。
ALLOWED_TOOLS = "Bash,Read,Write,Edit,Glob,Grep,WebFetch,WebSearch,Agent"


def load_queue_state() -> list:
    if not QUEUE_STATE_PATH.exists():
        return []
    return json.loads(QUEUE_STATE_PATH.read_text(encoding="utf-8"))


def save_queue_state(queue: list) -> None:
    QUEUE_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = QUEUE_STATE_PATH.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(queue, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(QUEUE_STATE_PATH)


def reset_stuck_in_progress(queue: list) -> None:
    """前回実行が5時間制限等で中断した痕跡(in_progressのまま残った項目)をpendingへ戻す。"""
    for item in queue:
        if item["status"] == "in_progress":
            print(f"前回実行の中断痕跡を検出、pendingへ戻します: {item['code']} {item['name']}")
            item["status"] = "pending"


def select_targets(queue: list) -> list:
    """FIFO(detected_date昇順)で先頭から、決算区分構成による動的上限ルールに従い今回の処理対象を選ぶ。"""
    pending = sorted(
        (item for item in queue if item["status"] == "pending"),
        key=lambda item: item["detected_date"],
    )
    candidates = pending[:3]
    if any(item["kubun"] == "本決算" for item in candidates):
        return candidates[:2]
    return candidates


def extract_step13_summary(text: str, max_len: int = MAX_SUMMARY_LEN) -> str:
    """raw_data.mdの`## ステップ13: 総括`セクションから「総合判定」を抜き出す。
    レポートによって`### 総合判定`見出し・`**総合判定**: `インライン表記のいずれかで
    書かれているため両対応し、どちらも無ければセクション内最後の段落にフォールバックする。
    """
    step13_match = re.search(r"^## ステップ13.*?\n(.*?)(?=\n## |\Z)", text, re.S | re.M)
    if not step13_match:
        return ""
    section = step13_match.group(1)

    heading_match = re.search(r"^### 総合判定\s*\n+(.+?)(?=\n#{2,3}\s|\Z)", section, re.S | re.M)
    if heading_match:
        picked = heading_match.group(1)
    else:
        inline_match = re.search(r"\*\*総合判定\*\*[:：]?\s*(.+?)(?=\n\n|\Z)", section, re.S)
        if inline_match:
            picked = inline_match.group(1)
        else:
            paragraphs = [p.strip() for p in re.split(r"\n\s*\n", section) if p.strip()]
            picked = paragraphs[-1] if paragraphs else ""

    picked = re.sub(r"\s+", " ", picked).strip()
    if len(picked) > max_len:
        picked = picked[:max_len].rstrip() + "…"
    return picked


def find_updated_raw_data(code: str, started_at: float) -> Path | None:
    """<code>_<name>/ディレクトリ内で、分析開始後に更新されたraw_data_*.mdを探す。
    見つからなければファイル名の日付が最も新しいものにフォールバックする。
    """
    matches = list((REPO_ROOT / "analysis_results").glob(f"{code}_*"))
    if not matches:
        return None
    company_dir = matches[0]
    raw_data_files = sorted(company_dir.glob("raw_data_*.md"))
    if not raw_data_files:
        return None
    fresh = [f for f in raw_data_files if f.stat().st_mtime >= started_at]
    if fresh:
        return max(fresh, key=lambda f: f.stat().st_mtime)
    return raw_data_files[-1]


def run_analysis(item: dict, claude_bin: str, model: str, timeout_sec: int) -> tuple[bool, str]:
    """1銘柄の更新分析を`claude -p`で無人実行する。戻り値は(成功したか, summary or エラー理由)。"""
    prompt = (
        f"{item['name']}(証券コード{item['code']})の決算短信({item['kubun']})を反映した"
        "更新分析をして。"
    )
    started_at = time.time()
    try:
        result = subprocess.run(
            [claude_bin, "-p", "--model", model, f"--allowedTools={ALLOWED_TOOLS}", prompt],
            cwd=REPO_ROOT,
            timeout=timeout_sec,
            capture_output=True,
            text=True,
        )
    except subprocess.TimeoutExpired:
        return False, f"タイムアウト({timeout_sec}秒)"

    if result.returncode != 0:
        stderr_tail = result.stderr.strip()[-500:]
        return False, f"exit_code={result.returncode}: {stderr_tail}"

    raw_data_path = find_updated_raw_data(item["code"], started_at)
    if raw_data_path is None:
        return False, "分析後にraw_data.mdが見つかりませんでした"

    summary = extract_step13_summary(raw_data_path.read_text(encoding="utf-8"))
    return True, summary


def main():
    parser = argparse.ArgumentParser(description="積み残しキューの夜間バッチ分析を実行する")
    parser.add_argument("--claude-bin", default="claude", help="claude CLIのパス(既定: PATH上のclaude)")
    parser.add_argument("--model", default="sonnet")
    parser.add_argument("--timeout-sec", type=int, default=2700, help="1銘柄あたりのタイムアウト(既定: 45分)")
    args = parser.parse_args()

    queue = load_queue_state()
    if not queue:
        print("キューは空です。処理対象はありません。")
        return

    reset_stuck_in_progress(queue)
    save_queue_state(queue)

    targets = select_targets(queue)
    if not targets:
        print("pending状態の項目はありません。処理対象はありません。")
        return

    print(f"今夜の処理対象: {len(targets)}件 ({', '.join(t['code'] + '_' + t['name'] for t in targets)})")

    today = date.today().isoformat()
    for target in targets:
        item = next(i for i in queue if i["code"] == target["code"])
        item["status"] = "in_progress"
        save_queue_state(queue)

        print(f"分析開始: {item['code']} {item['name']} ({item['kubun']})")
        ok, detail = run_analysis(item, args.claude_bin, args.model, args.timeout_sec)

        if ok:
            item["status"] = "done"
            item["processed_date"] = today
            item["summary"] = detail
            print(f"分析成功: {item['code']} {item['name']}")
        else:
            item["attempts"] += 1
            print(f"分析失敗({item['attempts']}/{MAX_ATTEMPTS}回目): {item['code']} {item['name']}: {detail}", file=sys.stderr)
            if item["attempts"] >= MAX_ATTEMPTS:
                item["status"] = "failed_needs_attention"
                item["processed_date"] = today
            else:
                item["status"] = "pending"
        save_queue_state(queue)


if __name__ == "__main__":
    main()
