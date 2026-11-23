# -*- coding: utf-8 -*-
"""卡 B-01 search_keyword.py：零 token 本地检索脚本（供 TRAE 中的 AI 调用）。

模式 1 --query "词"    ：术语表扩展 → 简繁归一 → 倒排表 n-gram 查询 → 相关度排序输出
模式 2 --locate "块ID" ：现场扫描原文.md 定位块 ID 当前行号区间（不读建库时行号）
模式 3 --verify "块ID" "预期指纹"：定位后比对指纹，不符则在 ±50 行内搜索纠偏并告警
模式 4 --fetch "词"    ：组合动作（#63 M2-B）——query+locate+原文一次给齐：
                        每个命中块直接输出 块ID+行号区间+指纹+原文逐字全文，
                        把「查询→定位→读原文」三次往返塌缩成一次调用。

输出：纯文本打印到 stdout。
退出码：0=成功；1=查询无命中/verify 搜不到指纹/倒排表或退休登记损坏（#65 M1：
显式报错退出，不静默漏书）；2=命中退休块（#21 M4 失效诚实：该块已随书籍更新退休，
输出退休日期/原因/后继块，绝不静默改指其他块）。
"""

import functools
import gc
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
# #66 块标签「导航」节默认开关与降权系数（经真实集实测留退门禁定案）
NAV_ENABLED_DEFAULT = True
NAV_DEFAULT_WEIGHT = 0.75
# #63 M2-B：--fetch 默认取前 5 块（完整档）；#66 速查档默认前 3 块全文 + 其余头行预览
FETCH_DEFAULT_N = 5
FETCH_QUICK_N = 3
FETCH_QUICK_POOL_N = 20
FETCH_QUICK_PREVIEW_SHOW = 5


def _ensure_utf8_stdio():
    """#66：启动即自设 stdout/stderr 为 UTF-8，消除未设 PYTHONIOENCODING 时的输出崩溃。"""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            enc = (getattr(stream, "encoding", "") or "").lower()
            if enc != "utf-8":
                try:
                    stream.reconfigure(encoding="utf-8", errors="replace")
                except Exception:
                    pass
# #65 M4（设计 §4.8.2）：单字查询提示——索引只有 2/3 字 n-gram，单字必然零命中，
# 与其打印「无命中」让人以为库里没有，不如直接说清原因并给改词方向。
SINGLE_CHAR_HINT = ("当前 2/3 字索引不支持单字查询，请改用更具体词语"
                    "（例：查「龙」→ 改查「排龙」「水龙」）。")



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


def _single_char_only(query: str) -> bool:
    """#65 M4（设计 §4.8.2）：查询词归一后是否**全部为单字**。

    索引只存 2/3 字 n-gram，单字永远匹配不到；此时应提示改词，而不是假装「无命中」。
    逗号多词里只要有一个 ≥2 字的部分就照常检索（可能是有效查询）。
    """
    parts = [normalize(p) for p in query.split(",")]
    parts = [p for p in parts if p]
    return bool(parts) and all(len(p) < 2 for p in parts)


def _iter_books(kb_root: Path):
    """遍历 书籍/书名 目录，返回 (书名, 目录Path)。"""
    books_dir = kb_root / "书籍"
    if not books_dir.exists():
        return
    for d in sorted(books_dir.iterdir()):
        if d.is_dir() and (d / "原文.md").exists():
            yield d.name, d


def _index_error(book_title: str, index_path: Path, reason: str) -> ValueError:
    """#65 M1（设计 §4.3）：倒排表缺失/损坏的显式错误——带书名、文件与修复建议，
    由 main 统一退出 1；不静默跳过造成「看似无命中」，也不打印伪完整榜单。"""
    return ValueError(
        f"《{book_title}》的倒排表不可用（文件：{index_path}；原因：{reason}）。"
        f"为避免静默漏书，本次检索已中止。修复：请在控制台「书库」页选中《{book_title}》"
        f"点「重建索引（含自动验证）」重建后再查；不要手工删除或清空该文件（旧「题干」节需保留）。"
    )


def _retire_error(book_title: str, reg_path: Path, reason: str) -> ValueError:
    """#65 M1（设计 §4.3）：退休登记损坏的显式错误——不伪装成普通「找不到块 ID」。"""
    return ValueError(
        f"《{book_title}》的退休登记已损坏（文件：{reg_path}；原因：{reason}）。"
        f"为避免把「登记损坏」伪装成「查无此块」，本次定位已中止。修复：修正该 JSON 语法"
        f"或从备份/git 恢复后重试；不要删除或清空该文件（重建索引不会重建退休登记）。"
    )


def _load_index(book_title: str, index_path: Path) -> dict:
    """#65 M1：读倒排表并做顶层结构校验；缺失/读失败/JSON 坏/非对象一律抛带详情的 ValueError。"""
    if not index_path.exists():
        raise _index_error(book_title, index_path, "文件缺失")
    try:
        raw = index_path.read_text(encoding="utf-8")
    except OSError as e:
        raise _index_error(book_title, index_path, f"读取失败：{e}")
    except ValueError as e:  # UnicodeDecodeError：文件不是合法 UTF-8（编码军规）
        raise _index_error(book_title, index_path, f"文件编码损坏：{e}")
    # #65 M3（设计 §4.5）：解析期间暂停 GC——倒排表是几十万个无环 dict/list，
    # 引用计数即可回收，代际 GC 在解析中反复扫描纯属开销（实测解析 0.52s→0.44s）。
    # 解析完立即恢复原状态；不改变任何解析结果，异常路径也保证恢复。
    gc_was_enabled = gc.isenabled()
    if gc_was_enabled:
        gc.disable()
    try:
        data = json.loads(raw)
    except ValueError as e:
        raise _index_error(book_title, index_path, f"JSON 解析失败：{e}")
    finally:
        if gc_was_enabled:
            gc.enable()
    if not isinstance(data, dict):
        raise _index_error(book_title, index_path,
                           f"结构异常：顶层应为对象，实为 {type(data).__name__}")
    return data


def _blocks_physically_adjacent(ctx, bid_a: str, bid_b: str) -> bool:
    """#65 M4（设计 §4.8.1）：两块在原文里是否**物理相邻**。

    判据：前一块的「结束行 + 1」正好是后一块 ID 行（原文单遍元数据的行号，0 基）。
    旧实现用「块 ID 出生序号差 1」推断相邻——出生序只保证建库那一刻连续，A-09 更新
    插入的块会领新号、物理位置与号大小不再一致，导致该合的不合、不该合的误合。
    """
    blocks = ctx.blocks()
    a, b = blocks.get(bid_a), blocks.get(bid_b)
    if a is None or b is None:
        return False
    if a["起始行"] > b["起始行"]:
        a, b = b, a
    return a["结束行"] + 1 == b["起始行"]


