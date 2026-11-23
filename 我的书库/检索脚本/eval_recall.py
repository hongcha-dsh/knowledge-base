# -*- coding: utf-8 -*-
"""R10 检索命中率评测尺（#4 M1；#66 扩展真实提问盲测尺）：只读脚本，不写库、不改任何数据文件。

对评测集每道**有效题**（答案块ID在现库存在）直接调用 search_keyword.query_keywords
（import 复用、不起子进程），分别按纯内容/含题干两种口径统计 top5/top10 与 MRR；
传入 `--集 真实` 时读取 `评测集_真实提问.md`（不进倒排的盲测尺），对比「关导航 vs 含块标签导航」。

用法：
    python eval_recall.py                    # 评测集.md 双口径（纯内容 vs 含题干）
    python eval_recall.py --集 真实          # 评测集_真实提问.md（关导航 vs 含块标签导航）
    python eval_recall.py --集 真实 --导航权重 0.25
    python eval_recall.py --自检             # 迷你沙箱库验证判分逻辑

退出码：0=成功；1=异常。全程只读，结果上屏。
"""

import json
import re
import sys
from pathlib import Path

# 定位公共模块与检索脚本（common.py 位于 项目根\资料预处理\脚本\）
_SCRIPT_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPT_DIR.parents[1]
sys.path.insert(0, str(_PROJECT_ROOT / "资料预处理" / "脚本"))
import common  # noqa: E402
import search_keyword  # noqa: E402

TOP_K = 10  # 榜长：只统计 top5 / top10
_QNO_RE = re.compile(r"^Q?\d+$")
_REAL_QNO_RE = re.compile(r"^R\d+$")


def _ensure_utf8_stdio():
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure") and (getattr(s, "encoding", "") or "").lower() != "utf-8":
            try:
                s.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def parse_evals(eval_path: Path) -> list:
    """解析 评测集.md → [{"题号","提问","答案块ID"}]。表头/分隔/说明行跳过。"""
    if not eval_path.exists():
        return []
    rows = []
    for ln in eval_path.read_text(encoding="utf-8").split("\n"):
        s = ln.strip()
        if not s.startswith("|") or s.startswith("|---"):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if len(cells) < 3 or not _QNO_RE.match(cells[0]):
            continue
        q, bid = cells[1].strip(), cells[2].strip()
        if q and bid:
            rows.append({"题号": cells[0], "提问": q, "答案块ID": bid})
    return rows


def parse_real_evals(eval_path: Path) -> list:
    """#66：解析 评测集_真实提问.md → [{"题号","提问","限定书","答案块ID","答案块ID列表","确认人"}]。"""
    if not eval_path.exists():
        return []
    rows = []
    for ln in eval_path.read_text(encoding="utf-8").split("\n"):
        s = ln.strip()
        if not s.startswith("|") or s.startswith("|---"):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if len(cells) < 4 or not _REAL_QNO_RE.match(cells[0]):
            continue
        q = cells[1].strip()
        scoped_book = cells[2].strip("《》 ").strip()
        raw_bids = cells[3].strip()
        bid_list = [b.strip() for b in re.split(r"[,，]", raw_bids) if b.strip()]
        confirmer = cells[4].strip() if len(cells) >= 5 else ""
        if q and bid_list:
            rows.append({
                "题号": cells[0],
                "提问": q,
                "限定书": scoped_book,
                "答案块ID": raw_bids,
                "答案块ID列表": bid_list,
                "确认人": confirmer,
            })
    return rows


def collect_valid_ids(kb_root: Path, query_cache: dict = None) -> set:
    """现库有效块ID集合；与本次评测查询共享同一份已解析倒排表。"""
    if query_cache is None:
        query_cache = search_keyword._new_query_cache(kb_root)
    valid = set()
    for _title, _book_dir, entries, _qgrams in query_cache["books"]:
        for bids in entries.values():
            valid.update(bids)
    return valid


