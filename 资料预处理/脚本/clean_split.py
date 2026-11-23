# -*- coding: utf-8 -*-
"""卡 A-03 clean_split.py：清洗+物理行+分块+块ID+检索副本。

输入：输出\\提取缓存\\{书号}\\
处理：① 页首/末模式行不删，改收入隔离区（⟦纯页码行⟧）；闸门②跨页重复行/目录同名行
      → 隔离区（⟦跨页重复行⟧/⟦目录同名⟧）；写 提取缓存\\{书号}\\隔离区\\ 与随书 隔离区.md；
      ② 物理换行（每行≤80字，段落间空行，字符零增删）；③ 分块（按自然段聚合，每块≤1500字）；
      ④ 每块前插块ID行 【{书号}·{序号:04d}】p.{页}；⑤ 生成检索副本（每物理行简繁归一+去标点空白，
      块ID行原样保留，行号与原文严格对齐）；⑥ 落闸门报告 输出\\报告\\{书名}_闸门.md（R1 切块统计内联）。
输出：知识库\\书籍\\{书名}\\原文.md、检索副本.md、隔离区.md
退出码：0=成功，1=失败（含分块后块数为 0），2=黄灯告警（只告警不阻塞，产物已出）
"""

import json
import re
import sys
from pathlib import Path

import common

MAX_PHYSICAL_LINE = 80
MAX_BLOCK_CHARS = 1500

# 页眉页脚/目录字段模式（单独成行才清理，避免误删正文）；命中行改收隔离区，不删除
_HEADER_PATTERNS = [
    re.compile(r"^[\s\-—_＝=·*]*\d+[\s\-—_＝=·*]*$"),          # 纯页码行
    re.compile(r"^第\s*\d+\s*[页Pp]\s*$"),                     # 第X页
    re.compile(r"^[Pp][Aa][Gg][Ee]\s*\d+$", re.IGNORECASE),   # Page N
    re.compile(r"^目录$"),
    re.compile(r"^\s*\d+\s*/\s*\d+\s*$"),                      # 7 / 503
]

# 目录条目形态（闸门② 目录同名判据）：第X章/节/卷/篇/部 或 X、/X．开头的行
_CHAPTER_LIKE_RE = re.compile(
    r"^(?:第\s*[一二三四五六七八九十百零〇0-9]+\s*[章节卷篇部]|[0-9一二三四五六七八九十百零〇]+[、．.．])\s*\S"
)

# 本通道（clean_split）写入隔离区缓存的行前缀。MinerU 通道行（⟦header⟧/⟦footer⟧/⟦page_number⟧）
# 不在其列，阶段3 过滤旧行时应保留，避免重跑 clean_split 误删 extract 产物。
_ISO_OWN_PREFIXES = ("⟦纯页码行⟧", "⟦跨页重复行⟧", "⟦目录同名⟧")


def _clean_page_text(text: str) -> tuple:
    """清洗单页/单段文本：页首/末命中模式的行**不删除**，改收入隔离区（⟦纯页码行⟧前缀）。

    返回 (清洗后文本, 隔离行列表)。风险⑦：代码块（```text ... ```）内保留行首空格，不做 strip。"""
    lines = text.split("\n")
    lines = [ln.rstrip() for ln in lines]
    isolated = []
    # 页眉（首行）与页脚（末行）命中模式 → 隔离区；其余正文照常清洗
    while lines and any(p.match(lines[0].strip()) for p in _HEADER_PATTERNS):
        isolated.append(f"⟦纯页码行⟧ {lines.pop(0).strip()}")
    while lines and any(p.match(lines[-1].strip()) for p in _HEADER_PATTERNS):
        isolated.append(f"⟦纯页码行⟧ {lines.pop().strip()}")
    out = []
    blank = 0
    in_code = False
    for ln in lines:
        if ln.strip() == "```text":
            in_code = True
            out.append(ln.rstrip())
            blank = 0
            continue
        if in_code:
            out.append(ln.rstrip())
            if ln.strip() == "```":
                in_code = False
            continue
        s = ln.strip()
        if not s:
            blank += 1
            if blank <= 1:
                out.append("")
            continue
        blank = 0
        out.append(s)
    return "\n".join(out).strip(), isolated


def _split_paragraphs(text: str) -> list:
    """把清洗后的文本拆为自然段列表（按空行）。"""
    parts = [p.strip() for p in text.split("\n\n") if p.strip()]
    return parts


def _wrap_lines(paragraph: str) -> list:
    """物理换行：先按段内单换行还原视觉行，再把超 80 字的行折行，字符零增删。

    md 表格行与代码块行豁免折行（整段原子保留，见清单阶段一-6/落地方式）。"""
    if paragraph.lstrip().startswith(("```text", "| ")):
        return paragraph.split("\n")
    lines = []
    for visual in paragraph.split("\n"):
        if len(visual) <= MAX_PHYSICAL_LINE:
            lines.append(visual)
        else:
            for i in range(0, len(visual), MAX_PHYSICAL_LINE):
                lines.append(visual[i : i + MAX_PHYSICAL_LINE])
    return lines


def _is_atomic_paragraph(para: str) -> bool:
    """原子段落：md 表格或代码块，分块时整段进入同一块，不许拦腰切。"""
    s = para.lstrip()
    return s.startswith("```text") or s.startswith("| ")


def _segment_no(f: Path) -> int:
    """取缓存文件名中的序号（页_/段_/表_/排_ 后的数字），按数值恢复原始顺序。

    缓存文件名数字可能跨位数（页_099.txt、页_1000.txt），若按文件名做字典序
    排序会错乱（"1000" < "107"），必须按数值排序。"""
    m = re.match(r"(?:页|段|表|排)_(\d+)", f.stem)
    return int(m.group(1)) if m else 0