def _dedup_same_page(results: list, is_adjacent=None) -> list:
    """#4 M2（R7）：同书同页相邻相似块合并代表（防"车轱辘块"霸榜）。

    判据：同书名 ＋ 印刷页码相同 ＋ **原文物理相邻（#65 M4 改，原为「块 ID 出生序差 1」）**
    ＋ 首行预览相似度 ≥ 阈值。
    组内保留分数最高者为代表（输入已按分数降序，先到先得即高分保留）；
    折叠后其余块顺次补上（输入含 max_results×DEDUP_POOL_MULT 候选，留足补位余量）。
    """
    import difflib

    if is_adjacent is None:  # 兜底：无上下文时不折叠（宁可少折叠，不做 ID 序猜测）
        is_adjacent = lambda _book, _a, _b: False
    selected = []
    for res in results:
        score, bid, book, page, preview = res
        dup = False
        for s in selected:
            if s[2] != book or s[3] != page:
                continue
            if not is_adjacent(book, bid, s[1]):
                continue
            if preview and s[4] and difflib.SequenceMatcher(None, preview, s[4]).ratio() >= DEDUP_PREVIEW_RATIO:
                dup = True
                break
        if not dup:
            selected.append(res)
    return selected


def _split_filter_values(raw) -> list:
    """拆解逗号分隔的书名或分类过滤条件（去空白与《》）。"""
    if raw is None:
        return []
    items = [raw] if isinstance(raw, str) else list(raw)
    out = []
    for item in items:
        for part in str(item).replace("，", ",").split(","):
            name = part.strip().strip("《》").strip()
            if name and name not in out:
                out.append(name)
    return out


def _resolve_allowed_books(kb_root: Path, books=None, category=None):
    """解析 --书 / --类 为允许检索的书名集合；未指定返回 None（全库）。"""
    if books is None and category is None:
        return None
    disk_books = [title for title, _d in _iter_books(kb_root)]
    disk_set = set(disk_books)

    allowed = None
    if books is not None:
        wanted = _split_filter_values(books)
        if not wanted:
            raise ValueError("--书 不能为空，请传入书名（可用英文或中文逗号分隔多本）。")
        missing = [b for b in wanted if b not in disk_set]
        if missing:
            avail = "、".join(f"《{b}》" for b in disk_books) or "（空）"
            raise ValueError(f"书库中找不到书籍《{missing[0]}》；可用书目：{avail}")
        allowed = set(wanted)

    if category is not None:
        wanted_cats = set(_split_filter_values(category))
        if not wanted_cats:
            raise ValueError("--类 不能为空，请传入分类名。")
        entries = _catalog_entries(kb_root)
        if not entries:
            raise ValueError("未找到《书目总索引.md》，无法按分类筛选；请先建索引或改用 --书。")
        cat_books = {
            e["书名"] for e in entries
            if e["分类"] in wanted_cats and e["书名"] in disk_set
        }
        if not cat_books:
            avail_cats = sorted({e["分类"] for e in entries if e["分类"] and e["书名"] in disk_set})
            cats_txt = "、".join(avail_cats) or "（暂无已填写分类的书）"
            raise ValueError(
                f"分类「{'、'.join(sorted(wanted_cats))}」下没有在库书籍；可用分类：{cats_txt}"
            )
        allowed = cat_books if allowed is None else (allowed & cat_books)
        if not allowed:
            raise ValueError("指定的 --书 与 --类 交集为空，没有可检索的书籍。")

    return allowed


def _load_query_books(kb_root: Path, allowed_books=None, nav_store: dict = None) -> list:
    """读入并校验生产查询所需索引；评测批次可复用返回对象，避免逐题解析全库。"""
    books = []
    for book_title, book_dir in _iter_books(kb_root):
        if allowed_books is not None and book_title not in allowed_books:
            continue
        index_path = book_dir / "倒排表.json"
        index = _load_index(book_title, index_path)
        entries = index.get("条目")
        if not isinstance(entries, dict):
            raise _index_error(book_title, index_path, "结构异常：缺少「条目」对象")
        qgrams = index.get("题干", {}) or {}
        if not isinstance(qgrams, dict):
            raise _index_error(book_title, index_path, "结构异常：「题干」节应为对象")
        nav = index.get("导航", {}) or {}
        if not isinstance(nav, dict):
            raise _index_error(book_title, index_path, "结构异常：「导航」节应为对象")
        if nav_store is not None:
            nav_store[book_title] = nav
        books.append((book_title, book_dir, entries, qgrams))
    return books


_CATALOG_CACHE = {}  # 进程内 书目总索引 缓存（只读 CLI 用；写任务另起进程）


def _new_query_cache(kb_root: Path, books=None, category=None) -> dict:
    """单次进程/评测批次的查询缓存；默认生产调用仍只在本函数一次调用内有效。

    #65 M3：books＝每书倒排表解析一次；ctx＝每书原文/副本与块元数据上下文
    （BookContext），供 query/fetch/locate/verify 共用，替代旧的 orig_lines 行表缓存。
    """
    allowed = _resolve_allowed_books(kb_root, books=books, category=category)
    nav_store = {}
    return {"syn": load_synonyms(kb_root),
            "books": _load_query_books(kb_root, allowed_books=allowed, nav_store=nav_store),
            "nav": nav_store,
            "ctx": {}}


def _ctx_of(store: dict, kb_root: Path, book_title: str) -> "BookContext":
    """取（或新建）某书上下文；同一进程内一本书只装一次。"""
    ctx = store.get(book_title)
    if ctx is None:
        ctx = store[book_title] = BookContext(book_title, kb_root / "书籍" / book_title)
    return ctx


def _scan_blocks(lines: list) -> dict:
    """原文单遍生成块元数据：{块ID: {"页码","起始行","结束行","预览"}}（行号 0 基）。

    #65 M3（设计 §4.5）：旧的 _lookup_page_preview 每个候选块都从文件头重扫全文，
    候选池 100 块即上百次全表扫描；改为每书一次单遍建字典，查询只做字典查找。
    预览口径保持逐字节一致：块后首个非空行前 30 字——块内无内容时会取到下一块 ID 行
    （旧实现同样如此），此处不修正以免输出变化。
    """
    blocks = {}
    current = None
    for i, line in enumerate(lines):
        s = line.strip()
        m = BLOCK_ID_RE.match(s)
        if m:
            if current is not None:
                current["结束行"] = i - 1
                if s and not current["预览"]:
                    current["预览"] = s[:30]
            bid = f"{m.group(1)}·{m.group(2)}"
            known = blocks.get(bid)
            if known is None:
                known = {"块ID": bid, "页码": m.group(3), "起始行": i,
                         "结束行": len(lines) - 1, "预览": ""}
                blocks[bid] = known
                current = known
            else:  # 重复块 ID（A-04 会报「块 ID 冲突」拒绝建库）：只认首现，不覆盖
                current = {"结束行": None, "预览": ""}
            continue
        if current is not None and s and not current["预览"]:
            current["预览"] = s[:30]
    return blocks


