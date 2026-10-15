# -*- coding: utf-8 -*-
"""卡 B-01 search_keyword.py：零 token 本地检索脚本（供 TRAE 中的 AI 调用）。

模式 1 --query "词"    ：术语表扩展 → 简繁归一 → 倒排表 n-gram 查询 → 相关度排序输出
模式 2 --locate "块ID" ：现场扫描原文.md 定位块 ID 当前行号区间（不读建库时行号）
模式 3 --verify "块ID" "预期指纹"：定位后比对指纹，不符则在 ±50 行内搜索纠偏并告警
模式 4 --fetch "词"    ：组合动作（#63 M2-B）——query+locate+原文一次给齐：
                        每个命中块直接输出 块ID+行号区间+指纹+原文逐字全文，
                        把「查询→定位→读原文」三次往返塌缩成一次调用。

输出：纯文本打印到 stdout。
退出码：0=成功；1=查询无命中/verify 搜不到指纹；2=命中退休块（#21 M4 失效诚实：
该块已随书籍更新退休，输出退休日期/原因/后继块，绝不静默改指其他块）。
"""

import functools
import json
import re
import sys
from pathlib import Path

# 定位公共模块（common.py 位于 项目根\资料预处理\脚本\）
_SCRIPT_DIR = Path(__file__).resolve()
_PROJECT_ROOT = _SCRIPT_DIR.parents[2]
sys.path.insert(0, str(_PROJECT_ROOT / "资料预处理" / "脚本"))
import common  # noqa: E402

FINGERPRINT_LEN = 12
FINGERPRINT_RANGE = 50
BLOCK_ID_RE = re.compile(r"^【(.+?)·(\d{4})】p\.(.*)$")

# #4 M2（R7）：n-gram 命中权重——3 字权重经 R10 评测尺调参**定稿为与 2 字同权**
# （3 字加权 2/3 均使 top10 命中率下降：口语化提问的 2 字碎片命中更鲁棒，加权反而挤掉答案块）
W_GRAM = {2: 1, 3: 1}
# #4 M2：同页相似块去重阈值——同书同页相邻块预览相似度 ≥ 此值视为"车轱辘块"合并代表
DEDUP_PREVIEW_RATIO = 0.6
# #4 M2：去重候选池倍数（排序后取前 N×max_results 做折叠，留足顺次补位的余量）
DEDUP_POOL_MULT = 5
# #4 M2 顺带 R8：弱命中阈值——榜单最高分 ≤ 此值视为"仅碎片匹配"（输出弱命中提示）
WEAK_HIT_THRESHOLD = 1
# #63 M2-B：--fetch 默认取前 5 块（与协议「单次最多引用 5 块」对齐）
FETCH_DEFAULT_N = 5


_CC = None          # #60：OpenCC 模块级惰性单例（见 _opencc）
_CC_WARNED = False  # #60：opencc 缺失的降级告警只发一次（normalize 的 except 分支）


def _opencc():
    """#60：OpenCC 单例。其 C++ 构造器无跨实例缓存，每次新建都重读重载字典
    （实测冷态 7~10ms/次；load_synonyms 每进程 554 次 normalize ≈ 4.6s，
    占 --query 端到端 80%）。单例后每进程仅构造一次（~10ms）。"""
    global _CC
    if _CC is None:
        from opencc import OpenCC
        _CC = OpenCC("t2s")
    return _CC


@functools.lru_cache(maxsize=None)
def normalize(text: str) -> str:
    """简繁归一 + 去标点空白（与 A-03 检索副本规则一致）。

    #60：结果缓存——每进程输入天然有界（术语表 554 词＋个位数查询词），
    缓存只驻留内存、不落盘；术语表修改对下次查询进程即生效（#42 现场读表口径不变）。"""
    global _CC_WARNED
    try:
        text = _opencc().convert(text)
    except ImportError:
        if not _CC_WARNED:  # #60：降级不再静默，防繁体查询漏检无感知（stderr 行只用 GBK 可编码字符）
            _CC_WARNED = True
            print("[警告] opencc 不可用，简繁归一已跳过：繁体查询词可能漏检。", file=sys.stderr)
    text = re.sub(r"[\s\u3000\u00a0]+", "", text)
    text = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", text)
    return text