def _norm_ws(line: str) -> str:
    """空白归一化：连续空白折叠为单空格（用于跨页重复比对）。

    书眉烤入正文后常出现单/多空格变体（如「第五章  玄空要论」双空格 vs
    「第五章 玄空要论」单空格），以原始字符串作字典键会让变体各自只出现 1 页、
    不满足 ≥2 页 的⟦目录同名⟧判据而漏网（表A验收第1条复现根因）。"""
    return re.sub(r"\s+", " ", line).strip()


def _gate2_duplicate_edges(cleaned_pages: list) -> tuple:
    """闸门②：跨页重复的页首/页尾行统计（全通道殿后，唯一抓得到烤入书眉的手段）。

    cleaned_pages: [(页序str, 清洗后文本)]。对每页取首个/末个非空行，按**空白归一化**
    后的行分组（单/多空格变体视为同一行），同一行在多页出现时：
      - 该行形如目录条目（_CHAPTER_LIKE_RE）且出现页数 ≥2 → ⟦目录同名⟧（铁证书眉）；
        只剥除「后续出现」的页，保留「首个出现」页原行（真实章标题，避免误伤）
      - 否则出现页数 / 总页数 > 30% → ⟦跨页重复行⟧（书眉/页脚），全部剥离
    返回 ({页序: [(前缀, 行内容)]}, 唯一命中行数)。
    """
    heads = {}
    feet = {}
    for page, cleaned in cleaned_pages:
        lines = [ln.strip() for ln in cleaned.split("\n") if ln.strip()]
        if not lines:
            continue
        heads.setdefault(_norm_ws(lines[0]), []).append((page, lines[0]))
        feet.setdefault(_norm_ws(lines[-1]), []).append((page, lines[-1]))
    total = max(len(cleaned_pages), 1)
    out = {}
    hit_lines = set()

    def _flag(entries: list) -> str:
        if _CHAPTER_LIKE_RE.match(entries[0][1]) and len(entries) >= 2:
            return "⟦目录同名⟧"
        if len(entries) / total > 0.30:
            return "⟦跨页重复行⟧"
        return ""

    def _collect(entries: list, tag: str):
        if not tag:
            return
        hit_lines.add(_norm_ws(entries[0][1]))
        ordered = sorted(entries, key=lambda e: int(e[0]))
        for i, (p, line) in enumerate(ordered):
            if tag == "⟦目录同名⟧" and i == 0:
                continue
            bucket = out.setdefault(p, [])
            if (tag, line) not in bucket:
                bucket.append((tag, line))

    for entries in heads.values():
        _collect(entries, _flag(entries))
    for entries in feet.values():
        _collect(entries, _flag(entries))
    return out, len(hit_lines)


def _strip_edge_lines(cleaned: str, must_remove: set) -> str:
    """从清洗后文本的首/末位置剥除 must_remove 中的行（只剥首/末非空行，正文中间不动）。"""
    lines = cleaned.split("\n")
    while lines and lines[0].strip() in must_remove:
        lines.pop(0)
    while lines and lines[-1].strip() in must_remove:
        lines.pop()
    return "\n".join(lines).strip()


CHART_MERGE_MIN = 5  # 连续原子段块 ≥5 且中间无正文段 → 合并为图表块


_SIGNAL_FILE = "结构信号.jsonl"


def _load_structure_signals(cache_dir: Path) -> list:
    """读 提取缓存\\{书号}\\结构信号.jsonl（A-01 原料级结构信号，#20）。缺失/异常返回空列表。"""
    p = cache_dir / _SIGNAL_FILE
    if not p.exists():
        return []
    try:
        content = p.read_text(encoding="utf-8")
    except Exception:
        return []
    out = []
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _norm_signal_title(s: str) -> str:
    """信号/标题行归一（匹配用）：剥 Markdown 前缀与包裹波浪线（MinerU 强调样式），去空白。"""
    s = re.sub(r"^#{1,6}\s*", "", (s or "").strip())
    s = re.sub(r"^\\?~\s*", "", s)
    s = re.sub(r"\s*\\?~$", "", s)
    return re.sub(r"\s+", "", s)


def _clean_signal_title(s: str) -> str:
    """结构.json 展示标题：剥 Markdown 前缀与包裹波浪线，保留词间空格。"""
    s = re.sub(r"^#{1,6}\s*", "", (s or "").strip())
    s = re.sub(r"^\\?~\s*", "", s)
    s = re.sub(r"\s*\\?~$", "", s)
    return s.strip()


def _match_signal_blocks(signals: list, blocks: list, book_id: str) -> tuple:
    """结构信号 → 块ID 换算（#20）：按「标题文本 + 出现顺序」在块内视觉行精确匹配定位。

    标题行短且折行风险低；用归一化后**整行相等**（非子串）避免正文提及误配；重名标题按
    第 N 信号配第 N 次出现消歧（游标只在已匹配块之后推进）。返回 (条目列表, 失败列表)。
    """
    block_lines = []
    for _s_page, _e_page, paras in blocks:
        lines = []
        for para in paras:
            for ln in para.split("\n"):
                n = _norm_signal_title(ln)
                if n:
                    lines.append(n)
        block_lines.append(lines)

    entries = []
    failed = []
    cursor = 0
    for sig in signals:
        title = _norm_signal_title(sig.get("标题", ""))
        if not title:
            failed.append(sig)
            continue
        found = None
        for bi in range(cursor, len(block_lines)):
            if title in block_lines[bi]:
                found = bi
                break
        if found is None:  # 兜底：容忍信号顺序与正文顺序错位
            for bi in range(0, len(block_lines)):
                if title in block_lines[bi]:
                    found = bi
                    break
        if found is None:
            failed.append(sig)
            continue
        entries.append({
            "级别": sig.get("级别"),
            "标题": _clean_signal_title(sig.get("标题", "")),
            "块ID": f"{book_id}·{found + 1:04d}",
            "匹配方式": "文本+顺序",
        })
        cursor = found
    return entries, failed