class BookContext:
    """#65 M3（设计 §4.5）：单进程内每书一次装入的只读上下文，不落盘、不跨进程。

    原文/副本行、块元数据、页码分段均按需装入一次：query 的页码与预览、fetch 的原文
    与指纹、locate/verify 的行号区间共用同一份数据，不再逐块重读整本文件。
    """

    __slots__ = ("title", "dir", "_orig", "_copy", "_blocks", "_page_segments", "_chapters")

    def __init__(self, title: str, book_dir: Path):
        self.title = title
        self.dir = book_dir
        self._orig = None
        self._copy = None
        self._blocks = None
        self._page_segments = None
        self._chapters = None

    def orig_lines(self) -> list:
        if self._orig is None:
            self._orig = (self.dir / "原文.md").read_text(encoding="utf-8").split("\n")
        return self._orig

    def copy_lines(self) -> list:
        if self._copy is None:
            self._copy = (self.dir / "检索副本.md").read_text(
                encoding="utf-8").split("\n")
        return self._copy

    def blocks(self) -> dict:
        if self._blocks is None:
            self._blocks = _scan_blocks(self.orig_lines())
        return self._blocks

    def block_span(self, block_id: str):
        """块 ID → (起始行, 结束行) 0 基；原文里没有该块返回 None（不编造）。"""
        blk = self.blocks().get(block_id)
        if blk is None:
            return None
        return blk["起始行"], blk["结束行"]

    def page_seq(self, tag: str):
        """块页码标签 → PDF 页序文本；页码映射每书至多解析一次。"""
        if self._page_segments is None:
            self._page_segments = _load_page_segments(self.dir)
        return _page_seq_from_segments(self._page_segments, tag)

    def chapter_name(self, block_id: str) -> str:
        """#66：按 目录骨架.md 物理序映射 块ID → 章名；无骨架或未归章返回「未分章」。"""
        if self._chapters is None:
            self._chapters = _load_chapter_map(self.dir, self.blocks())
        return self._chapters.get(block_id, "未分章")


def _load_chapter_map(book_dir: Path, blocks: dict) -> dict:
    """解析 目录骨架.md（兼容「起始块ID+块数」与旧「块ID区间」），返回 {块ID: 章名}。"""
    sk_path = book_dir / "目录骨架.md"
    if not sk_path.exists() or not blocks:
        return {}
    try:
        lines = sk_path.read_text(encoding="utf-8").split("\n")
    except OSError:
        return {}
    ordered_ids = list(blocks.keys())
    id_to_pos = {bid: idx for idx, bid in enumerate(ordered_ids)}
    chapters = []
    for line in lines:
        s = line.strip()
        if not s.startswith("|") or s.startswith("| 章号"):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if len(cells) < 3 or all(set(c) <= set("-: ") for c in cells):
            continue
        ch_name = cells[1]
        m_new = re.match(r"(.+?)·(\d{4})$", cells[2])
        if len(cells) >= 4 and m_new and cells[3].isdigit():
            chapters.append((ch_name, f"{m_new.group(1)}·{m_new.group(2)}", int(cells[3])))
            continue
        m_old = re.match(r"(.+?)·(\d{4})~(\d{4})$", cells[2])
        if m_old:
            cnt = int(m_old.group(3)) - int(m_old.group(2)) + 1
            chapters.append((ch_name, f"{m_old.group(1)}·{m_old.group(2)}", max(0, cnt)))
    mapping = {}
    for ch_name, start_bid, count in chapters:
        pos = id_to_pos.get(start_bid)
        if pos is None:
            continue
        for idx in range(pos, min(pos + count, len(ordered_ids))):
            mapping[ordered_ids[idx]] = ch_name
    return mapping




def query_keywords(kb_root: Path, query: str, max_results: int = 20,
                   include_question_index: bool = True, _cache: dict = None,
                   books=None, category=None,
                   include_nav: bool = None, nav_weight: float = None) -> list:
    """模式 1：关键词查询。返回 [(相关度, 块ID, 书名, 页码, 首行预览)]。

    #4 M2（R7）：同页相邻相似块合并代表（_dedup_same_page），车轱辘块不再霸榜；
    排序确定性化（分数降序、块ID升序，防 set 迭代随机化抖排名）。
    #4 M2 顺带 R9：`query` 支持逗号分隔多词（如 "排龙,水口"），各词分别按术语表
    扩展后合并 n-gram 统一打分（= 批量查询合并榜单）。
    #65 M2：include_question_index=False 只查「条目」，供纯内容评测使用；默认 True，
    生产行为不变。_cache 仅供评测复用本函数已加载数据，不改变评分、排序或去重逻辑。
    """
    if _cache is None:
        _cache = _new_query_cache(kb_root, books=books, category=category)
        allowed_books = None
    else:
        allowed_books = _resolve_allowed_books(kb_root, books=books, category=category)
    syn = _cache["syn"]
    ctx_store = _cache["ctx"]  # #65 M3/M4：每书原文/块元数据上下文，评分与预览共用
    grams_needed = set()
    for part in (p.strip() for p in query.split(",")):
        if not part:
            continue
        for v in expand_query(part, syn):
            chars = v
            for n in (2, 3):
                for i in range(len(chars) - n + 1):
                    grams_needed.add(chars[i : i + n])

    use_nav = NAV_ENABLED_DEFAULT if include_nav is None else bool(include_nav)
    eff_nav_w = NAV_DEFAULT_WEIGHT if nav_weight is None else float(nav_weight)
    nav_map = _cache.get("nav", {})

    score_map = {}  # 块ID → {"score", "书名", "页码", "首行"}
    for book_title, book_dir, entries, qgrams in _cache["books"]:
        if allowed_books is not None and book_title not in allowed_books:
            continue
        index_path = book_dir / "倒排表.json"
        sources = [(entries, "条目", 1.0)]
        if include_question_index:
            sources.append((qgrams, "题干", 1.0))
        if use_nav and eff_nav_w > 0:
            nav = nav_map.get(book_title)
            if nav:
                sources.append((nav, "导航", eff_nav_w))
        for g in grams_needed:
            w = W_GRAM.get(len(g), 1)
            for src, src_name, mult in sources:
                postings = src.get(g, [])
                if not isinstance(postings, list):
                    raise _index_error(book_title, index_path,
                                       f"结构异常：「{src_name}」中 gram「{g}」的块 ID 列表损坏")
                delta = w * mult
                for bid in postings:
                    if not isinstance(bid, str):
                        raise _index_error(book_title, index_path,
                                           f"结构异常：「{src_name}」中 gram「{g}」的块 ID 列表损坏")
                    info = score_map.setdefault(bid, {"score": 0, "书名": book_title})
                    info["score"] += delta
                    if isinstance(info["score"], float) and info["score"].is_integer():
                        info["score"] = int(info["score"])

    if not score_map:
        return []
    for info in score_map.values():
        sc = info["score"]
        if isinstance(sc, float):
            sc = round(sc, 2)
            info["score"] = int(sc) if sc.is_integer() else sc

    ranked = sorted(score_map.items(), key=lambda kv: (-kv[1]["score"], kv[0]))
    # 第二键=块ID：同分块顺序确定（set 迭代随进程 hash 随机化会抖排名，#4 M2 修复）
    results = []
    for bid, info in ranked[: max_results * DEDUP_POOL_MULT]:
        book = info["书名"]
        blk = _ctx_of(ctx_store, kb_root, book).blocks().get(bid)
        page, preview = (blk["页码"], blk["预览"]) if blk else ("", "")
        results.append((info["score"], bid, book, page, preview))
    def _adjacent(book: str, bid_a: str, bid_b: str) -> bool:
        return _blocks_physically_adjacent(
            _ctx_of(ctx_store, kb_root, book), bid_a, bid_b)

    results = _dedup_same_page(results, _adjacent)
    return results[:max_results]