def evaluate(kb_root: Path, eval_path: Path, query_func=None, top_k: int = TOP_K,
             include_question_index: bool = True, query_cache: dict = None,
             include_nav: bool = None, nav_weight: float = None,
             real_mode: bool = False) -> dict:
    """跑单口径评测；默认直接复用生产 query_keywords，可注入桩验证判分逻辑。"""
    rows = parse_real_evals(eval_path) if real_mode else parse_evals(eval_path)
    if query_cache is None:
        query_cache = search_keyword._new_query_cache(kb_root)
    valid_ids = collect_valid_ids(kb_root, query_cache)
    disk_books = {t for t, _d, _e, _q in query_cache["books"]}

    if query_func is None:
        def _default_query(query, books=None):
            return search_keyword.query_keywords(
                kb_root, query, max_results=top_k,
                include_question_index=include_question_index, _cache=query_cache,
                books=books, include_nav=include_nav, nav_weight=nav_weight)
        query_func = _default_query

    def _targets(r):
        return r.get("答案块ID列表") or [r["答案块ID"]]

    def _is_effective(r):
        if real_mode and r.get("限定书") and r["限定书"] not in disk_books:
            return False
        return any(bid in valid_ids for bid in _targets(r))

    effective = [r for r in rows if _is_effective(r)]
    dropped = [r for r in rows if not _is_effective(r)]

    hits5 = hits10 = 0
    mrr = 0.0
    rank6to10 = []   # 命中 top10 但未进 top5（排名 6~10）
    miss_top10 = []  # 未进 top10（含答案块压根没被检索到）
    for r in effective:
        if real_mode:
            results = query_func(r["提问"], books=(r.get("限定书") or None))
        else:
            results = query_func(r["提问"])
        targets = set(_targets(r))
        rank = next((i + 1 for i, res in enumerate(results) if res[1] in targets), None)
        if rank is None:
            miss_top10.append({**r, "排名": None})
            continue
        hits10 += 1
        mrr += 1.0 / rank
        if rank <= 5:
            hits5 += 1
        else:
            rank6to10.append({**r, "排名": rank})

    n = len(effective)
    return {
        "总题数": len(rows),
        "有效题": n,
        "剔除旧题": len(dropped),
        "top5命中": hits5,
        "top10命中": hits10,
        "mrr": mrr / n if n else 0.0,
        "rank6to10": rank6to10,
        "miss_top10": miss_top10,
    }


def evaluate_dual(kb_root: Path, eval_path: Path, top_k: int = TOP_K) -> dict:
    """同一有效题集、同一批缓存分别跑纯内容与含题干直达，生产评分逻辑只有一份。"""
    query_cache = search_keyword._new_query_cache(kb_root)
    return {
        "纯内容检索": evaluate(kb_root, eval_path, top_k=top_k,
                              include_question_index=False, include_nav=False,
                              query_cache=query_cache),
        "含题干直达": evaluate(kb_root, eval_path, top_k=top_k,
                              include_question_index=True, query_cache=query_cache),
    }


def evaluate_real(kb_root: Path, eval_path: Path, top_k: int = TOP_K,
                  nav_weight: float = None) -> dict:
    """#66：真实提问集对比——关导航纯内容 vs 开启块标签「导航」节。"""
    query_cache = search_keyword._new_query_cache(kb_root)
    w_label = nav_weight if nav_weight is not None else search_keyword.NAV_DEFAULT_WEIGHT
    return {
        "真实提问·纯内容（关导航）": evaluate(
            kb_root, eval_path, top_k=top_k,
            include_question_index=False, include_nav=False,
            query_cache=query_cache, real_mode=True),
        f"真实提问·含块标签导航（权重={w_label}）": evaluate(
            kb_root, eval_path, top_k=top_k,
            include_question_index=False, include_nav=True, nav_weight=nav_weight,
            query_cache=query_cache, real_mode=True),
    }


def _print_summary(res: dict, heading: str = "含题干直达"):
    n = res["有效题"]
    print(f"检索命中率评测尺（R10，{heading}）")
    print(f"评测集题数：{res['总题数']}　有效题（答案块ID在库）：{n}　"
          f"剔除旧题（块ID失效）：{res['剔除旧题']}")
    if not n:
        print("没有有效题，无法计算 top5/top10 与 MRR；请检查评测集及答案块 ID 是否仍有效。")
        return
    print(f"top5  命中率：{res['top5命中'] / n * 100:.1f}%（{res['top5命中']}/{n}）")
    print(f"top10 命中率：{res['top10命中'] / n * 100:.1f}%（{res['top10命中']}/{n}）")
    print(f"MRR（top{TOP_K}）：{res['mrr']:.4f}")
    if res["rank6to10"]:
        print(f"\n命中 top10 但未进 top5（{len(res['rank6to10'])} 道）：")
        for r in res["rank6to10"]:
            print(f"  {r['题号']} | 排名{r['排名']} | {r['答案块ID']} | {r['提问']}")
    if res["miss_top10"]:
        print(f"\n未命中 top10（{len(res['miss_top10'])} 道）：")
        for r in res["miss_top10"]:
            print(f"  {r['题号']} | {r['答案块ID']} | {r['提问']}")


