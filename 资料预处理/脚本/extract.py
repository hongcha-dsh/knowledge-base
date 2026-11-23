# -*- coding: utf-8 -*-
"""卡 A-01 extract.py：从待入库文件提取文本。

输入：待入库\\{文件}（.docx / .pdf / .txt / .md / .xlsx）
处理：
  - docx → 三层遍历（段落 + 表格 + 文本框），表格转 Markdown 表，图片输出占位行，
           "列=语义"的对齐排版段落聚成代码块（保留原始空白/制表符）；
           文本框版飞星盘按原文顺序排 3×3 md 表（逐格保真；原书未含宫位信息，
           行列不代表真实方位；8 格时第 9 格留空；合并失败保留平铺）
  - pdf  → PyMuPDF 尝试文本层；有文本层优先（含八字/飞星盘特判，一行不动）；
           无文本层按 4.8 矩阵分流（--ocr 三态 on|off|auto）：
             [MinerU 版面优选 开] + auto → MinerU 版面优选（is_ocr=False，全员）
             [MinerU 版面优选 开] + on   → MinerU 云端 OCR（is_ocr=True，>200 页自动分卷合并）
            [开关关] 或 off             → 文本层提取（产出近空），不阻挡不烧额度
          MinerU 不可用/失败 → 有文本层则退回文本层通道并打出原因，无文本层（扫描书）
          直接红灯报真实原因（不产空页）；闸门① diff 黄灯 → 自动退回文本层通道重跑；
           闸门① 字符 diff 数据写 输出\\提取缓存\\{书号}\\闸门数据.json（clean_split 汇总）
  - txt  → 直读
  - md   → 按纯文本通道处理（比照 txt 按空行分段；分页标记识别列入待办，本次不做）
  - xlsx → openpyxl 转 Markdown 表
输出：输出\\提取缓存\\{书号}\\{前缀}_{全局序号:03d}.txt
      （段_正文 / 表_Markdown表 / 排_对齐代码块 / 页_NNN，全局序号=文档顺序）
      附带 结构统计.json（docx 表格/文本框/图片 与转写计数，供 A-02 结构红灯校验）
      附带 结构信号.jsonl（#20 原料级章节结构信号：docx 段落大纲级别；无信号清理旧文件）
退出码：0=成功，1=文件打不开/MinerU 任务失败/忠实度不符，2=部分成功（OCR 失败页或 MinerU 空页/缺图）
"""

import json
import re
import sys
from pathlib import Path

import common

NS_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS_V = "urn:schemas-microsoft-com:vml"
NS_MC = "http://schemas.openxmlformats.org/markup-compatibility/2006"
NS_WP = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"

IMG_PLACEHOLDER = "【图】（原书此处有插图，待视觉转写）"

# #20 原料级结构信号文件名（随提取缓存，与 mineru_client 同源命名）
_SIGNAL_FILE = "结构信号.jsonl"

_DEFAULT_TAB_TWIPS = 720  # Word 默认制表位 0.5 英寸
_CHAR_TWIPS = 240         # 12pt 全角字符宽度近似值
_HALF_TWIPS = 120         # 半角字符宽度

# 图片尺寸阈值（英寸）：小于此尺寸的 drawing/VML 图片视为装饰细线，不计为正文图
_IMG_MIN_W_IN = 0.1
_IMG_MIN_H_IN = 0.05
_EMU_PER_IN = 914400

# 飞星盘格子：`数字组 + 空格 + 单个卦运汉字`（如 '74 九'、'2 9 五'）→ `数字组<br>卦运汉字`
_STAR_CELL_PAT = re.compile(r"^([0-9][0-9 ]*?)\s+([一二三四五六七八九])$")
_STAR_NUM_ONLY = re.compile(r"^[0-9][0-9 ]*$")        # 纯数字组（原书缺运星汉字的格子）
_STAR_LABEL_ONLY = re.compile(r"^[一二三四五六七八九]$")  # 裸运星汉字（碎片段，属上一格）

# 八字排盘（PDF 文本层，按坐标重建）：
# 锚点「乾造/坤造」+ 四柱干支（年/月/日/时，x 对齐）+ 上方/同行十神行。
# 原书系统性错印：天干「己」印成「已」、地支「巳」印成「已」，故容错集合含「已」；
# 缺字以 ○/□ 表示，允许「○○」「○申」等残缺柱（天干位同样接受 ○/□）。
_BAZI_TIANGAN = "甲乙丙丁戊己庚辛壬癸已"
_BAZI_DIZHI = "子丑寅卯辰巳午未申酉戌亥○□已"
_BAZI_GZ_RE = re.compile(rf"[{_BAZI_TIANGAN}○□][{_BAZI_DIZHI}]")
_BAZI_ANCHOR_RE = re.compile(r"^(?:例\d*[:：]?|例如)?(?:乾造|坤造)(?:[：:]|[^乾造坤造]{0,6})?$")
_BAZI_SS = "枭才伤食杀官劫比印财日元"
_BAZI_SS_RE = re.compile(rf"^[{_BAZI_SS}]+$")
_BAZI_Y_CLUSTER = 3.0      # 同一物理行的 y 容差（词级 y 存在 ≤3px 抖动）
_BAZI_ROW_DIST = 60.0      # 十神/柱 跨行搜索的最大 y 距离
_BAZI_ALIGN_X = 45.0       # 十神字与柱 x 对齐容差


def _qn_w(tag):
    return "{%s}%s" % (NS_W, tag)


def _qn_v(tag):
    return "{%s}%s" % (NS_V, tag)


def _qn_mc(tag):
    return "{%s}%s" % (NS_MC, tag)


def _style_name_map(d) -> dict:
    """python-docx 样式表 → {styleId: 样式名}（样式 ID 可能为纯数字，仅作兜底解析用）。"""
    m = {}
    try:
        for s in d.styles:
            sid = getattr(s, "style_id", None)
            if sid:
                m[sid] = s.name or ""
    except Exception:
        pass
    return m


def _para_outline_level(p_element, style_map: dict):
    """段落大纲级别（#20 结构信号，0 基）：优先内联 w:pPr/w:outlineLvl（docx 样式 ID 不可靠，
    M0 实测梁湘润为纯数字 ID），缺失时回落标题样式名（Heading N / 标题 N → N-1）；均无返回 None。"""
    try:
        pPr = p_element.find(_qn_w("pPr"))
        if pPr is not None:
            o = pPr.find(_qn_w("outlineLvl"))
            if o is not None:
                val = (o.get(_qn_w("val")) or "").strip()
                if re.fullmatch(r"\d+", val):
                    return int(val)
            st = pPr.find(_qn_w("pStyle"))
            if st is not None:
                name = style_map.get(st.get(_qn_w("val")) or "", "")
                m = re.match(r"^\s*(?:Heading|标题)\s*([1-9])\b", name)
                if m:
                    return int(m.group(1)) - 1
    except Exception:
        pass
    return None


def _para_text(p_element, exclude_txbx: bool = False) -> str:
    """取 w:p 元素内文本：w:t 文字 + w:tab 制表符（渲染为 \t，供对齐行展开）。

    exclude_txbx=True 时跳过文本框内文字（段落主文本与文本框产物分离，防重复）。"""
    parts = []
    for el in p_element.iter():
        if exclude_txbx and _in_txbx(el):
            continue
        if el.tag == _qn_w("t"):
            parts.append(el.text or "")
        elif el.tag == _qn_w("tab"):
            parts.append("\t")
    return "".join(parts)