def _catalog_entries(kb_root: Path) -> list:
    """解析 书目总索引.md → [{"书号", "书名", "分类"}]（进程内缓存）。"""
    key = str(kb_root)
    if key in _CATALOG_CACHE:
        return _CATALOG_CACHE[key]
    path = kb_root / "书目总索引.md"
    try:
        lines = path.read_text(encoding="utf-8").split("\n") if path.exists() else []
    except OSError:
        lines = []
    rows = []
    columns = None
    data_started = False
    for ln in lines:
        s = ln.strip()
        if not s.startswith("|"):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if columns is None:
            if "书号" in cells and "书名" in cells:
                cat_col = cells.index("分类") if "分类" in cells else None
                columns = (cells.index("书号"), cells.index("书名"), cat_col)
            continue
        if not data_started:
            if all(set(c) <= set("-: ") for c in cells):
                data_started = True
            continue
        id_col, name_col, cat_col = columns
        if max(id_col, name_col) < len(cells) and cells[id_col]:
            cat = cells[cat_col].strip() if (cat_col is not None and cat_col < len(cells)) else ""
            rows.append({"书号": cells[id_col], "书名": cells[name_col], "分类": cat})
    _CATALOG_CACHE[key] = rows
    return rows


def _catalog_book_map(kb_root: Path) -> dict:
    """书目总索引 → {书号: 书名}；文件缺失/格式异常返回 None（调用方回退全库扫描）。

    #65 M3（设计 §4.5.4）：书号→书名映射唯一源＝书目总索引（与 common.gen_book_id 同源），
    让 locate/verify/退休登记查询只读目标书，不再逐本解析全部倒排表。
    """
    entries = _catalog_entries(kb_root)
    mapping = {e["书号"]: e["书名"] for e in entries if e["书号"]}
    return mapping or None


def _route_book(kb_root: Path, book_id: str):
    """书号 → (书名, 目录)；未登记/目录缺失返回 None（回退旧的全库扫描）。"""
    mapping = _catalog_book_map(kb_root)
    if not mapping:
        return None
    title = mapping.get(book_id)
    if not title:
        return None
    book_dir = kb_root / "书籍" / title
    if not (book_dir / "原文.md").exists():
        return None
    return title, book_dir


def _read_retire_registry(book_title: str, book_dir: Path, block_id: str):
    """读某书 退休块.json 取指定块条目；无登记册/无该块返回 None，结构损坏显式报错。"""
    reg_path = book_dir / "退休块.json"
    if not reg_path.exists():
        return None
    try:
        raw = reg_path.read_text(encoding="utf-8")
    except OSError as ex:
        raise _retire_error(book_title, reg_path, f"读取失败：{ex}")
    except ValueError as ex:  # UnicodeDecodeError：文件不是合法 UTF-8（编码军规）
        raise _retire_error(book_title, reg_path, f"文件编码损坏：{ex}")
    try:
        data = json.loads(raw)
    except ValueError as ex:
        raise _retire_error(book_title, reg_path, f"JSON 解析失败：{ex}")
    if not isinstance(data, dict):
        raise _retire_error(book_title, reg_path,
                            f"结构异常：顶层应为对象，实为 {type(data).__name__}")
    entries = data.get("退休块")
    if not isinstance(entries, list):
        raise _retire_error(book_title, reg_path, "结构异常：缺少「退休块」数组")
    for e in entries:
        if not isinstance(e, dict):
            raise _retire_error(book_title, reg_path, "结构异常：「退休块」条目应为对象")
        if e.get("块ID") == block_id:
            return {"书名": book_title, "条目": e}
    return None


def find_retire_entry(kb_root: Path, block_id: str) -> dict:
    """#21 M4 失效诚实：查退休登记册。返回 {"书名", "条目"} 或 None。

    条目＝{块ID, 日期, 原因(修改/删除), 后继, 相似度}（知识库\\书籍\\{书名}\\退休块.json）。
    #65 M3：书号→书名先经书目总索引路由，只读目标书 退休块.json；总索引缺失时回退
    逐本读倒排表头部反查；总索引存在但未登记该书号 → 直接认定「不在库」（目录为映射
    唯一源），不再为查一个块解析全部倒排表。

    #65 M1（设计 §4.3）：反查用的倒排表或 退休块.json 损坏时抛带书名+文件+修复建议的
    ValueError（main 退出 1），不把「登记损坏」伪装成「找不到块 ID」、不从空表续读。"""
    if "·" not in block_id:
        return None
    book_id = block_id.split("·")[0]
    routed = _route_book(kb_root, book_id)
    if routed:
        title, book_dir = routed
        inv = book_dir / "倒排表.json"
        if not inv.exists():  # 有原文却缺倒排：按 M1 口径显式报错，不伪装成「无退休登记」
            raise _index_error(title, inv, "文件缺失")
        return _read_retire_registry(title, book_dir, block_id)
    mapping = _catalog_book_map(kb_root)
    if mapping is not None:
        return None  # 总索引可用但无此书号：目录为映射唯一源，无需再解析全部倒排表
    for book_title, book_dir in _iter_books(kb_root):
        inv = book_dir / "倒排表.json"
        index = _load_index(book_title, inv)
        index_book_id = index.get("书号")
        if not isinstance(index_book_id, str):
            raise _index_error(book_title, inv, "结构异常：缺少「书号」")
        if index_book_id != book_id:
            continue
        return _read_retire_registry(book_title, book_dir, block_id)
    return None


def locate_block(kb_root: Path, block_id: str, _ctx: dict = None) -> dict:
    """模式 2：现场扫描原文.md 定位块 ID 当前行号区间（1 基）。

    #65 M3（设计 §4.5）：书号先经书目总索引路由到目标书，只读该书原文（块元数据单遍
    生成、检索副本仅命中时才读），不再逐本读整本原文+副本；路由不可用/未登记时回退
    旧的逐本扫描，不静默退化成「找不到块 ID」。
    查无此块时先查退休登记册（#21 M4）：命中退休块返回 {"退休": 条目, ...}
    而不抛错——调用方按退休口径应答（退出码 2），绝不静默改指其他块。"""
    if "·" not in block_id:
        raise ValueError(f"块 ID 格式错误：{block_id}（应为 书号·NNNN）")
    store = _ctx if _ctx is not None else {}
    routed = _route_book(kb_root, block_id.split("·")[0])
    candidates = []
    if routed:
        candidates.append(routed)
    for book_title, book_dir in _iter_books(kb_root):
        if routed and book_title == routed[0]:
            continue
        candidates.append((book_title, book_dir))
    for book_title, book_dir in candidates:
        if not (book_dir / "检索副本.md").exists():
            continue
        ctx = _ctx_of(store, kb_root, book_title)
        span = ctx.block_span(block_id)
        if span is None:
            continue
        start, end = span
        return {
            "块ID": block_id,
            "书名": book_title,
            "起始行": start + 1,
            "结束行": end + 1,
            "页码": ctx.blocks()[block_id]["页码"],
            "指纹": common.block_fingerprint(ctx.copy_lines()[start + 1 : end + 1]),
        }
    retired = find_retire_entry(kb_root, block_id)
    if retired:
        return {"块ID": block_id, "书名": retired["书名"], "退休": retired["条目"]}
    raise ValueError(f"找不到块 ID：{block_id}")