def main():
    _ensure_utf8_stdio()
    args = sys.argv[1:]
    dataset = "默认"
    nav_weight = None
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--集" and i + 1 < len(args):
            i += 1
            dataset = args[i]
        elif a == "--导航权重" and i + 1 < len(args):
            i += 1
            nav_weight = float(args[i])
        else:
            print("用法：python eval_recall.py [--集 真实] [--导航权重 0.5]")
            sys.exit(1)
        i += 1

    kb_root = common.get_path("知识库")
    if dataset == "真实":
        eval_path = kb_root / "评测集_真实提问.md"
        if not eval_path.exists():
            print(f"错误：真实提问评测集不存在：{eval_path}")
            sys.exit(1)
        try:
            results = evaluate_real(kb_root, eval_path, nav_weight=nav_weight)
        except ValueError as e:
            print(f"错误：{e}")
            sys.exit(1)
    else:
        eval_path = common.get_path("评测集")
        if not eval_path.exists():
            print(f"错误：评测集不存在：{eval_path}")
            sys.exit(1)
        try:
            results = evaluate_dual(kb_root, eval_path)
        except ValueError as e:
            print(f"错误：{e}")
            sys.exit(1)
    for heading, res in results.items():
        _print_summary(res, heading)
        print()
    sys.exit(0)