def _merge_chart_blocks(blocks: list, exempt: bool) -> tuple:
    """图表块合并（4.6）：连续 ≥5 个原子段块（md 表格/代码块）且中间无正文段落 →
    合并为 1 个图表块（块内段落全部保留，允许超 1500 字）；单张表格夹杂正文中（连续 <5）不受影响。

    返回 (新块列表, 图表块标记列表[bool 与块一一对应])；exempt=True（书级豁免）时原样返回。
    """
    if exempt:
        return blocks, [False] * len(blocks)
    new_blocks = []
    chart_flags = []
    i, n = 0, len(blocks)
    while i < n:
        s0, e0, paras = blocks[i]
        if len(paras) == 1 and _is_atomic_paragraph(paras[0]):
            j = i
            while j < n and len(blocks[j][2]) == 1 and _is_atomic_paragraph(blocks[j][2][0]):
                j += 1
            if j - i >= CHART_MERGE_MIN:
                merged = [p for _, _, ps in blocks[i:j] for p in ps]
                new_blocks.append((s0, blocks[j - 1][1], merged))
                chart_flags.append(True)
                i = j
                continue
        new_blocks.append((s0, e0, paras))
        chart_flags.append(False)
        i += 1
    return new_blocks, chart_flags


def clean_split(book_id: str, book_title: str) -> tuple:
    """执行清洗分块。返回 (原文.md 内容, 检索副本.md 内容, 块数, 黄灯清单list)。

    黄灯清单非空时 main 以退出码 2 返回（只告警不阻塞）。"""
    cache_dir = common.get_path("输出") / "提取缓存" / book_id
    if not cache_dir.exists():
        raise FileNotFoundError(f"提取缓存不存在：{cache_dir}（请先运行 A-01 提取）")

    files = sorted(
        [f for f in cache_dir.iterdir() if f.is_file() and f.suffix == ".txt"],
        key=_segment_no,
    )
    if not files:
        raise FileNotFoundError(f"提取缓存为空：{cache_dir}")

    # 阶段1：逐文件清洗；页首/末命中模式行 → 隔离区（⟦纯页码行⟧）
    cleaned_pages = []   # [(page_str, cleaned_text)]
    iso_by_page = {}     # page_str → [⟦前缀⟧ 行内容]
    for f in files:
        with open(f, "r", encoding="utf-8") as fh:
            text = fh.read()
        cleaned, iso = _clean_page_text(text)
        m = re.match(r"(?:页|段|表|排)_(\d+)", f.stem)
        page = m.group(1) if m else f.stem
        for line in iso:
            iso_by_page.setdefault(page, []).append(line)
        if cleaned:
            cleaned_pages.append((page, cleaned))

    # 阶段2：闸门② 跨页重复行统计 + 目录同名判据 → 隔离区
    gate2, gate2_hits = _gate2_duplicate_edges(cleaned_pages)
    stripped_pages = {}
    for page, cleaned in cleaned_pages:
        strip_set = {line for _, line in gate2.get(page, [])}
        new_cleaned = _strip_edge_lines(cleaned, strip_set) if strip_set else cleaned
        stripped_pages[page] = new_cleaned
        for tag, line in gate2.get(page, []):
            iso_by_page.setdefault(page, []).append(f"{tag} {line}")

    if not stripped_pages:
        raise RuntimeError("清洗后无有效内容")

    # 阶段3：写隔离区缓存。MinerU 通道（extract）以覆盖写落 ⟦header⟧/⟦footer⟧/⟦page_number⟧ 行；
    # 本通道此前以「追加」方式写入防线行，重跑会跨次累积膨胀（表A验收复现路径）。
    # 改为：先滤掉缓存中本通道旧防线行（MinerU 行不在 _ISO_OWN_PREFIXES 之列，保留），
    # 再合并写入本次防线行 → 单次快照语义，重跑不膨胀。MinerU 锚点文件（页_001.txt）与本通道
    # 文件（页_1.txt）命名零填充不同，互不覆盖，此处按行前缀区隔即安全。
    iso_dir = cache_dir / "隔离区"
    if iso_dir.exists():
        for f in iso_dir.glob("页_*.txt"):
            kept = [ln for ln in f.read_text(encoding="utf-8").splitlines()
                    if not ln.startswith(_ISO_OWN_PREFIXES)]
            if kept:
                f.write_text("\n".join(kept) + "\n", encoding="utf-8")
            else:
                f.unlink()
    for page in sorted(iso_by_page, key=lambda s: int(s)):
        lines = iso_by_page[page]
        if not lines:
            continue
        iso_dir.mkdir(parents=True, exist_ok=True)
        p = iso_dir / f"页_{page}.txt"
        existing = p.read_text(encoding="utf-8").rstrip("\n") if p.exists() else ""
        merged = existing + "\n" + "\n".join(lines) if existing else "\n".join(lines)
        p.write_text(merged + "\n", encoding="utf-8")

    # 阶段4：组装随书 隔离区.md（三道防线裁下的内容统一落盘）
    iso_md = [
        f"# 隔离区：《{book_title}》",
        "> 三道防线裁下的内容，不进正文/检索/标注/评测；供页码锚点收割与人工复查。",
        "> 每节：【隔离·{来源类型}】p.{PDF页序}，后接原文行。",
    ]
    if iso_dir.exists():
        for f in sorted(iso_dir.glob("页_*.txt"), key=_segment_no):
            no = _segment_no(f)
            for line in f.read_text(encoding="utf-8").splitlines():
                m2 = re.match(r"^⟦(.+?)⟧\s*(.*)$", line)
                if not m2:
                    continue
                iso_md.append("")
                iso_md.append(f"【隔离·{m2.group(1)}】p.{no}")
                iso_md.append(m2.group(2))

    # 阶段5：分块（每块 ≤1500 字；md 表格/代码块为原子段落，整段独立成块）
    segments = []   # [(page_str, paragraph_str)]
    for page in sorted(stripped_pages, key=lambda s: int(s)):
        for p in _split_paragraphs(stripped_pages[page]):
            segments.append((page, p))
    if not segments:
        raise RuntimeError("清洗后无有效内容")

    blocks = []  # [(start_page, end_page, [paragraphs])]，跨页块记录起止页（M4）
    cur_start, cur_end, cur_paras, cur_chars = None, None, [], 0
    for page, para in segments:
        if _is_atomic_paragraph(para):
            if cur_paras:
                blocks.append((cur_start, cur_end, cur_paras))
                cur_start, cur_end, cur_paras, cur_chars = None, None, [], 0
            blocks.append((page, page, [para]))
            continue
        if cur_paras and cur_chars + len(para) > MAX_BLOCK_CHARS:
            blocks.append((cur_start, cur_end, cur_paras))
            cur_start, cur_end, cur_paras, cur_chars = None, None, [], 0
        if not cur_paras:
            cur_start = cur_end = page
        else:
            cur_end = page
        cur_paras.append(para)
        cur_chars += len(para)
    if cur_paras:
        blocks.append((cur_start, cur_end, cur_paras))

    # 图表块合并（4.6）：连续 ≥5 原子段块 → 合并为图表块；书级豁免（用户设置）时跳过
    books_dir = common.get_path("知识库") / "书籍"
    book_dir = books_dir / book_title
    exempt = bool(common.load_book_rules(book_title).get(common.RULE_CHART_EXEMPT, False))
    blocks, chart_flags = _merge_chart_blocks(blocks, exempt)

    # 阶段5.5：页码拟合（M4）—— 分页书（缓存含 页_ 文件）调 page_fit 得块标签；
    # 写 页码映射.json；锚点/稳定性不足 → 全书降级（p.PDF.）并亮黄灯。
    # 非分页书（docx/txt/md/xlsx）不拟合、不生成映射，块标签维持提取序号语义。
    import page_fit
    segments_fit, state_fit = [], None
    is_paginated = any(f.stem.startswith("页_") for f in files)
    yellows = []
    if is_paginated:
        iso_dir = cache_dir / "隔离区"
        segments_fit, state_fit = page_fit.fit_segments(page_fit.harvest_anchors(iso_dir))
        book_dir.mkdir(parents=True, exist_ok=True)
        common.atomic_write_text(
            book_dir / "页码映射.json",
            json.dumps(page_fit.build_page_map(book_id, state_fit, segments_fit),
                       ensure_ascii=False, indent=2))
        if state_fit == "降级":
            yellows.append("页码无法自动识别：引用位置将按 PDF 文件页数标注，与书上印刷页码可能不一致；不影响检索，无需处理。")

        # #27 LLM 分层复查：中置信度 D1 抽查 / 低置信度 D2 诊断补救
        import page_fit_llm
        segments_fit, state_fit, llm_used, review_msg = page_fit_llm.review(
            iso_dir, segments_fit, state_fit, book_title, book_dir
        )
        if review_msg:
            yellows.append(review_msg)

    # 阶段5.6：#20 结构信号换算 → 结构.json（原料级信号按「标题文本+出现顺序」定位块ID；
    # 无信号通道不产文件；匹配失败条目进黄灯告警，禁止静默丢弃）
    signals = _load_structure_signals(cache_dir)
    structure_entries, structure_failed = [], []
    if signals:
        structure_entries, structure_failed = _match_signal_blocks(signals, blocks, book_id)

    # 生成原文.md 与 检索副本.md（块 ID 行三态标签：印刷页 / 印刷区间 / p.PDF. 降级）
    orig_lines = []
    copy_lines = []
    chart_ids = []
    for idx, ((s_page, e_page, paras), is_chart) in enumerate(zip(blocks, chart_flags), start=1):
        bid = f"{book_id}·{idx:04d}"
        if is_paginated:
            label = page_fit.block_label(int(s_page), int(e_page), segments_fit, state_fit)
        else:
            label = s_page
        block_id_line = f"【{bid}】p.{label}"
        if is_chart:
            chart_ids.append(bid)
        orig_lines.append(block_id_line)
        copy_lines.append(block_id_line)
        for pi, para in enumerate(paras):
            for ln in _wrap_lines(para):
                orig_lines.append(ln)
                copy_lines.append(common.normalize_for_search(ln))
            if pi < len(paras) - 1:
                orig_lines.append("")
                copy_lines.append("")
        if idx < len(blocks):
            orig_lines.append("")
            copy_lines.append("")

    orig_text = "\n".join(orig_lines)
    copy_text = "\n".join(copy_lines)

    # 写入知识库（原文 + 检索副本 + 图表块清单 + 隔离区.md）
    book_dir.mkdir(parents=True, exist_ok=True)
    common.atomic_write_text(book_dir / "原文.md", orig_text)
    common.atomic_write_text(book_dir / "检索副本.md", copy_text)
    common.atomic_write_text(book_dir / "隔离区.md", "\n".join(iso_md) + "\n")
    common.atomic_write_text(book_dir / "图表块清单.json",
                            json.dumps({"图表块": chart_ids}, ensure_ascii=False, indent=2))
    (book_dir / "章节摘要").mkdir(exist_ok=True)
    # #20 成品 结构.json：只供 build_index 章节识别候选，不进倒排/任务包/检索视野
    if signals:
        struct_path = book_dir / "结构.json"
        if structure_entries:
            common.atomic_write_text(
                struct_path, json.dumps(structure_entries, ensure_ascii=False, indent=2))
        elif struct_path.exists():
            struct_path.unlink()

    # 阶段6：R1 切块统计 + 闸门报告 + 黄灯判定
    # 图表块（4.6）允许超 1500 字，不计入"单块超长告警"，只计入块数与块长分布
    block_lens = [sum(len(p) for p in paras) for _, _, paras in blocks]
    n = len(blocks)
    avg_len = round(sum(block_lens) / n) if n else 0
    frag_ratio = sum(1 for l in block_lens if l < 100) / n if n else 0.0
    over_len = sum(1 for i, l in enumerate(block_lens)
                   if l > MAX_BLOCK_CHARS and not chart_flags[i])
    if frag_ratio > 0.30:
        yellows.append(f"全书 {frag_ratio:.0%} 的段落不足 100 字，分段偏细；不影响入库和检索，无需处理。")
    over_blocks = []
    for i, (s_page, e_page, paras) in enumerate(blocks):
        if sum(len(p) for p in paras) > MAX_BLOCK_CHARS and not chart_flags[i]:
            tag = (page_fit.block_label(int(s_page), int(e_page), segments_fit, state_fit)
                   if is_paginated else s_page)
            over_blocks.append(f"p.{tag}")
    over_blocks = over_blocks[:5]
    if structure_failed:
        yellows.append(
            f"有 {len(structure_failed)} 条章节标题未能自动识别：目录里可能缺对应章节名；"
            f"不影响正文与检索，无需处理。")
    gate_report = _build_gate_report(book_id, book_title, gate2_hits, book_dir,
                                     n, avg_len, frag_ratio, over_len, over_blocks, yellows)
    # #14 全绿不留档：无黄灯删旧报告（自检照常写，自检链会读回 自检书_闸门.md）
    if yellows or common.is_selfcheck():
        common.write_report(f"{book_title}_闸门.md", gate_report)
    else:
        common.clear_report(f"{book_title}_闸门.md")

    return orig_text, copy_text, n, yellows