def _load_page_segments(book_dir: Path):
    """读 页码映射.json 的「分段」列表；缺失/损坏/空分段一律返回 None（降级口径不变）。"""
    map_path = book_dir / "页码映射.json"
    if not map_path.exists():
        return None
    try:
        segments = json.loads(map_path.read_text(encoding="utf-8")).get("分段", [])
    except Exception:
        return None
    return segments or None


def _page_seq_from_segments(segments, tag: str):
    """块标签 → PDF 页序文本（设计 4.4）。无分段返回 None。

    已拟合标签（印刷页码）：反查已拟合段，按 偏移 折算回 PDF 页；
    降级标签（PDF.x）：本身就是 PDF 页序，直接透传数字。"""
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


def _pdf_page_seq(book_dir: Path, tag: str) -> str:
    """块标签 → PDF 页序文本（无缓存入口；BookContext.page_seq 走每书一次的分段缓存）。"""
    return _page_seq_from_segments(_load_page_segments(book_dir), tag)


SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


def _parse_sha256_expect(text: str):
    """`sha256:<hex>` 或裸 64 位 hex → 小写 hex；不是 SHA-256 期望值返回 None。

    #65 M4：让 `--verify` 能与 12 字短指纹并存——短指纹照旧，`sha256:` 前缀走强校验。
    """
    t = (text or "").strip()
    if t.lower().startswith("sha256:"):
        t = t.split(":", 1)[1].strip()
    return t.lower() if SHA256_RE.match(t) else None


def verify_block_sha256(kb_root: Path, block_id: str, expected: str,
                        _ctx: dict = None) -> dict:
    """#65 M4（设计 §4.8.4）：整块 SHA-256 强校验（配合 A-04 生成 完整性清单.json）。

    先按现场原文重算整块哈希，再与清单登记值比对：**两处任一不符都如实报告**
    （清单能发现「原文被改但清单没更新」，清单缺失时仍按现场哈希判定）。
    12 字短指纹与任务包指纹列口径完全不变，本函数不参与日常引用校验。
    """
    import hashlib

    hex_expect = _parse_sha256_expect(expected)
    if not hex_expect:
        raise ValueError(f"不是合法的 SHA-256 期望值：{expected}（应为 sha256:<64 位 hex>）")
    store = _ctx if _ctx is not None else {}
    loc = locate_block(kb_root, block_id, _ctx=store)
    if "退休" in loc:
        return {"块ID": block_id, "书名": loc["书名"], "退休": loc["退休"]}
    ctx = _ctx_of(store, kb_root, loc["书名"])
    span = ctx.block_span(block_id)
    actual = hashlib.sha256(
        "\n".join(ctx.orig_lines()[span[0] + 1: span[1] + 1]).encode("utf-8")).hexdigest()
    manifest_path = kb_root / "书籍" / loc["书名"] / "完整性清单.json"
    listed = None
    if manifest_path.exists():
        try:
            listed = json.loads(manifest_path.read_text(encoding="utf-8")) \
                .get("块SHA256", {}).get(block_id)
        except ValueError:
            listed = "损坏"
    return {
        "块ID": block_id, "书名": loc["书名"],
        "起始行": loc["起始行"], "结束行": loc["结束行"],
        "预期SHA256": hex_expect, "实际SHA256": actual,
        "一致": actual == hex_expect,
        "清单": ("缺失" if listed is None else
                 "损坏" if listed == "损坏" else
                 "一致" if listed == actual else "不一致"),
    }


def verify_block(kb_root: Path, block_id: str, expected_fp: str,
                 _ctx: dict = None) -> dict:
    """模式 3：定位后比对指纹；不符则 ±50 行内搜索纠偏并告警。

    #65 M3：与 locate 共用同一份书上下文，纠偏时不再重读整本检索副本。
    命中退休块（#21 M4）直接返回 {"退休": 条目}，不做指纹比对。"""
    store = _ctx if _ctx is not None else {}
    loc = locate_block(kb_root, block_id, _ctx=store)
    if "退休" in loc:
        return {"块ID": block_id, "书名": loc["书名"], "退休": loc["退休"]}
    expected = normalize(expected_fp)
    actual = loc["指纹"]
    result = {"块ID": block_id, "书名": loc["书名"], "起始行": loc["起始行"], "结束行": loc["结束行"],
              "预期指纹": expected, "实际指纹": actual, "一致": expected == actual, "告警": None}

    if result["一致"]:
        return result

    # ±50 行内搜索纠偏
    copy_lines = _ctx_of(store, kb_root, loc["书名"]).copy_lines()
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


def _build_fetch_item(ctx: "BookContext", score, bid: str, book: str, page: str):
    """组装单个命中块的完整输出字典；原文里找不到该块时返回 None。"""
    span = ctx.block_span(bid)
    if span is None:
        return None
    start, end = span
    fp = common.block_fingerprint(ctx.copy_lines()[start + 1: end + 1])
    text = "\n".join(ctx.orig_lines()[start + 1: end + 1]).strip("\n")
    return {"分数": score, "块ID": bid, "书名": book, "页码": page,
            "章名": ctx.chapter_name(bid),
            "起始行": start + 1, "结束行": end + 1, "指纹": fp,
            "PDF页序": ctx.page_seq(page),
            "原文": text}


def fetch_blocks(kb_root: Path, query: str, max_blocks: int = FETCH_DEFAULT_N,
                 _cache: dict = None, books=None, category=None) -> list:
    """模式 4（#63 M2-B；#66 附章名与限书/限类）：组合动作——query+locate+原文一次给齐。

    返回 [{"分数","块ID","书名","页码","章名","起始行","结束行","指纹","PDF页序","原文"}]。
    """
    if _cache is None:
        _cache = _new_query_cache(kb_root, books=books, category=category)
    results = query_keywords(kb_root, query, max_results=max_blocks, _cache=_cache,
                             books=books, category=category)
    ctx_store = _cache["ctx"]
    out = []
    for score, bid, book, page, _preview in results:
        ctx = _ctx_of(ctx_store, kb_root, book)
        item = _build_fetch_item(ctx, score, bid, book, page)
        if item is not None:
            out.append(item)
    return out