def _selfcheck():
    """验收：①块存在性判定 ②判分逻辑 ③共享缓存与双口径 ④#66 真实集（多答案块/限定书/导航对比）。"""
    import tempfile
    import shutil

    tmp = Path(tempfile.mkdtemp(prefix="eval_recall_selfcheck_"))
    try:
        kb_root = tmp / "kb"
        book_dir = kb_root / "书籍" / "自检书"
        book_dir.mkdir(parents=True)
        (book_dir / "原文.md").write_text("【自检01·0001】p.1\n水口之法定向\n", encoding="utf-8")
        (book_dir / "倒排表.json").write_text(
            json.dumps({
                "书号": "自检01", "构建时间": "2026-09-17",
                "条目": {"排龙": ["自检01·0002"], "排龙诀": ["自检01·0002"],
                         "水口": ["自检01·0002", "自检01·0001"],
                         "找不到": ["自检01·0003"]},
                "题干": {"口诀": ["自检01·0002"], "诀如": ["自检01·0002"],
                         "如何": ["自检01·0002"], "口诀如": ["自检01·0002"],
                         "诀如何": ["自检01·0002"]},
                "导航": {"龙怎": ["自检01·0002"], "怎么": ["自检01·0002"],
                         "么排": ["自检01·0002"]},
            }, ensure_ascii=False, indent=2), encoding="utf-8")
        other = kb_root / "书籍" / "他书"
        other.mkdir(parents=True)
        (other / "原文.md").write_text("【自检02·0001】p.1\n他书内容\n", encoding="utf-8")
        (other / "倒排表.json").write_text(
            json.dumps({"书号": "自检02", "构建时间": "2026-09-17",
                        "条目": {"他书": ["自检02·0001"], "龙怎": ["自检02·0001"]}},
                       ensure_ascii=False, indent=2), encoding="utf-8")
        (kb_root / "书目总索引.md").write_text(
            "# 书目总索引\n\n| 书名 | 书号 | 分类 | 一句话主题 | 关键词 | 规模 |\n"
            "|---|---|---|---|---|---|\n"
            "| 自检书 | 自检01 | 风水 | - | - | 3块 |\n"
            "| 他书 | 自检02 | 命理 | - | - | 1块 |\n",
            encoding="utf-8")
        eval_path = kb_root / "评测集.md"
        eval_path.write_text(
            "# 评测集\n\n| 题号 | 提问 | 答案块ID | 密文 | 命中 | token |\n|---|---|---|---|---|---|\n"
            "| Q1 | 排龙诀的口诀是什么 | 自检01·0002 |  |  |  |\n"
            "| Q2 | 水口怎么定 | 自检01·0001 |  |  |  |\n"
            "| Q3 | 他书问题 | 自检02·0001 |  |  |  |\n"
            "| Q4 | 完全找不到 | 自检01·0003 |  |  |  |\n"
            "| Q5 | 旧题 | 玄空01·0007 |  |  |  |\n",
            encoding="utf-8")

        ok = True

        valid = collect_valid_ids(kb_root)
        if valid != {"自检01·0001", "自检01·0002", "自检01·0003", "自检02·0001"}:
            print(f"  ❌ 有效块ID集合异常：{sorted(valid)}")
            ok = False
        else:
            print("  ✅ 块存在性判定：跨书收集 4 块")

        def stub(query):
            table = {
                "排龙诀的口诀是什么": [("2", "自检01·0002", "自检书", "1", "排龙之法")],
                "水口怎么定": [("1", "自检01·0003", "自检书", "1", "x"),
                                ("1", "自检01·0003", "自检书", "1", "y"),
                                ("1", "自检01·0002", "自检书", "1", "z"),
                                ("1", "自检01·0001", "自检书", "1", "w")],
                "他书问题": [("1", f"自检01·000{i}", "自检书", "1", chr(96 + i))
                             for i in (1, 2, 3, 1, 2, 3, 1)]
                            + [("1", "自检02·0001", "他书", "1", "g")],
                "完全找不到": [("1", "自检01·0001", "自检书", "1", "a")],
            }
            return table.get(query, [])
        res = evaluate(kb_root, eval_path, query_func=stub)
        if res["有效题"] != 4 or res["剔除旧题"] != 1 \
                or res["top5命中"] != 2 or res["top10命中"] != 3 \
                or abs(res["mrr"] - 0.34375) > 1e-9:
            print(f"  ❌ 判分逻辑异常：{res}")
            ok = False
        else:
            print(f"  ✅ 判分逻辑：top5={res['top5命中']}/4 top10={res['top10命中']}/4 MRR={res['mrr']:.4f}")

        rows = parse_evals(eval_path)
        cache = search_keyword._new_query_cache(kb_root)
        for r in rows:
            q = r["提问"]
            got = search_keyword.query_keywords(kb_root, q, max_results=10, _cache=cache)
            want = search_keyword.query_keywords(kb_root, q, max_results=10)
            if got != want:
                print(f"  ❌ 共享缓存与生产查询不一致：{q}")
                ok = False
        if ok:
            print(f"  ✅ 共享缓存 ≡ 生产 query_keywords（{len(rows)} 道全等）")

        dual_path = kb_root / "双口径评测.md"
        dual_path.write_text(
            "# 评测集\n| 题号 | 提问 | 答案块ID |\n|---|---|---|\n"
            "| Q6 | 口诀如何 | 自检01·0002 |\n", encoding="utf-8")
        dual = evaluate_dual(kb_root, dual_path)
        pure, with_q = dual["纯内容检索"], dual["含题干直达"]
        if (pure["top5命中"], pure["top10命中"], with_q["top5命中"],
                with_q["top10命中"]) != (0, 0, 1, 1):
            print(f"  ❌ #65 M2 双口径异常：{dual}")
            ok = False
        else:
            print("  ✅ #65 M2 双口径：纯内容 0/1，含题干 1/1")

        zero_path = kb_root / "零有效题.md"
        zero_path.write_text(
            "# 评测集\n| 题号 | 提问 | 答案块ID |\n|---|---|---|\n"
            "| Q7 | 旧题 | 自检01·9999 |\n", encoding="utf-8")
        zero = evaluate_dual(kb_root, zero_path)
        if any(v["有效题"] != 0 for v in zero.values()):
            print(f"  ❌ #65 M2 零有效题计数异常：{zero}")
            ok = False
        else:
            print("  ✅ #65 M2 零有效题：两种口径均不除零")

        # ④ #66 真实提问盲测集：限定书过滤 + 多答案块逗号 + 关导航 vs 含块标签导航对比
        real_path = kb_root / "评测集_真实提问.md"
        real_path.write_text(
            "# 评测集 · 真实提问\n\n| 题号 | 提问（用户原话） | 限定书 | 答案块ID | 确认人 |\n"
            "|---|---|---|---|---|\n"
            "| R001 | 龙怎么排 | 自检书 | 自检01·0009, 自检01·0002 | 人工 |\n",
            encoding="utf-8")
        real_res = evaluate_real(kb_root, real_path, nav_weight=0.5)
        vals = list(real_res.values())
        if vals[0]["有效题"] != 1 or vals[0]["top5命中"] != 0 or vals[1]["top5命中"] != 1:
            print(f"  ❌ #66 真实提问评测与导航对比异常：{real_res}")
            ok = False
        else:
            print("  ✅ #66 真实提问集：限定书/多答案块/导航增益对比正常")

        print(f"  {'✅ R10 评测尺自检通过' if ok else '❌ R10 评测尺自检失败'}")
        sys.exit(0 if ok else 1)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    _ensure_utf8_stdio()
    if common.is_selfcheck():
        _selfcheck()
    else:
        main()