def _in_txbx(el) -> bool:
    """判断元素是否位于 w:txbxContent（文本框）内部。"""
    p = el.getparent()
    while p is not None:
        if p.tag == _qn_w("txbxContent"):
            return True
        p = p.getparent()
    return False


def _in_fallback(el) -> bool:
    """判断元素是否位于 mc:AlternateContent/mc:Fallback（旧版兼容分支）内。

    风险⑥：Word 文本框为兼容旧版会存两份（mc:Choice 新版 / mc:Fallback 旧版），
    必须只取 Choice，忽略 Fallback，否则每段文字被提取两遍。"""
    p = el.getparent()
    while p is not None:
        if p.tag == _qn_mc("Fallback"):
            return True
        p = p.getparent()
    return False


def _star_pan_kind(paras: list) -> bool:
    """粗判文本框段落组是否为一整张文本框版飞星盘。

    特征：8~14 个非空段；每段只能是「数字组+运星汉字 / 纯数字组 / 裸运星汉字」三种
    形态之一；≥6 段带运星汉字。"""
    if not (8 <= len(paras) <= 14):
        return False
    labeled = 0
    for p in paras:
        if _STAR_CELL_PAT.match(p):
            labeled += 1
        elif _STAR_NUM_ONLY.match(p) or _STAR_LABEL_ONLY.match(p):
            pass
        else:
            return False
    return labeled >= 6


def _star_merge_cells(paras: list):
    """文本框段落 → 9 格 [[数字组, 运星汉字|None]]。碎片裸汉字并入前面最近的无标格；
    无格可并或段落形态不符返回 None。"""
    cells = []
    for p in paras:
        m = _STAR_CELL_PAT.match(p)
        if m:
            cells.append([m.group(1), m.group(2)])
            continue
        if _STAR_NUM_ONLY.match(p):
            cells.append([p, None])
            continue
        if _STAR_LABEL_ONLY.match(p):
            for prev in reversed(cells):
                if prev[1] is None:
                    prev[1] = p
                    break
            else:
                return None
            continue
        return None
    return cells


def _star_table_seq(paras: list):
    """文本框版飞星盘 → 按原文段落顺序排 3×3 md 表（与表格版格式一致）。

    诊断结论（2026-08-05）：文本框盘为 Word 浮动对象，段落顺序无位置语义，真实宫位
    无法确定（实测相邻表格盘定运仅覆盖 11/150）。故**按原文顺序**逐格排入九宫表：
    每个格子的数字与运星逐格保真（不补字、不删字），但行列**不代表真实方位**。
    合并后 8~9 格才排表（8 格时第 9 格留空）；否则逐段原样平铺（保真优先）。"""
    cells = _star_merge_cells(paras)
    if cells is None or not (8 <= len(cells) <= 9):
        return None
    grid = [cells[i] if i < len(cells) else None for i in range(9)]
    rows = []
    for r in range(3):
        row = []
        for c in range(3):
            cell = grid[r * 3 + c]
            if cell is None:
                row.append("")
            elif cell[1]:
                row.append(f"{cell[0]}<br>{cell[1]}")
            else:
                row.append(cell[0])
        rows.append("| " + " | ".join(row) + " |")
    return "\n".join([rows[0], "|---|---|---|", rows[1], rows[2]])


def _collect_textboxes(p_element) -> list:
    """段落内文本框内容（VML v:textbox → w:txbxContent）。返回 [(kind, text)]。

    按风险⑥去重：仅取 mc:Choice 分支，忽略 mc:Fallback 兜底分支。
    kind="pan"：文本框版飞星盘（payload=按原顺序排的 3×3 md 表，见 _star_table_seq；
                排表失败时保留逐段平铺）；
    kind="text"：普通文本框（payload=平铺文本，_star_format 已逐段应用）。"""
    items = []
    for tbx in p_element.iter(_qn_w("txbxContent")):
        if _in_fallback(tbx):
            continue
        raw = []
        for tp in tbx.iter(_qn_w("p")):
            t = _para_text(tp).strip()
            if t:
                raw.append(t)
        if not raw:
            continue
        if _star_pan_kind(raw):
            rebuilt = _star_table_seq(raw)
            if rebuilt is not None:
                items.append(("pan", rebuilt))
            else:
                items.append(("text", "\n".join(_star_format(t) for t in raw)))
        else:
            items.append(("text", "\n".join(_star_format(t) for t in raw)))
    return items


def _has_image(p_element) -> bool:
    """段落是否含正文图片（w:drawing 或非文本框的 w:pict）。文本框内/兼容分支/装饰细线不算。"""
    for el in p_element.iter():
        if _in_txbx(el) or _in_fallback(el):
            continue
        if el.tag == _qn_w("drawing"):
            if _drawing_size_ok(el):
                return True
        if el.tag == _qn_w("pict"):
            if not el.findall(".//" + _qn_v("textbox")):
                if _vml_size_ok(el):
                    return True
    return False


def _vml_size_ok(pict_el) -> bool:
    """VML pict 图片尺寸检查（从 v:shape style 取宽高）。"""
    shape = pict_el.find(".//" + _qn_v("shape"))
    if shape is None:
        return True
    style = shape.get("style", "")
    w = _parse_style_dim(style, "width")
    h = _parse_style_dim(style, "height")
    if w is None or h is None:
        return True
    return w >= _IMG_MIN_W_IN and h >= _IMG_MIN_H_IN


def _count_element_images(container) -> int:
    """统计容器内正文图片元素数（文本框内/兼容分支/装饰细线排除；drawing 与 pict 同图不合并）。"""
    cnt = 0
    for el in container.iter():
        if _in_txbx(el) or _in_fallback(el):
            continue
        if el.tag == _qn_w("drawing"):
            if _drawing_size_ok(el):
                cnt += 1
        elif el.tag == _qn_w("pict"):
            if not el.findall(".//" + _qn_v("textbox")):
                if _vml_size_ok(el):
                    cnt += 1
    return cnt


def _star_format(text: str) -> str:
    """飞星盘格子格式：`数字组 + 空格 + 单个卦运汉字` → `数字组<br>卦运汉字`。

    用户要求"汉字在阿拉伯数字下面"，即九宫格每格数字与卦运分行。
    仅当整段形如「数字组 + 单卦运汉字」时转换：'74 九'→'74<br>九'、'2 9 五'→'2 9<br>五'；
    '65'（无汉字）、'二十四山'、多段文本等不转换。"""
    m = _STAR_CELL_PAT.match(text.strip())
    if m:
        return f"{m.group(1)}<br>{m.group(2)}"
    return text


def _char_units(ch: str) -> float:
    """字符半角宽度单位：全角≈2，半角≈1。"""
    return 2.0 if ord(ch) > 0x2E7F else 1.0


def _drawing_size_ok(drawing_el) -> bool:
    """w:drawing 图片尺寸检查：过小（装饰细线，如 0.2×0.02 英寸）不算正文图。

    全书大量此类细线（约 390 张）被误当插图输出【图】占位符，实际无内容。"""
    ext = drawing_el.find(".//{%s}extent" % NS_WP)
    if ext is None:
        return True  # 无尺寸信息则保守保留
    try:
        cx = int(ext.get("cx"))
        cy = int(ext.get("cy"))
    except (TypeError, ValueError):
        return True
    return cx >= _IMG_MIN_W_IN * _EMU_PER_IN and cy >= _IMG_MIN_H_IN * _EMU_PER_IN