def fetch_quick(kb_root: Path, query: str, full_blocks: int = FETCH_QUICK_N,
                max_candidates: int = FETCH_QUICK_POOL_N, _cache: dict = None,
                books=None, category=None) -> dict:
    """#66 速查档：前 full_blocks 块给逐字原文，其余命中块给头行 + 首行预览。"""
    if _cache is None:
        _cache = _new_query_cache(kb_root, books=books, category=category)
    pool_n = max(full_blocks, max_candidates)
    results = query_keywords(kb_root, query, max_results=pool_n, _cache=_cache,
                             books=books, category=category)
    ctx_store = _cache["ctx"]
    full = []
    previews = []
    for score, bid, book, page, preview in results:
        ctx = _ctx_of(ctx_store, kb_root, book)
        if len(full) < full_blocks:
            item = _build_fetch_item(ctx, score, bid, book, page)
            if item is not None:
                full.append(item)
            continue
        if ctx.block_span(bid) is None:
            continue
        previews.append({
            "分数": score, "块ID": bid, "书名": book, "页码": page,
            "章名": ctx.chapter_name(bid), "PDF页序": ctx.page_seq(page),
            "首行预览": preview,
        })
    top_score = full[0]["分数"] if full else (previews[0]["分数"] if previews else 0)
    return {
        "模式": "速查",
        "全文": full,
        "预览": previews,
        "另有命中": len(previews),
        "总命中": len(full) + len(previews),
        "最高分": top_score,
        "弱命中": bool(full or previews) and top_score <= WEAK_HIT_THRESHOLD,
    }


def _resolve_fetch_mode(explicit_mode: str = None) -> str:
    """档位优先级：命令行显式参数 > 路径配置.json「查询模式」> 默认「完整」。"""
    if explicit_mode in ("速查", "完整"):
        return explicit_mode
    try:
        mode = common.load_config().get("查询模式", "完整")
    except Exception:
        mode = "完整"
    return "速查" if mode == "速查" else "完整"


def _print_fetch(blocks: list):
    """模式 4 完整档输出：机械可分行解析——每块一个 ===【命中】头行 + 原文逐字行。"""
    if not blocks:
        print("无命中")
        return
    for i, b in enumerate(blocks):
        if i:
            print()
        tail = f" | PDF页序：{b['PDF页序']}" if b["PDF页序"] else ""
        print(f"===【命中 {b['分数']}分】{b['块ID']} | 《{b['书名']}》 p.{b['页码']} | "
              f"章：{b['章名']} | 行 {b['起始行']}-{b['结束行']} | 指纹：{b['指纹']}{tail}")
        print(b["原文"])
    if blocks[0]["分数"] <= WEAK_HIT_THRESHOLD:
        print(f"\n⚠️ 弱命中：以上结果最高分仅 {blocks[0]['分数']}（碎片匹配），"
              "可能不相关，请换词或加限定词重查。")


def _print_fetch_quick(data: dict):
    """#66 速查档输出：显式标明「当前模式：速查（另有 N 处命中）」，全文与预览严格分隔。"""
    full = data["全文"]
    previews = data["预览"]
    if not full and not previews:
        print("无命中")
        return
    rem = data["另有命中"]
    if rem > 0:
        print(f"当前模式：速查（另有 {rem} 处命中；以下前 {len(full)} 块为逐字原文——"
              "请仅引 1~2 块核心原文＋2~4句精简结论，勿写长篇四段式，文末询问是否展开完整版）")
    else:
        print(f"当前模式：速查（共 {len(full)} 处命中，已全展示——"
              "请仅引 1~2 块核心原文＋2~4句精简结论，勿写长篇四段式，文末询问是否展开完整版）")
    for b in full:
        print()
        tail = f" | PDF页序：{b['PDF页序']}" if b["PDF页序"] else ""
        print(f"===【命中 {b['分数']}分】{b['块ID']} | 《{b['书名']}》 p.{b['页码']} | "
              f"章：{b['章名']} | 行 {b['起始行']}-{b['结束行']} | 指纹：{b['指纹']}{tail}")
        print(b["原文"])
    if previews:
        shown = previews[:FETCH_QUICK_PREVIEW_SHOW]
        extra = len(previews) - len(shown)
        tail_note = f"（展示前 {len(shown)} 条，另有 {extra} 条略）" if extra > 0 else ""
        print(f"\n---【其余命中（仅头行+首行预览，不作全文引用）{tail_note}】---")
        for p in shown:
            tail = f" | PDF页序：{p['PDF页序']}" if p["PDF页序"] else ""
            print(f"---【预览 {p['分数']}分】{p['块ID']} | 《{p['书名']}》 p.{p['页码']} | "
                  f"章：{p['章名']}{tail} | 首行：{p['首行预览']}…")
    if data["弱命中"]:
        print(f"\n⚠️ 弱命中：以上结果最高分仅 {data['最高分']}（碎片匹配），"
              "可能不相关，请换词或加限定词重查。")


def _parse_cli_args(args: list) -> dict:
    """解析命令行参数，兼容旧写法（--fetch "词" [块数]）与 #66 命名参数。"""
    mode = None
    params = []
    books = None
    category = None
    fetch_mode = None
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("--query", "--locate", "--verify", "--fetch"):
            mode = a
        elif a == "--书":
            if i + 1 >= len(args):
                raise ValueError("参数 --书 缺少书名")
            i += 1
            books = args[i]
        elif a.startswith("--书="):
            books = a.split("=", 1)[1]
        elif a == "--类":
            if i + 1 >= len(args):
                raise ValueError("参数 --类 缺少分类名")
            i += 1
            category = args[i]
        elif a.startswith("--类="):
            category = a.split("=", 1)[1]
        elif a == "--速查":
            if fetch_mode == "完整":
                raise ValueError("--速查 与 --完整 不能同时使用")
            fetch_mode = "速查"
        elif a == "--完整":
            if fetch_mode == "速查":
                raise ValueError("--速查 与 --完整 不能同时使用")
            fetch_mode = "完整"
        elif a.startswith("--"):
            raise ValueError(f"未知参数：{a}")
        else:
            params.append(a)
        i += 1
    return {
        "mode": mode, "params": params,
        "books": books, "category": category, "fetch_mode": fetch_mode,
    }


