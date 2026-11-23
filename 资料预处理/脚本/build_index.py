# -*- coding: utf-8 -*-
"""卡 A-04 build_index.py：索引 + 倒排表 + 目录骨架 + 书目总索引登记。

输入：知识库\\书籍\\{书名}\\检索副本.md
处理：① 扫描块 ID 行建"块ID→(起始行,结束行,页码,指纹)"映射（指纹=块内首个有效正文行前12字，
      排盘表头等低信息行跳过，规则见 common.block_fingerprint）；
      ② 每个块内容生成 2/3 字 n-gram 写入倒排表；
      ③ 按章节标题切分生成目录骨架（起始块ID + 块数，#21 M2：块 ID 为出生序不编码位置）；
      ④ 书目总索引.md 登记/更新本书一行。
输出：倒排表.json、目录骨架.md、更新 书目总索引.md；
      映射表存 输出\\提取缓存\\{书号}\\锚点映射.json（B-01 现场解析参考格式，运行时以现场扫描为准）
异常：块 ID 重复→报"块 ID 冲突"；指纹为空→报该块"无正文"
"""

import hashlib
import json
import re
import sys
from datetime import date
from pathlib import Path

import common

BLOCK_ID_RE = re.compile(r"^【(.+?)·(\d{4})】p\.(.*)$")
CHAPTER_PATTERNS = [
    re.compile(r"^(第[一二三四五六七八九十百千万零0-9]+[章卷篇回部集])([^。；;，,]*)"),
    re.compile(r"^(卷[一二三四五六七八九十]+)([^。；;，,]*)"),
    re.compile(r"^(上篇|中篇|下篇|上部|中部|下部|上卷|中卷|下卷)([^。；;，,]*)"),
]
_TOC_LINE = re.compile(r"\.{3,}|…{2,}|·{4,}")
_PAGE_TAIL = re.compile(r"^[-—–\s]*\d{1,4}\s*[-—–\s]*$")
_FUNC_WORD_HEAD = "的里中内外与和及或并但因为所以如果在于把被让向着对从由按照依"


def _parse_book_files(book_title: str, kb_root: Path = None) -> tuple:
    """读取 原文.md 与 检索副本.md，解析块结构。

    返回 (block_map, orig_lines, copy_lines)。
    block_map: {块ID: {"起始行","结束行","页码","指纹","正文"}}
    """
    if kb_root is None:
        kb_root = common.get_path("知识库")
    book_dir = kb_root / "书籍" / book_title
    orig_path = book_dir / "原文.md"
    copy_path = book_dir / "检索副本.md"
    if not orig_path.exists() or not copy_path.exists():
        raise FileNotFoundError(f"缺少 原文.md/检索副本.md：{book_dir}")

    orig_lines = orig_path.read_text(encoding="utf-8").split("\n")
    copy_lines = copy_path.read_text(encoding="utf-8").split("\n")
    if len(orig_lines) != len(copy_lines):
        raise RuntimeError("原文.md 与 检索副本.md 行数不一致，请重跑 A-03")

    block_map = {}
    current = None
    for idx, (ol, cl) in enumerate(zip(orig_lines, copy_lines)):
        m = BLOCK_ID_RE.match(ol.strip())
        if m:
            if current is not None:
                current["结束行"] = idx - 1
            bid = f"{m.group(1)}·{m.group(2)}"
            if bid in block_map:
                raise RuntimeError(f"块 ID 冲突：{bid}")
            current = {
                "块ID": bid,
                "起始行": idx,
                "结束行": None,
                "页码": m.group(3),
                "指纹": "",
                "正文": [],
            }
            block_map[bid] = current
        else:
            if current is not None:
                if cl.strip():
                    current["正文"].append(cl.strip())
    if current is not None:
        current["结束行"] = len(orig_lines) - 1

    for bid, blk in block_map.items():
        blk["指纹"] = common.block_fingerprint(blk["正文"])
        if not blk["指纹"]:
            raise RuntimeError(f"块 {bid} 无正文（指纹为空）")
    return block_map, orig_lines, copy_lines