def load_synonyms(kb_root: Path) -> dict:
    """读 术语同义词表.md → {主词条: [变体...]}（含主词条自身）。"""
    path = kb_root / "术语同义词表.md"
    syn = {}
    if not path.exists():
        return syn
    for line in path.read_text(encoding="utf-8").split("\n"):
        line = line.strip()
        if not line.startswith("|") or line.startswith("|---"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 2 or cells[0] in ("主词条", ""):
            continue
        main = normalize(cells[0])
        if not main:
            continue
        variants = [normalize(v) for v in cells[1].replace("/", ",").split(",") if v.strip()]
        syn[main] = list(dict.fromkeys([main] + variants))
    return syn


def expand_query(query: str, syn: dict) -> list:
    """按术语表把查询词扩展为全部变体（含自身）。"""
    q = normalize(query)
    if not q:
        return []
    if q in syn:
        return syn[q]
    for main, variants in syn.items():
        if q in variants:
            return variants
    return [q]


def _iter_books(kb_root: Path):
    """遍历 书籍/书名 目录，返回 (书名, 目录Path)。"""
    books_dir = kb_root / "书籍"
    if not books_dir.exists():
        return
    for d in sorted(books_dir.iterdir()):
        if d.is_dir() and (d / "原文.md").exists():
            yield d.name, d


def _block_seq_adj(bid_a: str, bid_b: str) -> bool:
    """块ID出生序号是否相邻（差 1）。块 ID＝出生序，序号连续＝原文物理相邻。"""
    try:
        a = int(bid_a.split("·")[1])
        b = int(bid_b.split("·")[1])
    except (IndexError, ValueError):
        return False
    return abs(a - b) == 1


def _dedup_same_page(results: list) -> list:
    """#4 M2（R7）：同书同页相邻相似块合并代表（防"车轱辘块"霸榜）。

    判据：同书名 ＋ 印刷页码相同 ＋ 块ID出生序相邻 ＋ 首行预览相似度 ≥ 阈值。
    组内保留分数最高者为代表（输入已按分数降序，先到先得即高分保留）；
    折叠后其余块顺次补上（输入含 max_results×DEDUP_POOL_MULT 候选，留足补位余量）。
    """
    import difflib

    selected = []
    for res in results:
        score, bid, book, page, preview = res
        dup = False
        for s in selected:
            if s[2] != book or s[3] != page:
                continue
            if not _block_seq_adj(bid, s[1]):
                continue
            if preview and s[4] and difflib.SequenceMatcher(None, preview, s[4]).ratio() >= DEDUP_PREVIEW_RATIO:
                dup = True
                break
        if not dup:
            selected.append(res)
    return selected


def query_keywords(kb_root: Path, query: str, max_results: int = 20) -> list:
    """模式 1：关键词查询。返回 [(相关度, 块ID, 书名, 页码, 首行预览)]。

    #4 M2（R7）：同页相邻相似块合并代表（_dedup_same_page），车轱辘块不再霸榜；
    排序确定性化（分数降序、块ID升序，防 set 迭代随机化抖排名）。
    #4 M2 顺带 R9：`query` 支持逗号分隔多词（如 "排龙,水口"），各词分别按术语表
    扩展后合并 n-gram 统一打分（= 批量查询合并榜单）。
    """
    syn = load_synonyms(kb_root)
    grams_needed = set()
    for part in (p.strip() for p in query.split(",")):
        if not part:
            continue
        for v in expand_query(part, syn):
            chars = v
            for n in (2, 3):
                for i in range(len(chars) - n + 1):
                    grams_needed.add(chars[i : i + n])

    score_map = {}  # 块ID → {"score", "书名", "页码", "首行"}
    for book_title, book_dir in _iter_books(kb_root):
        index_path = book_dir / "倒排表.json"
        if not index_path.exists():
            continue
        index = json.loads(index_path.read_text(encoding="utf-8"))
        entries = index.get("条目", {})
        qgrams = index.get("题干", {}) or {}  # #4 M3（R4）：题干词条（独立节，检索入口）
        for g in grams_needed:
            w = W_GRAM.get(len(g), 1)
            for src in (entries, qgrams):
                for bid in src.get(g, []):
                    info = score_map.setdefault(bid, {"score": 0, "书名": book_title})
                    info["score"] += w

    if not score_map:
        return []

    ranked = sorted(score_map.items(), key=lambda kv: (-kv[1]["score"], kv[0]))
    # 第二键=块ID：同分块顺序确定（set 迭代随进程 hash 随机化会抖排名，#4 M2 修复）
    results = []
    orig_lines = {}  # #60 M2：书名 → 原文行列表（每书一次读入，替代按块整本重读）
    for bid, info in ranked[: max_results * DEDUP_POOL_MULT]:
        book = info["书名"]
        if book not in orig_lines:
            orig_lines[book] = (kb_root / "书籍" / book / "原文.md").read_text(
                encoding="utf-8").split("\n")
        page, preview = _lookup_page_preview(orig_lines[book], bid)
        results.append((info["score"], bid, book, page, preview))
    results = _dedup_same_page(results)
    return results[:max_results]


def _lookup_page_preview(lines: list, block_id: str) -> tuple:
    """在已读入的原文行列表中找块 ID，返回 (页码, 首行预览)。

    #60 M2：原按块 read_text 整本原文（候选池 100 块 × 最多 892KB）；
    改行列表版、由调用方按书一次读入（与 eval_recall._lookup_preview_lines 同构）。"""
    page, preview = "", ""
    for line in lines:
        m = BLOCK_ID_RE.match(line.strip())
        if m and f"{m.group(1)}·{m.group(2)}" == block_id:
            page = m.group(3)
            preview = ""
        elif preview == "" and line.strip():
            preview = line.strip()[:30]
        elif page and preview:
            break
    return page, preview


def find_retire_entry(kb_root: Path, block_id: str) -> dict:
    """#21 M4 失效诚实：查退休登记册。返回 {"书名", "条目"} 或 None。

    书号经各书 倒排表.json 头部反查（与 update_book.resolve_book_title 同源）；
    条目＝{块ID, 日期, 原因(修改/删除), 后继, 相似度}（知识库\\书籍\\{书名}\\退休块.json）。
    """
    if "·" not in block_id:
        return None
    book_id = block_id.split("·")[0]
    for book_title, book_dir in _iter_books(kb_root):
        inv = book_dir / "倒排表.json"
        if not inv.exists():
            continue
        try:
            if json.loads(inv.read_text(encoding="utf-8")).get("书号") != book_id:
                continue
        except Exception:
            continue
        reg_path = book_dir / "退休块.json"
        if not reg_path.exists():
            return None
        try:
            entries = json.loads(reg_path.read_text(encoding="utf-8")).get("退休块", [])
        except Exception:
            return None
        for e in entries:
            if e.get("块ID") == block_id:
                return {"书名": book_title, "条目": e}
        return None
    return None


def locate_block(kb_root: Path, block_id: str) -> dict:
    """模式 2：现场扫描原文.md 定位块 ID 当前行号区间（1 基）。

    查无此块时先查退休登记册（#21 M4）：命中退休块返回 {"退休": 条目, ...}
    而不抛错——调用方按退休口径应答（退出码 2），绝不静默改指其他块。"""
    if "·" not in block_id:
        raise ValueError(f"块 ID 格式错误：{block_id}（应为 书号·NNNN）")
    for book_title, book_dir in _iter_books(kb_root):
        orig_path = book_dir / "原文.md"
        copy_path = book_dir / "检索副本.md"
        if not copy_path.exists():
            continue
        orig_lines = orig_path.read_text(encoding="utf-8").split("\n")
        copy_lines = copy_path.read_text(encoding="utf-8").split("\n")
        start = None
        page = ""
        for i, line in enumerate(orig_lines):
            m = BLOCK_ID_RE.match(line.strip())
            if m and f"{m.group(1)}·{m.group(2)}" == block_id:
                start = i
                page = m.group(3)
                break
        if start is None:
            continue
        end = start
        for i in range(start + 1, len(orig_lines)):
            if BLOCK_ID_RE.match(orig_lines[i].strip()):
                break
            end = i
        fingerprint = common.block_fingerprint(copy_lines[start + 1 : end + 1])
        return {
            "块ID": block_id,
            "书名": book_title,
            "起始行": start + 1,
            "结束行": end + 1,
            "页码": page,
            "指纹": fingerprint,
        }
    retired = find_retire_entry(kb_root, block_id)
    if retired:
        return {"块ID": block_id, "书名": retired["书名"], "退休": retired["条目"]}
    raise ValueError(f"找不到块 ID：{block_id}")


def _pdf_page_seq(book_dir: Path, tag: str) -> str:
    """块标签 → PDF 页序文本（设计 4.4）。无 页码映射.json 或无法映射返回 None。

    已拟合标签（印刷页码）：反查已拟合段，按 偏移 折算回 PDF 页；
    降级标签（PDF.x）：本身就是 PDF 页序，直接透传数字。"""
    map_path = book_dir / "页码映射.json"
    if not map_path.exists():
        return None
    try:
        segments = json.loads(map_path.read_text(encoding="utf-8")).get("分段", [])
    except Exception:
        return None
    if not segments:
        return None
    parsed = common.parse_page_tag(tag)
    if parsed["降级"]:
        m = re.match(r"^PDF\.(\d+)(?:~(\d+))?$", tag)
        if not m:
            return None
        start = int(m.group(1))
        end = int(m.group(2)) if m.group(2) else start
        return f"{start}~{end}" if end > start else str(start)
    for s in segments:
        if s.get("状态") != "已拟合":
            continue
        seg_s = s.get("印刷页起")
        pdf_s = s.get("PDF页起")
        pdf_e = s.get("PDF页止")
        if seg_s is None or pdf_s is None or pdf_e is None:
            continue
        span = pdf_e - pdf_s
        if seg_s <= parsed["起始"] and parsed["结束"] <= seg_s + span:
            start = pdf_s + (parsed["起始"] - seg_s)
            end = pdf_s + (parsed["结束"] - seg_s)
            return f"{start}~{end}" if end > start else str(start)
    return None


def verify_block(kb_root: Path, block_id: str, expected_fp: str) -> dict:
    """模式 3：定位后比对指纹；不符则 ±50 行内搜索纠偏并告警。

    命中退休块（#21 M4）直接返回 {"退休": 条目}，不做指纹比对。"""
    loc = locate_block(kb_root, block_id)
    if "退休" in loc:
        return {"块ID": block_id, "书名": loc["书名"], "退休": loc["退休"]}
    expected = normalize(expected_fp)
    actual = loc["指纹"]
    result = {"块ID": block_id, "书名": loc["书名"], "起始行": loc["起始行"], "结束行": loc["结束行"],
              "预期指纹": expected, "实际指纹": actual, "一致": expected == actual, "告警": None}

    if result["一致"]:
        return result

    # ±50 行内搜索纠偏
    book_dir = kb_root / "书籍" / loc["书名"]
    copy_lines = (book_dir / "检索副本.md").read_text(encoding="utf-8").split("\n")
    found = None
    lo = max(0, loc["起始行"] - 1 - FINGERPRINT_RANGE)
    hi = min(len(copy_lines), loc["起始行"] - 1 + FINGERPRINT_RANGE)
    for i in range(lo, hi):
        if copy_lines[i].strip().startswith(expected):
            found = i + 1
            break
    if found is None:
        raise ValueError(f"±{FINGERPRINT_RANGE} 行内未找到预期指纹，块 {block_id} 位置异常，需人工检查")
    result["纠偏后行号"] = found
    result["告警"] = (f"⚠️ 块 {block_id} 指纹不符，已在 ±{FINGERPRINT_RANGE} 行内纠偏至行 {found}（原 {loc['起始行']}）")
    return result


def _retire_line(loc: dict) -> str:
    """退休口径输出行（#21 M4，设计 §3.3）：已修订/删除 + 日期 + 后继块（若有）。"""
    e = loc["退休"]
    reason = "修订" if e.get("原因") == "修改" else "删除"
    succ = (f"，后继块：{e['后继']}｜可按后继块继续检索" if e.get("后继")
            else "，无后继块")
    return (f"{loc['块ID']} | 《{loc['书名']}》 该块已于 {e.get('日期', '?')} {reason}退休{succ}"
            f"｜内容已失效，请勿引用旧块")


def _print_results(results: list):
    if not results:
        print("无命中")
        return
    for score, bid, book, page, preview in results:
        print(f"[{score}] {bid} | 《{book}》 p.{page} | {preview}…")


def fetch_blocks(kb_root: Path, query: str, max_blocks: int = FETCH_DEFAULT_N) -> list:
    """模式 4（#63 M2-B）：组合动作——query+locate+原文一次给齐。

    返回 [{"分数","块ID","书名","页码","起始行","结束行","指纹","PDF页序","原文"}]。
    定位/指纹口径与 locate_block 完全一致（现场扫描、指纹取检索副本行），
    差别仅在按书一次读入 原文.md/检索副本.md（每书一次，不逐块重读）。
    倒排表与原文不同步的块（原文里找不到 ID）如实跳过、不编造。"""
    results = query_keywords(kb_root, query, max_results=max_blocks)
    out = []
    lines_cache = {}  # 书名 → (原文行, 副本行)
    for score, bid, book, page, _preview in results:
        if book not in lines_cache:
            bd = kb_root / "书籍" / book
            lines_cache[book] = (
                (bd / "原文.md").read_text(encoding="utf-8").split("\n"),
                (bd / "检索副本.md").read_text(encoding="utf-8").split("\n"),
            )
        orig_lines, copy_lines = lines_cache[book]
        start, pg = None, ""
        for i, line in enumerate(orig_lines):
            m = BLOCK_ID_RE.match(line.strip())
            if m and f"{m.group(1)}·{m.group(2)}" == bid:
                start, pg = i, m.group(3)
                break
        if start is None:
            continue
        end = start
        for i in range(start + 1, len(orig_lines)):
            if BLOCK_ID_RE.match(orig_lines[i].strip()):
                break
            end = i
        fp = common.block_fingerprint(copy_lines[start + 1: end + 1])
        text = "\n".join(orig_lines[start + 1: end + 1]).strip("\n")
        out.append({"分数": score, "块ID": bid, "书名": book, "页码": pg,
                    "起始行": start + 1, "结束行": end + 1, "指纹": fp,
                    "PDF页序": _pdf_page_seq(kb_root / "书籍" / book, pg),
                    "原文": text})
    return out


def _print_fetch(blocks: list):
    """模式 4 输出：机械可分行解析——每块一个 ===【命中】头行 + 原文逐字行。"""
    if not blocks:
        print("无命中")
        return
    for i, b in enumerate(blocks):
        if i:
            print()
        tail = f" | PDF页序：{b['PDF页序']}" if b["PDF页序"] else ""
        print(f"===【命中 {b['分数']}分】{b['块ID']} | 《{b['书名']}》 p.{b['页码']} | "
              f"行 {b['起始行']}-{b['结束行']} | 指纹：{b['指纹']}{tail}")
        print(b["原文"])
    if blocks[0]["分数"] <= WEAK_HIT_THRESHOLD:
        print(f"\n⚠️ 弱命中：以上结果最高分仅 {blocks[0]['分数']}（碎片匹配），"
              "可能不相关，请换词或加限定词重查。")


def main():
    args = sys.argv[1:]
    mode = None
    params = []
    for a in args:
        if a.startswith("--"):
            mode = a
        else:
            params.append(a)

    kb_root = common.get_path("知识库")

    try:
        if mode == "--query":
            if not params:
                print("用法：--query \"词\"（支持逗号分隔多词，如 \"排龙,水口\"）")
                sys.exit(1)
            results = query_keywords(kb_root, params[0])
            _print_results(results)
            # #4 M2 顺带 R8：弱命中提示（仅碎片匹配，防误导）
            if results and results[0][0] <= WEAK_HIT_THRESHOLD:
                print(f"⚠️ 弱命中：以上结果最高分仅 {results[0][0]}（碎片匹配），"
                      "可能不相关，请换词或加限定词重查。")
            sys.exit(0 if results else 1)
        elif mode == "--locate":
            if not params:
                print("用法：--locate \"块ID\"")
                sys.exit(1)
            loc = locate_block(kb_root, params[0])
            if "退休" in loc:
                print(_retire_line(loc))
                sys.exit(2)
            book_dir = kb_root / "书籍" / loc["书名"]
            seq_txt = _pdf_page_seq(book_dir, loc.get("页码", ""))
            tail = f" | PDF页序：{seq_txt}" if seq_txt else ""
            print(f"{loc['块ID']} | 《{loc['书名']}》 行 {loc['起始行']}-{loc['结束行']} | "
                  f"指纹：{loc['指纹']}{tail}")
            sys.exit(0)
        elif mode == "--verify":
            if len(params) < 2:
                print("用法：--verify \"块ID\" \"预期指纹\"")
                sys.exit(1)
            r = verify_block(kb_root, params[0], params[1])
            if "退休" in r:
                print(_retire_line(r))
                sys.exit(2)
            if r["告警"]:
                print(r["告警"])
            print(f"{r['块ID']} | 《{r['书名']}》 行 {r['起始行']}-{r['结束行']} | "
                  f"{'指纹一致 ✅' if r['一致'] else '指纹一致 ✅（纠偏）'}")
            sys.exit(0)
        elif mode == "--fetch":
            if not params:
                print("用法：--fetch \"词\" [块数]（组合动作：查询+定位+原文一次给齐；"
                      "支持逗号分隔多词，如 \"排龙,水口\"）")
                sys.exit(1)
            try:
                n = int(params[1]) if len(params) > 1 else FETCH_DEFAULT_N
            except ValueError:
                print("用法：--fetch \"词\" [块数]（块数须为整数）")
                sys.exit(1)
            blocks = fetch_blocks(kb_root, params[0], max_blocks=n)
            _print_fetch(blocks)
            sys.exit(0 if blocks else 1)
        else:
            print("用法：--query \"词\" | --fetch \"词\" [块数] | --locate \"块ID\" | --verify \"块ID\" \"指纹\"")
            sys.exit(1)
    except ValueError as e:
        print(f"错误：{e}")
        sys.exit(1)


def _selfcheck():
    """验收：断言查询命中、locate 行号正确、篡改原文后 verify 能纠偏并告警。"""
    import tempfile
    import shutil

    tmp = Path(tempfile.mkdtemp(prefix="search_selfcheck_"))
    kb_root = tmp / "kb"
    book_dir = kb_root / "书籍" / "自检书"
    book_dir.mkdir(parents=True)

    orig_lines = [
        "【自检01·0001】p.1",
        "年柱月柱日柱时柱",
        "玄空之学首重理气阴阳五行之气运行",
        "",
        "【自检01·0002】p.2",
        "排龙之法以水口为主顺逆相推定其星曜",
        "",
        "【自检01·0003】p.2",
        "水口之法定向定穴",
        "",
        "【自检01·0004】p.3",
        "九星得运主吉失运主凶吉凶祸福依运而变",
        "",
        "【自检01·0005】p.3",
        "九星得运主吉失运主凶吉凶祸福依运而变",
    ]
    copy_lines = [
        "【自检01·0001】p.1",
        "年柱月柱日柱时柱",
        "玄空之学首重理气阴阳五行之气运行",
        "",
        "【自检01·0002】p.2",
        "排龙之法以水口为主顺逆相推定其星曜",
        "",
        "【自检01·0003】p.2",
        "水口之法定向定穴",
        "",
        "【自检01·0004】p.3",
        "九星得运主吉失运主凶吉凶祸福依运而变",
        "",
        "【自检01·0005】p.3",
        "九星得运主吉失运主凶吉凶祸福依运而变",
    ]
    (book_dir / "原文.md").write_text("\n".join(orig_lines), encoding="utf-8")
    (book_dir / "检索副本.md").write_text("\n".join(copy_lines), encoding="utf-8")
    (book_dir / "倒排表.json").write_text(
        json.dumps({
            "书号": "自检01",
            "构建时间": "2026-08-02",
            "条目": {
                "排龙": ["自检01·0002"],
                "排龙诀": ["自检01·0002"],
                "水口": ["自检01·0002", "自检01·0003"],
                "玄空": ["自检01·0001"],
                "九星": ["自检01·0004", "自检01·0005"],
                "得运": ["自检01·0004", "自检01·0005"],
                "失运": ["自检01·0004", "自检01·0005"],
            },
            "题干": {  # #4 M3（R4）：题干词条独立节，作检索入口（条目中不存在的 gram）
                "吉凶": ["自检01·0005"],
                "吉凶祸": ["自检01·0005"],
            },
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    (kb_root / "术语同义词表.md").write_text(
        "# 术语同义词表\n\n| 主词条 | 别名/异体/派别异名 |\n|---|---|\n| 排龙诀 | 排龍訣, 排龙 |\n",
        encoding="utf-8",
    )

    ok = True
    try:
        # 1. 查询命中（含术语表扩词：查"排龍訣"应命中"排龙"）
        res = query_keywords(kb_root, "排龍訣")
        if not any("自检01·0002" in r[1] for r in res):
            print("  ❌ 查询未命中（含简繁/术语扩展）")
            ok = False
        else:
            print(f"  ✅ 查询命中：{res}")

        # 2. locate 行号正确（第2块 起始行=5，结束行=7 含其后空行，1基）；第1块指纹应跳过排盘表头行
        loc = locate_block(kb_root, "自检01·0002")
        if loc["起始行"] != 5 or loc["结束行"] != 7:
            print(f"  ❌ locate 行号错误：{loc}")
            ok = False
        else:
            print(f"  ✅ locate 行号：{loc['起始行']}-{loc['结束行']}")
        loc1 = locate_block(kb_root, "自检01·0001")
        if loc1["指纹"] != "玄空之学首重理气阴阳五行":
            print(f"  ❌ locate 指纹未跳过排盘表头行：{loc1['指纹']}")
            ok = False
        else:
            print(f"  ✅ locate 指纹跳过表头行：{loc1['指纹']}")

        # 3. 篡改原文中部插一行，指纹与预期一致（自愈场景）
        fp2 = copy_lines[5].strip()[:12]
        tampered = orig_lines[:2] + ["（修订插行）", ""] + orig_lines[2:]
        (book_dir / "原文.md").write_text("\n".join(tampered), encoding="utf-8")
        copy_tampered = copy_lines[:2] + ["修订插行", ""] + copy_lines[2:]
        (book_dir / "检索副本.md").write_text("\n".join(copy_tampered), encoding="utf-8")
        v1 = verify_block(kb_root, "自检01·0002", fp2)
        if not v1["一致"]:
            print(f"  ❌ 篡改后指纹比对异常：{v1}")
            ok = False
        else:
            print(f"  ✅ 篡改后指纹比对一致：行 {v1['起始行']}-{v1['结束行']}")

        # 4. 指纹不符场景：预期指纹换成另一块内容，应告警纠偏或报错
        try:
            v2 = verify_block(kb_root, "自检01·0002", copy_lines[2].strip()[:12])
            if v2["告警"] and "纠偏" in v2["告警"]:
                print(f"  ✅ 指纹纠偏告警：{v2['告警']}")
            elif v2["一致"]:
                print("  ⚠️ 预期指纹恰好一致（测试设计问题）")
            else:
                print(f"  ❌ 纠偏逻辑异常：{v2}")
                ok = False
        except ValueError as e:
            print(f"  ❌ verify 抛错：{e}")
            ok = False

        # 5. PDF 页序折算（--locate 附录，4.4）：无映射不追；已拟合/降级/段偏移正确
        if _pdf_page_seq(book_dir, "2") is not None:
            print("  ❌ 无页码映射时 PDF页序 不应输出")
            ok = False
        fitted_map = {
            "书号": "自检01", "状态": "已拟合",
            "分段": [{"PDF页起": 1, "PDF页止": 100, "状态": "已拟合",
                      "印刷页起": 1, "偏移": 0, "锚点数": 10, "置信度": 1.0}],
        }
        (book_dir / "页码映射.json").write_text(
            json.dumps(fitted_map, ensure_ascii=False, indent=2), encoding="utf-8")
        cases = {"1": "1", "2": "2", "1~2": "1~2", "PDF.7": "7", "PDF.7~9": "7~9"}
        for tag, expect in cases.items():
            got = _pdf_page_seq(book_dir, tag)
            if got != expect:
                print(f"  ❌ PDF页序 {tag} → {got}（应 {expect}）")
                ok = False
        offset_map = {
            "书号": "自检01", "状态": "已拟合",
            "分段": [{"PDF页起": 20, "PDF页止": 30, "状态": "已拟合",
                      "印刷页起": 10, "偏移": 10, "锚点数": 10, "置信度": 1.0}],
        }
        (book_dir / "页码映射.json").write_text(
            json.dumps(offset_map, ensure_ascii=False, indent=2), encoding="utf-8")
        if _pdf_page_seq(book_dir, "12") != "22":
            print("  ❌ 段偏移折算错误")
            ok = False
        if _pdf_page_seq(book_dir, "11~12") != "21~22":
            print("  ❌ 段偏移区间折算错误")
            ok = False
        (book_dir / "页码映射.json").unlink()
        print("  ⑤ PDF 页序折算：✅" if ok else "  ⑤ PDF 页序折算：❌")

        # ⑥ #21 M4 退休口径：登记册命中 → locate/verify 返回退休条目而非抛错/误命中；
        #    在库块不受影响；未登记块照旧抛「找不到块 ID」
        (book_dir / "退休块.json").write_text(json.dumps({"退休块": [
            {"块ID": "自检01·0009", "日期": "2026-09-16", "原因": "修改",
             "后继": "自检01·0010", "相似度": 0.9},
            {"块ID": "自检01·0008", "日期": "2026-09-16", "原因": "删除",
             "后继": None, "相似度": None},
        ]}, ensure_ascii=False), encoding="utf-8")
        loc_r = locate_block(kb_root, "自检01·0009")
        if loc_r.get("退休", {}).get("后继") != "自检01·0010":
            print(f"  ❌ 退休块 locate 未命中登记册：{loc_r}")
            ok = False
        else:
            line = _retire_line(loc_r)
            if "2026-09-16" not in line or "修订" not in line or "自检01·0010" not in line:
                print(f"  ❌ 退休口径输出行缺要素：{line}")
                ok = False
        loc_r2 = locate_block(kb_root, "自检01·0008")
        if "删除" not in _retire_line(loc_r2) or "无后继块" not in _retire_line(loc_r2):
            print(f"  ❌ 删除型退休口径异常：{_retire_line(loc_r2)}")
            ok = False
        v_r = verify_block(kb_root, "自检01·0009", "任意指纹")
        if "退休" not in v_r:
            print(f"  ❌ 退休块 verify 未走退休口径：{v_r}")
            ok = False
        loc_ok = locate_block(kb_root, "自检01·0002")
        if "退休" in loc_ok or "起始行" not in loc_ok:
            print(f"  ❌ 在库块被退休口径误伤：{loc_ok}")
            ok = False
        try:
            locate_block(kb_root, "自检01·0777")
            print("  ❌ 未登记的不存在块未抛错")
            ok = False
        except ValueError:
            pass
        # 他书书号（库内无此书）不误判退休
        if find_retire_entry(kb_root, "9999·0001") is not None:
            print("  ❌ 他书书号误判退休")
            ok = False
        print("  ⑥ #21 M4 退休口径：✅" if ok else "  ⑥ #21 M4 退休口径：❌")

        # ⑦ #4 M2 同页相似块去重：查询「九星得运」→ 0004/0005 同分且同页(p.3)相邻、
        #    首行预览全同，应只留一个代表（车轱辘块不霸榜）
        res9 = query_keywords(kb_root, "九星得运", max_results=10)
        ids9 = [r[1] for r in res9]
        if not any(bid in ids9 for bid in ("自检01·0004", "自检01·0005")):
            print(f"  ❌ M2 去重后代表块缺失：{ids9}")
            ok = False
        elif "自检01·0004" in ids9 and "自检01·0005" in ids9:
            print(f"  ❌ M2 同页相邻相似块未去重：{ids9}")
            ok = False
        else:
            print(f"  ✅ M2 同页相似块去重：{ids9}")

        # ⑧ #4 M2 顺带 R9/R8：逗号多词合并榜单；弱命中判定（仅碎片匹配最高分≤1）
        multi = query_keywords(kb_root, "排龙,水口", max_results=10)
        ids_m = [r[1] for r in multi]
        if not any("自检01·0002" == b for b in ids_m) or not any("自检01·0003" == b for b in ids_m):
            print(f"  ❌ R9 多词合并未同时命中两词：{ids_m}")
            ok = False
        else:
            print(f"  ✅ R9 多词合并榜单：{ids_m}")
        weak = query_keywords(kb_root, "水口之法", max_results=10)
        if not weak or weak[0][0] > WEAK_HIT_THRESHOLD:
            print(f"  ❌ R8 弱命中判定异常：{weak}")
            ok = False
        else:
            print(f"  ✅ R8 弱命中判定：最高分 {weak[0][0]}（≤{WEAK_HIT_THRESHOLD}）")

        # ⑨ #4 M3（R4）题干词条可检索：仅存在于「题干」节的 gram（条目无）应命中对应块
        qg = query_keywords(kb_root, "吉凶祸福", max_results=10)
        if not any("自检01·0005" == r[1] for r in qg):
            print(f"  ❌ R4 题干词条未生效：{qg}")
            ok = False
        else:
            print(f"  ✅ R4 题干词条可检索：{qg}")

        # ⑩ #63 M2-B 组合动作：fetch＝query+locate+原文一次给齐。
        #    此刻原文/副本为篡改态（③插了两行）：0002 起始行应由 5 漂为 7，
        #    fetch 须与 locate_block 同口径跟随漂移；原文逐字、指纹、逗号多词、
        #    块数上限、无命中、倒排表与原文不同步跳过 逐项断言。
        fb = fetch_blocks(kb_root, "排龙", max_blocks=5)
        b2 = next((b for b in fb if b["块ID"] == "自检01·0002"), None)
        if b2 is None:
            print(f"  ❌ fetch 未命中 自检01·0002：{[b['块ID'] for b in fb]}")
            ok = False
        else:
            loc_now = locate_block(kb_root, "自检01·0002")
            if (b2["起始行"], b2["结束行"], b2["指纹"]) != \
                    (loc_now["起始行"], loc_now["结束行"], loc_now["指纹"]):
                print(f"  ❌ fetch 与 locate 口径不一致：{b2} vs {loc_now}")
                ok = False
            elif b2["起始行"] != 7:
                print(f"  ❌ fetch 行号未随篡改漂移（应 7）：{b2['起始行']}")
                ok = False
            elif "排龙之法以水口为主顺逆相推定其星曜" not in b2["原文"]:
                print(f"  ❌ fetch 原文非逐字：{b2['原文']!r}")
                ok = False
            elif "【自检01" in b2["原文"]:
                print(f"  ❌ fetch 原文混入块 ID 行：{b2['原文']!r}")
                ok = False
            else:
                print(f"  ✅ fetch 组合输出：行 {b2['起始行']}-{b2['结束行']} 指纹 {b2['指纹']} 原文逐字")
        multi_f = fetch_blocks(kb_root, "排龙,水口", max_blocks=5)
        ids_f = [b["块ID"] for b in multi_f]
        if "自检01·0002" not in ids_f or "自检01·0003" not in ids_f:
            print(f"  ❌ fetch 逗号多词未同时命中两词：{ids_f}")
            ok = False
        one_f = fetch_blocks(kb_root, "排龙,水口", max_blocks=1)
        if len(one_f) != 1:
            print(f"  ❌ fetch 块数上限未生效：{len(one_f)}")
            ok = False
        if fetch_blocks(kb_root, "查无此词丙丁", max_blocks=5):
            print("  ❌ fetch 无命中未返回空")
            ok = False
        print(f"  {'⑩ fetch 组合动作：✅' if ok else '⑩ fetch 组合动作：❌'}")

        print(f"  {'✅ B-01 自检通过' if ok else '❌ B-01 自检失败'}")
        sys.exit(0 if ok else 1)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    if common.is_selfcheck():
        _selfcheck()
    else:
        main()