def main():
    _ensure_utf8_stdio()
    kb_root = common.get_path("知识库")

    try:
        parsed = _parse_cli_args(sys.argv[1:])
        mode = parsed["mode"]
        params = parsed["params"]
        books = parsed["books"]
        category = parsed["category"]
        explicit_fetch_mode = parsed["fetch_mode"]

        if mode == "--query":
            if explicit_fetch_mode is not None:
                raise ValueError("--速查/--完整 仅用于 --fetch 模式")
            if not params:
                print("用法：--query \"词\" [--书 \"书名\"] [--类 \"分类\"]（支持逗号分隔多词，如 \"排龙,水口\"）")
                sys.exit(1)
            if _single_char_only(params[0]):
                print(SINGLE_CHAR_HINT)
                sys.exit(1)
            results = query_keywords(kb_root, params[0], books=books, category=category)
            _print_results(results)
            # #4 M2 顺带 R8：弱命中提示（仅碎片匹配，防误导）
            if results and results[0][0] <= WEAK_HIT_THRESHOLD:
                print(f"⚠️ 弱命中：以上结果最高分仅 {results[0][0]}（碎片匹配），"
                      "可能不相关，请换词或加限定词重查。")
            sys.exit(0 if results else 1)
        elif mode == "--locate":
            if books is not None or category is not None or explicit_fetch_mode is not None:
                raise ValueError("--书/--类/--速查/--完整 不适用于 --locate 模式")
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
            if books is not None or category is not None or explicit_fetch_mode is not None:
                raise ValueError("--书/--类/--速查/--完整 不适用于 --verify 模式")
            if len(params) < 2:
                print("用法：--verify \"块ID\" \"预期指纹\"")
                sys.exit(1)
            if _parse_sha256_expect(params[1]):
                rh = verify_block_sha256(kb_root, params[0], params[1])
                if "退休" in rh:
                    print(_retire_line(rh))
                    sys.exit(2)
                if not rh["一致"]:
                    raise ValueError(
                        f"块 {rh['块ID']} 的 SHA-256 强校验不符：预期 {rh['预期SHA256']}，"
                        f"实际 {rh['实际SHA256']}（完整性清单：{rh['清单']}）。"
                        f"内容可能被改动或索引与原文不同步，需人工检查；请勿在未核实前引用该块。")
                tail = "" if rh["清单"] == "一致" else f" | 完整性清单：{rh['清单']}"
                print(f"{rh['块ID']} | 《{rh['书名']}》 行 {rh['起始行']}-{rh['结束行']} | "
                      f"SHA-256 一致 ✅{tail}")
                sys.exit(0)
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
                print("用法：--fetch \"词\" [块数] [--书 \"书名\"] [--类 \"分类\"] [--速查|--完整]")
                sys.exit(1)
            has_count = len(params) > 1
            if has_count:
                try:
                    explicit_n = int(params[1])
                except ValueError:
                    print("用法：--fetch \"词\" [块数]（块数须为整数）")
                    sys.exit(1)
                if explicit_n < 1:
                    raise ValueError("块数须为正整数")
            else:
                explicit_n = None
            if _single_char_only(params[0]):
                print(SINGLE_CHAR_HINT)
                sys.exit(1)
            eff_mode = _resolve_fetch_mode(explicit_fetch_mode)
            if eff_mode == "速查":
                n = explicit_n if explicit_n is not None else FETCH_QUICK_N
                qdata = fetch_quick(kb_root, params[0], full_blocks=n,
                                    books=books, category=category)
                _print_fetch_quick(qdata)
                sys.exit(0 if (qdata["全文"] or qdata["预览"]) else 1)
            n = explicit_n if explicit_n is not None else FETCH_DEFAULT_N
            blocks = fetch_blocks(kb_root, params[0], max_blocks=n,
                                  books=books, category=category)
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
        # ⑪-b #65 M4：物理相邻判据（原文行号）替代出生 ID 差 1
        ctx_self = _ctx_of({}, kb_root, "自检书")
        if not _blocks_physically_adjacent(ctx_self, "自检01·0004", "自检01·0005"):
            print("  ❌ #65 M4 物理相邻判据未识别相邻块")
            ok = False
        elif _blocks_physically_adjacent(ctx_self, "自检01·0001", "自检01·0004"):
            print("  ❌ #65 M4 物理相邻判据把不相邻块判成相邻")
            ok = False
        elif not _single_char_only("龙") or _single_char_only("排龙") \
                or _single_char_only("龙,排龙") or _single_char_only(" "):
            print("  ❌ #65 M4 单字查询判定异常")
            ok = False
        else:
            # 非单调块号样例：物理相邻但出生序号差 4（A-09 更新插入后会这样），
            # 旧「ID 差 1」判据会漏去重，物理序判据必须认出来。
            syn = _scan_blocks(["【自检01·0003】p.9", "同页重复文字甲", "",
                                "【自检01·0007】p.9", "同页重复文字甲", ""])

            class _StubCtx:
                def blocks(self):
                    return syn

            deduped = _dedup_same_page(
                [(2, "自检01·0003", "自检书", "9", "同页重复文字甲"),
                 (2, "自检01·0007", "自检书", "9", "同页重复文字甲")],
                lambda _b, x, y: _blocks_physically_adjacent(_StubCtx(), x, y))
            if not _blocks_physically_adjacent(_StubCtx(), "自检01·0003", "自检01·0007"):
                print("  ❌ #65 M4 非单调块号的物理相邻判据失效")
                ok = False
            elif len(deduped) != 1:
                print(f"  ❌ #65 M4 物理相邻块未去重：{deduped}")
                ok = False
            else:
                print("  ✅ #65 M4 物理相邻判据（含非单调块号）与单字查询判定")
            # ⑫ #65 M4：整块 SHA-256 强校验（现场重算 + 完整性清单比对 + 错值必须拦）
            import hashlib as _hashlib
            lines_ck = (book_dir / "原文.md").read_text(encoding="utf-8").split("\n")
            blk_ck = _scan_blocks(lines_ck)["自检01·0001"]
            hx = _hashlib.sha256(
                "\n".join(lines_ck[blk_ck["起始行"] + 1: blk_ck["结束行"] + 1]
                          ).encode("utf-8")).hexdigest()
            (book_dir / "完整性清单.json").write_text(json.dumps(
                {"书号": "自检01", "块数": 5,
                 "块SHA256": {"自检01·0001": hx}}, ensure_ascii=False), encoding="utf-8")
            sha_ok = verify_block_sha256(kb_root, "自检01·0001", "sha256:" + hx)
            sha_bad = verify_block_sha256(kb_root, "自检01·0001", "sha256:" + "0" * 64)
            try:
                _parse_sha256_expect("不是哈希")
                raise_bad_ok = not _parse_sha256_expect("不是哈希")
            except Exception:
                raise_bad_ok = False
            if sha_ok["一致"] and sha_ok["清单"] == "一致" \
                    and not sha_bad["一致"] and raise_bad_ok:
                print("  ✅ #65 M4 整块 SHA-256 强校验（含清单比对、错值判不符）")
            else:
                print(f"  ❌ #65 M4 SHA-256 强校验异常：{sha_ok} / 错值={sha_bad}")
                ok = False

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
        qg_default = query_keywords(kb_root, "吉凶祸福", max_results=10,
                                    include_question_index=True)
        qg_content = query_keywords(kb_root, "吉凶祸福", max_results=10,
                                    include_question_index=False)
        if qg_default != qg or any("自检01·0005" == r[1] for r in qg_content):
            print(f"  ❌ #65 M2 题干开关异常：默认={qg_default} 纯内容={qg_content}")
            ok = False
        else:
            print("  ✅ #65 M2 默认含题干不变，纯内容查询排除题干节")

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

        # ⑪ #65 M3：单遍块元数据与现场定位同口径；书目总索引路由与全库扫描同结果；
        #    同一缓存复用不改变生产查询结果（BookContext 只复用已读数据，不改评分/排序）。
        lines_now = (book_dir / "原文.md").read_text(encoding="utf-8").split("\n")
        blocks_m3 = _scan_blocks(lines_now)
        mismatch = []
        for bid in ("自检01·0001", "自检01·0002"):
            loc_chk = locate_block(kb_root, bid)
            want = (loc_chk["页码"], loc_chk["起始行"], loc_chk["结束行"])
            got = (blocks_m3[bid]["页码"], blocks_m3[bid]["起始行"] + 1,
                   blocks_m3[bid]["结束行"] + 1)
            if got != want:
                mismatch.append((bid, got, want))
        (kb_root / "书目总索引.md").write_text(
            "# 书目总索引\n\n| 书名 | 书号 | 分类 | 一句话主题 | 关键词 | 规模 |\n"
            "|---|---|---|---|---|---|\n| 自检书 | 自检01 |  | 待标注 | 待标注 | 5块 |\n",
            encoding="utf-8")
        _CATALOG_CACHE.clear()
        routed = locate_block(kb_root, "自检01·0002")
        (kb_root / "书目总索引.md").unlink()
        _CATALOG_CACHE.clear()
        fallback = locate_block(kb_root, "自检01·0002")
        cache_q = _new_query_cache(kb_root)
        q1 = query_keywords(kb_root, "排龙", max_results=10, _cache=cache_q)
        q2 = query_keywords(kb_root, "排龙", max_results=10, _cache=cache_q)
        if mismatch:
            print(f"  ❌ #65 M3 块元数据与 locate 不一致：{mismatch}")
            ok = False
        elif routed != fallback:
            print(f"  ❌ #65 M3 书号路由与全库扫描结果不一致：{routed} vs {fallback}")
            ok = False
        elif q1 != q2:
            print("  ❌ #65 M3 缓存复用改变了查询结果")
            ok = False
        else:
            print("  ✅ #65 M3 块元数据一致 / 书号路由≡全库扫描 / 缓存复用结果不变")

        # ⑬ #66 阶段 1：锁书/锁类、fetch 带章名、速查/完整双档、CLI 兼容解析、UTF-8 输出
        book2_dir = kb_root / "书籍" / "他书"
        book2_dir.mkdir(parents=True)
        (book2_dir / "原文.md").write_text(
            "【自检02·0001】p.1\n排龙另一本书的干扰段落\n", encoding="utf-8")
        (book2_dir / "检索副本.md").write_text(
            "【自检02·0001】p.1\n排龙另一本书的干扰段落\n", encoding="utf-8")
        (book2_dir / "倒排表.json").write_text(json.dumps({
            "书号": "自检02", "构建时间": "2026-11-20",
            "条目": {"排龙": ["自检02·0001"], "他词": ["自检02·0001"]},
        }, ensure_ascii=False), encoding="utf-8")
        (kb_root / "书目总索引.md").write_text(
            "# 书目总索引\n\n| 书名 | 书号 | 分类 | 一句话主题 | 关键词 | 规模 |\n"
            "|---|---|---|---|---|---|\n"
            "| 自检书 | 自检01 | 风水 | - | - | 5块 |\n"
            "| 他书 | 自检02 | 命理 | - | - | 1块 |\n",
            encoding="utf-8")
        (book_dir / "目录骨架.md").write_text(
            "# 《自检书》目录骨架\n\n| 章号 | 章名 | 起始块ID | 块数 |\n"
            "|---|---|---|---|\n"
            "| 01 | 卷一·总纲 | 自检01·0001 | 1 |\n"
            "| 02 | 卷二·排龙诀 | 自检01·0002 | 4 |\n",
            encoding="utf-8")
        _CATALOG_CACHE.clear()
        only_1 = query_keywords(kb_root, "排龙", books="自检书")
        only_cat = query_keywords(kb_root, "排龙", category="命理")
        if not only_1 or any(r[2] != "自检书" for r in only_1):
            print(f"  ❌ #66 --书 过滤异常：{only_1}")
            ok = False
        elif not only_cat or any(r[2] != "他书" for r in only_cat):
            print(f"  ❌ #66 --类 过滤异常：{only_cat}")
            ok = False
        try:
            query_keywords(kb_root, "排龙", books="不存在的书")
            print("  ❌ #66 未知书名未报错")
            ok = False
        except ValueError:
            pass
        fb_chap = fetch_blocks(kb_root, "排龙", books="自检书")
        if not fb_chap or fb_chap[0].get("章名") != "卷二·排龙诀":
            print(f"  ❌ #66 fetch 未附带章名：{fb_chap}")
            ok = False
        fq = fetch_quick(kb_root, "排龙,水口,九星", full_blocks=1, books="自检书")
        if len(fq["全文"]) != 1 or fq["另有命中"] < 1:
            print(f"  ❌ #66 fetch_quick 分档异常：{fq}")
            ok = False
        p_old = _parse_cli_args(["--fetch", "排龙", "5"])
        p_new = _parse_cli_args(["--fetch", "排龙", "--书", "自检书", "--速查"])
        if p_old["mode"] != "--fetch" or p_old["params"] != ["排龙", "5"] \
                or p_new["books"] != "自检书" or p_new["fetch_mode"] != "速查":
            print(f"  ❌ #66 CLI 解析异常：{p_old} / {p_new}")
            ok = False
        else:
            print("  ✅ #66 阶段 1：锁书/锁类、章名、速查分档与旧参兼容")

        # ⑮ #66 阶段 3：「导航」节降权检索、纯内容开关与物理隔离
        inv_s1 = json.loads((book_dir / "倒排表.json").read_text(encoding="utf-8"))
        inv_s1["导航"] = {"著书": ["自检01·0001"], "书缘": ["自检01·0001"], "缘起": ["自检01·0001"]}
        (book_dir / "倒排表.json").write_text(
            json.dumps(inv_s1, ensure_ascii=False), encoding="utf-8")
        nav_hit = query_keywords(kb_root, "著书缘起", books="自检书",
                                 include_nav=True, nav_weight=0.5)
        nav_off = query_keywords(kb_root, "著书缘起", books="自检书", include_nav=False)
        nav_fetch = fetch_blocks(kb_root, "著书缘起", books="自检书")
        if not nav_hit or nav_hit[0][1] != "自检01·0001" or nav_hit[0][0] != 1.5:
            print(f"  ❌ #66 导航节降权打分异常：{nav_hit}")
            ok = False
        elif nav_off:
            print(f"  ❌ #66 include_nav=False 未关闭导航：{nav_off}")
            ok = False
        elif not nav_fetch or "著书缘起" in nav_fetch[0]["原文"]:
            print(f"  ❌ #66 导航标签泄露进原文或 fetch 未命中：{nav_fetch}")
            ok = False
        else:
            print(f"  ✅ #66 导航节降权与物理隔离：{nav_hit[0][:2]} 分={nav_hit[0][0]}")

        # ⑭ #66 阶段 2：同源协议防漂移（protocol_sync）
        sync_mod_path = _SCRIPT_DIR.parent / "protocol_sync.py"
        if sync_mod_path.exists():
            sys.path.insert(0, str(_SCRIPT_DIR.parent))
            import protocol_sync  # noqa: E402
            sync_ok, sync_errs = protocol_sync.check_protocol_sync(_PROJECT_ROOT)
            if not sync_ok:
                print(f"  ❌ #66 同源协议漂移：{sync_errs}")
                ok = False
            else:
                print("  ✅ #66 同源协议防漂移检查通过")

        print(f"  {'✅ B-01 自检通过' if ok else '❌ B-01 自检失败'}")
        sys.exit(0 if ok else 1)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    _ensure_utf8_stdio()
    if common.is_selfcheck():
        _selfcheck()
    else:
        main()