def _file_sha256(path: Path) -> str:
    """文件级 SHA-256（分块读，避免整本原文一次进内存）。#65 M4 完整性清单用。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _block_sha256(orig_lines: list, blk: dict) -> str:
    """整块正文 SHA-256：块 ID 行之后到下一块 ID 行之前，逐字（含空行）拼接后取哈希。"""
    text = "\n".join(orig_lines[blk["起始行"] + 1: blk["结束行"] + 1])
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _ngrams(text: str) -> set:
    """生成 2/3 字 n-gram（只取中文+字母数字字符）。"""
    chars = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", text)
    grams = set()
    for n in (2, 3):
        for i in range(len(chars) - n + 1):
            grams.add(chars[i : i + n])
    return grams


def _load_and_prune_block_tags(book_dir: Path, live_ids: set,
                                 book_title: str) -> tuple:
    """#66：读取 块标签.json，剔除已退休/不存在块并回写；返回 (导航节 dict, 清理块列表)。"""
    tags_path = book_dir / "块标签.json"
    if not tags_path.exists():
        return {}, []
    try:
        data = json.loads(tags_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise RuntimeError(
            f"块标签文件损坏：书《{book_title}》文件 {tags_path}：{e}。"
            f"请修复或重新导回/回收块标签后重跑 A-04。") from e
    if not isinstance(data, dict) or not isinstance(data.get("块"), dict):
        raise RuntimeError(
            f"块标签结构异常：书《{book_title}》文件 {tags_path} 缺少「块」对象。")
    raw_blocks = data["块"]
    live_blocks = {}
    removed = []
    for bid, tags in raw_blocks.items():
        if bid not in live_ids:
            removed.append(bid)
            continue
        if isinstance(tags, list):
            live_blocks[bid] = [str(t).strip() for t in tags if str(t).strip()]
    if removed:
        data["块"] = dict(sorted(live_blocks.items()))
        common.atomic_write_text(
            tags_path, json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    nav = {}
    for bid, tags in live_blocks.items():
        block_grams = set()
        for t in tags:
            norm = common.normalize_for_search(t)
            if norm:
                block_grams.update(_ngrams(norm))
        for g in block_grams:
            nav.setdefault(g, []).append(bid)
    return dict(sorted(nav.items())), removed


def _prune_question_postings(question: dict, live_ids: set,
                             book_title: str, index_path: Path) -> tuple:
    """保留仍指向活在库块的题干 posting，返回 (清理后题干节, 失效引用明细)。

    只清索引入口，不改评测集；词条及活块顺序保持原样。结构损坏显式拒绝，
    不把坏数据当成空题干节继续写。
    """
    cleaned, removed = {}, []
    for gram, postings in question.items():
        if not isinstance(postings, list) or any(not isinstance(bid, str) for bid in postings):
            raise RuntimeError(
                f"旧倒排表「题干」节 posting 损坏：书《{book_title}》文件 {index_path}，"
                f"词条 {gram!r}。请修复或从备份恢复后重试。")
        kept = [bid for bid in postings if bid in live_ids]
        removed.extend({"词条": gram, "块ID": bid} for bid in postings if bid not in live_ids)
        if kept:
            cleaned[gram] = kept
    return cleaned, removed


def _load_chart_ids(book_dir: Path) -> set:
    """读 图表块清单.json（A-03 生成），返回图表块ID集合；无清单/异常返回空集。

    M3 消费方（4.6）：图表块不进倒排表，避免盘面/表格文字污染关键词检索。"""
    p = book_dir / "图表块清单.json"
    if not p.exists():
        return set()
    try:
        return set(json.loads(p.read_text(encoding="utf-8")).get("图表块", []))
    except Exception:
        return set()


def _looks_like_toc(block_lines: list) -> bool:
    """目录页特征：块内 ≥4 个内容行且 ≥25% 行含引导符或纯页码尾。"""
    content = [ln.strip() for ln in block_lines if ln.strip() and not BLOCK_ID_RE.match(ln.strip())]
    if len(content) < 4:
        return False
    hits = sum(1 for t in content if _TOC_LINE.search(t) or _PAGE_TAIL.match(t))
    return hits / len(content) >= 0.25


def _match_chapter_title(t: str) -> str:
    """判断一行是否为章节标题（行级特征）。是则返回清洗后的章名，否则返回空串。

    排除项：行长 >30、含目录引导符（.../……/····）、章号后接虚词开头、
    rest 去空格后超 12 字（正文句子）、"第X、Y、Z章"式序号列举（正文总结句）。
    章名带空格/顿号（如"第四章 四柱测官运、事业"）可正常识别。
    MinerU 标题块的 Markdown 前缀（## 第一章 …）先剥除再判（#19）。
    """
    t = re.sub(r"^#{1,6}\s+", "", t)
    if not t or len(t) > 30 or _TOC_LINE.search(t):
        return ""
    for pat in CHAPTER_PATTERNS:
        m = pat.match(t)
        if not m:
            continue
        rest = (m.group(2) if (m.lastindex or 0) >= 2 else "").strip()
        if not rest:
            return t.replace("|", "／").strip()
        if rest[0] in _FUNC_WORD_HEAD or len(rest) > 12:
            return ""
        if re.match(r"^第[一二三四五六七八九十0-9]+[、，]", rest):
            return ""
        return (m.group(1) + rest).replace("|", "／").strip()
    return ""


def _load_structure(book_dir: Path) -> list:
    """读 #20 成品 结构.json（章节识别候选，clean_split 换算）；缺失/异常返回空列表。"""
    p = book_dir / "结构.json"
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []
    return data if isinstance(data, list) else []


def _load_book_cfg(book_dir: Path) -> dict:
    """读书级配置 书配置.json（#20 可选字段「章级级别」等）；缺失/异常返回空 dict。"""
    p = book_dir / "书配置.json"
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _clean_signal_title(t: str) -> str:
    """信号标题展示净化：剥 Markdown 前缀与包裹波浪线（MinerU 强调样式）。"""
    t = re.sub(r"^#{1,6}\s*", "", (t or "").strip())
    t = re.sub(r"^\\?~\s*", "", t)
    t = re.sub(r"\s*\\?~$", "", t)
    return t.strip()


def _select_signal_starts(entries: list, level_cfg=None, exclude=None, include=None) -> list:
    """结构.json 条目 → [(块ID, 章名)] 候选（块ID 去重，不校验块ID 是否真存在）。

    - level_cfg 为 None：自判路径，章级过滤复用 _match_chapter_title（章级模式 ∩ 排除项）；
    - level_cfg 为 int：按书级配置取该「章级级别」条目，并应用「章级补充」（显式加入、可跨
      级别）与「章级排除」（显式剔除）清单修边角；仍挡超长/TOC 噪声。补充优先于排除。

    抽为独立函数供 build_index 消费与 book_cfg_gen 自判评估（#20）复用，保证过滤同源。
    """
    exclude = set(exclude or [])
    include = set(include or [])
    starts = []
    seen = set()
    for e in entries:
        if not isinstance(e, dict):
            continue
        bid = e.get("块ID")
        if not bid or bid in seen:
            continue
        raw = e.get("标题", "")
        if level_cfg is None:
            name = _match_chapter_title(raw)
            if not name:
                continue
        else:
            clean = _clean_signal_title(raw)
            if clean in include:
                pass
            elif clean in exclude:
                continue
            elif e.get("级别") != level_cfg:
                continue
            if not clean or len(clean) > 30 or _TOC_LINE.search(clean):
                continue
            name = clean
        seen.add(bid)
        starts.append((bid, name))
    return starts


def signal_selfjudge_empty(book_dir: Path) -> bool:
    """自判（章级模式）过滤后是否为空：True = 需 LLM 书级配置生成（#20 方案 F）。

    结构.json 缺失/为空 → False（无信号书不纳入 LLM 生成范围，走启发式兜底）。
    """
    entries = _load_structure(book_dir)
    if not entries:
        return False
    return not _select_signal_starts(entries)


def _chapters_from_starts(chapter_starts: list, block_map: dict) -> list:
    """由 [(起始块ID, 章名)] 生成 [(章号, 章名, 起始块ID, 结束块ID)]，区间互不重叠；
    正文首章之前的所有块（封面/目录/序言/简介）并入第 01 章不丢失。"""
    block_ids = list(block_map.keys())
    bid_pos = {bid: i for i, bid in enumerate(block_ids)}
    if not chapter_starts:
        return [("01", "全书", block_ids[0], block_ids[-1])]
    chapters = []
    for i, (bid, name) in enumerate(chapter_starts):
        if i + 1 < len(chapter_starts):
            nxt_pos = bid_pos[chapter_starts[i + 1][0]]
            end_bid = block_ids[max(bid_pos[bid], nxt_pos - 1)]
        else:
            end_bid = block_ids[-1]
        chapters.append((f"{i + 1:02d}", name, bid, end_bid))
    if bid_pos[chapter_starts[0][0]] > 0:
        first = chapters[0]
        chapters[0] = (first[0], first[1], block_ids[0], first[3])
    return chapters


def _detect_chapters(block_map: dict, orig_lines: list, book_dir: Path = None) -> tuple:
    """按章节标题识别章节。返回 ([(章号, 章名, 起始块ID, 结束块ID)], 识别通道)。

    #20 结构信号优先：结构.json 条目为候选，章级过滤复用 _match_chapter_title（章级模式 ∩
    排除项）；书配置.json「章级级别」存在时按级别取候选（并应用「章级排除/补充」清单修边角，
    仍挡超长/TOC 噪声）；过滤后无候选 → 通道标「启发式(退化)」并回落启发式全链（不产出比
    现状更差的结果）。

    无 结构.json / 无信号 → 现有启发式全链兜底：防误检三道闸——①目录块整块跳过；②行级特征
    过滤（见 _match_chapter_title）；③标题行前一行必须是空行/块边界/短行（≤20 字）。OCR
    连排标题可正常识别；MinerU 标题块的 `##` 前缀先剥除再判（#19 止血，兜底链保留）。
    """
    block_ids = list(block_map.keys())
    bid_pos = {bid: i for i, bid in enumerate(block_ids)}

    if book_dir is not None:
        entries = _load_structure(book_dir)
        if entries:
            cfg = _load_book_cfg(book_dir)
            selected = _select_signal_starts(
                entries, cfg.get("章级级别"), cfg.get("章级排除"), cfg.get("章级补充"))
            starts = [s for s in selected if s[0] in block_map]
            if starts:
                starts.sort(key=lambda s: bid_pos[s[0]])
                return _chapters_from_starts(starts, block_map), "结构信号"

    chapter_starts = []
    for bid in block_ids:
        start = block_map[bid]["起始行"]
        end = block_map[bid]["结束行"]
        block_lines = orig_lines[start : end + 1]
        if _looks_like_toc(block_lines):
            continue
        for off, ln in enumerate(block_lines):
            name = _match_chapter_title(ln.strip())
            if not name:
                continue
            gi = start + off
            prev_len = 0 if gi == start else len(orig_lines[gi - 1].strip())
            if prev_len <= 20:
                chapter_starts.append((bid, name))
                break

    channel = "启发式(退化)" if (book_dir is not None and _load_structure(book_dir)) else "启发式"
    return _chapters_from_starts(chapter_starts, block_map), channel


def _book_page_range(block_map: dict) -> str:
    """全书页码范围：经 parse_page_tag 取首块起始页、末块结束页（设计 4.9）。

    按原文行序取第一个/最后一个带页码的块；已拟合标签为印刷页数字，
    降级标签（PDF.x）带前缀原样透传，如实标明是 PDF 页序而非印刷页码。"""
    tags = [blk["页码"] for blk in block_map.values() if blk["页码"]]
    if not tags:
        return "-"
    first = common.parse_page_tag(tags[0])
    last = common.parse_page_tag(tags[-1])
    start_txt = tags[0] if first["降级"] else str(first["起始"])
    end_txt = tags[-1] if last["降级"] else str(last["结束"])
    return f"p.{start_txt}~p.{end_txt}"


def build_index(book_id: str, book_title: str, kb_root: Path = None) -> dict:
    """构建索引。返回汇总信息 dict。"""
    if kb_root is None:
        kb_root = common.get_path("知识库")
    block_map, orig_lines, copy_lines = _parse_book_files(book_title, kb_root)
    book_dir = kb_root / "书籍" / book_title

    # 倒排表：块内容 n-gram → 块 ID 列表（图表块清单内块跳过，见 4.6）
    chart_ids = _load_chart_ids(book_dir)
    inverted = {}
    for bid, blk in block_map.items():
        if bid in chart_ids:
            continue
        text = "".join(blk["正文"])
        # #65 M3（设计 §4.6）：_ngrams 返回 set（块内 gram 必然唯一），posting 里按块循环
        # 逐块追加天然不重复，删除原来的「块 ID 已在列表中」逐个线性查找；产物对象等价。
        for g in _ngrams(text):
            posting = inverted.get(g)
            if posting is None:
                inverted[g] = [bid]
            else:
                posting.append(bid)

    # #4 M3（R4）：保留既有「题干」词条——原文重建不覆盖题干检索入口（独立节、幂等）
    # #65 M1：旧倒排存在但损坏时必须显式报错（含书名/文件/修复建议），不得静默清空「题干」节
    prev_question = {}
    prev_inv = book_dir / "倒排表.json"
    if prev_inv.exists():
        try:
            prev_data = json.loads(prev_inv.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise RuntimeError(
                f"旧倒排表损坏，拒绝重建（避免静默丢失「题干」检索入口）："
                f"书《{book_title}》({book_id}) 文件 {prev_inv}：{e}。"
                f"请修复该 JSON 或从备份恢复后重跑 A-04。") from e
        if not isinstance(prev_data, dict):
            raise RuntimeError(
                f"旧倒排表结构损坏（顶层不是 JSON 对象），拒绝重建（避免静默丢失「题干」检索入口）："
                f"书《{book_title}》({book_id}) 文件 {prev_inv}。请修复或从备份恢复后重跑 A-04。")
        prev_question = prev_data.get("题干")
        if prev_question is None:
            prev_question = {}
        elif not isinstance(prev_question, dict):
            raise RuntimeError(
                f"旧倒排表「题干」节结构损坏（不是 JSON 对象），拒绝重建（避免静默丢失题干检索入口）："
                f"书《{book_title}》({book_id}) 文件 {prev_inv}。请修复或从备份恢复后重跑 A-04。")

    # #65 M2：保留有效题干入口，同时清掉更新/退休后已不存在块的引用。
    live_ids = set(block_map)
    prev_question, question_removed = _prune_question_postings(
        prev_question, live_ids, book_title, prev_inv)
    nav_index, nav_removed = _load_and_prune_block_tags(book_dir, live_ids, book_title)
    index_data = {
        "书号": book_id,
        "构建时间": date.today().isoformat(),
        "条目": dict(sorted(inverted.items())),
    }
    if prev_question:
        index_data["题干"] = prev_question
    if nav_index:
        index_data["导航"] = nav_index
    common.atomic_write_text(
        book_dir / "倒排表.json",
        common.dump_index_json(index_data), encoding="utf-8")

    # 目录骨架（#20：结构信号优先 + 章级过滤；无信号/过滤后空 → 启发式兜底）
    chapters, chapter_channel = _detect_chapters(block_map, orig_lines, book_dir)
    if chapter_channel == "启发式(退化)":
        print("🟡 结构信号候选经章级过滤后为空，已回落启发式全链兜底（不产出比现状更差的结果）")
    page_range = _book_page_range(block_map)
    skeleton = [
        f"# 《{book_title}》目录骨架",
        f"书号：{book_id} | 总块数：{len(block_map)} | 页码范围：{page_range}",
        "",
        "| 章号 | 章名 | 起始块ID | 块数 |",
        "|---|---|---|---|",
    ]
    bid_pos = {bid: i for i, bid in enumerate(block_map)}
    for ch, name, s_bid, e_bid in chapters:
        n_blocks = bid_pos[e_bid] - bid_pos[s_bid] + 1
        skeleton.append(f"| {ch} | {name} | {s_bid} | {n_blocks} |")
    common.atomic_write_text(
        book_dir / "目录骨架.md", "\n".join(skeleton) + "\n", encoding="utf-8")

    # #65 M4（设计 §4.8.4）：整块 SHA-256 + 文件完整性清单，作强校验。
    # 既有 12 字短指纹（任务包指纹列）保持不变——短指纹不是唯一 ID，也不冒充唯一 ID。
    integrity = {
        "书号": book_id,
        "生成时间": date.today().isoformat(),
        "原文SHA256": _file_sha256(book_dir / "原文.md"),
        "检索副本SHA256": _file_sha256(book_dir / "检索副本.md"),
        "块数": len(block_map),
        "块SHA256": {bid: _block_sha256(orig_lines, blk)
                     for bid, blk in block_map.items()},
    }
    common.atomic_write_text(
        book_dir / "完整性清单.json",
        common.dump_index_json(integrity), encoding="utf-8")

    # 锚点映射（参考格式，存提取缓存）
    anchor_map = {}
    for bid, blk in block_map.items():
        parsed = common.parse_page_tag(blk["页码"]) if blk["页码"] else None
        anchor_map[bid] = {
            "起始行": blk["起始行"],
            "结束行": blk["结束行"],
            "页码": blk["页码"],
            "印刷页码": parsed["起始"] if (parsed and not parsed["降级"]) else None,
            "降级": parsed["降级"] if parsed else False,
            "指纹": blk["指纹"],
        }
    cache_dir = common.get_path("输出") / "提取缓存" / book_id
    cache_dir.mkdir(parents=True, exist_ok=True)
    common.atomic_write_text(
        cache_dir / "锚点映射.json",
        json.dumps(anchor_map, ensure_ascii=False, indent=2), encoding="utf-8")

    # 书目总索引登记
    update_catalog(book_title, book_id, len(block_map), page_range, kb_root)

    return {
        "书号": book_id,
        "书名": book_title,
        "块数": len(block_map),
        "章节数": len(chapters),
        "倒排条目数": len(inverted),
        "页码范围": page_range,
        "章节识别": chapter_channel,
        "题干清理": question_removed,
        "导航清理": nav_removed,
        "导航条目数": len(nav_index),
    }


CATALOG_HEADER = "| 书名 | 书号 | 分类 | 一句话主题 | 关键词 | 规模 |"
CATALOG_SEP = "|---|---|---|---|---|---|"
CATALOG_PLACEHOLDER = "待标注"


def _catalog_path(kb_root: Path = None) -> Path:
    if kb_root is None:
        kb_root = common.get_path("知识库")
    return kb_root / "书目总索引.md"


def _catalog_cells(line: str) -> list:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _is_sep_cells(cells: list) -> bool:
    return bool(cells) and all(set(c) <= set("-: ") for c in cells)


def _catalog_col(lines: list, name: str):
    """表头列名 → 下标；无该列返回 None。"""
    for ln in lines:
        s = ln.strip()
        if s.startswith("|") and "书名" in s and "书号" in s:
            cells = _catalog_cells(s)
            return cells.index(name) if name in cells else None
    return None


def _catalog_title(lines: list) -> str:
    """取标题行（首个 # 开头行），无则默认。"""
    for ln in lines:
        if ln.strip().startswith("#"):
            return ln.strip()
    return "# 书目总索引"


def _catalog_data_rows(lines: list) -> list:
    """取数据行并拆成单元格列表 [[...], ...]（去掉标题/表头/分隔/空行）。"""
    rows = []
    for ln in lines:
        s = ln.strip()
        if not s.startswith("|"):
            continue
        cells = _catalog_cells(s)
        if "书名" in cells and "书号" in cells:   # 表头
            continue
        if _is_sep_cells(cells):                  # 分隔行
            continue
        rows.append(cells)
    return rows


def _row_to6(cells: list, has_cat: bool) -> list:
    """把一行规整为 6 列（书名/书号/分类/主题/关键词/规模）；旧 5 列在书号后补空「分类」。"""
    cells = list(cells)
    if not has_cat and len(cells) >= 5:
        cells = cells[:2] + [""] + cells[2:]
    while len(cells) < 6:
        cells.append("")
    return cells[:6]


def _write_catalog(path: Path, title: str, rows: list):
    """统一出口：标题 + 空行 + 表头 + 分隔 + 数据行（不产生多余空行）。"""
    out = [title, "", CATALOG_HEADER, CATALOG_SEP]
    out += ["| " + " | ".join(r) + " |" for r in rows]
    common.atomic_write_text(path, "\n".join(out) + "\n", encoding="utf-8")


def catalog_get_category(book_id: str, kb_root: Path = None) -> str:
    """读某书「分类」格（#15 阶段②）；无登记行/无该列返回空串。"""
    p = _catalog_path(kb_root)
    if not p.exists() or not book_id:
        return ""
    lines = p.read_text(encoding="utf-8").split("\n")
    ci = _catalog_col(lines, "分类")
    if ci is None:
        return ""
    for ln in lines:
        s = ln.strip()
        if s.startswith("| ") and f"| {book_id} |" in s:
            cells = _catalog_cells(s)
            if ci < len(cells):
                return cells[ci]
    return ""


def catalog_categories(kb_root: Path = None) -> list:
    """全部已填「分类」去重（供书库页下拉选已有分类）。"""
    p = _catalog_path(kb_root)
    if not p.exists():
        return []
    lines = p.read_text(encoding="utf-8").split("\n")
    ci = _catalog_col(lines, "分类")
    if ci is None:
        return []
    cats = []
    for ln in lines:
        s = ln.strip()
        if not s.startswith("| ") or "书名" in s:
            continue
        cells = _catalog_cells(s)
        if _is_sep_cells(cells):
            continue
        if ci < len(cells) and cells[ci] and cells[ci] not in cats:
            cats.append(cells[ci])
    return cats


def catalog_set_category(book_id: str, category: str, kb_root: Path = None) -> bool:
    """按书号定位总索引行，写「分类」格（空串＝清除）；未找到登记行返回 False。

    读取→规整为 6 列→重写（顺带清洗历史遗留的空行）。"""
    p = _catalog_path(kb_root)
    if not p.exists() or not book_id:
        return False
    lines = p.read_text(encoding="utf-8").split("\n")
    has_cat = _catalog_col(lines, "分类") is not None
    title = _catalog_title(lines)
    rows = [_row_to6(r, has_cat) for r in _catalog_data_rows(lines)]
    for r in rows:
        if r[1] == book_id:
            r[2] = category
            _write_catalog(p, title, rows)
            return True
    return False


def update_catalog(book_title: str, book_id: str, block_count: int, page_range: str, kb_root: Path = None):
    """在 书目总索引.md 登记/更新本书一行（≤2000 token，每书 3~5 行）。

    #15 阶段②：表头含「分类」列；替换已有行时保留其「分类/主题/关键词」手填内容
    （分类非空即保留；主题/关键词非空且≠「待标注」才保留），新增行分类留空、
    主题/关键词写「待标注」。旧版 5 列表头文件自动迁移为 6 列；每次重写顺带清洗空行。

    #32：定位改为「书号优先，书号未命中则按书名兜底」——重入库换书号时旧行被替换而非
    追加新行；写前去重，同书号或同书名的其它行一律删除（自愈历史重复登记）。"""
    p = _catalog_path(kb_root)
    title = "# 书目总索引"
    rows = []
    has_cat = False
    if p.exists():
        lines = p.read_text(encoding="utf-8").split("\n")
        title = _catalog_title(lines)
        has_cat = _catalog_col(lines, "分类") is not None
        rows = [_row_to6(r, has_cat) for r in _catalog_data_rows(lines)]
    scale = f"{block_count}块/{page_range}"
    idx = next((i for i, r in enumerate(rows) if r[1] == book_id), None)
    if idx is None and book_title:
        idx = next((i for i, r in enumerate(rows) if r[0] == book_title), None)
    if idx is not None:
        r = rows[idx]
        topic = r[3] if r[3] and r[3] != CATALOG_PLACEHOLDER else ""
        keyword = r[4] if r[4] and r[4] != CATALOG_PLACEHOLDER else ""
        rows[idx] = [book_title, book_id, r[2], topic or CATALOG_PLACEHOLDER,
                     keyword or CATALOG_PLACEHOLDER, scale]
    else:
        rows.append([book_title, book_id, "", CATALOG_PLACEHOLDER, CATALOG_PLACEHOLDER, scale])
        idx = len(rows) - 1
    rows = [r for i, r in enumerate(rows)
            if i == idx or (r[1] != book_id and r[0] != book_title)]
    _write_catalog(p, title, rows)


def main():
    args = sys.argv[1:]
    book_id = book_title = None
    for i, a in enumerate(args):
        if a == "--书号" and i + 1 < len(args):
            book_id = args[i + 1]
        elif a == "--书名" and i + 1 < len(args):
            book_title = args[i + 1]
    if not book_id or not book_title:
        print("用法：python build_index.py --书号 <书号> --书名 <书名>")
        sys.exit(1)
    try:
        info = build_index(book_id, book_title)
        common.clear_report(f"{book_title or book_id}_建索引.md")
        print(f"索引构建完成：{info}")
        sys.exit(0)
    except Exception as e:
        common.write_report(f"{book_title or book_id}_建索引.md",
                            f"# 建索引报告\n结论：🔴 失败\n原因：{e}\n")
        print(e)
        sys.exit(1)


def _selfcheck():
    """验收：断言倒排表可查询、骨架「起始块ID+块数」与物理序一致、指纹非空。"""
    import tempfile
    import shutil

    tmp = Path(tempfile.mkdtemp(prefix="build_index_selfcheck_"))
    fake_kb = tmp / "fake_kb"
    book_dir = fake_kb / "书籍" / "自检书"
    book_dir.mkdir(parents=True)

    orig_lines = [
        "【自检01·0001】p.1",
        "上部",
        "基础理论",
        "第一章",
        "阴阳五行概述",
        "玄空之学首重理气阴阳五行之气运行",
        "",
        "【自检01·0002】p.2",
        "理气之要在天干地支之配合",
        "",
        "【自检01·0003】p.2~3",
        "第二章排龙法",
        "排龙之法以水口为主顺逆相推定其星曜",
        "",
        "【自检01·0004】p.PDF.4",
        "年柱月柱日柱时柱",
        "十神枢纽明其气势通其变化",
        "",
        "【自检01·0005】p.5",
        "纳音五行表",
        "甲子乙丑海中金丙寅丁卯炉中火",
    ]
    copy_lines = list(orig_lines)
    (book_dir / "原文.md").write_text("\n".join(orig_lines), encoding="utf-8")
    (book_dir / "检索副本.md").write_text("\n".join(copy_lines), encoding="utf-8")
    # M3 图表块（4.6）：块 0005 标记为图表块，倒排构建时应跳过
    (book_dir / "图表块清单.json").write_text(
        json.dumps({"图表块": ["自检01·0005"]}, ensure_ascii=False, indent=2), encoding="utf-8")

    ok = True
    try:
        # 0. 标题识别单元断言：带空格+顿号 8 字章名须识别；长正文句须排除
        if _match_chapter_title("第四章 四柱测官运、事业") != "第四章四柱测官运、事业":
            print("  ❌ 带空格/顿号章名未识别")
            ok = False
        if _match_chapter_title("## 第一章 风水简述") != "第一章风水简述":
            print("  ❌ MinerU ## 标题前缀未剥除（#19 回归）")
            ok = False
        if _match_chapter_title("第四章 某某注曰这一段其实是很长的正文超过十二个字"):
            print("  ❌ 长正文句被误判为章名")
            ok = False
        # 0b. 指纹低信息行跳过：含排盘表头块的指纹应取下一正文行
        fp4 = common.block_fingerprint(["年柱月柱日柱时柱", "十神枢纽明其气势通其变化"])
        if fp4 != "十神枢纽明其气势通其变化":
            print(f"  ❌ 指纹未跳过排盘表头行：{fp4}")
            ok = False

        info = build_index("自检01", "自检书", fake_kb)
        raw_inv = (book_dir / "倒排表.json").read_text(encoding="utf-8")
        index = json.loads(raw_inv)
        # #65 M3：倒排表统一紧凑序列化（ensure_ascii=False + separators），不得再带缩进换行
        if "\n" in raw_inv:
            print("  ❌ #65 M3 倒排表不是紧凑格式（仍含换行缩进）")
            ok = False
        hits = index["条目"].get("排龙")
        if not hits:
            print("  ❌ 倒排表查不到 '排龙'")
            ok = False
        else:
            print(f"  ✅ 倒排表查询 '排龙' → {hits}")
        # M3 图表块（4.6）：清单内块不进倒排表（0005 查无'海中'/'纳音'），普通块不受影响
        if index["条目"].get("海中") or index["条目"].get("纳音"):
            print(f"  ❌ 图表块 0005 进了倒排表：{index['条目'].get('海中') or index['条目'].get('纳音')}")
            ok = False
        integ = json.loads((book_dir / "完整性清单.json").read_text(encoding="utf-8"))
        b1 = _parse_book_files("自检书", fake_kb)[0]["自检01·0001"]
        orig_ck = (book_dir / "原文.md").read_text(encoding="utf-8").split("\n")
        want_h = hashlib.sha256(
            "\n".join(orig_ck[b1["起始行"] + 1: b1["结束行"] + 1]
                      ).encode("utf-8")).hexdigest()
        if (integ.get("块数") != 5 or len(integ.get("块SHA256", {})) != 5
                or len(integ.get("原文SHA256", "")) != 64
                or integ["块SHA256"].get("自检01·0001") != want_h):
            print(f"  ❌ #65 M4 完整性清单异常：块数={integ.get('块数')} "
                  f"条目={len(integ.get('块SHA256', {}))}")
            ok = False
        else:
            print("  ✅ #65 M4 完整性清单：块级 SHA-256 与文件哈希齐全")
        skeleton = (book_dir / "目录骨架.md").read_text(encoding="utf-8")
        if "| 章号 | 章名 | 起始块ID | 块数 |" not in skeleton:
            ok = False
            print("  ❌ 目录骨架缺「起始块ID+块数」列（#21 M2）")
        sk_lines = [l for l in skeleton.split("\n")
                    if l.strip().startswith("|") and not l.strip().startswith("| 章号")
                    and not l.strip().startswith("|---")]
        if len(sk_lines) != 2:
            ok = False
            print(f"  ❌ OCR 连排标题分章失败，应得 2 章，实际 {len(sk_lines)} 章：{sk_lines}")
        elif "上部" not in sk_lines[0] or "第二章排龙法" not in sk_lines[1]:
            ok = False
            print(f"  ❌ 章名识别错误：{sk_lines}")
        # #21 M2：起始块ID + 块数（物理序计数）；章1=块0001起2块、章2=块0003起3块
        elif "| 自检01·0001 | 2 |" not in sk_lines[0] or "| 自检01·0003 | 3 |" not in sk_lines[1]:
            ok = False
            print(f"  ❌ 骨架起始块ID/块数与物理序不一致：{sk_lines}")
        anchor = json.loads((common.get_path("输出") / "提取缓存" / "自检01" / "锚点映射.json").read_text(encoding="utf-8"))
        for bid, a in anchor.items():
            if not a["指纹"]:
                print(f"  ❌ 块 {bid} 指纹为空")
                ok = False
        if anchor.get("自检01·0004", {}).get("指纹") != "十神枢纽明其气势通其变化":
            print(f"  ❌ 建索引指纹未跳过排盘表头：{anchor.get('自检01·0004', {}).get('指纹')}")
            ok = False
        # M4 页码三态（4.1/4.9）：区间/降级标签经 parse_page_tag 消费，范围与锚点字段正确
        if "页码范围：p.1~p.5" not in skeleton:
            print(f"  ❌ 骨架页码范围错误：{skeleton.split(chr(10))[1] if chr(10) in skeleton else skeleton}")
            ok = False
        a3 = anchor.get("自检01·0003", {})
        if a3.get("印刷页码") != 2 or a3.get("降级") is not False:
            print(f"  ❌ 区间标签 0003 解析错误：{a3}")
            ok = False
        a4 = anchor.get("自检01·0004", {})
        if a4.get("印刷页码") is not None or a4.get("降级") is not True:
            print(f"  ❌ 降级标签 0004 解析错误：{a4}")
            ok = False
        # #15 阶段②：总索引含「分类」列；新增行分类留空，填分类后重建不丢
        if catalog_get_category("自检01", fake_kb) != "":
            print("  ❌ #15 新增行「分类」应留空")
            ok = False
        if not catalog_set_category("自检01", "风水", fake_kb):
            print("  ❌ #15 分类写入失败（未找到登记行）")
            ok = False
        build_index("自检01", "自检书", fake_kb)
        if catalog_get_category("自检01", fake_kb) != "风水":
            print(f"  ❌ #15 重建后「分类」丢失：{catalog_get_category('自检01', fake_kb)!r}")
            ok = False
        if catalog_categories(fake_kb) != ["风水"]:
            print(f"  ❌ #15 分类去重集合错误：{catalog_categories(fake_kb)}")
            ok = False
        # 旧版 5 列表头文件：读/写分类触发迁移后仍可用
        legacy = fake_kb / "书目总索引.md"
        legacy.write_text(
            "# 书目总索引\n\n| 书名 | 书号 | 一句话主题 | 关键词 | 规模 |\n"
            "|---|---|---|---|---|\n| 旧书 | 旧01 | 待标注 | 待标注 | 1块/p.1 |\n",
            encoding="utf-8")
        if not catalog_set_category("旧01", "命理", fake_kb) or catalog_get_category("旧01", fake_kb) != "命理":
            print("  ❌ #15 旧版表头未迁移（分类列不可读写）")
            ok = False
        else:
            print("  ✅ #15 分类列：新增留空、重建保留、旧表头迁移")
        # #32 重入库换书号：书号未命中时按书名兜底替换（保留手填分类）+ 同名/同号重复行去重
        legacy.write_text(
            "# 书目总索引\n\n" + CATALOG_HEADER + "\n" + CATALOG_SEP + "\n"
            "| 自检书 | 自检01 | 命理 | 待标注 | 待标注 | 1块/p.1 |\n"
            "| 自检书 | 自检02 |  | 待标注 | 待标注 | 1块/p.1 |\n",
            encoding="utf-8")
        build_index("自检03", "自检书", fake_kb)
        rows32 = [l for l in legacy.read_text(encoding="utf-8").split("\n")
                  if l.strip().startswith("|") and "自检书" in l]
        if len(rows32) != 1 or "| 自检03 |" not in rows32[0] or "命理" not in rows32[0]:
            print(f"  ❌ #32 换书号未按书名兜底替换/去重：{rows32}")
            ok = False
        else:
            print("  ✅ #32 换书号：书名兜底替换 + 同名去重 + 保留分类")
        # #20 结构信号优先：结构.json 条目为候选，章级过滤（_match_chapter_title）挡住非章级噪声
        (book_dir / "结构.json").write_text(json.dumps([
            {"级别": 1, "标题": "上部", "块ID": "自检01·0001", "匹配方式": "文本+顺序"},
            {"级别": 1, "标题": "第三章 测试新章", "块ID": "自检01·0002", "匹配方式": "文本+顺序"},
            {"级别": 1, "标题": "第二章排龙法", "块ID": "自检01·0003", "匹配方式": "文本+顺序"},
            {"级别": 1, "标题": "纳音五行表", "块ID": "自检01·0005", "匹配方式": "文本+顺序"},
        ], ensure_ascii=False, indent=2), encoding="utf-8")
        info_sig = build_index("自检01", "自检书", fake_kb)
        sk_sig = [l for l in (book_dir / "目录骨架.md").read_text(encoding="utf-8").split("\n")
                  if l.strip().startswith("|") and not l.strip().startswith("| 章号")
                  and not l.strip().startswith("|---")]
        if info_sig.get("章节识别") != "结构信号" or len(sk_sig) != 3:
            print(f"  ❌ #20 结构信号通道未生效：{info_sig.get('章节识别')}，{len(sk_sig)} 章 {sk_sig}")
            ok = False
        elif any("纳音五行表" in l for l in sk_sig):
            print(f"  ❌ #20 非章级噪声信号未被过滤：{sk_sig}")
            ok = False
        else:
            print(f"  ✅ #20 结构信号通道：{len(sk_sig)} 章，噪声已过滤")
        # #20 过滤后无候选 → 退化回落启发式全链（通道标「启发式(退化)」，仍产 2 章）
        (book_dir / "结构.json").write_text(json.dumps([
            {"级别": 1, "标题": "纳音五行表", "块ID": "自检01·0005", "匹配方式": "文本+顺序"},
        ], ensure_ascii=False, indent=2), encoding="utf-8")
        info_deg = build_index("自检01", "自检书", fake_kb)
        if info_deg.get("章节识别") != "启发式(退化)" or info_deg.get("章节数") != 2:
            print(f"  ❌ #20 过滤后无候选未回落兜底：{info_deg}")
            ok = False
        (book_dir / "结构.json").unlink()
        # #20 书级配置消费：按「章级级别」取候选 + 「章级排除」剔噪 + 「章级补充」跨级别加入
        (book_dir / "结构.json").write_text(json.dumps([
            {"级别": 1, "标题": "上部", "块ID": "自检01·0001", "匹配方式": "文本+顺序"},
            {"级别": 2, "标题": "第一章 玄空要旨", "块ID": "自检01·0002", "匹配方式": "文本+顺序"},
            {"级别": 1, "标题": "第二章排龙法", "块ID": "自检01·0003", "匹配方式": "文本+顺序"},
            {"级别": 1, "标题": "纳音五行表", "块ID": "自检01·0005", "匹配方式": "文本+顺序"},
        ], ensure_ascii=False, indent=2), encoding="utf-8")
        (book_dir / "书配置.json").write_text(json.dumps(
            {"章级级别": 1, "章级排除": ["纳音五行表"], "章级补充": ["第一章 玄空要旨"],
             "章级配置来源": "llm"}, ensure_ascii=False, indent=2), encoding="utf-8")
        info_cfg = build_index("自检01", "自检书", fake_kb)
        sk_cfg = [l for l in (book_dir / "目录骨架.md").read_text(encoding="utf-8").split("\n")
                  if l.strip().startswith("|") and not l.strip().startswith("| 章号")
                  and not l.strip().startswith("|---")]
        if info_cfg.get("章节识别") != "结构信号" or len(sk_cfg) != 3:
            print(f"  ❌ #20 书级配置（级别+排除+补充）未生效：{info_cfg.get('章节识别')}，{len(sk_cfg)} 章")
            ok = False
        elif any("纳音五行表" in l for l in sk_cfg) or not any("玄空要旨" in l for l in sk_cfg):
            print(f"  ❌ #20 章级排除/补充未按预期生效：{sk_cfg}")
            ok = False
        else:
            print(f"  ✅ #20 书级配置消费：{len(sk_cfg)} 章（排除剔噪 + 补充跨级别生效）")
        # #20 自判评估（供 book_cfg_gen 触发门槛复用）：第X章 存在→非空；全挡→空
        if signal_selfjudge_empty(book_dir):
            print("  ❌ #20 自判评估：存在「第一章」却判为空")
            ok = False
        (book_dir / "结构.json").write_text(json.dumps([
            {"级别": 1, "标题": "~玩家行动轮~", "块ID": "自检01·0001", "匹配方式": "文本+顺序"},
            {"级别": 1, "标题": "消耗影响点数。", "块ID": "自检01·0002", "匹配方式": "文本+顺序"},
        ], ensure_ascii=False, indent=2), encoding="utf-8")
        if not signal_selfjudge_empty(book_dir):
            print("  ❌ #20 自判评估：全挡条目未判为空")
            ok = False
        (book_dir / "结构.json").unlink()
        (book_dir / "书配置.json").unlink()
        # #4 M3（R4）：重建倒排表保留既有「题干」节（原文重建不覆盖题干检索入口）
        inv_p = book_dir / "倒排表.json"
        data_pre = json.loads(inv_p.read_text(encoding="utf-8"))
        data_pre["题干"] = {"吉凶祸福": ["自检01·0005"],
                            "失效入口": ["自检01·9999"],
                            "混合入口": ["自检01·0005", "自检01·9999"]}
        inv_p.write_text(json.dumps(data_pre, ensure_ascii=False, indent=2), encoding="utf-8")
        info_q = build_index("自检01", "自检书", fake_kb)
        data_post = json.loads(inv_p.read_text(encoding="utf-8"))
        expect_q = {"吉凶祸福": ["自检01·0005"], "混合入口": ["自检01·0005"]}
        if data_post.get("题干") != expect_q or len(info_q["题干清理"]) != 2:
            print(f"  ❌ #65 M2 题干保留/失效清理异常：{data_post.get('题干')}；"
                  f"清理={info_q['题干清理']}")
            ok = False
        else:
            print("  ✅ #4 M3 保留有效题干；#65 M2 清理 2 条失效引用")

        # #66 阶段 3：块标签.json 写入「导航」节并同步清理退休块
        tags_p = book_dir / "块标签.json"
        tags_p.write_text(json.dumps({
            "书号": "自检01", "来源": "AI标注",
            "块": {
                "自检01·0001": ["著书缘起", "总纲导言"],
                "自检01·9999": ["退休废标签"],
            },
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        info_nav = build_index("自检01", "自检书", fake_kb)
        data_nav = json.loads(inv_p.read_text(encoding="utf-8"))
        tags_after = json.loads(tags_p.read_text(encoding="utf-8"))["块"]
        if "导航" not in data_nav or "著书" not in data_nav["导航"] \
                or "退休" in data_nav["导航"] or "自检01·9999" in tags_after \
                or info_nav["导航清理"] != ["自检01·9999"]:
            print(f"  ❌ #66 导航节构建/退休清理异常：{info_nav} / {data_nav.get('导航')}")
            ok = False
        else:
            print("  ✅ #66 导航节构建与块标签退休清理正常")
        print(f"  build_index 自检：{info['块数']} 块，{info['章节数']} 章，倒排条目 {info['倒排条目数']}")
        print(f"  {'✅ 通过' if ok else '❌ 失败'}")
        sys.exit(0 if ok else 1)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        cache_root = common.get_path("输出") / "提取缓存"
        if cache_root.exists():
            for c in cache_root.glob("自检*"):
                shutil.rmtree(c, ignore_errors=True)
        # 清理自检在书目总索引的登记（仅在确有残留行时才写回，避免每次自检都追加空行）
        catalog = common.get_path("知识库") / "书目总索引.md"
        if catalog.exists():
            lines = catalog.read_text(encoding="utf-8").split("\n")
            kept = [l for l in lines if "自检书" not in l and "| 自检0" not in l]
            if len(kept) != len(lines):
                catalog.write_text("\n".join(kept) + "\n", encoding="utf-8")


if __name__ == "__main__":
    if common.is_selfcheck():
        _selfcheck()
    else:
        common.run_with_write_lock(main, "建索引")