def _build_gate_report(book_id: str, book_title: str, gate2_hits: int, book_dir: Path,
                       n: int, avg_len: int, frag_ratio: float, over_len: int,
                       over_blocks: list, yellows: list) -> str:
    """组装三道闸门报告（输出\\报告\\{书名}_闸门.md）。闸门①数据由 extract 写缓存（M2），
    此处缺省显示未采集；页码映射.json 由 page_fit（M4）生成，缺省显示未启用。"""
    import json as _json
    cfg = common.load_config()
    pref = bool(cfg.get("mineru_版面优选", True))

    gate_data = {}
    gd_path = common.get_path("输出") / "提取缓存" / book_id / "闸门数据.json"
    if gd_path.exists():
        try:
            gate_data = _json.loads(gd_path.read_text(encoding="utf-8"))
        except Exception:
            gate_data = {}
    g1 = gate_data.get("闸门1", {}) or {}
    if g1:
        sim = g1.get("加权相似度")
        low_ratio = g1.get("低相似页占比", 0) or 0
        ch = g1.get("通道", "")
        if sim is None:
            if g1.get("不适用"):
                g1_res = f"不适用（{g1['不适用']}）"
            elif ch == "pdf_text":
                g1_res = "不适用（文本层通道，无 MinerU 对照）"
            else:
                g1_res = "不适用（未启用 MinerU 通道）"
        else:
            g1_res = f"加权相似度 {sim}，低相似页 {low_ratio:.1%}"
        if g1.get("已退回文本层"):
            g1_action = "退回文本层重跑"
        elif sim is not None and (sim < 0.98 or low_ratio > 0.05):
            g1_action = "黄灯"
        else:
            g1_action = "通过"
        channel = f"MinerU 版面优选（{'开' if ch in ('mineru_auto', 'mineru_ocr') else '关'}）｜后端：{g1.get('后端', '-')}"
    else:
        g1_res, g1_action = "未采集（无 MinerU 通道数据）", "-"
        channel = f"MinerU 版面优选（{'开' if pref else '关'}）｜后端：-（未启用）"

    excl = "开" if common.load_book_rules(book_title).get(common.RULE_CHART_EXEMPT) else "关"

    fit_line = "页码拟合：未启用（M4 生成）"
    map_path = book_dir / "页码映射.json"
    if map_path.exists():
        try:
            mp = _json.loads(map_path.read_text(encoding="utf-8"))
            segs = mp.get("分段", [])
            fitted_n = sum(1 for s in segs if s.get("状态") == "已拟合")
            anchors = sum(s.get("锚点数", 0) for s in segs)
            detail = f"（已拟合段 {fitted_n}，锚点 {anchors}）" if segs else "（无锚点）"
            fit_line = f"页码拟合：{mp.get('状态', '-')}｜{len(segs)} 段 {detail}"
        except Exception:
            pass

    lines = [
        f"# 检查报告：《{book_title}》",
        f"通道：{channel}｜结论：{'🟡' if yellows else '🟢'}"
        + ("（有警告但不影响使用，详情如下）" if yellows else ""),
        "| 闸门 | 结果 | 阈值 | 动作 |",
        "|---|---|---|---|",
        f"| ① 字符 diff | {g1_res} | <0.98 或 >5% | {g1_action} |",
        f"| ② 跨页重复行 | {gate2_hits} 行命中入隔离区 | >30% 页首/页尾 | 已隔离 |",
        f"| ③ 内容型特判 | 未触发（书级豁免：{excl}） | 书级开关 | - |",
        fit_line,
        f"切块统计（R1 内联）：块数 {n}，平均块长 {avg_len} 字，碎块率（<100字）{frag_ratio:.0%}",
    ]
    if over_len:
        lines.append(f"单块超 {MAX_BLOCK_CHARS} 字告警：{over_len} 块（{ '，'.join(over_blocks) } 等）")
    if yellows:
        lines.append("")
        lines.append("⚠️ 黄灯（只告警不阻塞，退出码 2）：")
        lines.extend(f"- {y}" for y in yellows)
    return "\n".join(lines)


