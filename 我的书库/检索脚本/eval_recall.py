# -*- coding: utf-8 -*-
"""R10 检索命中率评测尺（#4 M1）：只读脚本，不写库、不改任何数据文件。

对评测集每道**有效题**（答案块ID在现库存在）直接调用 search_keyword.query_keywords
（import 复用、不起子进程），统计答案块是否进 top5 / top10，输出命中率与 MRR。
旧题（答案块ID已失效，如旧缩写书号题）自动剔除并计数——旧题迁移归 #33，与本尺无关。

用法：
    python eval_recall.py          # 在现库有效题上跑 top5/top10 命中率基线 + MRR
    python eval_recall.py --自检    # 迷你沙箱库验证判分逻辑（块存在性判定/命中率/MRR）

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


def collect_valid_ids(kb_root: Path) -> set:
    """现库有效块ID集合：收集各书 倒排表.json 条目中的全部块ID（只读）。"""
    valid = set()
    for _title, book_dir in search_keyword._iter_books(kb_root):
        inv_path = book_dir / "倒排表.json"
        if not inv_path.exists():
            continue
        try:
            entries = json.loads(inv_path.read_text(encoding="utf-8")).get("条目", {})
        except Exception:
            continue
        for bids in entries.values():
            valid.update(bids)
    return valid


def _lookup_preview_lines(lines: list, block_id: str) -> tuple:
    """行列表版预览查找（与 search_keyword._lookup_page_preview 同逻辑；#60 起主脚本同为行列表版）。

    返回 (页码, 首行预览)：找块 ID 行取页码，取该块下一非空行的前 30 字。"""
    page, preview = "", ""
    for line in lines:
        m = search_keyword.BLOCK_ID_RE.match(line.strip())
        if m and f"{m.group(1)}·{m.group(2)}" == block_id:
            page = m.group(3)
            preview = ""
        elif preview == "" and line.strip():
            preview = line.strip()[:30]
        elif page and preview:
            break
    return page, preview


def _cached_query_factory(kb_root: Path, top_k: int = TOP_K):
    """构造带进程级缓存的查询函数（语义与 query_keywords 完全一致，复用其公共函数）。

    68 道题逐题调 query_keywords 会反复 json.loads 各书倒排表、整本读 原文.md
    （分钟级）；缓存后各书倒排表与原文只读一次，全库基线压缩到秒级。
    评分/排序与 query_keywords 相同：命中 n-gram 一律 score+=1、稳定降序取前 top_k。
    """
    syn = search_keyword.load_synonyms(kb_root)
    books = []  # [(书名, book_dir, 条目dict, 题干dict)]
    for title, book_dir in search_keyword._iter_books(kb_root):
        inv = book_dir / "倒排表.json"
        if not inv.exists():
            continue
        try:
            data = json.loads(inv.read_text(encoding="utf-8"))
        except Exception:
            continue
        books.append((title, book_dir, data.get("条目", {}), data.get("题干", {}) or {}))
    orig_cache = {}  # 书名 → 原文行列表（懒加载）

    def query(q):
        # 与 query_keywords 同构：逗号拆词（R9）→ 各词扩词收集 grams → 同权打分
        grams_needed = set()
        for part in (p.strip() for p in q.split(",")):
            if not part:
                continue
            for v in search_keyword.expand_query(part, syn):
                chars = v
                for n in (2, 3):
                    for i in range(len(chars) - n + 1):
                        grams_needed.add(chars[i : i + n])
        score_map = {}
        for g in grams_needed:
            w = search_keyword.W_GRAM.get(len(g), 1)  # #4 M2：与 B-01 同权重
            for title, _dir, entries, qgrams in books:
                for src in (entries, qgrams):  # #4 M3（R4）：与 B-01 同查「条目＋题干」
                    for bid in src.get(g, []):
                        info = score_map.setdefault(bid, {"score": 0, "书名": title})
                        info["score"] += w
        if not score_map:
            return []
        # 与 B-01 同排序：分数降序、块ID升序（确定性，防 set 迭代随机化抖排名）
        ranked = sorted(score_map.items(), key=lambda kv: (-kv[1]["score"], kv[0]))
        results = []
        for bid, info in ranked[: top_k * search_keyword.DEDUP_POOL_MULT]:
            title = info["书名"]
            if title not in orig_cache:
                orig_cache[title] = (
                    kb_root / "书籍" / title / "原文.md"
                ).read_text(encoding="utf-8").split("\n")
            page, preview = _lookup_preview_lines(orig_cache[title], bid)
            results.append((info["score"], bid, title, page, preview))
        results = search_keyword._dedup_same_page(results)  # #4 M2：与 B-01 同去重
        return results[:top_k]

    return query


def evaluate(kb_root: Path, eval_path: Path, query_func=None, top_k: int = TOP_K) -> dict:
    """跑评测尺。

    query_func(query) → [(score, bid, 书名, 页码, 首行预览)]（默认带缓存的
    query_keywords 等价查询器，可注入桩函数供自检验证判分逻辑）。返回汇总 dict。
    """
    rows = parse_evals(eval_path)
    valid_ids = collect_valid_ids(kb_root)
    if query_func is None:
        query_func = _cached_query_factory(kb_root, top_k)

    effective = [r for r in rows if r["答案块ID"] in valid_ids]
    dropped = [r for r in rows if r["答案块ID"] not in valid_ids]

    hits5 = hits10 = 0
    mrr = 0.0
    rank6to10 = []   # 命中 top10 但未进 top5（排名 6~10）
    miss_top10 = []  # 未进 top10（含答案块压根没被检索到）
    for r in effective:
        results = query_func(r["提问"])
        rank = next((i + 1 for i, res in enumerate(results) if res[1] == r["答案块ID"]), None)
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


def _print_summary(res: dict):
    n = res["有效题"]
    print("检索命中率评测尺（R10，#4 M1）")
    print(f"评测集题数：{res['总题数']}　有效题（答案块ID在库）：{n}　"
          f"剔除旧题（块ID失效）：{res['剔除旧题']}")
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
    kb_root = common.get_path("知识库")
    eval_path = common.get_path("评测集")
    if not eval_path.exists():
        print(f"错误：评测集不存在：{eval_path}")
        sys.exit(1)
    res = evaluate(kb_root, eval_path)
    _print_summary(res)
    sys.exit(0)


def _selfcheck():
    """验收：①块存在性判定（跨书收集）②判分逻辑（桩注入）③真实 query_keywords 冒烟。"""
    import tempfile
    import shutil

    tmp = Path(tempfile.mkdtemp(prefix="eval_recall_selfcheck_"))
    try:
        kb_root = tmp / "kb"
        # 沙箱库：2 本书，验证跨书收集有效块ID
        book_dir = kb_root / "书籍" / "自检书"
        book_dir.mkdir(parents=True)
        (book_dir / "原文.md").write_text("【自检01·0001】p.1\n水口之法定向\n", encoding="utf-8")
        (book_dir / "倒排表.json").write_text(
            json.dumps({
                "书号": "自检01", "构建时间": "2026-09-17",
                "条目": {"排龙": ["自检01·0002"], "排龙诀": ["自检01·0002"],
                         "水口": ["自检01·0002", "自检01·0001"],
                         "找不到": ["自检01·0003"]},
            }, ensure_ascii=False, indent=2), encoding="utf-8")
        other = kb_root / "书籍" / "他书"
        other.mkdir(parents=True)
        (other / "原文.md").write_text("【自检02·0001】p.1\n他书内容\n", encoding="utf-8")
        (other / "倒排表.json").write_text(
            json.dumps({"书号": "自检02", "构建时间": "2026-09-17",
                        "条目": {"他书": ["自检02·0001"]}},
                       ensure_ascii=False, indent=2), encoding="utf-8")
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

        # ① 块存在性判定（真实沙箱库）：Q5（玄空01·0007）不在库应剔除
        valid = collect_valid_ids(kb_root)
        if valid != {"自检01·0001", "自检01·0002", "自检01·0003", "自检02·0001"}:
            print(f"  ❌ 有效块ID集合异常：{sorted(valid)}")
            ok = False
        else:
            print("  ✅ 块存在性判定：跨书收集 4 块")

        # ② 判分逻辑（桩注入，结果可控）：
        #    Q1 排名1 → top5 命中、MRR 1.0；Q2 排名4 → top5 命中、MRR 0.25
        #    Q3 排名8 → top10 命中（非 top5）、MRR 0.125；Q4 未命中 → MRR 0
        #    Q5 剔除不计。期望 top5=2/4=50%、top10=3/4=75%、MRR=(1+0.25+0.125)/4=0.34375
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
        if res["有效题"] != 4 or res["剔除旧题"] != 1:
            print(f"  ❌ 有效/剔除计数异常：{res}")
            ok = False
        elif res["top5命中"] != 2 or res["top10命中"] != 3:
            print(f"  ❌ 命中率异常：top5={res['top5命中']} top10={res['top10命中']}（应 2/3）")
            ok = False
        elif abs(res["mrr"] - 0.34375) > 1e-9:
            print(f"  ❌ MRR 异常：{res['mrr']}（应 0.34375）")
            ok = False
        else:
            print(f"  ✅ 判分逻辑：top5={res['top5命中']}/4 top10={res['top10命中']}/4 MRR={res['mrr']:.4f}")
        if len(res["rank6to10"]) != 1 or len(res["miss_top10"]) != 1:
            print(f"  ❌ 未命中清单异常：rank6to10={len(res['rank6to10'])} miss_top10={len(res['miss_top10'])}")
            ok = False
        else:
            print("  ✅ 未命中清单：top5 外 1 道、top10 外 1 道")

        # ③ 缓存查询器 ≡ query_keywords（沙箱库上逐题比对结果完全一致）
        rows = parse_evals(eval_path)
        cached = _cached_query_factory(kb_root)
        for r in rows:
            q = r["提问"]
            got = cached(q)
            want = search_keyword.query_keywords(kb_root, q, max_results=10)
            if got != want:
                print(f"  ❌ 缓存查询器与 query_keywords 不一致：{q}\n    got={got}\n    want={want}")
                ok = False
        if ok:
            print(f"  ✅ 缓存查询器 ≡ query_keywords（{len(rows)} 道全等）")

        print(f"  {'✅ R10 评测尺自检通过' if ok else '❌ R10 评测尺自检失败'}")
        sys.exit(0 if ok else 1)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    if common.is_selfcheck():
        _selfcheck()
    else:
        main()