def _parse_style_dim(style: str, key: str):
    """从 VML style（如 'width:80pt;height:30pt'）提取尺寸（英寸），失败返回 None。"""
    m = re.search(key + r"\s*:\s*([\d.]+)\s*(pt|px)?", style)
    if not m:
        return None
    val = float(m.group(1))
    unit = m.group(2)
    if unit == "pt":
        return val / 72.0
    if unit == "px":
        return val / 96.0
    return val  # 无单位按 twips 处理



def _expand_tabs(text: str, p_element) -> str:
    """按段落 tab_stops 把 \t 展开成空格（风险⑧）。

    制表位位置（w:pos）以 twips 计；到下一制表位的差 ÷240 换算为等宽空格数。
    无显式制表位时按 Word 默认 0.5 英寸（720 twips）递进。"""
    stops = []
    for tab in p_element.iter(_qn_w("tab")):
        try:
            pos = int(tab.get(_qn_w("pos")) or 0)
        except (TypeError, ValueError):
            pos = 0
        if pos > 0:
            stops.append(pos)
    stops = sorted(set(stops))
    cur = 0.0
    out = []
    for ch in text:
        if ch == "\t":
            cur_twips = int(cur * _HALF_TWIPS)
            target = next((s for s in stops if s > cur_twips), None)
            if target is None:
                target = ((cur_twips // _DEFAULT_TAB_TWIPS) + 1) * _DEFAULT_TAB_TWIPS
            n = max(1, round((target - cur_twips) / _CHAR_TWIPS))
            out.append(" " * n)
            cur += n  # 每个空格=半角宽 1 单位
        else:
            out.append(ch)
            cur += _char_units(ch)
    return "".join(out)


def _is_align_para(text: str) -> bool:
    """对齐排版启发式：含制表符或 ≥4 个连续半角空格（"列=语义"排版特征）。"""
    if not text:
        return False
    if "\t" in text:
        return True
    if re.search(r" {4,}", text):
        return True
    return False


def _table_to_md(tbl_el, stats: dict = None) -> str:
    """w:tbl → Markdown 表格。单元格多段用 <br> 连接，含 | 转义，合并单元格去重。

    嵌套表格并入父单元格文本（风险⑤），存在时向 stats["嵌套表格"] 累计告警计数。"""
    from docx.table import Table

    table = Table(tbl_el, None)
    nested_count = 0
    rows = []
    for row in table.rows:
        cells = []
        seen = set()
        for cell in row.cells:
            tc = cell._tc
            if id(tc) in seen:
                continue
            seen.add(id(tc))
            parts = [p.text.strip().replace("\n", "<br>")
                     for p in cell.paragraphs if p.text.strip()]
            for sub in cell.tables:
                nested_count += 1
                for srow in sub.rows:
                    sc = [c.text.strip() for c in srow.cells if c.text.strip()]
                    if sc:
                        parts.append(" · ".join(sc))
            text = "<br>".join(_star_format(p) for p in parts).replace("|", "\\|")
            cells.append(text)
        rows.append(cells)
    if stats is not None and nested_count:
        stats["嵌套表格"] = stats.get("嵌套表格", 0) + nested_count
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    header = rows[0] + [""] * (width - len(rows[0]))
    lines = ["| " + " | ".join(header) + " |"]
    lines.append("|" + "|".join(["---"] * width) + "|")
    for r in rows[1:]:
        r = r + [""] * (width - len(r))
        lines.append("| " + " | ".join(r) + " |")
    return "\n".join(lines)


def _extract_docx(path: Path) -> tuple:
    """docx → (产物列表[(文件名, 文本)], 结构统计 dict, 结构信号列表)。

    按 body 元素顺序遍历段落/表格；段落内文本框（VML）并入正文；图片输出占位行；
    对齐排版段聚成代码块；表格转 Markdown。全局序号保证文档顺序可还原。
    #20：段落大纲级别（内联 outlineLvl 优先、标题样式名兜底）采集为结构信号，原文行为不变。
    """
    try:
        import docx
    except ImportError:
        raise RuntimeError("缺少 python-docx 依赖，请重新双击 启动控制台.vbs（缺依赖会自动安装）")
    d = docx.Document(str(path))
    body = d.element.body
    style_map = _style_name_map(d)

    stats = {"表格": 0, "文本框": 0, "图片": 0, "表格转写": 0, "文本框转写": 0, "图片占位": 0,
             "嵌套表格": 0, "飞星盘排表": 0}
    products = []
    signals = []
    seq = 0

    def add(name_prefix: str, text: str) -> str:
        nonlocal seq
        seq += 1
        name = f"{name_prefix}_{seq:03d}.txt"
        products.append((name, text))
        return name

    def flush_align(buf: list):
        if buf:
            # 对齐排版段 → 代码块包裹（原文.md 保行首空格、禁用折行，见清单阶段一-4）
            add("排", "```text\n" + "\n".join(buf) + "\n```")
            buf.clear()

    align_buf = []
    for child in body.iterchildren():
        tag = child.tag
        if tag == _qn_w("p"):
            t = _para_text(child, exclude_txbx=True)
            textboxes = _collect_textboxes(child)
            img = _has_image(child)
            if _is_align_para(t):
                align_buf.append(_expand_tabs(t, child).rstrip())
            else:
                flush_align(align_buf)
                if t.strip():
                    fname = add("段", t.strip())
                    lv = _para_outline_level(child, style_map)
                    if lv is not None:
                        signals.append({"级别": lv, "标题": t.strip(),
                                        "来源文件": fname, "来源序号": 1})
            for kind, payload in textboxes:
                flush_align(align_buf)
                stats["文本框"] += 1
                stats["文本框转写"] += 1
                if kind == "pan":
                    stats["飞星盘排表"] += 1
                    add("表", payload)
                else:
                    add("段", payload)
            if img:
                flush_align(align_buf)
                stats["图片"] += 1
                stats["图片占位"] += 1
                add("段", IMG_PLACEHOLDER)
        elif tag == _qn_w("tbl"):
            flush_align(align_buf)
            stats["表格"] += 1
            md = _table_to_md(child, stats)
            if md:
                stats["表格转写"] += 1
                add("表", md)
            tbl_imgs = _count_element_images(child)
            if tbl_imgs:
                stats["图片"] += tbl_imgs
                stats["图片占位"] += tbl_imgs
                for _ in range(tbl_imgs):
                    add("段", IMG_PLACEHOLDER)
    flush_align(align_buf)
    return products, stats, signals


def _extract_txt(path: Path) -> list:
    """txt → 按空行分段。"""
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    parts = [p.strip() for p in content.split("\n\n") if p.strip()]
    return parts or [content.strip()]


def _extract_md(path: Path) -> tuple:
    """md → (段落列表, 结构信号列表)（#20 M3）。

    按空行分段（比照 txt）；行首 `#{1,6}\\s` 行视为标题信号（级别＝`#` 个数），**剥离标记后
    原文写纯文本**——Markdown 标记属版面判断，不得混入原文层（回归逐字转录红线）。
    来源文件＝该段最终产出的 段_NNN.txt，来源序号＝段内行号。
    """
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    paragraphs = [p for p in content.split("\n\n") if p.strip()]
    parts = []
    signals = []
    for pi, raw in enumerate(paragraphs, start=1):
        out_lines = []
        for li, ln in enumerate(raw.strip().split("\n"), start=1):
            m = re.match(r"^(#{1,6})\s+(.*)$", ln.strip())
            title = (m.group(2).strip() if m else "")
            if m and title:
                signals.append({"级别": len(m.group(1)), "标题": title,
                                "来源文件": f"段_{pi:03d}.txt", "来源序号": li})
                out_lines.append(title)
            else:
                out_lines.append(ln)
        parts.append("\n".join(out_lines).strip())
    if not parts:
        parts = [content.strip()]
    return parts, signals


def _extract_xlsx(path: Path) -> list:
    """xlsx → 每个 sheet 转 Markdown 表。"""
    try:
        import openpyxl
    except ImportError:
        raise RuntimeError("缺少 openpyxl 依赖，请重新双击 启动控制台.vbs（缺依赖会自动安装）")
    wb = openpyxl.load_workbook(str(path), data_only=True)
    tables = []
    for ws in wb.worksheets:
        rows = []
        for row in ws.iter_rows(values_only=True):
            vals = ["" if v is None else str(v).strip() for v in row]
            if any(vals):
                rows.append(vals)
        if not rows:
            continue
        width = max(len(r) for r in rows)
        header = rows[0]
        header = header + [""] * (width - len(header))
        table = ["| " + " | ".join(header) + " |"]
        table.append("|" + "|".join(["---"] * width) + "|")
        for r in rows[1:]:
            r = r + [""] * (width - len(r))
            table.append("| " + " | ".join(r) + " |")
        tables.append(f"## Sheet：{ws.title}\n" + "\n".join(table))
    return tables or ["（空表格）"]


def _bazi_segs(text):
    """文本中形如「天干+地支」的干支对片段（含 ○/□ 容错），返回 [(起始字符偏移, 片段)]。"""
    out = []
    i = 0
    n = len(text)
    while i < n:
        m = _BAZI_GZ_RE.match(text[i:])
        if m:
            out.append((i, m.group()))
            i += 2
        else:
            i += 1
    return out


def _bazi_char_x(x0, x1, text, k):
    """词内第 k 字符的估算 x（按字宽均匀分布）。"""
    if not text:
        return x0
    return x0 + (x1 - x0) * k / len(text)


def _bazi_page_rows(page):
    """页 → 聚类行 [(y_key, [(x0, x1, text)])]，行内按 x 排序，行间按 y 升序。

    用 get_text("dict") 的行级 bbox 取坐标；y 按 _BAZI_Y_CLUSTER 容差聚类，
    消除同一物理行词的微小 y 抖动（如 632.2 vs 632.3 被拆成两行的问题）。"""
    d = page.get_text("dict")
    raw = []
    for block in d.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            bbox = line.get("bbox")
            if not bbox:
                continue
            text = "".join(sp.get("text", "") for sp in line.get("spans", [])).strip()
            if text:
                raw.append((round(bbox[1] / _BAZI_Y_CLUSTER), bbox[0], bbox[2], text))
    rows = {}
    for key, x0, x1, t in raw:
        rows.setdefault(key, []).append((x0, x1, t))
    out = []
    for key in sorted(rows):
        out.append((key, sorted(rows[key], key=lambda it: it[0])))
    return out


def _bazi_ss_chars(items):
    """十神行的字符列表 [(x, 字符)]。整词须为十神字；「乾造/坤造」前缀词剥离后
    剩余部分若为十神字（如「例如乾造：才」→ 才）也计入，x 按词内位置估算。"""
    out = []
    for x0, x1, t in items:
        n0 = len(t)
        off = 0
        if not _BAZI_SS_RE.match(t):
            m = re.match(r"^(?:例\d*[:：]?|例如)?(?:乾造|坤造)[：:]?", t)
            if m:
                off = m.end()
                t = t[m.end():]
                if not _BAZI_SS_RE.match(t):
                    continue
            else:
                continue
        if "日元" in t:
            rest = t.replace("日元", "")
            out.append((x0 + (x1 - x0) * off / n0, "日元"))
            for i, ch in enumerate(rest):
                out.append((x0 + (x1 - x0) * (off + len("日元") + i) / n0, ch))
        else:
            for i, ch in enumerate(t):
                out.append((x0 + (x1 - x0) * (off + i) / n0, ch))
    return out


def _bazi_rebuild_page(page):
    """单页八字排盘识别与重建。

    返回 (page_text, pan_count)：
      - 锚点行（含「乾造/坤造」）直收四柱；不足 4 柱时从下方 60px 内最近干支行补收；
        仍不足时把锚点行孤天干与后续行孤地支按 x 距离(<45px)合并成柱（如 庚+子=庚子）。
      - 无任何柱的锚点视为正文引用（伪锚点），不重建。
      - 十神行：锚点行自身 / 上方 60px 内最近十神行；十神字按 x 对齐到四柱列。
      - 排盘区域（十神行+锚点行+柱行）替换为 5 列 md 表：
        |  | 年柱 | 月柱 | 日柱 | 时柱 |
        | 乾造 | 壬辰 | 乙巳 | 甲子 | 乙丑 |
        | 十神 | 枭 | 劫 | 日元 | 劫 |
    """
    rows = _bazi_page_rows(page)
    # 重建每盘的柱与十神，记录待删除的 y_key 与 md 表
    blocks = []       # [(y_key, md_table)]
    del_keys = set()
    for li, (yA, row) in enumerate(rows):
        anchors = [(x0, x1, t) for x0, x1, t in row
                   if _BAZI_ANCHOR_RE.match(re.sub(r"\s+", "", t))]
        if not anchors:
            continue
        # 柱：锚点行直收
        cols = []
        for x0, x1, t in row:
            for k, seg in _bazi_segs(t):
                cols.append((_bazi_char_x(x0, x1, t, k), seg))
        nxt = None
        nxt_key = None
        if len(cols) < 4:
            for yB, items in rows[li + 1:]:
                if (yB - yA) * _BAZI_Y_CLUSTER > _BAZI_ROW_DIST:
                    break
                if any(_bazi_segs(t) for _x0, _x1, t in items):
                    nxt = items
                    nxt_key = yB
                    break
            if nxt is not None:
                for x0, x1, t in nxt:
                    for k, seg in _bazi_segs(t):
                        cols.append((_bazi_char_x(x0, x1, t, k), seg))
        # 孤天干 + 后续行孤地支（如 庚 + 子 → 庚子）
        lone_key = None
        if len(cols) < 4:
            tg = None
            for x0, x1, t in row:
                for k, ch in enumerate(t):
                    if ch in _BAZI_TIANGAN and (k + 1 >= len(t) or t[k + 1] not in _BAZI_DIZHI):
                        tg = (_bazi_char_x(x0, x1, t, k), ch)
                        break
                if tg:
                    break
            if tg:
                for yB, items in rows[li + 1:]:
                    if (yB - yA) * _BAZI_Y_CLUSTER > _BAZI_ROW_DIST:
                        break
                    for x0, x1, t in items:
                        for k, ch in enumerate(t):
                            if ch in _BAZI_DIZHI and (k == 0 or t[k - 1] not in _BAZI_TIANGAN) \
                                    and abs(_bazi_char_x(x0, x1, t, k) - tg[0]) < _BAZI_ALIGN_X:
                                cols.append((tg[0], tg[1] + ch))
                                lone_key = yB
                                break
                        if lone_key is not None:
                            break
                    if lone_key is not None:
                        break
        if not cols:
            continue  # 伪锚点：正文中的「乾造/坤造」字样，无排盘
        cols.sort()
        pillars = [seg for _x, seg in cols[:4]]
        sex = re.match(r"^(?:例\d*[:：]?|例如)?(乾造|坤造)",
                       re.sub(r"\s+", "", anchors[0][2]))
        sex = sex.group(1) if sex else "乾造"
        # 十神：锚点行自身 / 上方 / 下方
        ss = None
        ss_key = None
        if any(_BAZI_SS_RE.match(t) for _x0, _x1, t in row):
            ss = _bazi_ss_chars(row)
            ss_key = yA
        else:
            for yB, items in reversed(rows[:li]):
                if (yA - yB) * _BAZI_Y_CLUSTER > _BAZI_ROW_DIST:
                    break
                if any(_BAZI_SS_RE.match(t) for _x0, _x1, t in items):
                    ss = _bazi_ss_chars(items)
                    ss_key = yB
                    break
        assigned = [None] * 4
        if ss:
            for sx, ch in ss:
                best_i, best_d = None, 1e9
                for i in range(min(len(cols), 4)):
                    d = abs(sx - cols[i][0])
                    if d < best_d:
                        best_d, best_i = d, i
                if best_i is not None and best_d < _BAZI_ALIGN_X and assigned[best_i] is None:
                    assigned[best_i] = ch
        heads = ["", "年柱", "月柱", "日柱", "时柱"]
        cells = [sex] + pillars
        gods = [""] + [s or "" for s in assigned]
        md = "\n".join([
            "| " + " | ".join(heads) + " |",
            "|---|---|---|---|---|",
            "| " + " | ".join(cells) + " |",
            "| " + " | ".join(gods) + " |",
        ])
        blocks.append((yA, md))
        del_keys.add(yA)
        if ss_key is not None:
            del_keys.add(ss_key)
        if nxt_key is not None:
            del_keys.add(nxt_key)
        if lone_key is not None:
            del_keys.add(lone_key)
    # 重建页面文本：锚点行位置插入 md 表（前后加空行，避免与批断/正文粘连），
    # 其余排盘区域行删除；批断等正文行保留为独立段落，不与命局表混为同一记录模式。
    lines = []
    block_map = {k: md for k, md in blocks}
    for y_key, items in rows:
        if y_key in block_map:
            if lines and lines[-1] != "":
                lines.append("")
            lines.extend(block_map[y_key].split("\n"))
            lines.append("")
            continue
        if y_key in del_keys:
            continue
        text = " ".join(t for _x0, _x1, t in items).strip()
        if text:
            lines.append(text)
    return "\n".join(lines), len(blocks)


def _extract_pdf_text(path: Path) -> tuple:
    """PDF 文本层提取（含八字排盘按坐标重建）。

    返回 (pages_text, has_text_layer, bazi_count)。"""
    try:
        import fitz
    except ImportError:
        raise RuntimeError("缺少 pymupdf 依赖，请重新双击 启动控制台.vbs（缺依赖会自动安装）")
    doc = fitz.open(str(path))
    pages = []
    total_chars = 0
    bazi_total = 0
    for page in doc:
        text, n = _bazi_rebuild_page(page)
        pages.append(text)
        bazi_total += n
        total_chars += len(text)
    avg = total_chars / max(len(pages), 1)
    has_text_layer = avg >= 30 and total_chars > 0
    return pages, has_text_layer, bazi_total


# 闸门①字符 diff 阈值（设计文档 §4.5）
_GATE1_SIM_OK = 0.98      # 全书按字符数加权相似度下限
_GATE1_PAGE_OK = 0.90     # 单页相似度下限
_GATE1_LOW_RATIO = 0.05   # 低相似页页数占比上限


def _decide_pdf_channel(ocr_mode: str, pref: bool, has_text_layer: bool) -> str:
    """4.8 矩阵分流决策（纯函数，供自检测试）。

    返回：
      pdf_text      有文本层 → 现状文本层提取（不变）
      mineru_auto   pref开 + auto + 无文本层 → MinerU 版面优选（is_ocr=False）
      mineru_ocr    pref开 + on   + 无文本层 → MinerU 云端 OCR（is_ocr=True）
      fallback_text pref关/ocr off → 现状文本层提取（产出近空，不阻挡不烧额度）
    """
    if has_text_layer:
        return "pdf_text"
    if not pref or ocr_mode == "off":
        return "fallback_text"
    if ocr_mode == "on":
        return "mineru_ocr"
    return "mineru_auto"


def _gate1_char_diff(pdf_pages: list, mineru_products: list) -> dict:
    """闸门①字符 diff（仅文本层书适用，扫描书由调用方跳过）：双侧
    common.normalize_for_search 归一后逐页 difflib.SequenceMatcher。

    MinerU 空页在产物中缺席，故按文件名页号重建 {页号: 文本} 再按 PDF 页序对齐，
    缺失页按空串参与比对（PDF 有文而 MinerU 空 → 相似度 0 → 落低相似页）。
    返回 {加权相似度, 低相似页占比, 低相似页, 双侧页数}。
    """
    import difflib
    page_map = {}
    for name, text in mineru_products:
        m = re.match(r"页_(\d+)\.", name)
        if m:
            page_map[int(m.group(1))] = text
    np_ = len(pdf_pages)
    total_chars = 0
    weighted = 0.0
    low_pages = []
    for i, a in enumerate(pdf_pages, start=1):
        b = page_map.get(i, "")
        na = common.normalize_for_search(a)
        nb = common.normalize_for_search(b)
        n = max(len(na), len(nb))
        if n == 0:
            ratio = 1.0
        else:
            ratio = difflib.SequenceMatcher(None, na, nb).ratio()
        total_chars += n
        weighted += ratio * n
        if ratio < _GATE1_PAGE_OK:
            low_pages.append(i)
    return {"加权相似度": round(weighted / max(total_chars, 1), 4),
            "低相似页占比": round(len(low_pages) / max(np_, 1), 4),
            "低相似页": low_pages, "双侧页数": np_}


def _write_gate_data(book_id: str, gate1: dict):
    """落盘 输出\\提取缓存\\{书号}\\闸门数据.json（clean_split 汇总闸门报告时读取，只写闸门1节）。"""
    import json
    out_dir = common.get_path("输出") / "提取缓存" / book_id
    out_dir.mkdir(parents=True, exist_ok=True)
    p = out_dir / "闸门数据.json"
    data = {}
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data["闸门1"] = gate1
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_structure_signals(book_id: str, signals):
    """#20 原料级结构信号落盘（随提取缓存）。

    signals 为 None＝该通道不自管（MinerU 通道由 mineru_client 落盘）；为列表则非空写文件、
    空列表清理旧文件防陈旧（重建索引/换通道后不留残signal）。
    """
    if signals is None or not book_id:
        return
    sig_dir = common.get_path("输出") / "提取缓存" / book_id
    sig_path = sig_dir / _SIGNAL_FILE
    if signals:
        sig_dir.mkdir(parents=True, exist_ok=True)
        sig_path.write_text(
            "\n".join(json.dumps(s, ensure_ascii=False) for s in signals) + "\n",
            encoding="utf-8")
    elif sig_path.exists():
        sig_path.unlink()


def extract_file(filename: str, book_id: str, ocr: str = "off") -> tuple:
    """提取单个文件。返回 (书号, 产物列表, 失败页, 是否OCR, 结构统计或None, MinerU结果或None,
    结构信号列表或None)。

    ocr 三态（4.8 矩阵）：on=强制 MinerU OCR；auto=有文本层走文本层、无文本层且
    mineru_版面优选 开 则 MinerU（is_ocr=False）；off=强制现状通道（默认，不烧额度）。
    结构信号（#20）：文本类通道自管（docx 采集大纲级别；txt/md/xlsx/文本层 PDF 暂无＝空列表），
    MinerU 通道返回 None（由 mineru_client 落盘）。
    """
    input_dir = common.get_path("待入库")
    src = input_dir / filename
    if not src.exists():
        raise FileNotFoundError(f"文件不存在：{src}")

    suffix = src.suffix.lower()
    if suffix == ".docx":
        paras, stats, signals = _extract_docx(src)
        return book_id, paras, [], False, stats, None, signals
    elif suffix == ".txt":
        parts = _extract_txt(src)
        products = [(f"段_{i:03d}.txt", p) for i, p in enumerate(parts, start=1)]
        return book_id, products, [], False, None, None, []
    elif suffix == ".md":
        parts, md_signals = _extract_md(src)
        products = [(f"段_{i:03d}.txt", p) for i, p in enumerate(parts, start=1)]
        return book_id, products, [], False, None, None, md_signals
    elif suffix == ".xlsx":
        tables = _extract_xlsx(src)
        products = [(f"表_{i:03d}.txt", t) for i, t in enumerate(tables, start=1)]
        return book_id, products, [], False, None, None, []
    elif suffix == ".pdf":
        pages, has_text_layer, bazi_total = _extract_pdf_text(src)
        cfg = common.load_config()
        pref = bool(cfg.get("mineru_版面优选", True))
        channel = _decide_pdf_channel(ocr, pref, has_text_layer)
        if channel in ("pdf_text", "fallback_text"):
            products = [(f"页_{i:03d}.txt", p) for i, p in enumerate(pages, start=1)]
            stats = {"八字排盘": bazi_total} if bazi_total and channel == "pdf_text" else None
            _write_gate_data(book_id, {"通道": channel, "后端": "pymupdf",
                                       "已退回文本层": False, "加权相似度": None,
                                       "低相似页占比": None, "低相似页": []})
            return book_id, products, [], False, stats, None, []
        import mineru_client
        try:
            result = mineru_client.run_ocr(src, book_id, common.clean_book_title(filename),
                                           is_ocr=(channel == "mineru_ocr"))
        except Exception as e:
            _write_gate_data(book_id, {"通道": channel, "后端": "mineru(失败退回)",
                                       "已退回文本层": True, "加权相似度": None,
                                       "低相似页占比": None, "低相似页": [],
                                       "失败原因": str(e)})
            if not has_text_layer:
                raise RuntimeError(f"MinerU 通道失败，且本书无文本层可退回：{e}")
            print(f"  🟡 MinerU 通道失败（{e}），已退回文本层通道")
            products = [(f"页_{i:03d}.txt", p) for i, p in enumerate(pages, start=1)]
            return book_id, products, [], False, None, None, []
        if not has_text_layer:
            gate1 = {"加权相似度": None, "低相似页占比": None, "低相似页": [],
                     "双侧页数": 0, "通道": channel, "后端": "mineru",
                     "已退回文本层": False,
                     "不适用": "扫描书无文本层参照（MinerU 页级自判取字），闸门①跳过"}
            _write_gate_data(book_id, gate1)
            result["_gate1"] = gate1
            return book_id, result["products"], [], True, None, result, None
        gate1 = _gate1_char_diff(pages, result["products"])
        retreat = (gate1["加权相似度"] < _GATE1_SIM_OK
                   or gate1["低相似页占比"] > _GATE1_LOW_RATIO)
        gate1["通道"] = channel
        gate1["后端"] = "mineru"
        gate1["已退回文本层"] = retreat
        _write_gate_data(book_id, gate1)
        result["_gate1"] = gate1
        if retreat:
            print(f"  🟡 内容比对警告（加权相似度 {gate1['加权相似度']}，"
                  f"低相似页 {gate1['低相似页占比']:.1%}），已自动退回文字层通道重跑")
            products = [(f"页_{i:03d}.txt", p) for i, p in enumerate(pages, start=1)]
            return book_id, products, [], False, None, result, []
        return book_id, result["products"], [], True, None, result, None
    else:
        raise ValueError(f"不支持的文件格式：{suffix}（支持 .docx / .pdf / .txt / .md / .xlsx）")


def main():
    args = sys.argv[1:]
    filename = None
    book_id = None
    ocr = "off"
    for i, a in enumerate(args):
        if a == "--文件" and i + 1 < len(args):
            filename = args[i + 1]
        elif a == "--书号" and i + 1 < len(args):
            book_id = args[i + 1]
        elif a == "--ocr" and i + 1 < len(args):
            ocr = args[i + 1] if args[i + 1] in ("on", "off", "auto") else "off"

    if not filename:
        print("用法：python extract.py --文件 <待入库文件名> [--书号 <书号>] [--ocr on|off|auto]")
        sys.exit(1)

    if not book_id:
        book_id = common.gen_book_id(filename)

    try:
        bid, products, failed, used_ocr, stats, mineru, signals = extract_file(filename, book_id, ocr)
    except FileNotFoundError as e:
        common.write_report(f"{common.clean_book_title(filename)}_提取.md",
                            f"# 提取报告：{filename}\n结论：🔴 失败\n原因：{e}\n")
        print(e)
        sys.exit(1)
    except (ValueError, RuntimeError) as e:
        common.write_report(f"{common.clean_book_title(filename)}_提取.md",
                            f"# 提取报告：{filename}\n结论：🔴 失败\n原因：{e}\n")
        print(e)
        sys.exit(1)

    out_dir = common.get_path("输出") / "提取缓存" / bid
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, text in products:
        with open(out_dir / name, "w", encoding="utf-8") as f:
            f.write(text)
    # #20 原料级结构信号落盘（文本类通道自管；MinerU 通道由 mineru_client 落盘）
    _write_structure_signals(bid, signals)
    if signals:
        print(f"  #20 结构信号：{len(signals)} 条（docx 大纲级别/标题样式）")
    if stats:
        import json
        (out_dir / "结构统计.json").write_text(
            json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
        if stats.get("嵌套表格", 0):
            print(f"  [告警] 发现嵌套表格 {stats['嵌套表格']} 处，已并入父单元格文本")

    total_chars = sum(len(t) for _, t in products)
    print(f"提取完成：{filename} → {bid}，共 {len(products)} 个文件，{total_chars} 字"
          + ("（OCR 通道）" if used_ocr else ""))
    if stats:
        print(f"结构统计：表格 {stats.get('表格', 0)}/{stats.get('表格转写', 0)}，文本框 {stats.get('文本框', 0)}/{stats.get('文本框转写', 0)}，"
              f"图片占位 {stats.get('图片占位', 0)}（{stats.get('图片', 0)}），"
              f"飞星盘排表 {stats.get('飞星盘排表', 0)}，八字排盘 {stats.get('八字排盘', 0)}")
    # #14 全绿不留档：跑到这里即提取成功，清除旧报告（非绿分支在下方各自写报告）
    common.clear_report(f"{common.clean_book_title(filename)}_提取.md")
    if mineru is not None:
        import mineru_client
        m_code = mineru.get("code", 0)
        gate1 = mineru.get("_gate1") or {}
        if m_code != 0 or gate1.get("已退回文本层"):
            common.write_report(f"{common.clean_book_title(filename)}_提取.md",
                                mineru_client.report_text(filename, bid, mineru))
        if m_code == 1:
            print("🔴 MinerU 提取失败：忠实度自检不通过（见报告）")
            sys.exit(1)
        if gate1.get("不适用"):
            print("  ⚪ 内容比对跳过：扫描书无文字层参照（MinerU 页级自判取字）")
        if gate1.get("已退回文本层"):
            print(f"🟡 内容比对未通过（与文字层相似度不足），已自动退回文字层通道重跑"
                  f"（低相似页 {gate1.get('低相似页')}，见检查报告）")
            sys.exit(2)
        if m_code == 2:
            print(f"🟡 MinerU 提取部分成功：空页 {len(mineru['report'].get('空页清单', []))} 个，"
                  f"缺图 {len(mineru['report'].get('图对账', {}).get('缺图', []))} 张（见报告）")
            sys.exit(2)
        sys.exit(0)
    if failed:
        common.write_report(f"{common.clean_book_title(filename)}_提取.md",
                            f"# 提取报告：{filename}\n结论：🟡 部分成功\n"
                            f"OCR 失败页：{failed}\n产物：{out_dir}\n")
        print(f"OCR 失败页：{failed}")
        sys.exit(2)
    sys.exit(0)


def _selfcheck():
    """验收：构造含 段落/表格/文本框(VML)/图片/对齐排版 的 docx，断言四类产物与结构统计正确。"""
    import tempfile
    import docx as docx_lib
    from docx.oxml import parse_xml

    tmp = Path(tempfile.mkdtemp(prefix="extract_selfcheck_"))
    sample = tmp / "测试样本.docx"
    d = docx_lib.Document()
    d.add_paragraph("玄空之学，首重理气。阴阳五行之气运行于天地之间。")
    d.add_paragraph("挨星之法，以九星配九宫，察其生旺衰死。")
    d.add_paragraph("食\t杀\t日元\t才")
    d.add_paragraph("己卯\t癸酉\t丁丑\t庚")
    # #20 结构信号样例：标题样式名（兜底）+ 段落内联 outlineLvl（优先）
    d.add_heading("第二章 分论", level=2)
    lvl_para = d.add_paragraph("第三章 玄空要旨")
    lvl_para._p.get_or_add_pPr().append(parse_xml(
        '<w:outlineLvl xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
        ' w:val="0"/>'))
    t = d.add_table(rows=2, cols=2)
    t.cell(0, 0).text = "85\n六"
    t.cell(0, 1).text = "41 二"
    t.cell(1, 0).text = "74 五"
    t.cell(1, 1).text = "96\n七"
    vml_p = parse_xml(
        '<w:p xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
        'xmlns:v="urn:schemas-microsoft-com:vml">'
        '<w:r><w:pict>'
        '<v:shape style="width:80pt;height:30pt">'
        '<v:textbox><w:txbxContent>'
        '<w:p><w:r><w:t>文本框内容甲</w:t></w:r></w:p>'
        '</w:txbxContent></v:textbox>'
        '</v:shape></w:pict></w:r></w:p>'
    )
    d.element.body.append(vml_p)
    # 风险⑥：AlternateContent 双份（Choice 新版 / Fallback 旧版），只能取一遍
    ac_p = parse_xml(
        '<w:p xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
        'xmlns:v="urn:schemas-microsoft-com:vml" '
        'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006">'
        '<mc:AlternateContent>'
        '<mc:Choice Requires="wps">'
        '<w:r><w:pict><v:shape style="width:80pt;height:30pt">'
        '<v:textbox><w:txbxContent><w:p><w:r><w:t>文本框内容乙</w:t></w:r></w:p>'
        '</w:txbxContent></v:textbox></v:shape></w:pict></w:r>'
        '</mc:Choice>'
        '<mc:Fallback>'
        '<w:r><w:pict><v:shape style="width:80pt;height:30pt">'
        '<v:textbox><w:txbxContent><w:p><w:r><w:t>文本框内容乙</w:t></w:r></w:p>'
        '</w:txbxContent></v:textbox></v:shape></w:pict></w:r>'
        '</mc:Fallback>'
        '</mc:AlternateContent>'
        '</w:p>'
    )
    d.element.body.append(ac_p)
    img_p = parse_xml(
        '<w:p xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
        'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing">'
        '<w:r><w:drawing>'
        '<wp:inline><wp:extent cx="2000000" cy="2000000"/><wp:docPr id="1" name="图1"/>'
        '</wp:inline></w:drawing></w:r></w:p>'
    )
    d.element.body.append(img_p)
    # 装饰细线（0.2×0.02 英寸）：必须不计为正文图
    tiny_p = parse_xml(
        '<w:p xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
        'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing">'
        '<w:r><w:drawing>'
        '<wp:inline><wp:extent cx="180022" cy="20002"/><wp:docPr id="2" name="细线"/>'
        '</wp:inline></w:drawing></w:r></w:p>'
    )
    d.element.body.append(tiny_p)
    d.save(str(sample))

    bid = "测试书01"
    input_dir = common.get_path("待入库")
    input_dir.mkdir(parents=True, exist_ok=True)
    import shutil
    saved = input_dir / "测试样本.docx"
    shutil.copy(sample, saved)
    md_sample = tmp / "测试样本.md"
    md_sample.write_text(
        "# 第一章 总论\n\n玄空之学，首重理气。\n## 第一节 阴阳\n\n| 日 | 月 |\n|---|---|\n| 甲 | 乙 |",
        encoding="utf-8")
    saved_md = input_dir / "测试样本.md"
    shutil.copy(md_sample, saved_md)
    saved_pdf = None
    try:
        import fitz as _fitz
        blank_pdf = tmp / "空白样本.pdf"
        bd = _fitz.open()
        for _ in range(2):
            bd.new_page()
        bd.save(str(blank_pdf))
        bd.close()
        saved_pdf = input_dir / "空白样本.pdf"
        shutil.copy(blank_pdf, saved_pdf)
    except Exception as e:
        print(f"  ⚠ pymupdf 不可用，跳过空白 PDF 通道用例：{e}")

    try:
        _, products, _, _, stats, _, signals = extract_file("测试样本.docx", bid)
        names = [n for n, _ in products]
        all_text = "\n".join(t for _, t in products)
        print(f"  [调试] names={names}")
        print(f"  [调试] 文本片段={[t[:20] for n, t in products]}")
        ok = True
        ok = ok and any(n.startswith("表_") for n in names)
        ok = ok and any(n.startswith("排_") for n in names)
        ok = ok and any(n.startswith("段_") for n in names)
        ok = ok and any("文本框内容甲" in t for _, t in products)
        ok = ok and sum(t.count("文本框内容乙") for _, t in products) == 1
        ok = ok and any("【图】" in t for _, t in products)
        table_md = [t for n, t in products if n.startswith("表_")][0]
        ok = ok and "<br>" in table_md and "|" in table_md
        ok = ok and "85<br>六" in table_md and "41<br>二" in table_md and "74<br>五" in table_md
        ok = ok and stats["表格"] == 1 and stats["表格转写"] == 1
        ok = ok and stats["文本框"] == 2 and stats["文本框转写"] == 2
        ok = ok and stats["图片"] == 1 and stats["图片占位"] == 1
        ok = ok and sum("【图】" in t for _, t in products) == 1  # 细线不计图
        # #20 docx 结构信号：内联 outlineLvl=0 与标题样式 Heading2（→级别 1）双通道采集，且锚定已产段文件
        ok = ok and any(s["级别"] == 0 and s["标题"] == "第三章 玄空要旨" for s in signals)
        ok = ok and any(s["级别"] == 1 and s["标题"] == "第二章 分论" for s in signals)
        ok = ok and all(s["来源文件"] in names for s in signals)
        print(f"  #20 docx 结构信号：{len(signals)} 条 {[(s['级别'], s['标题']) for s in signals]}")
        # #20 结构信号落盘 + 空信号清理陈旧（防重建索引/换通道留残signal）
        _write_structure_signals(bid, signals)
        sig_path = common.get_path("输出") / "提取缓存" / bid / "结构信号.jsonl"
        ok = ok and sig_path.exists() and len(sig_path.read_text(encoding="utf-8").strip().splitlines()) == len(signals)
        _write_structure_signals(bid, [])
        ok = ok and not sig_path.exists()
        align_txt = [t for n, t in products if n.startswith("排_")]
        ok = ok and len(align_txt) == 1 and align_txt[0].startswith("```text")
        _, md_products, _, _, _, _, md_signals = extract_file("测试样本.md", "测试书02")
        md_ok = len(md_products) == 3 and all(n.startswith("段_") for n, _ in md_products)
        md_ok = md_ok and md_products[0][1].startswith("第一章")
        # #20 M3：md `#` 行信号化——标记剥离（原文不含 #）+ 级别＝# 个数 + 锚定段文件
        md_text = "\n".join(t for _, t in md_products)
        md_ok = md_ok and "#" not in md_text
        md_ok = md_ok and any(s["级别"] == 1 and s["标题"] == "第一章 总论" for s in md_signals)
        md_ok = md_ok and any(s["级别"] == 2 and s["标题"] == "第一节 阴阳" for s in md_signals)
        md_ok = md_ok and all(s["来源文件"] in [n for n, _ in md_products] for s in md_signals)
        ok = ok and md_ok
        print(f"  md 通道：{len(md_products)} 个段文件（{md_products[0][0]}…{md_products[-1][0]}），"
              f"#20 结构信号 {len(md_signals)} 条 {[(s['级别'], s['标题']) for s in md_signals]}")

        # ── M2.2/2.3 用例：4.8 矩阵决策 + 闸门① diff + 空白 PDF fallback 通道 ──
        import json as _json
        matrix = [
            ("auto", True, True, "pdf_text"),
            ("auto", True, False, "mineru_auto"),
            ("on", True, True, "pdf_text"),
            ("on", True, False, "mineru_ocr"),
            ("off", True, False, "fallback_text"),
            ("off", False, False, "fallback_text"),
            ("auto", False, False, "fallback_text"),
            ("on", False, False, "fallback_text"),
            ("off", False, True, "pdf_text"),
        ]
        for ocr_mode, pref, has_layer, expect in matrix:
            got = _decide_pdf_channel(ocr_mode, pref, has_layer)
            if got != expect:
                print(f"  ❌ 4.8 矩阵 {ocr_mode}/{pref}/{has_layer} → {got}（期望 {expect}）")
                ok = False

        same_pdf = [f"第{i}页 玄空之学首重理气" for i in range(1, 6)]
        same_min = [(f"页_{i:03d}.txt", t) for i, t in enumerate(same_pdf, start=1)]
        g1 = _gate1_char_diff(same_pdf, same_min)
        if g1["加权相似度"] != 1.0 or g1["低相似页"]:
            print(f"  ❌ diff 一致页：{g1}")
            ok = False

        gap_pdf = [f"第{i}页 玄空之学术数秘旨" for i in range(1, 5)]
        gap_min = [(f"页_001.txt", "第1页 玄空之学术数秘旨"),
                   (f"页_003.txt", "第3页 玄空之学术数秘旨"),
                   (f"页_004.txt", "完全不同的内容完全不相关")]
        g2 = _gate1_char_diff(gap_pdf, gap_min)
        if g2["低相似页"] != [2, 4]:
            print(f"  ❌ diff 缺页/异页：{g2}")
            ok = False

        empty_pdf = ["", "第2页 内容正文", "第3页 内容正文"]
        empty_min = [(f"页_002.txt", "第2页 内容正文")]
        g3 = _gate1_char_diff(empty_pdf, empty_min)
        if g3["低相似页"] != [3]:
            print(f"  ❌ diff 空页对齐：{g3}")
            ok = False

        if saved_pdf:
            _, pdf_products, _, _, _, _, _ = extract_file("空白样本.pdf", "测试书03", "off")
            gd_path = common.get_path("输出") / "提取缓存" / "测试书03" / "闸门数据.json"
            g1d = _json.loads(gd_path.read_text(encoding="utf-8")).get("闸门1", {})
            if len(pdf_products) != 2 or g1d.get("通道") != "fallback_text" \
                    or g1d.get("后端") != "pymupdf":
                print(f"  ❌ 空白 PDF fallback 通道：{len(pdf_products)} 产物，闸门 {g1d}")
                ok = False

            # 扫描书（无文本层）走 MinerU auto 时：闸门①必须跳过、不得误退回文本层
            from unittest.mock import patch
            fake_mineru = {"code": 0,
                           "products": [(f"页_{i:03d}.txt", f"扫描书第{i}页魔法骑士规则正文内容")
                                        for i in range(1, 3)],
                           "report": {"书号": "测试书M", "书名": "扫描样本"}}
            with patch("mineru_client.run_ocr", return_value=fake_mineru):
                _, scan_products, _, scan_ocr, _, scan_result, _ = \
                    extract_file("空白样本.pdf", "测试书M", "auto")
            gd_path_m = common.get_path("输出") / "提取缓存" / "测试书M" / "闸门数据.json"
            g1_m = _json.loads(gd_path_m.read_text(encoding="utf-8")).get("闸门1", {})
            if not scan_ocr or len(scan_products) != 2 or g1_m.get("已退回文本层") \
                    or not g1_m.get("不适用") or not scan_result.get("_gate1", {}).get("不适用") \
                    or "扫描书第1页" not in scan_products[0][1]:
                print(f"  ❌ 扫描书 MinerU auto 不误退回：产物 {len(scan_products)}，"
                      f"ocr {scan_ocr}，闸门 {g1_m}")
                ok = False

            # 扫描书 MinerU 失败：必须红灯报真实原因，不得静默退回产空页（#18 回归）
            with patch("mineru_client.run_ocr",
                       side_effect=RuntimeError("MinerU 解析失败：number of pages exceeds limit (200 pages)")):
                try:
                    extract_file("空白样本.pdf", "测试书F", "auto")
                    print("  ❌ 扫描书 MinerU 失败未拦截：仍产出了文本层产物")
                    ok = False
                except RuntimeError as e:
                    if "200 pages" not in str(e):
                        print(f"  ❌ 扫描书失败原因丢失：{e}")
                        ok = False
        print(f"  M2 用例：矩阵 {len(matrix)} 组 + diff 3 组 + 空白 PDF 通道用例")
        print(f"  extract 自检：产物 {len(products)} 个文件，总字数 {len(all_text)}，统计 {stats}")
        print(f"  表格样例：{table_md.replace(chr(10), ' / ')}")
        print(f"  {'✅ 通过' if ok else '❌ 失败'}")
        sys.exit(0 if ok else 1)
    finally:
        if saved.exists():
            saved.unlink()
        if saved_md.exists():
            saved_md.unlink()
        if saved_pdf and saved_pdf.exists():
            saved_pdf.unlink()
        shutil.rmtree(common.get_path("输出") / "提取缓存" / "测试书01", ignore_errors=True)
        shutil.rmtree(common.get_path("输出") / "提取缓存" / "测试书03", ignore_errors=True)
        shutil.rmtree(common.get_path("输出") / "提取缓存" / "测试书M", ignore_errors=True)
        shutil.rmtree(common.get_path("输出") / "提取缓存" / "测试书F", ignore_errors=True)
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    if common.is_selfcheck():
        _selfcheck()
    else:
        common.run_with_write_lock(main, "提取")