def main():
    args = sys.argv[1:]
    book_id = book_title = None
    for i, a in enumerate(args):
        if a == "--书号" and i + 1 < len(args):
            book_id = args[i + 1]
        elif a == "--书名" and i + 1 < len(args):
            book_title = args[i + 1]
    if not book_id or not book_title:
        print("用法：python clean_split.py --书号 <书号> --书名 <书名>")
        sys.exit(1)
    try:
        _, _, n, yellows = clean_split(book_id, book_title)
        common.clear_report(f"{book_title or book_id}_清洗分块.md")
        msg = f"整理正文完成：{book_title}（{book_id}），共 {n} 块"
        if yellows:
            msg += "｜警告：" + "；".join(yellows)
        print(msg)
        sys.exit(2 if yellows else 0)
    except Exception as e:
        common.write_report(f"{book_title or book_id}_清洗分块.md",
                            f"# 清洗分块报告\n结论：🔴 失败\n原因：{e}\n")
        print(e)
        sys.exit(1)


def _selfcheck():
    """验收：断言原文每行≤80字、每块有唯一块ID、检索副本与原文行数相等。"""
    import tempfile
    import shutil

    tmp = Path(tempfile.mkdtemp(prefix="clean_split_selfcheck_"))
    book_id = "自检01"
    cache_dir = common.get_path("输出") / "提取缓存" / book_id
    if cache_dir.exists():
        shutil.rmtree(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)

    long_para = "玄空之学首重理气。" * 40
    (cache_dir / "页_001.txt").write_text(
        "第1页\n\n玄空秘旨\n卷上\n\n" + long_para + "\n\n1 / 503", encoding="utf-8"
    )
    # M1 目录同名用例（空格变体）：章节形态行以不同空白形态出现在 2 页及以上
    # → 归一化后视为同一行（铁证书眉）；保留首个出现（双空格真章名），剥除后续（单空格书眉变体）
    (cache_dir / "页_002.txt").write_text(
        "第五章  玄空要论\n" + "排龙之法以水口为主。" * 30, encoding="utf-8"
    )
    # 对齐排版代码块（风险⑦：行首空格保留；原子：超长行不折行）
    (cache_dir / "排_003.txt").write_text(
        "```text\n        食    杀    日元  才\n"
        "坤造： 己卯  癸酉  丁丑  庚\n"
        + "星曜排列占位" * 30 + "\n```",
        encoding="utf-8",
    )
    # md 表格（风险①：<br> 不进检索副本；原子：整表一块不拦腰切）
    (cache_dir / "表_004.txt").write_text(
        "| 85<br>六 | 41<br>二 |\n|---|---|\n| 74<br>五 | 96<br>七 |", encoding="utf-8"
    )
    # M3 图表块（4.6）：页_041 正文段打断原子连跑（使 排_003+表_004 的 2 连原子块不合并）；
    # 表_042 连续 12 张表格（单行≤80 字、单表>100 字、合计>1500 字）→ 应合并为 1 个图表块
    (cache_dir / "页_041.txt").write_text("玄空第41页正文。" * 100, encoding="utf-8")
    tables = []
    for k in range(12):
        a = k * 10 + 1
        tables.append(
            f"| {a}<br>甲子 | {a+1}<br>乙丑 | {a+2}<br>丙寅 | {a+3}<br>丁卯 | {a+4}<br>戊辰 |\n"
            "|---|---|---|---|---|\n"
            f"| {a+5}<br>己巳 | {a+6}<br>庚午 | {a+7}<br>辛未 | {a+8}<br>壬申 | {a+9}<br>癸酉 |"
        )
    (cache_dir / "表_042.txt").write_text("\n\n".join(tables) + "\n", encoding="utf-8")
    # 跨位数页序回归：文件名字典序会得到 100,1000,107（错位），数值排序应为 100,107,1000；
    # 每页内容 1000 字保证独立成块，页码序列可被断言捕获
    (cache_dir / "页_100.txt").write_text(
        "第五章 玄空要论\n" + "玄空第100页内容测试。" * 100, encoding="utf-8"
    )
    (cache_dir / "页_107.txt").write_text("玄空第107页内容测试。" * 100, encoding="utf-8")
    (cache_dir / "页_1000.txt").write_text("玄空第1000页内容测试。" * 100, encoding="utf-8")

    # M4 页码拟合数据：预写隔离区锚点（⟦page_number⟧ 候选 = PDF 页序 → 全书偏移 0，
    # 6 锚点 ≥3 且占比 100% → 已拟合；块标签 = 起止页原数值 / 区间）
    iso_dir = cache_dir / "隔离区"
    iso_dir.mkdir(parents=True, exist_ok=True)
    for pg, val in [(1, 1), (2, 2), (41, 41), (100, 100), (107, 107), (1000, 1000)]:
        (iso_dir / f"页_{pg:03d}.txt").write_text(f"⟦page_number⟧ {val}\n", encoding="utf-8")

    # 输出到临时知识库，避免污染真实库
    import json
    cfg_path = common.PROJECT_ROOT / "路径配置.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    saved_kb = cfg["知识库"]
    fake_kb = tmp / "fake_kb"
    cfg["知识库"] = str(fake_kb)
    saved_rules = cfg["书规则"]
    cfg["书规则"] = str(tmp / "书规则")
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    # #20 结构信号（原料级）：一条可换算信号，验证 原料信号 → 结构.json 链路
    (cache_dir / "结构信号.jsonl").write_text(
        json.dumps({"级别": 2, "标题": "第五章 玄空要论", "来源文件": "页_002.txt", "来源序号": 1},
                   ensure_ascii=False) + "\n",
        encoding="utf-8")
    try:
        orig_text, copy_text, n, yellows = clean_split(book_id, "自检书")
        ok = True
        orig_lines = orig_text.split("\n")
        copy_lines = copy_text.split("\n")
        in_code = False
        for i, ln in enumerate(orig_lines, start=1):
            if ln.strip() == "```text":
                in_code = True
            if not in_code and ln and len(ln) > MAX_PHYSICAL_LINE:
                print(f"  ❌ 第 {i} 行超 {MAX_PHYSICAL_LINE} 字：{len(ln)}")
                ok = False
            if ln.strip() == "```":
                in_code = False
        # 风险⑦：代码块行首空格保留
        if "        食    杀    日元  才" not in orig_text:
            print("  ❌ 代码块行首空格被 strip")
            ok = False
        # 原子性：超长代码块行整体保留（不折行）
        if ("星曜排列占位" * 30) not in orig_text:
            print("  ❌ 代码块超长行被折行")
            ok = False
        # 原子性：md 表格整块（表头与表体同块）
        blocks_of_orig = orig_text.split("\n\n")
        if not any("85<br>六" in b and "74<br>五" in b for b in blocks_of_orig):
            print("  ❌ md 表格被拦腰切成多块")
            ok = False
        # 风险①：检索副本无 <br> 残留垃圾词
        if "br六" in copy_text or "<" in copy_text or ">" in copy_text:
            print("  ❌ 检索副本含 HTML 残留")
            ok = False
        ids = re.findall(r"【自检01·(\d{4})】", orig_text)
        if len(ids) != len(set(ids)) or len(ids) != n:
            print(f"  ❌ 块ID 唯一性：{len(ids)} 个，块数 {n}")
            ok = False
        tags = [common.parse_page_tag(m.group(1))
                for m in re.finditer(r"【自检01·\d{4}】p\.(.*)", orig_text)]
        starts = [t["起始"] for t in tags]
        valid_starts = [s for s in starts if s is not None]
        if valid_starts != sorted(valid_starts):
            print(f"  ❌ 块标签起始页非数值递增：{tags}")
            ok = False
        for t in tags:
            if (t["起始"] is not None and t["结束"] is not None
                    and t["起始"] > t["结束"]):
                print(f"  ❌ 标签区间起>止：{t}")
                ok = False
        if len(orig_lines) != len(copy_lines):
            print(f"  ❌ 行数不一致：原文 {len(orig_lines)} / 副本 {len(copy_lines)}")
            ok = False
        # M1 隔离区：页眉/页脚/页码行不删正文，改收 隔离区.md
        iso_md_path = common.get_path("知识库") / "书籍" / "自检书" / "隔离区.md"
        iso_text = iso_md_path.read_text(encoding="utf-8") if iso_md_path.exists() else ""
        if "第1页" in orig_text or "1 / 503" in orig_text:
            print("  ❌ 纯页码行未隔离，仍残留正文")
            ok = False
        if "【隔离·纯页码行】p.1" not in iso_text or "1 / 503" not in iso_text:
            print("  ❌ 隔离区.md 未收录纯页码行 p.1")
            ok = False
        # M1 闸门②目录同名空格变体：双空格真章名归一化后与单空格书眉变体同组（≥2 页）
        # → 保留首个出现页（双空格真章名），剥除后续页（单空格书眉变体）入隔离区
        if "第五章 玄空要论" in orig_text:
            print("  ❌ 目录同名单空格书眉变体未剥离，仍残留正文")
            ok = False
        if "第五章  玄空要论" not in orig_text:
            print("  ❌ 首个出现（双空格真章名）被误剥离")
            ok = False
        if "【隔离·目录同名】" not in iso_text or "第五章 玄空要论" not in iso_text:
            print("  ❌ 目录同名空格变体未按多页收录隔离区")
            ok = False
        if iso_text.count("第五章 玄空要论") > 1:
            print("  ❌ 目录同名单行页首=页尾被重复收录隔离区")
            ok = False
        # M1 排_ 前缀回归：排盘代码块页号不产生 p.排_ 脏标签
        if "p.排_" in orig_text or "p.排_" in copy_text:
            print("  ❌ 排_ 页号产生脏标签")
            ok = False
        # M3 图表块（4.6）：连续 12 表合成 1 个图表块（块 0005），清单.json 标记，2 连原子块不并入
        book_dir = common.get_path("知识库") / "书籍" / "自检书"
        box = {}
        cur = None
        for ln in orig_text.split("\n"):
            m = re.match(r"^【自检01·(\d{4})】p\..+$", ln)
            if m:
                cur = m.group(1)
                box.setdefault(cur, [])
                continue
            if cur is not None:
                box[cur].append(ln)
        chart_ids = json.loads((book_dir / "图表块清单.json").read_text(encoding="utf-8"))["图表块"]
        merged = "\n".join(box.get("0005", []))
        if len(chart_ids) != 1 or not chart_ids[0].startswith("自检01·0005"):
            print(f"  ❌ 图表块清单.json 未标记合并块：{chart_ids}")
            ok = False
        if "| 1<br>甲子 |" not in merged or "120<br>癸酉" not in merged:
            print("  ❌ 连续 12 表未合并为 1 个图表块")
            ok = False
        if "85<br>六" in merged or "星曜排列占位" in merged:
            print("  ❌ 2 连原子块（排_003+表_004）被误并入图表块")
            ok = False
        if n != 8:
            print(f"  ❌ 合并后块数应为 8，实际 {n}")
            ok = False
        # 图表块允许超 1500 字，不应触发"单块超长告警"（报告由本次运行生成）
        gate_txt = (common.report_dir() / "自检书_闸门.md").read_text(encoding="utf-8")
        if "单块超" in gate_txt:
            print("  ❌ 图表块被计入单块超长告警（允许超 1500 字）")
            ok = False
        y1, y2 = False, False
        if yellows:
            y1 = True
        # M3 书级豁免（4.6）：书规则「豁免图表块收纳」=开 → 12 表各自独立成块，清单为空
        book_dir.mkdir(parents=True, exist_ok=True)
        common.save_book_rules("自检书", {common.RULE_CHART_EXEMPT: True})
        _, _, n2, yellows2 = clean_split(book_id, "自检书")
        chart_ids2 = json.loads((book_dir / "图表块清单.json").read_text(encoding="utf-8"))["图表块"]
        if chart_ids2:
            print(f"  ❌ 豁免=开 仍合并图表块：{chart_ids2}")
            ok = False
        if n2 != 19:
            print(f"  ❌ 豁免=开 块数应为 19（12 表独立），实际 {n2}")
            ok = False
        # 重跑不膨胀：豁免轮是第 2 次运行，隔离区缓存防线行不得跨次累积；
        # MinerU 模拟锚点行（⟦page_number⟧）非本通道前缀，必须保留
        iso_100 = (iso_dir / "页_100.txt").read_text(encoding="utf-8")
        iso_md2 = iso_md_path.read_text(encoding="utf-8")
        if iso_100.count("⟦目录同名⟧") != 1:
            print(f"  ❌ 重跑后隔离区缓存目录同名行应 1 条，实际 {iso_100.count('⟦目录同名⟧')}（追加膨胀）")
            ok = False
        if iso_100.count("第五章 玄空要论") != 1:
            print(f"  ❌ 重跑后隔离区缓存目录同名内容应 1 行，实际 {iso_100.count('第五章 玄空要论')}")
            ok = False
        if "⟦page_number⟧ 100" not in iso_100:
            print("  ❌ 重跑后 MinerU 模拟锚点行（⟦page_number⟧）被误删")
            ok = False
        if iso_md2.count("第五章 玄空要论") > 1:
            print(f"  ❌ 重跑后随书隔离区.md 目录同名行累积为 {iso_md2.count('第五章 玄空要论')} 条")
            ok = False
        if yellows2:
            y2 = True
        # 黄灯应为空（当前数据碎块率低且拟合成功，不触发 R1/降级 告警）
        if y1 or y2:
            print(f"  ❌ 不应出现黄灯：第1轮 {yellows}，豁免轮 {yellows2}")
            ok = False
        # #20 结构信号换算：原料级 结构信号.jsonl → 成品 结构.json（标题 + 出现顺序定位块ID）
        struct_file = book_dir / "结构.json"
        struct = json.loads(struct_file.read_text(encoding="utf-8")) if struct_file.exists() else []
        if (len(struct) != 1 or struct[0].get("标题") != "第五章 玄空要论"
                or struct[0].get("匹配方式") != "文本+顺序"):
            print(f"  ❌ #20 结构.json 换算异常：{struct}")
            ok = False
        # #20 换算单元：整行精确匹配 + 失败条目不静默丢弃
        u_entries, u_failed = _match_signal_blocks(
            [{"级别": 2, "标题": "第五章 玄空要论", "来源文件": "x", "来源序号": 1},
             {"级别": 2, "标题": "并不存在的幽灵章节", "来源文件": "x", "来源序号": 2}],
            [("1", "1", ["第五章  玄空要论", "正文甲"]), ("2", "2", ["别的正文"])],
            "自检01")
        if len(u_entries) != 1 or u_entries[0].get("块ID") != "自检01·0001":
            print(f"  ❌ #20 信号换算单元（整行匹配）：{u_entries}")
            ok = False
        if len(u_failed) != 1 or u_failed[0].get("标题") != "并不存在的幽灵章节":
            print(f"  ❌ #20 失败信号未进 failed 清单：{u_failed}")
            ok = False
        # M4 降级路径：清空隔离区锚点 → 无锚 → 状态=降级 → p.PDF. 标签 + 黄灯
        shutil.rmtree(iso_dir, ignore_errors=True)
        iso_dir.mkdir(parents=True, exist_ok=True)
        (iso_dir / "页_001.txt").write_text("⟦纯页码行⟧ 序\n", encoding="utf-8")
        _, _, n3, yellows3 = clean_split(book_id, "自检书")
        orig3 = (book_dir / "原文.md").read_text(encoding="utf-8")
        map3 = json.loads((book_dir / "页码映射.json").read_text(encoding="utf-8"))
        if "p.PDF." not in orig3:
            print("  ❌ 降级轮块标签应含 p.PDF.")
            ok = False
        if map3.get("状态") != "降级":
            print(f"  ❌ 降级轮页码映射.json 状态应为 降级：{map3.get('状态')}")
            ok = False
        if n3 != 19:
            print(f"  ❌ 降级轮块数应仍为 19（豁免开），实际 {n3}")
            ok = False
        if not any("页码无法自动识别" in y for y in yellows3):
            print(f"  ❌ 降级轮应亮警告：{yellows3}")
            ok = False
        print(f"  clean_split 自检：{n} 块，原文 {len(orig_lines)} 行，副本 {len(copy_lines)} 行")
        print(f"  {'✅ 通过' if ok else '❌ 失败'}")
        sys.exit(0 if ok else 1)
    finally:
        cfg["知识库"] = saved_kb
        cfg["书规则"] = saved_rules
        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(cache_dir, ignore_errors=True)


if __name__ == "__main__":
    if common.is_selfcheck():
        _selfcheck()
    else:
        common.run_with_write_lock(main, "清洗分块")
