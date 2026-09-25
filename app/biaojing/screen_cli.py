# -*- coding: utf-8 -*-
"""标镜 P2 筛查 CLI：归一化事实 JSON -> R001—R008 筛查 -> JSON。

用法：
    python -m biaojing.screen <facts.json> [--db 路径] [-o 输出.json]

流程：加载 JSON -> SQLite 幂等累计（同 event_id 同内容跳过、不同内容冲突
拒绝）-> 从库读回全量事实 -> R001—R008 筛查 -> 输出 findings + 摘要。
不给 --db 时使用进程内内存库（不持久化，输出中注明）。
"""

from __future__ import annotations

import argparse
import datetime
import json
import sys

from . import PRODUCT_NAME, VERSION
from . import rules, store


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog=PRODUCT_NAME,
        description=f"{PRODUCT_NAME}（python -m biaojing.screen）P2 规则筛查："
                    "对已人工确认/归一化的结构化事实执行 R001—R008 筛查并输出"
                    "机器可读 JSON。筛查结果为人工核查线索，不是串标概率或违法"
                    "结论；不实现任意原始投标文件的全自动字段抽取。",
    )
    ap.add_argument("facts", help="归一化事实 JSON（schema biaojing.p2/1）")
    ap.add_argument("-o", "--output", default=None,
                    help="结果 JSON 输出文件（缺省打印到 stdout）")
    ap.add_argument("--db", default=None,
                    help="SQLite 库路径（幂等累计）；缺省用内存库不持久化")
    ap.add_argument("--compact", action="store_true", help="紧凑 JSON")
    return ap


def run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    with open(args.facts, encoding="utf-8") as f:
        data = json.load(f)

    conn = store.connect(args.db) if args.db else store.connect(":memory:")
    try:
        import_result = store.import_batch(data, conn)
        fb = store.load_factbase(conn)
        event_count_after = store.event_count(conn)
    finally:
        conn.close()

    outcome = rules.screen_all(fb)
    payload = {
        "product": PRODUCT_NAME,
        "product_name": PRODUCT_NAME,
        "module": "biaojing.screen",
        "version": VERSION,
        "rule_version": rules.RULE_VERSION,
        "schema": rules.SCHEMA,
        "generated_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "database": args.db or "内存库（未持久化）",
        "import": {**import_result.as_dict(),
                   "event_count_after": event_count_after},
        "conflicts": import_result.conflicts,
        "findings": outcome["findings"],
        "excluded_facts": outcome["excluded_facts"],
        "summary": outcome["summary"],
        "r004_stats": outcome["r004_stats"],
        "unresolved_evidence": outcome["unresolved_evidence"],
        "disclaimer": outcome["disclaimer"],
    }
    text = json.dumps(payload, ensure_ascii=False,
                      indent=None if args.compact else 2)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        print(f"已写出：{args.output}", file=sys.stderr)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(run())
