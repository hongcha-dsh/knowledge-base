# -*- coding: utf-8 -*-
"""卡 A-06 annotate_pack.py：标注任务包生成 + 导回校验 + 任务包管理。

生成：按书分目录（输出\\任务包\\{书名}\\），每章一个瘦身章包；超大章（>20 块）
      自动拆子包（04a/04b…，每子包至多20块）；全书配套 _任务说明.md（规则一份）+
      _清单.md（章号/状态）+ 回填\\ 子文件夹。
      命令：--生成（单章）/ --生成全书（批量，已导回章自动跳过）。
导回：--导回（默认读 回填\\{章号}{子包}_任务包.md，--子包 指定 a/b，也可 --文件 指定）
      / --导回全书（批量）。子包导回时自动合并该章所有已回填子包后写入章节摘要，
      .done 记录已导回子包，重复导回跳过。
      校验：①表格列数/块ID存在/标签非空 ②指纹与该块实际内容匹配 ③评测题答案块ID存在
      ④章摘要非空。任一不过→列出失败行，整包不写入。
写入：校验全过 → 章节摘要\\{章号}.md；术语候选合并进 输出\\验证\\{书名}_术语候选.json（控制台确认页）；
      评测题 → 追加 评测集.md（含密文）；随后刷新 _清单.md 状态。
管理：--删除（带 --章号 删单章；不带删全书目录）。
自动回填：--自动回填（全书，可 --章号 指定单章）经 llm_client 调标注 AI 逐包回填到 回填\，
      #35 起改「瘦输出」协议：AI 只回 摘要+块标签(无指纹列)+评测题，程序过四道闸
      （①块ID集合等价 ②术语出自本块原文 ③同包标签重合度≥0.8 拦截 ④退化评测题拦截），
      通过后由程序合成标准回填文件（指纹列程序回填）再走原 import_pack 导回；
      取结果（含重试）多路并发（默认 8，api.annotate_api.并发 可配），落盘主线程串行；
      每包至多重试 AUTO_FILL_MAX_RETRY 次，仍败跳过写报告；
      任务包缺失时自动生成；.done 幂等，已导回子包自动跳过。
退出码：0=成功，1=失败/校验拦截/异常，2=部分成功（--导回全书/--自动回填 有成功有失败）。
"""

import json
import re
import shutil
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import common
import llm_client

# 引入 B-01 检索脚本（随「知识库」配置目录走；沙箱模式下也指向真实库，因为该目录不参与沙箱重定向）
sys.path.insert(0, str(common.PROJECT_ROOT / common.load_config()["知识库"] / "检索脚本"))
import search_keyword  # noqa: E402

BLOCK_ID_RE = re.compile(r"^【(.+?)·(\d{4})】p\.(.*)$")
TAG_TABLE_HEAD = "| 块ID | 指纹"
EVAL_TABLE_HEAD = "| 题号 | 提问"
FILLED_PACK_RE = re.compile(r"^(\d+)([a-z]?)_任务包\.md$")
SUBPACK_MAX_BLOCKS = 20  # 单章块数超过则自动拆子包（每子包至多20块）
SUBPACK_MAX_BYTES = 30 * 1024  # #35：子包体积阈值（字节）——块数或包体超阈值均拆包
AUTO_FILL_MAX_RETRY = 3  # API 自动回填每包重试上限（见模块头 docstring）
AUTO_FILL_MAX_TOKENS = 32000  # #35 瘦输出额度：答题卡本体仅几千 token，余量留给思考链（DeepSeek 等默认思考模式，#24 坑）

# 节标题行（拆子包优先落节边界用，见 _block_starts_section / 设计 §4.7）
_SECTION_HEAD_RE = re.compile(
    r"^(?:"
    r"第[一二三四五六七八九十百零0-9]{1,4}[章节篇回]"
    r"|[一二三四五六七八九十]{1,4}[、．.]"
    r"|[0-9]{1,3}[、．.]"
    r"|（[一二三四五六七八九十]{1,4}）"
    r"|\([0-9]{1,3}\)"
    r"|[A-Z][、．.])"
    r"[^\s…·]*"
)


def pack_dir(book_title: str) -> Path:
    """返回 输出\\任务包\\{书名}\\，不存在则创建。"""
    d = common.get_path("输出") / "任务包" / book_title
    d.mkdir(parents=True, exist_ok=True)
    return d


def pack_filename(chapter_no: str, sub: str = "") -> str:
    """返回任务包文件名：单章 04_任务包.md；子包 04a_任务包.md / 04b_任务包.md。"""
    return f"{chapter_no}{sub}_任务包.md"


def subpack_suffixes(n_sub: int) -> list:
    """按子包个数返回后缀列表。1 个返回 [""]；多个返回 ["a","b",...]。"""
    if n_sub <= 1:
        return [""]
    import string
    return [string.ascii_lowercase[i] for i in range(min(n_sub, 26))]


def _subpack_count(blocks: list) -> int:
    """拆子包个数（#35 双判据）：块数 >20 或估算包体 >30KB 均增加子包数。"""
    n = len(blocks)
    if n == 0:
        return 0
    cnt = (n + SUBPACK_MAX_BLOCKS - 1) // SUBPACK_MAX_BLOCKS
    size = sum(sum(len(ln.encode("utf-8")) + 1 for ln in b["正文"]) + 120 for b in blocks)
    cnt = max(cnt, (size + SUBPACK_MAX_BYTES - 1) // SUBPACK_MAX_BYTES)
    return min(cnt, n)


def _block_starts_section(b: dict) -> bool:
    """判断块是否为「节边界块」：块首条正文行形如节/子标题（第X节、一、1.、（一）等）。

    拆子包优先落节边界（设计 §4.7）时用：块首行是节标题 → 该块是新一节的起点，可在其前切分。
    行长 >24 视为正文句子，不认作标题。"""
    for ln in b.get("正文", [])[1:]:  # [0] 是块ID行
        s = ln.strip()
        if not s:
            continue
        return len(s) <= 24 and bool(_SECTION_HEAD_RE.match(s))
    return False


def _split_blocks_by_section(blocks: list, n_sub: int) -> list:
    """拆子包切分（设计 §4.7）：返回 n_sub 个子块列表，拼接连续、覆盖全部块。

    先按块均分取粗边界；再在每段的后半段内找「节边界块」（块首为节标题行），
    取其中最后一个为真正切点（最接近均分点，且不切开后续节标题之前的内容）——
    节边界落在子包后半段才采纳，避免严重不均；找不到则退回按块均分。"""
    total = len(blocks)
    if n_sub <= 1:
        return [blocks]
    target = total // n_sub + (1 if total % n_sub else 0)
    section_heads = [i for i, b in enumerate(blocks) if _block_starts_section(b)]
    groups, start = [], 0
    for _ in range(n_sub - 1):
        end_p = min(start + target, total)
        cut = end_p
        half = start + (end_p - start) // 2  # 只采纳落在本段后半段的节边界
        for j in section_heads:
            if start < j < end_p and j >= half:
                cut = j  # 取最后一个（最接近均分点）
        groups.append(blocks[start:cut])
        start = cut
    groups.append(blocks[start:])
    return groups


def _read_skeleton(book_title: str, kb_root: Path) -> list:
    """读目录骨架.md。返回 [{章号, 章名, 书号, 起始块ID, 块数}]。

    #21 M2：新格式「起始块ID | 块数」（块 ID＝出生序不编码位置，取块按物理序）；
    旧格式「书号·起~止」区间列兼容读取（序号连续时区间块数＝止-起+1，两格式等价）。"""
    book_dir = kb_root / "书籍" / book_title
    sk_path = book_dir / "目录骨架.md"
    if not sk_path.exists():
        raise FileNotFoundError(f"缺少 目录骨架.md：{book_dir}（请先建索引）")
    chapters = []
    for line in sk_path.read_text(encoding="utf-8").split("\n"):
        line = line.strip()
        if not line.startswith("|") or line.startswith("| 章号"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 3:
            continue
        ch_no, ch_name = cells[0], cells[1]
        m_new = re.match(r"(.+?)·(\d{4})$", cells[2])
        if len(cells) >= 4 and m_new and cells[3].isdigit():
            chapters.append({
                "章号": ch_no,
                "章名": ch_name,
                "书号": m_new.group(1),
                "起始块ID": f"{m_new.group(1)}·{m_new.group(2)}",
                "块数": int(cells[3]),
            })
            continue
        m_old = re.match(r"(.+?)·(\d{4})~(\d{4})", cells[2])
        if m_old:
            chapters.append({
                "章号": ch_no,
                "章名": ch_name,
                "书号": m_old.group(1),
                "起始块ID": f"{m_old.group(1)}·{m_old.group(2)}",
                "块数": int(m_old.group(3)) - int(m_old.group(2)) + 1,
            })
    return chapters


def _load_chart_ids(book_dir: Path) -> set:
    """读 图表块清单.json（A-03 生成），返回图表块ID集合；无清单/异常返回空集。

    M3 消费方（4.6）：图表块不进任务包，避免盘面/表格文字占用人力标注。"""
    p = book_dir / "图表块清单.json"
    if not p.exists():
        return set()
    try:
        return set(json.loads(p.read_text(encoding="utf-8")).get("图表块", []))
    except Exception:
        return set()


def _extract_chapter_blocks(book_title: str, chapter: dict, kb_root: Path) -> list:
    """从 原文.md 提取该章全部块。返回 [{块ID, 页码, 行号, 正文, 指纹}]。

    #21 M2：按物理序取块——自「起始块ID」起依原文物理顺序数「块数」个块（图表块计入
    块数但不进输出），不再按序号区间过滤（出生证制下章内块 ID 可跳跃）。"""
    book_dir = kb_root / "书籍" / book_title
    chart_ids = _load_chart_ids(book_dir)
    orig_lines = (book_dir / "原文.md").read_text(encoding="utf-8").split("\n")
    copy_lines = (book_dir / "检索副本.md").read_text(encoding="utf-8").split("\n")

    blocks = []
    cur = None
    seen = 0  # 章内已数物理块数（含图表块）
    for i, (ol, cl) in enumerate(zip(orig_lines, copy_lines)):
        m = BLOCK_ID_RE.match(ol.strip())
        if m:
            if cur:
                if not cur["正文"][-1].strip():
                    cur["正文"].pop()  # 去掉块尾空行（无空行则保留，如书尾最后一行）
                blocks.append(cur)
                cur = None
            bid_full = f"{m.group(1)}·{m.group(2)}"
            if seen >= chapter["块数"]:
                continue
            if seen > 0 or bid_full == chapter["起始块ID"]:
                seen += 1
                if bid_full not in chart_ids:
                    cur = {"块ID": bid_full, "页码": m.group(3),
                           "正文": [ol.strip()], "指纹": "", "检索行": []}
        elif cur is not None:
            cur["正文"].append(ol)
            if cl.strip():
                cur["检索行"].append(cl.strip())
    if cur:
        if not cur["正文"][-1].strip():
            cur["正文"].pop()
        blocks.append(cur)

    for b in blocks:
        b["指纹"] = common.block_fingerprint(b.pop("检索行"))
    return blocks


def _eval_sentences(book_title: str) -> list:
    """读 输出\\验证\\{书名}_评测原句.txt。返回 [{块ID, 密文, 原句}]。"""
    path = common.get_path("输出") / "验证" / f"{book_title}_评测原句.txt"
    rows = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").split("\n"):
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) >= 3:
            rows.append({"块ID": parts[0], "密文": parts[1], "原句": parts[2]})
        elif len(parts) == 2:
            rows.append({"块ID": parts[0], "密文": "", "原句": parts[1]})
    return rows


def _render_pack(book_title: str, chapter: dict, blocks: list, evals: list,
                 subpack: str = "", n_sub: int = 1,
                 prev_id: str = "无", next_id: str = "无") -> str:
    """渲染瘦身章包（规则详见 _任务说明.md，包内只含本张表格与原文）。
    subpack="" 为单章整包；subpack="a" 为该章子包 a。
    prev_id/next_id：本书块序列中本包首块的前一块、末块的后一块（首包/末包为「无」），
    渲染为标题下固定两行衔接行（设计 §4.7）；导回校验不校验这两行。"""
    title = f"# 标注任务包：《{book_title}》第 {chapter['章号']} 章 {chapter['章名']}"
    if subpack:
        title += f"（子包 {subpack} / {n_sub}）"
    summary_head = "## 章摘要（约 10 句，忠实本章内容）"
    if subpack:
        summary_head = "## 本段摘要（约 3~5 句，忠实本子包内容；整章摘要由各子包合并）"
    eval_section = ""
    eval_placeholders = ""
    if evals:
        eval_section = "## 评测原句（逐句改写为口语化提问，答案块ID不变）\n\n"
        for e in evals:
            eval_section += f"- 【{e['块ID']}】{e['原句']}\n"
        eval_placeholders = "\n".join(
            f"| Q{i + 1} |  | {e['块ID']} |" for i, e in enumerate(evals))

    lines = [
        title,
        f"> 上接块ID：{prev_id}",
        f"> 下接块ID：{next_id}",
        "> 先看同目录《_任务说明.md》里的任务、铁律与输出模板，再按模板回填本包。",
        "> 回填后把本文件（文件名不改）存入 回填\\ 子文件夹。",
        "",
        summary_head,
        "（在此填写，替换本行）",
        "",
        "## 块标签表（严格 5 列；指纹列已预填，禁止改动）",
        "| 块ID | 指纹 | 标签（逗号分隔，中英文逗号均可） | 术语候选 | 别名候选 |",
        "|---|---|---|---|---|",
    ]
    for b in blocks:
        lines.append(f"| {b['块ID']} | {b['指纹']} |  |  |  |")
    lines.append("")
    lines.append("## 评测题输出模板")
    lines.append("| 题号 | 提问 | 答案块ID |")
    lines.append("|---|---|---|")
    if eval_placeholders:
        lines.append(eval_placeholders)
    lines.append("")
    if eval_section:
        lines.append(eval_section)
    lines.append("## 原文")
    lines.append("")
    for b in blocks:
        lines.extend(b["正文"])
        lines.append("")
    return "\n".join(lines)


def _write_instructions(book_title: str, kb_root: Path):
    """写全书规则说明 _任务说明.md（规则只写一份，保证整文件夹 AI 处理一致性）。"""
    chapters = _read_skeleton(book_title, kb_root)
    total_blocks = 0
    ch_lines = []
    for c in chapters:
        n_blocks = len(_extract_chapter_blocks(book_title, c, kb_root))
        total_blocks += n_blocks
        ch_lines.append(f"- 第 {c['章号']} 章《{c['章名']}》：{n_blocks} 块")
    sk_path = kb_root / "书籍" / book_title / "目录骨架.md"
    book_id = ""
    for line in sk_path.read_text(encoding="utf-8").split("\n"):
        if line.startswith("书号："):
            book_id = line.replace("书号：", "").strip()
            break
    text = (
        f"# 标注任务说明：《{book_title}》\n"
        f"书号：{book_id} | 共 {len(chapters)} 章 | 按章合计 {total_blocks} 块（块数以各章包内实际为准）\n\n"
        "## AI 启动指令（对 AI 直接说：按本任务说明处理本文件夹）\n"
        "请先通读本说明，然后对同目录下每个 XX_任务包.md 逐包执行：\n"
        "① 填章摘要/本段摘要；② 给每个块填 5~8 个标签、术语候选、别名候选（不确定的标【存疑】）；\n"
        "③ 把【评测原句】逐句改写为口语化提问。\n"
        "只回填文件里的表格；文件名不能改；指纹列不能动。内容必须忠实原文，禁止编造原书没有的内容。\n"
        "处理完后，把每个文件按原文件名保存成 md，写入本目录下 回填\\ 子文件夹。\n\n"
        "## 任务（对每个任务包逐包执行）\n"
        "1. 为包内每个块填写：标签（5~8 个/块，逗号分隔，中英文逗号均可）\n"
        "2. 每块填写术语候选与别名候选（不确定的标【存疑】）\n"
        "3. 填写摘要：整章包填「章摘要」（约 10 句）；子包填「本段摘要」（约 3~5 句，"
        "导回时各子包自动合并为整章摘要）\n"
        "4. 把【评测原句】逐句改写为口语化提问（评测题），答案块ID不变\n\n"
        "## 填写规范\n"
        "- 摘要直接陈述内容本身，禁止「本段」「接续前文」「上文提到」等承接词——"
        "多段摘要将独立拼接，彼此无上下文；\n"
        "- 标签与别名候选不得使用干支组合（如 己酉）、大运/流年干支（如 大运壬子）、命例编号；\n"
        "- 术语候选须为可复用的学术语，单条填写、不带括号注解"
        "（如写「婚姻星」而非「婚姻星（配偶星）」）。\n\n"
        "## 忠实原文要求（铁律）\n"
        "- 摘要与标签必须基于所给原文，禁止补充原文没有的内容；\n"
        "- 不确定的术语标【存疑】，不要编造。\n\n"
        "## 输出模板（严格按章包内表格回填，列不可增减；指纹列已预填，禁止改动）\n"
        "块标签表：| 块ID | 指纹 | 标签（逗号分隔，中英文逗号均可） | 术语候选 | 别名候选 |\n"
        "评测题表：| 题号 | 提问 | 答案块ID |\n\n"
        "## 回填规则\n"
        "- 逐包处理，文件名不得改动（沿用 章号[子包]_任务包.md 命名，如 04a_任务包.md）；\n"
        "- 处理完的文件全部存到本目录下 回填\\ 子文件夹；\n"
        "- 指纹列是导回校验锚点，禁止改动；块ID 存在性、指纹匹配任一不符将整包拦截不写入；\n"
        "- 包头「上接块ID/下接块ID」两行为跨包衔接参考行（首/末包显示「无」），"
        "回填 AI 无需处理该两行，跨包续查时供使用方参考。\n\n"
        "## 全部回填完成后（具备本地命令执行能力的 AI 必做；纯对话环境忽略本节）\n"
        f"全部回填完成后，请直接运行：python 资料预处理\\脚本\\annotate_pack.py --导回全书 --书名 \"{book_title}\"\n"
        "如有失败行，按报错修正对应回填文件后重跑该命令，直至全部通过（退出码 0）。\n"
        "若你处于无法运行命令的纯对话环境，忽略本节，把文件交给用户导回即可。\n\n"
        "## 本书章节\n"
        + "\n".join(ch_lines)
        + "\n"
    )
    (pack_dir(book_title) / "_任务说明.md").write_text(text, encoding="utf-8")


def _write_manifest(book_title: str, kb_root: Path):
    """写任务包清单 _清单.md（每次生成/导回/删除后刷新）。超大章显示全部子包文件名。"""
    chapters = _read_skeleton(book_title, kb_root)
    pdir = pack_dir(book_title)
    back_dir = pdir / "回填"
    summary_dir = kb_root / "书籍" / book_title / "章节摘要"
    lines = [
        f"# 《{book_title}》标注任务包清单",
        "",
        "| 章号 | 章名 | 块数 | 文件名 | 状态 |",
        "|---|---|---|---|---|",
    ]
    for c in chapters:
        blocks = _extract_chapter_blocks(book_title, c, kb_root)
        n_blocks = len(blocks)
        suffixes = subpack_suffixes(_subpack_count(blocks))
        names = [pack_filename(c["章号"], sub) for sub in suffixes]
        fn = " / ".join(names)
        done = _chapter_done_set(book_title, c["章号"])
        done_subs = [sub for sub in suffixes if sub in done]
        if done_subs and len(done_subs) == len(suffixes):
            state = "🟢已导入"
        elif done_subs:
            state = f"🟢部分导入 {len(done_subs)}/{len(suffixes)}"
        else:
            filled_names = [n for n in names if (back_dir / n).exists()]
            if filled_names:
                state = f"🟡已填写 {len(filled_names)}/{len(names)}"
            else:
                state = "⚪待填写"
        lines.append(f"| {c['章号']} | {c['章名']} | {n_blocks} | {fn} | {state} |")
    (pdir / "_清单.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def generate_pack(book_title: str, chapter_no: str, kb_root: Path = None) -> list:
    """生成单章标注任务包（新结构：输出\\任务包\\{书名}\\）。块数 > SUBPACK_MAX_BLOCKS
    自动拆成多个子包（04a/04b...）。返回生成的文件路径列表。"""
    if kb_root is None:
        kb_root = common.get_path("知识库")
    chapters = _read_skeleton(book_title, kb_root)
    chapter = next((c for c in chapters if c["章号"] == chapter_no), None)
    if not chapter:
        raise ValueError(f"章号 {chapter_no} 不存在（骨架含章：{[c['章号'] for c in chapters]}）")

    blocks = _extract_chapter_blocks(book_title, chapter, kb_root)
    if not blocks:
        raise RuntimeError("该章无块内容")

    # 评测原句按章过滤（全书原句只进对应章）
    ch_ids = {b["块ID"] for b in blocks}
    evals = [e for e in _eval_sentences(book_title) if e["块ID"] in ch_ids]

    out_dir = pack_dir(book_title)
    (out_dir / "回填").mkdir(parents=True, exist_ok=True)

    suffixes = subpack_suffixes(_subpack_count(blocks))
    groups = _split_blocks_by_section(blocks, len(suffixes))
    written = []
    for i, sub in enumerate(suffixes):
        sub_blocks = groups[i]
        # 包衔接行（§4.7）：首包上接=无、末包下接=无，否则取块序列中紧邻的块ID
        prev_id = groups[i - 1][-1]["块ID"] if i > 0 else "无"
        next_id = groups[i + 1][0]["块ID"] if i < len(groups) - 1 else "无"
        sub_ids = {b["块ID"] for b in sub_blocks}
        sub_evals = [e for e in evals if e["块ID"] in sub_ids]
        pack_path = out_dir / pack_filename(chapter_no, sub)
        pack_path.write_text(_render_pack(book_title, chapter, sub_blocks, sub_evals,
                                          subpack=sub, n_sub=len(suffixes),
                                          prev_id=prev_id, next_id=next_id),
                             encoding="utf-8")
        written.append(pack_path)

    _write_instructions(book_title, kb_root)
    _write_manifest(book_title, kb_root)
    return written


def generate_all_packs(book_title: str, kb_root: Path = None, overwrite: bool = False) -> dict:
    """批量生成全书任务包。已导回章（.done 记录覆盖全部子包）自动跳过——与导回幂等判据同源，
    均以 回填\\{章号}.done 为唯一状态源。返回汇总。"""
    if kb_root is None:
        kb_root = common.get_path("知识库")
    chapters = _read_skeleton(book_title, kb_root)
    generated, skipped, failed = [], [], []
    pdir = pack_dir(book_title)
    (pdir / "回填").mkdir(parents=True, exist_ok=True)
    for c in chapters:
        try:
            if not overwrite:
                done = _chapter_done_set(book_title, c["章号"])
                if done:
                    blocks = _extract_chapter_blocks(book_title, c, kb_root)
                    if set(subpack_suffixes(_subpack_count(blocks))) <= done:
                        skipped.append(c["章号"])
                        continue
            generate_pack(book_title, c["章号"], kb_root)
            generated.append(c["章号"])
        except Exception as e:
            failed.append((c["章号"], str(e)))
    _write_manifest(book_title, kb_root)
    return {"生成": generated, "跳过": skipped, "失败": failed}


def _parse_filled(filled_text: str) -> dict:
    """解析回填任务包。返回 {摘要, 标签行[], 评测题行[]}。
    表格只在所属章节标题（块标签表/评测题输出模板）下解析——原文区的排盘三列表
    （如 | 3<br>七 | 8<br>三 | 1<br>五 |）不得误判为评测题行（#35 实测修正）。"""
    lines = filled_text.split("\n")
    summary = ""
    tag_rows = []
    eval_rows = []
    in_summary = False
    table_section = None
    summary_buf = []
    for i, ln in enumerate(lines):
        s = ln.strip()
        if s.startswith("## "):
            in_summary = "摘要" in s
            if "块标签表" in s:
                table_section = "标签"
            elif "评测题" in s:
                table_section = "评测"
            else:
                table_section = None
            continue
        if in_summary:
            if s and not s.startswith("（在此填写"):
                summary_buf.append(s)
            continue
        if s.startswith(TAG_TABLE_HEAD) or s.startswith("|---"):
            continue
        if s.startswith("|"):
            cells = [c.strip() for c in s.strip("|").split("|")]
            if table_section == "标签" and re.match(r"^【?.+·\d{4}", cells[0]):
                tag_rows.append({"行号": i + 1, "块ID": cells[0],
                                 "指纹": cells[1] if len(cells) > 1 else "",
                                 "标签": cells[2] if len(cells) > 2 else "",
                                 "术语候选": cells[3] if len(cells) > 3 else "",
                                 "别名候选": cells[4] if len(cells) > 4 else "",
                                 "列数": len(cells)})
            elif table_section == "评测" and len(cells) == 3 and re.match(r"^Q?\d+", cells[0]):
                eval_rows.append({"行号": i + 1, "题号": cells[0], "提问": cells[1],
                                  "答案块ID": cells[2]})
    summary = "\n".join(summary_buf).strip()
    return {"摘要": summary, "标签行": tag_rows, "评测题行": eval_rows}


def _chapter_done_set(book_title: str, chapter_no: str) -> set:
    """已导回记录：回填\\{章号}.done，内容为已导回的子包名集合（整章以「整章」占位表示""）。"""
    p = pack_dir(book_title) / "回填" / f"{chapter_no}.done"
    if p.exists():
        try:
            raw = p.read_text(encoding="utf-8").strip()
            if not raw:
                return {""}  # 旧格式：空文件 = 整章已导回
            return set("" if t == "整章" else t for t in raw.split())
        except Exception:
            return set()
    return set()


def book_annotation_status(book_title: str, kb_root: Path = None) -> dict:
    """书级标注状态（#15 阶段①）：状态唯一源＝`.done` 现算，不另造状态、不写缓存。

    返回 {"状态": "未开始"|"进行中"|"已完成", "已导回": x, "总章数": n}。
    未开始＝任务包目录不存在；章已导回＝`.done` 集合覆盖该章全部子包；
    已完成＝x == n 且 n > 0；n 取目录骨架章数（"还差几章"对用户最有用）。"""
    if kb_root is None:
        kb_root = common.get_path("知识库")
    pdir = common.get_path("输出") / "任务包" / book_title
    if not pdir.exists():
        return {"状态": "未开始", "已导回": 0, "总章数": 0}
    try:
        chapters = _read_skeleton(book_title, kb_root)
    except FileNotFoundError:
        return {"状态": "未开始", "已导回": 0, "总章数": 0}
    total = len(chapters)
    done_chapters = 0
    for c in chapters:
        blocks = _extract_chapter_blocks(book_title, c, kb_root)
        suffixes = subpack_suffixes(_subpack_count(blocks))
        if suffixes and set(suffixes) <= _chapter_done_set(book_title, c["章号"]):
            done_chapters += 1
    state = "已完成" if (total > 0 and done_chapters == total) else "进行中"
    return {"状态": state, "已导回": done_chapters, "总章数": total}


def _collect_filled_subpacks(book_title: str, chapter_no: str, extra_file: Path = None) -> list:
    """收集该章全部已回填子包（含 extra_file 当前文件），按子包名字典序排列。
    返回 [{"名称", "块数", "摘要", "标签行", "评测题行"}]。排序确定后，
    任意导回顺序得到的整章摘要字节级一致。"""
    back_dir = pack_dir(book_title) / "回填"
    files = {f.name: f for f in back_dir.glob(f"{chapter_no}*_任务包.md")}
    if extra_file is not None:
        files[extra_file.name] = Path(extra_file)

    def _sort_key(name: str):
        m = re.match(r"^(\d+)([a-z]?)", name)
        return (m.group(1), m.group(2)) if m else (chapter_no, "z" + name)

    entries = []
    for name in sorted(files, key=_sort_key):
        fd = _parse_filled(files[name].read_text(encoding="utf-8-sig"))
        entries.append({
            "名称": name,
            "块数": len(fd["标签行"]),
            "摘要": fd["摘要"],
            "标签行": fd["标签行"],
            "评测题行": fd["评测题行"],
        })
    return entries


def _append_evals_dedup(eval_rows: list, kb_root: Path = None):
    """评测题追加（去重：按 提问|答案块ID，已存在则跳过，避免子包重复导回重复追加）。"""
    if kb_root is None:
        kb_root = common.get_path("知识库")
    eval_path = kb_root / "评测集.md"
    if not eval_path.exists():
        eval_path.write_text(
            "# 评测集\n\n> 判分方式：AI 按正常查询流程作答，检查其引用的块是否含对应密文。\n\n"
            "| 题号 | 提问 | 答案块ID | 密文 | 命中 | token |\n|---|---|---|---|---|---|\n",
            encoding="utf-8",
        )
    lines = eval_path.read_text(encoding="utf-8").split("\n")
    existing = set()
    existing_nos = set()
    for ln in lines:
        if ln.strip().startswith("|") and not ln.strip().startswith("|---"):
            cells = [c.strip() for c in ln.strip().strip("|").split("|")]
            if len(cells) >= 4 and re.match(r"^Q?\d+$", cells[0]):
                existing_nos.add(cells[0])
                existing.add(f"{cells[1]}|{cells[2]}")
    new_rows = [r for r in eval_rows if f"{r['提问']}|{r['答案块ID']}" not in existing]
    for r in new_rows:
        q_no = re.sub(r"[^0-9A-Za-z]", "", r["题号"]) or "Q"
        m = re.match(r"^Q?(\d+)$", q_no)
        n = int(m.group(1)) if m else len(existing_nos) + 1
        while f"Q{n}" in existing_nos:
            n += 1
        q_no = f"Q{n}"
        existing_nos.add(q_no)
        existing.add(f"{r['提问']}|{r['答案块ID']}")
        lines.append(f"| {q_no} | {r['提问']} | {r['答案块ID']} | {r['密文']} |  |  |")
    eval_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    # #4 M3（R4）：题干入倒排（幂等）——导回收尾自动把当次评测题题干挂进答案块所在书
    _append_question_grams(eval_rows, kb_root)


def _question_ngrams(text: str) -> set:
    """题干 → 2/3 字 n-gram（与 build_index._ngrams 同规则，只取中文+字母数字）。"""
    chars = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", text)
    grams = set()
    for n in (2, 3):
        for i in range(len(chars) - n + 1):
            grams.add(chars[i : i + n])
    return grams


def _append_question_grams(eval_rows: list, kb_root: Path = None) -> list:
    """R4：把评测题题干 n-gram 追加进答案块所在书 倒排表.json 的「题干」节（幂等）。

    词条只作检索入口：独立「题干」节与原文「条目」区分来源（AI 生成内容只进索引侧、
    不作引用出处展示）；检索侧查两节合并。旧题（答案块不在库）自动跳过。
    幂等：gram→块ID 已存在不重复追加，重跑结果一致。返回写入过的书名列表。
    """
    if kb_root is None:
        kb_root = common.get_path("知识库")
    # 按书号反查书目录（倒排表头部书号，与 update_book.resolve_book_title 同源）
    book_by_id = {}
    books_dir = kb_root / "书籍"
    if books_dir.exists():
        for d in books_dir.iterdir():
            inv = d / "倒排表.json"
            if not inv.exists():
                continue
            try:
                book_id = json.loads(inv.read_text(encoding="utf-8")).get("书号")
            except Exception:
                continue
            if book_id:
                book_by_id.setdefault(book_id, d.name)
    # #65 M3（设计 §4.6）：先按书归并题面 gram，再每书只读/写一次倒排表；旧实现逐题
    # 整份解析并回写，62 道题会把同一本 13MB 倒排表解析/序列化 62 次。写入词条与块 ID
    # 集合、幂等语义不变（同 gram 的块 ID 按块 ID 升序追加）。
    grams_by_title = {}
    for r in eval_rows:
        answer = r["答案块ID"]
        if "·" not in answer:
            continue
        book_id, _seq = answer.split("·", 1)
        title = book_by_id.get(book_id)
        if not title:
            continue  # 旧题/他书，跳过
        grams = _question_ngrams(r["提问"])
        if not grams:
            continue
        per_book = grams_by_title.setdefault(title, {})
        for g in grams:
            per_book.setdefault(g, set()).add(answer)
    changed = set()
    for title, grams_of_book in grams_by_title.items():
        inv_path = kb_root / "书籍" / title / "倒排表.json"
        try:
            data = json.loads(inv_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        qg = data.setdefault("题干", {})
        added = False
        for g in sorted(grams_of_book):
            lst = qg.setdefault(g, [])
            for answer in sorted(grams_of_book[g]):
                if answer not in lst:
                    lst.append(answer)
                    added = True
        if added:
            # #65 M1：题干倒排写回走原子写，避免半截 JSON 损坏检索入口
            # #65 M3：统一紧凑序列化，避免重建后又把倒排表重新格式化
            common.atomic_write_text(
                inv_path, common.dump_index_json(data), encoding="utf-8")
            changed.add(title)
    return sorted(changed)


def backfill_question_grams(kb_root: Path = None) -> dict:
    """存量补写入口（`--题干补写`）：读全局评测集全部题，题干 n-gram 写进各书「题干」节。

    幂等（_append_question_grams 去重），已写过的题重跑不产重复词条。不需要 --书名。
    #65 M2：补写后清理指向不存在块的题干 posting，返回清理明细与计数。
    """
    if kb_root is None:
        kb_root = common.get_path("知识库")
    eval_path = kb_root / "评测集.md"
    if not eval_path.exists():
        return {"写入书数": 0, "题干题数": 0, "跳过": f"评测集不存在：{eval_path}"}
    rows = []
    for ln in eval_path.read_text(encoding="utf-8").split("\n"):
        s = ln.strip()
        if not s.startswith("|") or s.startswith("|---"):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if len(cells) >= 3 and re.match(r"^Q?\d+$", cells[0]):
            rows.append({"题号": cells[0], "提问": cells[1], "答案块ID": cells[2]})
    changed = _append_question_grams(rows, kb_root)
    maintenance = _prune_stale_question_grams(kb_root)
    written = set(changed) | set(maintenance)
    return {"写入书数": len(written), "题干题数": len(rows), "跳过": None,
            "清理引用": sum(len(items) for items in maintenance.values()),
            "清理详情": maintenance}


def _prune_stale_question_grams(kb_root: Path = None) -> dict:
    """#65 M2：按书清理题干节中指向不存在块的 posting，返回 {书名: 失效引用明细}。

    活块以当前 原文.md 的块 ID 为准；只写确有失效引用的书，保持有效词条顺序。
    """
    import build_index
    if kb_root is None:
        kb_root = common.get_path("知识库")
    maintenance = {}
    for title, book_dir in search_keyword._iter_books(kb_root):
        inv_path = book_dir / "倒排表.json"
        if not inv_path.exists():
            # #65 M3 补记：没有倒排表＝没有题干节可清，跳过即可；「存在但损坏」仍按 M1 口径
            # 显式报错（不能把损坏当空表继续写）。此分支修复 A-06 原生 --自检 在
            # 「有原文无索引」的假书上被 M1 显式错误打断（M2 引入的组合缺陷）。
            continue
        data = search_keyword._load_index(title, inv_path)
        question = data.get("题干", {})
        if question is None:
            question = {}
        if not isinstance(question, dict):
            raise search_keyword._index_error(title, inv_path, "结构异常：「题干」节应为对象")
        block_map, _orig, _copy = build_index._parse_book_files(title, kb_root)
        cleaned, removed = build_index._prune_question_postings(
            question, set(block_map), title, inv_path)
        if not removed:
            continue
        if cleaned:
            data["题干"] = cleaned
        else:
            data.pop("题干", None)
        common.atomic_write_text(
            inv_path, common.dump_index_json(data), encoding="utf-8")
        maintenance[title] = removed
    return maintenance


def _purge_real_evals(book_id: str, kb_root: Path, book_title: str = None) -> int:
    """#66：删书联动清理 评测集_真实提问.md 中该书题行并接续重编 R001..Rnnn。"""
    real_path = kb_root / "评测集_真实提问.md"
    if not real_path.exists():
        return 0
    prefix = f"{book_id}·" if book_id else None
    lines = real_path.read_text(encoding="utf-8").split("\n")
    kept, rows_at = [], []
    removed = 0
    for ln in lines:
        s = ln.strip()
        cells = None
        if s.startswith("|") and not s.startswith("|---"):
            cells = [c.strip() for c in s.strip("|").split("|")]
        if cells and len(cells) >= 4 and re.match(r"^R\d+$", cells[0]):
            scoped_book = cells[2].strip("《》 ").strip()
            raw_bids = [b.strip() for b in re.split(r"[,，]", cells[3]) if b.strip()]
            keep_bids = [b for b in raw_bids if not (prefix and b.startswith(prefix))]
            book_matched = bool(book_title and scoped_book == book_title)
            if book_matched or (raw_bids and not keep_bids):
                removed += 1
                continue
            if prefix and len(keep_bids) != len(raw_bids):
                cells[3] = ", ".join(keep_bids)
                ln = "| " + " | ".join(cells) + " |"
            rows_at.append(len(kept))
        kept.append(ln)
    if not removed:
        return 0
    for offset, idx in enumerate(rows_at):
        cells = [c.strip() for c in kept[idx].strip().strip("|").split("|")]
        new_no = f"R{offset + 1:03d}"
        if cells[0] != new_no:
            cells[0] = new_no
            kept[idx] = "| " + " | ".join(cells) + " |"
    common.atomic_write_text(real_path, "\n".join(kept), encoding="utf-8")
    return removed


def purge_book_evals(book_id: str, kb_root: Path = None, book_title: str = None) -> dict:
    """#56 删书联动：从全局评测集删掉该书的题行（答案块ID 以 `{书号}·` 开头），其余题号重连。

    根因：评测集是**跨书混合单文件**、按 `{书号}·{序号}` 答案块ID 关联；删书若不按书号删行，
    该书旧题永久残留。书号被新书复用后，旧题块号在现库「有效」——检索会串到新书块
    （#33 收钥清理实例：魔法下架→0006 归诗经），即「答案块ID 有效」≠「题干来源书正确」。

    题号重连：保留的题行按原顺序重编为连续题号（起始号＝现存最小题号，保留 Q66 起头这类
    既有偏移，不整体归零，避免冲击既有引用/基线）；表头、分组空行与未变题号行原样保留。
    幂等：无该书题行时原样返回，重跑结果一致。返回 {"删除题数", "剩余题数"}。
    """
    result = {"删除题数": 0, "剩余题数": 0, "真实集删除题数": 0}
    if not book_id and not book_title:
        return result
    if kb_root is None:
        kb_root = common.get_path("知识库")
    result["真实集删除题数"] = _purge_real_evals(book_id, kb_root, book_title=book_title)
    if not book_id:
        return result
    eval_path = kb_root / "评测集.md"
    if not eval_path.exists():
        return result
    prefix = f"{book_id}·"
    lines = eval_path.read_text(encoding="utf-8").split("\n")
    kept, rows_at, nums = [], [], []
    for ln in lines:
        s = ln.strip()
        cells = None
        if s.startswith("|") and not s.startswith("|---"):
            cells = [c.strip() for c in s.strip("|").split("|")]
        if cells and len(cells) >= 3 and re.match(r"^Q?\d+$", cells[0]):
            if cells[2].startswith(prefix):
                result["删除题数"] += 1
                continue
            rows_at.append(len(kept))
            m = re.match(r"^Q?(\d+)$", cells[0])
            nums.append(int(m.group(1)) if m else None)
        kept.append(ln)
    result["剩余题数"] = len(rows_at)
    if not result["删除题数"]:
        return result
    valid = [n for n in nums if n is not None]
    start = min(valid) if valid else 1
    for offset, idx in enumerate(rows_at):
        cells = [c.strip() for c in kept[idx].strip().strip("|").split("|")]
        new_no = f"Q{start + offset}"
        if cells[0] == new_no:
            continue
        cells[0] = new_no
        kept[idx] = "| " + " | ".join(cells) + " |"
    eval_path.write_text("\n".join(kept), encoding="utf-8")
    return result


def _chapter_page_range(blocks: list) -> str:
    """章页码范围拼接：经 parse_page_tag 取首块起始页、末块结束页（设计 4.9）。

    已拟合标签为印刷页数字；降级标签（PDF.x）带前缀原样透传，如实标明 PDF 页序；
    避免区间标签直接拼出 2~3~4~5 一类毛刺。"""
    if not blocks:
        return "-"
    first = blocks[0]["页码"]
    last = blocks[-1]["页码"]
    t1 = common.parse_page_tag(first)
    t2 = common.parse_page_tag(last)
    start_txt = first if t1["降级"] else str(t1["起始"])
    end_txt = last if t2["降级"] else str(t2["结束"])
    return f"p.{start_txt}~p.{end_txt}"


def _split_tags(tag_text: str) -> list:
    return list(dict.fromkeys(
        t.strip() for t in re.split(r"[,，、/]", tag_text or "") if t.strip()
    ))


def _save_block_tags(book_title: str, book_id: str, tag_rows: list,
                     kb_root: Path = None) -> Path:
    """#66：按块持久化 块标签.json（原子写，保留已有其他章节块标签）。"""
    if kb_root is None:
        kb_root = common.get_path("知识库")
    book_dir = kb_root / "书籍" / book_title
    tags_path = book_dir / "块标签.json"
    data = {"书号": book_id, "来源": "AI标注", "块": {}}
    if tags_path.exists():
        try:
            loaded = json.loads(tags_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict) and isinstance(loaded.get("块"), dict):
                data["块"] = dict(loaded["块"])
                if loaded.get("书号"):
                    data["书号"] = loaded["书号"]
        except (OSError, ValueError) as e:
            raise RuntimeError(f"《{book_title}》的 块标签.json 损坏（{tags_path}）：{e}") from e
    for row in tag_rows:
        bid = str(row.get("块ID", "")).strip()
        tags = _split_tags(row.get("标签", ""))
        if bid and tags:
            data["块"][bid] = tags
    data["块"] = dict(sorted(data["块"].items()))
    common.atomic_write_text(
        tags_path, json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return tags_path


def recover_block_tags(book_title: str = None, kb_root: Path = None) -> dict:
    """#66：从现存 任务包/{书名}/回填/*.md 无损回收块标签写 块标签.json（零 API 费用）。"""
    if kb_root is None:
        kb_root = common.get_path("知识库")
    pack_root = common.get_path("输出") / "任务包"
    if book_title:
        targets = [book_title]
    else:
        targets = sorted(
            d.name for d in pack_root.iterdir()
            if d.is_dir() and (d / "回填").is_dir()
        ) if pack_root.exists() else []

    summary = {"回收书数": 0, "回收块数": 0, "跳过": [], "明细": {}}
    for title in targets:
        book_dir = kb_root / "书籍" / title
        copy_path = book_dir / "检索副本.md"
        orig_path = book_dir / "原文.md"
        if not copy_path.exists() or not orig_path.exists():
            summary["跳过"].append((title, "书库目录或原文缺失"))
            continue
        back_dir = pack_root / title / "回填"
        if not back_dir.exists():
            summary["跳过"].append((title, "无回填目录"))
            continue
        md_files = sorted(f for f in back_dir.glob("*.md") if not f.name.startswith("_"))
        if not md_files:
            summary["跳过"].append((title, "回填目录无 .md 文件"))
            continue

        # 单遍扫描现存活块ID与指纹，确保只收现存有效块且指纹匹配
        live_fp = {}
        book_id = ""
        lines = copy_path.read_text(encoding="utf-8").split("\n")
        cur_id, buf = None, []
        for ln in lines:
            m = search_keyword.BLOCK_ID_RE.match(ln.strip())
            if m:
                if cur_id is not None:
                    live_fp[cur_id] = search_keyword.normalize(common.block_fingerprint(buf))
                book_id = book_id or m.group(1)
                cur_id = f"{m.group(1)}·{m.group(2)}"
                buf = []
            elif cur_id is not None:
                buf.append(ln)
        if cur_id is not None:
            live_fp[cur_id] = search_keyword.normalize(common.block_fingerprint(buf))

        collected = {}
        for f in md_files:
            parsed = _parse_filled(f.read_text(encoding="utf-8-sig"))
            for row in parsed["标签行"]:
                if row.get("列数") != 5:
                    continue
                bid = row["块ID"]
                if bid not in live_fp:
                    continue
                if search_keyword.normalize(row["指纹"]) != live_fp[bid]:
                    continue
                tags = _split_tags(row["标签"])
                if tags:
                    collected[bid] = {"块ID": bid, "标签": ",".join(tags)}
        if not collected:
            summary["跳过"].append((title, "未解析到有效块标签"))
            continue
        _save_block_tags(title, book_id, list(collected.values()), kb_root)
        summary["回收书数"] += 1
        summary["回收块数"] += len(collected)
        summary["明细"][title] = len(collected)
    return summary


def import_pack(book_title: str, chapter_no: str, sub: str = "",
                filled_path: str = None, kb_root: Path = None) -> dict:
    """导回校验并写入。sub 为空=整章；sub="a"=该章子包 a。filled_path 为 None 时自动取
    回填\\{章号}{sub}_任务包.md。子包导回时自动合并该章所有已回填子包后写入章节摘要，
    已导回子包（.done 记录）重复导回直接跳过，防止重复追加评测题。"""
    if kb_root is None:
        kb_root = common.get_path("知识库")
    if filled_path is None:
        filled_path = pack_dir(book_title) / "回填" / pack_filename(chapter_no, sub)
    filled_path = Path(filled_path)
    if not filled_path.exists():
        raise FileNotFoundError(
            f"回填文件不存在：{filled_path}（应把 AI 处理后的同名文件放入 回填\\ 子文件夹）")

    chapters = _read_skeleton(book_title, kb_root)
    chapter = next((c for c in chapters if c["章号"] == chapter_no), None)
    if not chapter:
        raise ValueError(f"章号 {chapter_no} 不存在")

    # 幂等：该子包已导回则跳过写入
    done = _chapter_done_set(book_title, chapter_no)
    if sub in done:
        return {"通过": True, "已存在": True, "失败行": [], "摘要": "",
                "标签行": [], "评测题行": []}

    filled = _parse_filled(filled_path.read_text(encoding="utf-8-sig"))
    book_dir = kb_root / "书籍" / book_title

    # 取该章真实块及其指纹（现场扫描）
    real_blocks = _extract_chapter_blocks(book_title, chapter, kb_root)
    real_ids = {b["块ID"] for b in real_blocks}
    real_fp = {b["块ID"]: b["指纹"] for b in real_blocks}

    failures = []

    # 校验① 标签表：列数/块ID存在/标签非空
    if not filled["标签行"]:
        failures.append("标签表为空（未填写块标签）")
    for row in filled["标签行"]:
        if row["列数"] != 5:
            failures.append(f"行 {row['行号']}：表格列数错误（应为 5 列：块ID/指纹/标签/术语候选/别名候选）")
            continue
        if row["块ID"] not in real_ids:
            failures.append(f"行 {row['行号']}：块ID {row['块ID']} 不存在或不在本章")
            continue
        if not row["标签"].strip():
            failures.append(f"行 {row['行号']}：块 {row['块ID']} 标签为空")

    # 校验② 指纹匹配（防标注错位：回填指纹 normalize 后必须等于该块实际前12字 normalize 后）
    for row in filled["标签行"]:
        if row["列数"] != 5 or row["块ID"] not in real_ids:
            continue
        filled_fp = search_keyword.normalize(row["指纹"])
        actual_fp = search_keyword.normalize(real_fp.get(row["块ID"], ""))
        if filled_fp != actual_fp:
            failures.append(
                f"行 {row['行号']}：块 {row['块ID']} 指纹不符"
                f"（回填「{row['指纹']}」≠实际「{real_fp.get(row['块ID'], '')}」），疑似标注错位，请核对后重填")

    # 校验③ 评测题答案块ID存在
    for row in filled["评测题行"]:
        if row["答案块ID"] not in real_ids:
            failures.append(f"行 {row['行号']}：答案块ID {row['答案块ID']} 不存在或不在本章")

    # 摘要校验
    if not filled["摘要"]:
        failures.append("章摘要/本段摘要为空（未填写摘要）")

    if failures:
        return {"通过": False, "失败行": failures, "摘要": filled["摘要"],
                "标签行": filled["标签行"], "评测题行": filled["评测题行"]}

    # ===== 写入 =====
    # 合并该章全部已回填子包（含当前文件），按子包名排序拼接，整章内容以合并结果写入；
    # 每段摘要前加小标题消解跨段指代（#21 M2：块 ID＝出生序不承诺连续，区间表述废止，
    # 改「第n子包·m块」/ 整章包「全章·n块」）
    entries = _collect_filled_subpacks(book_title, chapter_no, extra_file=filled_path)
    merged_tag_rows = [r for e in entries for r in e["标签行"]]
    merged_eval_rows = [r for e in entries for r in e["评测题行"]]
    multi = sum(1 for e in entries if e["摘要"]) > 1
    seg_no = 0

    def _seg_title(e: dict) -> str:
        nonlocal seg_no
        seg_no += 1
        if multi:
            return f"**【第{seg_no}子包·{e['块数']}块】**"
        return f"**【全章·{e['块数']}块】**"

    merged_summary = "\n\n".join(
        f"{_seg_title(e)}\n{e['摘要']}"
        for e in entries if e["摘要"]).strip()
    merged_tags = list(dict.fromkeys(
        t.strip() for row in merged_tag_rows
        for t in re.split(r"[,，、/]", row["标签"]) if t.strip()))

    # ① 章节摘要.md（页码经 parse_page_tag 拼接，降级标签透传 PDF.x，见 4.1/4.9）
    anchor_lines = ["| 块ID | 页 | 指纹（首个有效正文行前12字） |", "|---|---|---|"]
    for b in real_blocks:
        anchor_lines.append(f"| {b['块ID']} | p.{b['页码']} | {b['指纹']} |")
    summary_text = [
        f"# {chapter_no} {chapter['章名']}",
        f"起始块ID：{real_blocks[0]['块ID']} | 块数：{len(real_blocks)} | 页码：{_chapter_page_range(real_blocks)}",
        "",
        "## 摘要",
        merged_summary,
        "",
        "## 标签",
        ", ".join(merged_tags),
        "",
        "## 锚点表",
    ] + anchor_lines + [""]
    summary_dir = book_dir / "章节摘要"
    summary_dir.mkdir(parents=True, exist_ok=True)
    (summary_dir / f"{chapter_no}.md").write_text("\n".join(summary_text), encoding="utf-8")

    # ①-b #66：按块持久化 块标签.json（外发与 API 两通道同一落点）
    _save_block_tags(book_title, chapter["书号"], merged_tag_rows, kb_root)

    # ② 术语候选 → 输出\验证\{书名}_术语候选.json（与既有候选合并，避免互相覆盖）
    term_candidates = []
    for row in merged_tag_rows:
        for col_name in ("术语候选", "别名候选"):
            for t in re.split(r"[,，、/]", row[col_name]):
                t = t.strip()
                if t and "存疑" not in t:
                    term_candidates.append({"术语": t, "块ID": row["块ID"], "来源": col_name})
    verify_dir = common.get_path("输出") / "验证"
    verify_dir.mkdir(parents=True, exist_ok=True)
    cand_path = verify_dir / f"{book_title}_术语候选.json"
    existing_cands = []
    if cand_path.exists():
        try:
            existing_cands = json.loads(cand_path.read_text(encoding="utf-8"))
        except Exception:
            existing_cands = []
    seen = {t.get("术语") for t in existing_cands}
    for t in term_candidates:
        if t["术语"] not in seen:
            seen.add(t["术语"])
            existing_cands.append(t)
    cand_path.write_text(json.dumps(existing_cands, ensure_ascii=False, indent=2), encoding="utf-8")

    # ③ 评测题 → 评测集.md（含密文：查 评测原句.txt，同块多针按序轮转；按提问+答案块ID去重）
    evals = _eval_sentences(book_title)
    needles_by_block = {}
    for e in evals:
        needles_by_block.setdefault(e["块ID"], []).append(e["密文"])
    used = {}
    eval_rows_out = []
    for row in merged_eval_rows:
        bid = row["答案块ID"]
        needles = needles_by_block.get(bid, [""])
        pos = min(used.get(bid, 0), len(needles) - 1)
        used[bid] = used.get(bid, 0) + 1
        eval_rows_out.append({"题号": row["题号"], "提问": row["提问"],
                              "答案块ID": bid, "密文": needles[pos]})
    if eval_rows_out:
        _append_evals_dedup(eval_rows_out, kb_root)

    # 记录该子包已导回（整章以「整章」占位，避免写空文件后读回丢失）
    done.add(sub)
    token = "\n".join("整章" if s == "" else s for s in sorted(done))
    (pack_dir(book_title) / "回填" / f"{chapter_no}.done").write_text(token, encoding="utf-8")

    _write_manifest(book_title, kb_root)
    return {"通过": True, "已存在": False, "失败行": [], "摘要": merged_summary,
            "标签行": merged_tag_rows, "评测题行": eval_rows_out}


# ===== #42 术语自动配对（术语确认功能重构：全自动异名配对，人工零操作） =====

TERM_PAIR_BATCH = 500          # 候选超 500 条分批（控 prompt 规模）
TERM_PAIR_MAX_TOKENS = 16000   # #24/#29 口径：推理模型思考链先吃额度，禁止调小

_TERM_PAIR_SYSTEM_PROMPT = """你是「术语同义词配对」整理器。用户会给你一本书 AI 标注产出的候选术语清单\
（每条一个独立词，可能混有术语候选与别名候选），需要你找出其中**有异名关系**的词（同一概念的不同叫法\
/派别异名/繁体异体等），配成「主词条 + 别名」对。

只输出一个 JSON 对象，不要输出任何解释文字、不要使用代码围栏。字段：
- "词条"：数组，每项为 {"主词条": "...", "别名": ["...", "..."]}。

规则：
- **只输出有异名关系的词对**：每个主词条至少一个别名；孤词（无别名）一律不输出——孤词对检索扩词零增益。
- 主词条与别名都必须**逐字取自候选清单**，禁止编造、禁止改写、禁止组合出清单里没有的词。
- 主词条不得等于自己的别名。
- **主词条与别名归一（简体/去标点）后必须不同**——检索端已做简繁归一，简体/繁体异写\
（如 排龙诀/排龍訣）不是有效异名，不要配对。
- 同一词只能作主词条一次；若某词既是别人的别名又另有别名，以它为主词条。
- 别名之间不得重复。"""


def _term_candidates(book_title: str) -> list:
    """读 输出\\验证\\{书名}_术语候选.json → 去重词列表（首现序，过滤空/【存疑】）。"""
    cand_path = common.get_path("输出") / "验证" / f"{book_title}_术语候选.json"
    if not cand_path.exists():
        return []
    try:
        cands = json.loads(cand_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    seen, out = set(), []
    for c in cands:
        t = str(c.get("术语", "")).strip()
        if not t or "存疑" in t:
            continue
        key = search_keyword.normalize(t)
        if key and key not in seen:
            seen.add(key)
            out.append(t)
    return out


def _parse_pair_json(text: str) -> dict:
    """剥离代码围栏并解析最外层 JSON 对象；失败抛 ValueError。"""
    s = re.sub(r"^```[a-zA-Z]*\s*", "", (text or "").strip())
    s = re.sub(r"```\s*$", "", s).strip()
    m = re.search(r"\{.*\}", s, re.S)
    if not m:
        raise ValueError("未找到 JSON 对象")
    obj = json.loads(m.group(0))
    if not isinstance(obj, dict):
        raise ValueError("返回不是 JSON 对象")
    return obj


def _validate_term_pairs(obj: dict, pool: dict) -> tuple:
    """硬校验 LLM 配对产出 → (规范化词条列表 or None, 失败原因)。

    pool: {normalize词: 候选原始词}（当批候选集合）。任一条不过 → 整批判失败、不部分写入
    （防 AI 编造词入表：主词条与别名都必须 ⊆ 当批候选集合）。"""
    raw = obj.get("词条")
    if not isinstance(raw, list):
        return None, "「词条」非数组"
    pairs = []
    for i, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            return None, f"第 {i} 条不是对象"
        main = str(item.get("主词条", "") or "").strip()
        if not main:
            return None, f"第 {i} 条主词条为空"
        aliases = item.get("别名")
        if not isinstance(aliases, list) or not aliases:
            return None, f"第 {i} 条「{main}」无别名（孤词不应输出）"
        main_key = search_keyword.normalize(main)
        if main_key not in pool:
            return None, f"第 {i} 条主词条「{main}」不在候选集合（疑似编造）"
        canon, seen = [], set()
        for a in aliases:
            a = str(a or "").strip()
            a_key = search_keyword.normalize(a)
            if not a_key:
                return None, f"第 {i} 条别名非法：{a!r}"
            if a_key == main_key:
                return None, f"第 {i} 条「{main}」自指别名（主词条等于别名）"
            if a_key not in pool:
                return None, f"第 {i} 条别名「{a}」不在候选集合（疑似编造）"
            if a_key in seen:
                continue  # 别名重复：去重容忍，不判失败
            seen.add(a_key)
            canon.append(pool[a_key])
        pairs.append({"主词条": pool[main_key], "别名": canon})
    return pairs, ""


def _merge_term_table(kb_root: Path, pairs: list) -> int:
    """配对结果合并写入 术语同义词表.md（表头与两行格式不变 → search_keyword 消费端零改动）。

    主词条（normalize 键）已存在 → 别名并入去重；新词条 → 追加行。返回新增主词条数。"""
    path = kb_root / "术语同义词表.md"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# 术语同义词表\n\n| 主词条 | 别名/异体/派别异名 |\n|---|---|\n", encoding="utf-8")
    lines = path.read_text(encoding="utf-8").split("\n")
    idx_by_key = {}
    for i, ln in enumerate(lines):
        s = ln.strip()
        if not s.startswith("|") or s.startswith("|---") or s.startswith("| 主词条"):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if len(cells) < 2 or not cells[0]:
            continue
        k = search_keyword.normalize(cells[0])
        if k:
            idx_by_key[k] = i
    added = 0
    for p in pairs:
        main = p["主词条"]
        key = search_keyword.normalize(main)
        new_aliases = list(dict.fromkeys(p["别名"]))
        i = idx_by_key.get(key)
        if i is not None:
            cells = [c.strip() for c in lines[i].strip("|").split("|")]
            cur = cells[1] if len(cells) > 1 else ""
            if re.match(r"^（来自.*待补别名）$", cur.strip()):  # 旧占位别名，配对后替换
                cur = ""
            existing = [a.strip() for a in re.split(r"[,，/]", cur) if a.strip()]
            merged = list(dict.fromkeys(existing + new_aliases))
            cells[1] = ", ".join(merged)
            lines[i] = "| " + " | ".join(cells) + " |"
        else:
            lines.append(f"| {main} | {', '.join(new_aliases)} |")
            added += 1
    path.write_text("\n".join(lines), encoding="utf-8")
    return added


def _write_pair_report(book_title: str, reasons: str):
    """黄灯报告（#14 待办清单口径：非绿落盘 + 统一附销单指引）。"""
    report = (
        f"# 术语自动配对报告：《{book_title}》\n"
        f"结论：🟡 部分候选未配对（只告警不阻塞）\n\n"
        f"⚠️ 黄灯（只告警不阻塞，退出码 2）：\n"
        f"- 以下批次候选未能自动配对，候选已保留，下次导回后会自动重试：\n{reasons}\n"
        f"- 处置：可运行 `python 资料预处理\\脚本\\annotate_pack.py --术语配对 --书名 \"{book_title}\"` "
        f"立即重试；重试成功（全绿）后本报告自动删除。\n")
    common.write_report(f"{book_title}_术语配对.md", report)


def pair_terms(book_title: str, kb_root: Path = None, on_line=print) -> dict:
    """#42 术语自动配对：候选 json → LLM 配对（>500 条分批）→ 硬校验 → 合并写表。

    角色＝一次性翻译器（同 A-08）：LLM 产出 → 程序硬校验 → 落盘生效，之后纯确定性消费。
    触发时机＝导回后自动跑（import_all / auto_fill 收尾），单章导回不触发；CLI --术语配对 供存量补跑/重试。
    成功批的词条（含未配对的孤词）从候选清出，失败批保留待重试（幂等+省钱：已处理候选不再送 AI）。

    返回 {"配对": int(新写/更新词条数), "跳过": str|None, "批数": int, "失败批数": int}。
    降级：未配置标注 AI 时只返回跳过（不写报告——外发通道未配 API 属常态，由调用方决定提示口径）；
    调用失败/硬校验不过 → 写 报告\\{书名}_术语配对.md（黄灯+销单指引，#14），不阻塞导回主流程。"""
    cands = _term_candidates(book_title)
    if not cands:
        common.clear_report(f"{book_title}_术语配对.md")
        return {"配对": 0, "跳过": None, "批数": 0, "失败批数": 0}
    if not llm_client.is_configured():
        return {"配对": 0, "跳过": "未配置标注 AI（术语自动配对跳过，不影响导回）", "批数": 0, "失败批数": 0}
    if kb_root is None:
        kb_root = common.get_path("知识库")
    batches = [cands[i:i + TERM_PAIR_BATCH] for i in range(0, len(cands), TERM_PAIR_BATCH)]
    done_keys, failed, total_added = set(), [], 0
    for bi, batch in enumerate(batches, 1):
        pool = {search_keyword.normalize(t): t for t in batch}
        user = (f"书籍：《{book_title}》\n"
                f"候选术语（第 {bi}/{len(batches)} 批，共 {len(batch)} 条）：\n"
                + "\n".join(f"- {t}" for t in batch)
                + "\n\n请只输出 JSON。")
        try:
            out = llm_client.chat(_TERM_PAIR_SYSTEM_PROMPT, user,
                                  max_tokens=TERM_PAIR_MAX_TOKENS, temperature=0.1)
        except RuntimeError as e:
            failed.append((bi, f"LLM 调用失败：{e}"))
            on_line(f"🟡 术语自动配对：第 {bi}/{len(batches)} 批调用失败（{book_title}）：{e}")
            continue
        try:
            obj = _parse_pair_json(out)
            pairs, err = _validate_term_pairs(obj, pool)
        except Exception as e:
            failed.append((bi, f"LLM 返回非合法 JSON：{e}"))
            on_line(f"🟡 术语自动配对：第 {bi}/{len(batches)} 批输出非法（{book_title}）：{e}")
            continue
        if pairs is None:
            failed.append((bi, f"硬校验不过：{err}"))
            on_line(f"🟡 术语自动配对：第 {bi}/{len(batches)} 批硬校验不过（{book_title}）：{err}")
            continue
        total_added += _merge_term_table(kb_root, pairs)
        done_keys.update(search_keyword.normalize(t) for t in batch)  # 整批已处理（孤词丢弃，避免再送 AI）
        on_line(f"✅ 术语自动配对：第 {bi}/{len(batches)} 批成功（{book_title}，新写/更新 {len(pairs)} 条异名词条）")
    # 候选清空（全成）或保留失败批（下次导回后自动重试）
    rest = [t for t in cands if search_keyword.normalize(t) not in done_keys]
    cand_path = common.get_path("输出") / "验证" / f"{book_title}_术语候选.json"
    if rest:
        cand_path.write_text(json.dumps([{"术语": t} for t in rest], ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        cand_path.unlink(missing_ok=True)
    if failed:
        _write_pair_report(book_title, "\n".join(f"- 第 {b} 批：{r}" for b, r in failed))
    else:
        common.clear_report(f"{book_title}_术语配对.md")
    return {"配对": total_added, "跳过": None, "批数": len(batches), "失败批数": len(failed)}


def import_all(book_title: str, kb_root: Path = None) -> dict:
    """一键导回：扫描 回填\\*.md 批量导回。返回 {"成功":[], "失败":[(章号,失败行)], "跳过":[], "无法识别":[]}。"""
    if kb_root is None:
        kb_root = common.get_path("知识库")
    back_dir = pack_dir(book_title) / "回填"
    if not back_dir.exists():
        return {"成功": [], "失败": [], "跳过": [], "无法识别": []}
    ok, bad, skipped, unknown = [], [], [], []
    for f in sorted(back_dir.glob("*.md")):
        if f.name.endswith(".done"):
            continue
        m = FILLED_PACK_RE.match(f.name)
        if not m:
            unknown.append(f.name)
            continue
        ch, sub = m.group(1), m.group(2)
        try:
            r = import_pack(book_title, ch, sub, f, kb_root)
            if r.get("已存在"):
                skipped.append(ch + sub)
            elif r["通过"]:
                ok.append(ch + sub)
            else:
                bad.append((ch + sub, r["失败行"]))
        except Exception as e:
            bad.append((ch + sub, [str(e)]))
    _write_manifest(book_title, kb_root)
    # #42 术语自动配对（导回后自动跑；未配置标注 AI 时跳过只提示，不写报告不阻塞）
    try:
        r = pair_terms(book_title, kb_root)
        if r.get("跳过"):
            print(f"⏭️ {r['跳过']}")
        elif r["失败批数"]:
            print(f"🟡 术语自动配对：配对 {r['配对']} 条，失败 {r['失败批数']}/{r['批数']} 批"
                  f"（候选已保留，详见 报告\\{book_title}_术语配对.md）")
    except Exception as e:
        print(f"⚠️ 术语自动配对异常（不影响导回）：{e}")
    return {"成功": ok, "失败": bad, "跳过": skipped, "无法识别": unknown}


def _strip_code_fence(text: str) -> str:
    """去掉 AI 回答外层可能包裹的 markdown 代码围栏。"""
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\n", "", t)
        t = re.sub(r"\n```\s*$", "", t)
    return t


# ===== #35 瘦输出协议（API 自动回填专用，与外发通道 _任务说明.md 分离）=====

def _api_system_prompt(book_title: str, kb_root: Path) -> str:
    """API 自动回填专用 system prompt。与 _任务说明.md 分离（该文件是外发 AI 说明书，
    共用会把外发流程带偏）。瘦输出契约 + 质量规则加严；全书各包共用同一文本
    （字节级稳定，供服务端输入缓存命中）。"""
    chapters = _read_skeleton(book_title, kb_root)
    sk_path = kb_root / "书籍" / book_title / "目录骨架.md"
    book_id = ""
    for line in sk_path.read_text(encoding="utf-8").split("\n"):
        if line.startswith("书号："):
            book_id = line.replace("书号：", "").strip()
            break
    ch_lines = "\n".join(f"- 第 {c['章号']} 章《{c['章名']}》" for c in chapters)
    return (
        f"# 标注任务：《{book_title}》（API 自动通道）\n"
        f"书号：{book_id} | 共 {len(chapters)} 章\n\n"
        "## 任务\n"
        "用户消息是一个标注任务包（含原文与空表格）。请阅读包内原文，完成三件事：\n"
        "1. 写摘要：整章包写「章摘要」（约 10 句）；子包写「本段摘要」（约 3~5 句）；\n"
        "2. 给每个块填标签（5~8 个/块，逗号分隔，中英文逗号均可）、术语候选、别名候选"
        "（不确定的标【存疑】）；\n"
        "3. 把包内【评测原句】逐句改写为口语化提问（评测题），答案块ID不变。\n\n"
        "## 输出格式（严格遵守，只输出以下三段，禁止回显原文与指纹）\n"
        "## 本段摘要（或：## 章摘要）\n"
        "（摘要正文）\n"
        "## 块标签\n"
        "| 块ID | 标签 | 术语候选 | 别名候选 |\n"
        "|---|---|---|---|\n"
        "（每个块一行；块ID 逐字照抄包内表格第一列，禁止增减块、禁止重复）\n"
        "## 评测题\n"
        "| 题号 | 提问 | 答案块ID |\n"
        "|---|---|---|\n"
        "（每条评测原句一行；题号沿用包内占位题号；包内无评测原句则整段省略）\n\n"
        "## 填写规范\n"
        "- 摘要直接陈述内容本身，禁止「本段」「接续前文」「上文提到」等承接词；\n"
        "- 每个块的标签须体现该块独有的内容维度（对象/方法/图表名称/时间范围等），"
        "同包内任意两块标签高度雷同将判失败；\n"
        "- 术语候选与别名候选应尽量出自原文；个别属于该块实际所讲内容、但原文未逐字出现的"
        "领域术语可保留并标【存疑】；禁止引用其他块的内容、禁止凭空编造；\n"
        "- 标签与别名候选不得使用干支组合（如 己酉）、大运/流年干支（如 大运壬子）、命例编号；\n"
        "- 术语候选须为可复用的学术语，单条填写、不带括号注解"
        "（如写「婚姻星」而非「婚姻星（配偶星）」）；\n"
        "- 评测题提问必须是内容型提问（问「是什么/如何/为何」），禁止「哪一块」「第几块」"
        "「标题在哪」一类问位置的退化提问，禁止与评测原句字面雷同。\n\n"
        "## 忠实原文要求（铁律）\n"
        "- 摘要与标签必须基于所给原文，禁止补充原文没有的内容；\n"
        "- 不确定的术语标【存疑】，不要编造。\n\n"
        "## 本书章节\n" + ch_lines + "\n"
    )


def _parse_thin(resp: str) -> dict:
    """解析瘦输出。返回 {摘要, 标签行[{块ID,标签,术语候选,别名候选}], 评测题行[{题号,提问,答案块ID}]}。"""
    summary_buf, tag_rows, eval_rows = [], [], []
    section = None
    for i, ln in enumerate(resp.split("\n")):
        s = ln.strip()
        if s.startswith("## "):
            if "摘要" in s:
                section = "摘要"
            elif "标签" in s:
                section = "标签"
            elif "评测" in s:
                section = "评测"
            else:
                section = None
            continue
        if section == "摘要":
            if s:
                summary_buf.append(s)
            continue
        if not s.startswith("|") or s.startswith("|---"):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if section == "标签":
            if not re.match(r"^【?.+·\d{4}", cells[0]):
                continue
            tag_rows.append({"行号": i + 1, "块ID": cells[0].strip("【】"),
                             "标签": cells[1] if len(cells) > 1 else "",
                             "术语候选": cells[2] if len(cells) > 2 else "",
                             "别名候选": cells[3] if len(cells) > 3 else ""})
        elif section == "评测":
            if len(cells) >= 3 and re.match(r"^Q?\d+", cells[0]):
                eval_rows.append({"行号": i + 1, "题号": cells[0], "提问": cells[1],
                                  "答案块ID": cells[2].strip("【】")})
    return {"摘要": "\n".join(summary_buf).strip(), "标签行": tag_rows, "评测题行": eval_rows}


def _validate_thin(thin: dict, expected_ids: list, eval_ph: list, eval_src: dict) -> list:
    """机械闸（#35 设计 §3.3）。返回失败行列表（空=通过）。
    硬闸三道：①块ID集合等价 ③同包标签重合度≥0.8 ④退化评测题；
    原闸②（术语出自原文）实测误伤合理领域推断词（排盘块的山星/向星等），
    降为软闸：不拦截，合成时对包内原文找不到的词自动标【存疑】（见 _compose_filled），
    最终把关交回既有术语确认窗口（人工）。"""
    fails = []
    got = [r["块ID"] for r in thin["标签行"]]
    dup = sorted({b for b in got if got.count(b) > 1})
    missing = [b for b in expected_ids if b not in got]
    extra = [b for b in got if b not in expected_ids]
    if dup:
        fails.append(f"块ID重复返回：{'、'.join(dup)}")
    if missing:
        fails.append(f"缺块（未返回）：{'、'.join(missing[:5])}{' 等' if len(missing) > 5 else ''}")
    if extra:
        fails.append(f"多块（本包不存在）：{'、'.join(extra[:5])}{' 等' if len(extra) > 5 else ''}")
    if not thin["摘要"]:
        fails.append("摘要为空")
    for r in thin["标签行"]:
        if not r["标签"].strip():
            fails.append(f"块 {r['块ID']} 标签为空")
    tag_sets = []
    for r in thin["标签行"]:
        tag_sets.append((r["块ID"], {t.strip() for t in re.split(r"[,，、/]", r["标签"]) if t.strip()}))
    for i in range(len(tag_sets)):
        for j in range(i + 1, len(tag_sets)):
            a, sa = tag_sets[i]
            b, sb = tag_sets[j]
            if not sa or not sb:
                continue
            jac = len(sa & sb) / len(sa | sb)
            if jac >= 0.8:
                fails.append(f"块 {a} 与块 {b} 标签高度雷同（重合度 {jac:.0%}），请分别加入该块独有维度加以区分"
                             "（如对应案例名/方位/序号/图表名；若两块是同一宅运盘图复现，标注各自所属案例）")
    ph_qs = [re.sub(r"[^0-9A-Za-z]", "", r["题号"]) for r in eval_ph]
    ph_ans = {re.sub(r"[^0-9A-Za-z]", "", r["题号"]): r["答案块ID"] for r in eval_ph}
    got_qs = [re.sub(r"[^0-9A-Za-z]", "", r["题号"]) for r in thin["评测题行"]]
    miss_q = [q for q in ph_qs if q not in got_qs]
    if miss_q:
        fails.append(f"评测题缺失：{'、'.join('Q' + q for q in miss_q)}")
    for r in thin["评测题行"]:
        q = r["提问"].strip()
        if not q:
            fails.append(f"评测题 {r['题号']} 提问为空")
            continue
        qkey = re.sub(r"[^0-9A-Za-z]", "", r["题号"])
        if qkey in ph_ans and r["答案块ID"] != ph_ans[qkey]:
            fails.append(f"评测题 {r['题号']} 答案块ID被改动（应为 {ph_ans[qkey]}，不得改动）")
            continue
        if re.search(r"哪一块|哪块|第几块|哪个块|标题在哪|位于哪", q):
            fails.append(f"评测题「{q[:20]}…」为退化提问（问位置/答案即题干），请改为内容型提问")
            continue
        src = eval_src.get(r["答案块ID"], "")
        nq, ns = search_keyword.normalize(q), search_keyword.normalize(src)
        if len(ns) >= 6 and (ns in nq or nq in ns):
            fails.append(f"评测题「{q[:20]}…」与评测原句字面重合，请口语化改写")
        if r["答案块ID"] not in expected_ids:
            fails.append(f"评测题答案块ID {r['答案块ID']} 不在本包")
    return fails


def _clean_term_cell(cell: str, pack_norm: str) -> str:
    """清洗术语候选/别名候选列（#35 软闸）：剔除「无/-」占位；包内原文找不到的词
    自动标【存疑】（下游 import_pack 不带入确认页，防编造词污染术语表）。"""
    terms = []
    for t in re.split(r"[,，、/]", cell):
        t = t.strip()
        if not t or t in ("无", "無", "-", "—", "无。"):
            continue
        if "存疑" not in t and pack_norm and search_keyword.normalize(t) not in pack_norm:
            t += "【存疑】"
        terms.append(t)
    return ", ".join(terms)


def _compose_filled(pack_text: str, thin: dict, pack_norm: str = "") -> str:
    """程序合成标准回填文件：以原任务包为底，摘要/标签/术语/别名/评测提问来自瘦输出，
    指纹列保留原包预填值（程序真源，替代 AI 回显）；术语列经 _clean_term_cell 软闸清洗。"""
    tags = {r["块ID"]: r for r in thin["标签行"]}
    evals = {re.sub(r"[^0-9A-Za-z]", "", r["题号"]): r for r in thin["评测题行"]}
    out = []
    for ln in pack_text.split("\n"):
        s = ln.strip()
        if s.startswith("（在此填写"):
            out.append(thin["摘要"])
            continue
        if s.startswith("|") and not s.startswith("|---"):
            cells = [c.strip() for c in s.strip("|").split("|")]
            if len(cells) >= 5 and re.match(r"^【?.+·\d{4}", cells[0]):
                t = tags.get(cells[0].strip("【】"))
                if t:
                    out.append(f"| {cells[0]} | {cells[1]} | {t['标签']} | "
                               f"{_clean_term_cell(t['术语候选'], pack_norm)} | "
                               f"{_clean_term_cell(t['别名候选'], pack_norm)} |")
                    continue
            elif len(cells) == 3 and re.match(r"^Q?\d+$", cells[0]) and not cells[1]:
                e = evals.get(re.sub(r"[^0-9A-Za-z]", "", cells[0]))
                if e:
                    out.append(f"| {cells[0]} | {e['提问']} | {cells[2]} |")
                    continue
        out.append(ln)
    return "\n".join(out)


def auto_fill(book_title: str, chapter_no: str = None, kb_root: Path = None,
              on_line=print) -> dict:
    """API 自动回填（#35 瘦输出协议）：并发调标注 AI 取「摘要+块标签+评测题」瘦输出，
    过四道闸后由程序合成标准回填文件（指纹列程序回填），主线程串行走 import_pack 导回。
    失败把校验报错喂回 AI 重试（每包至多 AUTO_FILL_MAX_RETRY 次），仍败跳过。
    任务包缺失自动生成；.done 幂等，已导回子包自动跳过。
    返回 {"成功":[], "失败":[(包名,原因)], "跳过":[]}。API 层错误（未配置/断网/鉴权）直接抛 RuntimeError。"""
    if kb_root is None:
        kb_root = common.get_path("知识库")
    pdir = pack_dir(book_title)
    chapters = _read_skeleton(book_title, kb_root)
    if chapter_no:
        chapters = [c for c in chapters if c["章号"] == chapter_no]
        if not chapters:
            raise ValueError(f"章号 {chapter_no} 不存在")
    instructions = (pdir / "_任务说明.md")
    if not instructions.exists():
        generate_all_packs(book_title, kb_root)  # 首次：生成全书任务包（外发通道说明书同步生成）
    sys_prompt = _api_system_prompt(book_title, kb_root)

    # 任务收集（跳过已导回）；评测原句与包级归一文本一次提取（闸④/软闸复用）
    tasks, skipped = [], []
    for c in chapters:
        blocks = _extract_chapter_blocks(book_title, c, kb_root)
        done = _chapter_done_set(book_title, c["章号"])
        for sub in subpack_suffixes(_subpack_count(blocks)):
            name = pack_filename(c["章号"], sub)
            if sub in done:
                skipped.append(name)
                continue
            pack_file = pdir / name
            if not pack_file.exists():
                generate_pack(book_title, c["章号"], kb_root)
            pack_text = pack_file.read_text(encoding="utf-8")
            parsed = _parse_filled(pack_text)  # 未填模板：标签行 5 列空、评测题提问空
            eval_src = {}
            for m in re.finditer(r"^-\s*【(.+?)】\s*(.+)$", pack_text, re.M):
                eval_src[m.group(1)] = m.group(2).strip()
            tasks.append({"name": name, "章号": c["章号"], "sub": sub,
                          "pack_text": pack_text,
                          "expected": [r["块ID"] for r in parsed["标签行"]],
                          "eval_ph": parsed["评测题行"],
                          "eval_src": eval_src,
                          "包norm": search_keyword.normalize(pack_text)})

    conc = llm_client.get_annotate_config()["并发"]
    log_lock = threading.Lock()

    def log(msg: str):
        with log_lock:
            on_line(msg)

    def worker(t: dict):
        """纯内存取结果（可并发）：调用 → 解析 → 四道闸；失败喂回重试。
        致命错误（未配置/Key 无效/404/断网）直接抛出中止全书；单包调用失败
        （截断/空正文/服务端错误等）记为一次失败尝试，重试耗尽可能只跳过该包。"""
        errors = []
        call_failed = False
        for attempt in range(1, AUTO_FILL_MAX_RETRY + 1):
            user_msg = t["pack_text"]
            if errors and not call_failed:
                user_msg += ("\n\n# 上次输出未通过程序校验，请按下述问题修正后重新输出"
                             "（仍按三段瘦格式，禁止回显原文与指纹）\n"
                             + "\n".join(f"- {e}" for e in errors[:20]))
            log(f"[{t['name']}] 第 {attempt}/{AUTO_FILL_MAX_RETRY} 次调用标注 AI…")
            try:
                resp = _strip_code_fence(
                    llm_client.chat(sys_prompt, user_msg, max_tokens=AUTO_FILL_MAX_TOKENS))
            except RuntimeError as e:
                msg = str(e)
                if any(k in msg for k in ("未配置", "API Key 无效", "接口 404", "网络请求失败", "请求超时")):
                    raise
                errors = [f"调用失败：{msg}"]
                call_failed = True
                log(f"[{t['name']}] {msg}"
                    + ("" if attempt < AUTO_FILL_MAX_RETRY else "（已达重试上限）"))
                continue
            call_failed = False
            thin = _parse_thin(resp)
            t["last_resp"] = resp
            errors = _validate_thin(thin, t["expected"], t["eval_ph"], t["eval_src"])
            if not errors:
                return t, thin, []
            log(f"[{t['name']}] 校验未过 {len(errors)} 处，"
                + ("喂回重试" if attempt < AUTO_FILL_MAX_RETRY else "已达重试上限"))
        return t, None, errors

    results = []
    if tasks:
        with ThreadPoolExecutor(max_workers=conc) as ex:
            for res in ex.map(worker, tasks):
                results.append(res)

    # 落盘串行（tasks 构造顺序即 章号+子包 字典序；共享产物读改写全部在主线程）
    ok, bad = [], []
    (pdir / "回填").mkdir(parents=True, exist_ok=True)
    for t, thin, errors in results:
        if thin is None:
            bad.append((t["name"], errors))
            if t.get("last_resp"):
                dbg = pdir / "回填" / f"_调试_{t['name']}"
                dbg.write_text(t["last_resp"], encoding="utf-8")
                log(f"[{t['name']}] 最后一次模型原件已留档：回填\\{dbg.name}")
            continue
        (pdir / "回填" / t["name"]).write_text(
            _compose_filled(t["pack_text"], thin, t["包norm"]), encoding="utf-8")
        r = import_pack(book_title, t["章号"], t["sub"], None, kb_root)
        if r["通过"]:
            ok.append(t["name"])
            log(f"[{t['name']}] ✅ 导回成功")
        else:
            bad.append((t["name"], r["失败行"]))
            log(f"[{t['name']}] ❌ 导回校验未过 {len(r['失败行'])} 处")
    _write_manifest(book_title, kb_root)
    # #42 术语自动配对（API 通道收尾自动跑；失败只黄灯不阻塞，退出码仍按回填结果）
    try:
        r = pair_terms(book_title, kb_root, on_line=log)
        if r.get("跳过"):
            log(f"⏭️ {r['跳过']}")
        elif r["失败批数"]:
            log(f"🟡 术语自动配对：配对 {r['配对']} 条，失败 {r['失败批数']}/{r['批数']} 批"
                f"（候选已保留，详见 报告\\{book_title}_术语配对.md）")
        elif r["配对"]:
            log(f"✅ 术语自动配对完成：新写/更新 {r['配对']} 条异名词条（{book_title}）")
    except Exception as e:
        log(f"⚠️ 术语自动配对异常（不影响导回）：{e}")
    return {"成功": ok, "失败": bad, "跳过": skipped}


def remove_packs(book_title: str, chapter_no: str = None, kb_root: Path = None):
    """删除任务包：带章号删单章（原包+回填+done 记录），不带章号删 输出\\任务包\\{书名}\\ 整个目录。"""
    if kb_root is None:
        kb_root = common.get_path("知识库")
    pdir = pack_dir(book_title)
    if chapter_no:
        for p in list(pdir.glob(f"{chapter_no}*_任务包.md")) + \
                 list((pdir / "回填").glob(f"{chapter_no}*_任务包.md")) + \
                 list((pdir / "回填").glob(f"{chapter_no}.done")):
            if p.exists():
                p.unlink()
        _write_manifest(book_title, kb_root)
    else:
        if pdir.exists():
            shutil.rmtree(pdir)


def _print_import_result(book_title: str, r: dict):
    if r.get("已存在"):
        print("该子包/本章已导回过（跳过重复写入）；如需重导，请先删除 回填\\ 下的 {章号}.done "
              "记录与 章节摘要\\ 下的对应文件")
        return
    if r["通过"]:
        term_json = common.get_path("输出") / "验证" / f"{book_title}_术语候选.json"
        term_count = 0
        if term_json.exists():
            term_count = len(json.loads(term_json.read_text(encoding="utf-8")))
        print(f"导回成功：章节摘要已写入，术语候选累计 {term_count} 条待确认，"
              f"评测题 {len(r['评测题行'])} 条")
    else:
        print("导回失败（整包未写入）：")
        for f in r["失败行"]:
            print(f"  ❌ {f}")


def main():
    args = sys.argv[1:]
    mode = None
    book_title = chapter_no = filled_path = None
    sub = ""
    for i, a in enumerate(args):
        if a == "--生成":
            mode = "生成"
        elif a == "--生成全书":
            mode = "生成全书"
        elif a == "--导回":
            mode = "导回"
        elif a == "--导回全书":
            mode = "导回全书"
        elif a == "--删除":
            mode = "删除"
        elif a == "--自动回填":
            mode = "自动回填"
        elif a == "--术语配对":
            mode = "术语配对"
        elif a == "--题干补写":
            mode = "题干补写"
        elif a == "--回收块标签":
            mode = "回收块标签"
        elif a == "--书名" and i + 1 < len(args):
            book_title = args[i + 1]
        elif a == "--章号" and i + 1 < len(args):
            chapter_no = args[i + 1]
        elif a == "--子包" and i + 1 < len(args):
            sub = args[i + 1]
        elif a == "--文件" and i + 1 < len(args):
            filled_path = args[i + 1]
    usage = ("用法：python annotate_pack.py --生成|--生成全书|--导回|--导回全书|--删除|--自动回填|--术语配对|--题干补写|--回收块标签 "
             "--书名 <书名> [--章号 <章号>] [--子包 <a|b|...>] [--文件 <回填文件>]")
    if not book_title and mode not in ("题干补写", "回收块标签"):
        print(usage)
        sys.exit(1)
    try:
        if mode == "生成":
            if not chapter_no:
                print("生成模式需要 --章号 <章号>")
                sys.exit(1)
            p = generate_pack(book_title, chapter_no)
            print(f"任务包已生成：{p}")
            sys.exit(0)
        elif mode == "生成全书":
            r = generate_all_packs(book_title)
            print(f"全书任务包生成完成：生成 {len(r['生成'])} 章，"
                  f"跳过 {len(r['跳过'])} 章（已导回），失败 {len(r['失败'])} 章")
            for ch, err in r["失败"]:
                print(f"  ❌ 第 {ch} 章：{err}")
            print(f"任务包目录：{pack_dir(book_title)}")
            sys.exit(0 if not r["失败"] else 1)
        elif mode == "导回":
            if not chapter_no:
                print("导回模式需要 --章号 <章号>")
                sys.exit(1)
            r = import_pack(book_title, chapter_no, sub, filled_path)
            _print_import_result(book_title, r)
            sys.exit(0 if r["通过"] else 1)
        elif mode == "导回全书":
            r = import_all(book_title)
            for ch in r["成功"]:
                print(f"  ✅ 第 {ch} 章 导回成功")
            for ch, fails in r["失败"]:
                print(f"  ❌ 第 {ch} 章 导回失败：")
                for f in fails:
                    print(f"     - {f}")
            for name in r["无法识别"]:
                print(f"  ⚠️ 无法识别文件名：{name}（应为 章号[子包]_任务包.md）")
            print(f"导回汇总：成功 {len(r['成功'])} 章，失败 {len(r['失败'])} 章，"
                  f"跳过 {len(r['跳过'])} 章（已导回），无法识别 {len(r['无法识别'])} 个文件")
            if r["失败"]:
                sys.exit(2 if r["成功"] else 1)
            sys.exit(0)
        elif mode == "删除":
            if chapter_no:
                remove_packs(book_title, chapter_no)
                print(f"已删除第 {chapter_no} 章任务包")
            else:
                remove_packs(book_title)
                print(f"已删除《{book_title}》全部任务包")
            sys.exit(0)
        elif mode == "自动回填":
            r = auto_fill(book_title, chapter_no)
            for name in r["成功"]:
                print(f"  ✅ {name} 自动回填并导回成功")
            for name in r["跳过"]:
                print(f"  ⏭️ {name} 已导回过（跳过）")
            for name, fails in r["失败"]:
                print(f"  ❌ {name} 自动回填失败（已达重试上限）：")
                for f in fails:
                    print(f"     - {f}")
            print(f"自动回填汇总：成功 {len(r['成功'])} 包，失败 {len(r['失败'])} 包，"
                  f"跳过 {len(r['跳过'])} 包")
            if r["失败"]:
                report = ["# 自动回填失败报告", ""]
                for name, fails in r["失败"]:
                    report.append(f"## {name}")
                    report.extend(f"- {f}" for f in fails)
                common.write_report(f"{book_title}_自动回填.md", "\n".join(report) + "\n")
                sys.exit(2 if r["成功"] or r["跳过"] else 1)
            common.clear_report(f"{book_title}_自动回填.md")
            sys.exit(0)
        elif mode == "术语配对":
            # #42 存量补跑/人工重试入口：0=成功/无候选，2=部分失败或未配置（降级）
            r = pair_terms(book_title)
            if r.get("跳过"):
                _write_pair_report(book_title, f"- 未配置标注 AI：{r['跳过']}")
                print(f"🟡 {r['跳过']}（详见 报告\\{book_title}_术语配对.md）")
                sys.exit(2)
            if r["失败批数"]:
                print(f"🟡 术语自动配对：配对 {r['配对']} 条，失败 {r['失败批数']}/{r['批数']} 批"
                      f"（候选已保留，详见 报告\\{book_title}_术语配对.md）")
                sys.exit(2)
            print(f"✅ 术语自动配对完成：新写/更新 {r['配对']} 条异名词条（{book_title}）"
                  + ("" if r["批数"] else "（无候选，幂等跳过）"))
            sys.exit(0)
        elif mode == "题干补写":
            # #4 M3（R4）存量补写入口：读全局评测集，题干 n-gram 挂进各书倒排表「题干」节（幂等）
            r = backfill_question_grams()
            if r.get("跳过"):
                print(f"🟡 {r['跳过']}")
                sys.exit(1)
            print(f"✅ 题干补写完成：{r['题干题数']} 道题 → {r['写入书数']} 本书倒排表「题干」节（幂等，重跑不重复）")
            if r["清理引用"]:
                print(f"🟡 同时清理题干失效引用 {r['清理引用']} 条（仅索引入口，未改评测集）")
            sys.exit(0)
        elif mode == "回收块标签":
            r = recover_block_tags(book_title)
            print(f"✅ 块标签回收完成：{r['回收书数']} 本书、共 {r['回收块数']} 块（零 API 费用）")
            for t, cnt in r["明细"].items():
                print(f"  - 《{t}》：{cnt} 块")
            for t, reason in r["跳过"]:
                print(f"  ⏭️ 《{t}》跳过：{reason}")
            sys.exit(0)
        else:
            print(usage)
            sys.exit(1)
    except Exception as e:
        print(f"错误：{e}")
        sys.exit(1)


def _selfcheck():
    """验收：批量生成结构/瘦身/评测句按章过滤/导回（不传文件路径）/幂等跳过/批量拦截。"""
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="annotate_selfcheck_"))
    fake_kb = tmp / "kb"
    book_dir = fake_kb / "书籍" / "自检书"
    book_dir.mkdir(parents=True)

    # 构造书（2 章，每章 2 块）
    orig_lines = [
        "【自检01·0001】p.1",
        "玄空之学首重理气阴阳五行之气运行",
        "",
        "【自检01·0002】p.2~3",
        "排龙之法以水口为主顺逆相推定其星曜",
        "",
        "【自检01·0003】p.4",
        "天干地支配合五行生克以断吉凶",
        "",
        "【自检01·0004】p.PDF.5",
        "十神枢纽明其气势通其变化",
    ]
    copy_lines = list(orig_lines)
    (book_dir / "原文.md").write_text("\n".join(orig_lines), encoding="utf-8")
    (book_dir / "检索副本.md").write_text("\n".join(copy_lines), encoding="utf-8")
    (book_dir / "目录骨架.md").write_text(
        "# 《自检书》目录骨架\n书号：自检01 | 总块数：4\n\n| 章号 | 章名 | 起始块ID | 块数 |\n|---|---|---|---|\n"
        "| 01 | 第一章 | 自检01·0001 | 2 |\n"
        "| 02 | 第二章 | 自检01·0003 | 2 |\n", encoding="utf-8")
    (book_dir / "章节摘要").mkdir(exist_ok=True)

    # 评测原句：两行分属两章（验证按章过滤）
    verify_dir = common.get_path("输出") / "验证"
    verify_dir.mkdir(parents=True, exist_ok=True)
    (verify_dir / "自检书_评测原句.txt").write_text(
        "自检01·0001 | ⌈验·甲一·子丑⌋ | 玄空之学首重理气\n"
        "自检01·0003 | ⌈验·甲二·寅卯⌋ | 天干地支配合五行\n", encoding="utf-8")

    ok = True
    # #42 防联网：自检期间把标注 AI 桩为未配置，杜绝中途导回触发真实 LLM 配对（烧额度）
    _sc_llm_conf = llm_client.is_configured
    llm_client.is_configured = lambda: False
    try:
        pdir = common.get_path("输出") / "任务包" / "自检书"
        fp1 = copy_lines[1].strip()[:12]
        fp2 = copy_lines[4].strip()[:12]

        # ① 批量生成：结构完整 + 瘦身 + 评测句按章过滤
        r = generate_all_packs("自检书", fake_kb)
        if r["生成"] != ["01", "02"]:
            print(f"  ❌ 批量生成章号不符：{r['生成']}")
            ok = False
        for name in ("_任务说明.md", "_清单.md", "01_任务包.md", "02_任务包.md"):
            if not (pdir / name).exists():
                print(f"  ❌ 缺 {name}")
                ok = False
        if not (pdir / "回填").is_dir():
            print("  ❌ 缺 回填\\ 目录")
            ok = False
        p01 = (pdir / "01_任务包.md").read_text(encoding="utf-8")
        if "## 任务" in p01:
            print("  ❌ 章包未瘦身（仍含大段任务说明）")
            ok = False
        if "> 上接块ID：无" not in p01 or "> 下接块ID：无" not in p01:
            print("  ❌ 01 章整包衔接行应为 上接无/下接无（§4.7）")
            ok = False
        if "玄空之学首重理气" not in p01 or "天干地支配合五行" in p01:
            print("  ❌ 01 章评测原句过滤错误")
            ok = False
        if "天干地支配合五行" not in (pdir / "02_任务包.md").read_text(encoding="utf-8"):
            print("  ❌ 02 章缺本章评测原句")
            ok = False
        p02 = (pdir / "02_任务包.md").read_text(encoding="utf-8")
        if "十神枢纽明其气势通其变化" not in p02:
            print("  ❌ 书尾最后一行（无尾随空行）被误删")
            ok = False
        print(f"  ① 批量生成：{r}")

        # ①c #15 书级标注状态：无任务包目录＝未开始；已生成未导回＝进行中 0/n
        st_none = book_annotation_status("无任务包书", fake_kb)
        if st_none["状态"] != "未开始":
            print(f"  ❌ #15 未开始态判定错误：{st_none}")
            ok = False
        st_gen = book_annotation_status("自检书", fake_kb)
        if (st_gen["状态"], st_gen["已导回"], st_gen["总章数"]) != ("进行中", 0, 2):
            print(f"  ❌ #15 已生成未导回态错误：{st_gen}")
            ok = False
        print(f"  ①c #15 标注状态（未导回）：{st_gen}")

        # ①b _任务说明.md 含外发 AI 自动导回指令；代码围栏剥离正确
        instr = (pdir / "_任务说明.md").read_text(encoding="utf-8")
        if "--导回全书" not in instr:
            print("  ❌ _任务说明.md 缺自动导回指令")
            ok = False
        if "无需处理该两行" not in instr:
            print("  ❌ _任务说明.md 缺衔接行说明句（§4.7）")
            ok = False
        if _strip_code_fence("```markdown\n# 标题\n```") != "# 标题":
            print("  ❌ 代码围栏剥离异常")
            ok = False
        print("  ①b 任务说明自动导回指令/围栏剥离：✅")

        # ② 回填 01 → 不传 filled_path 自动读 回填\01_任务包.md
        good = p01.replace(
            f"| 自检01·0001 | {fp1} |  |  |  |",
            f"| 自检01·0001 | {fp1} | 理气, 阴阳, 五行 | 玄空, 理气 | 玄空学 |")
        good = good.replace(
            f"| 自检01·0002 | {fp2} |  |  |  |",
            f"| 自检01·0002 | {fp2} | 排龙, 水口, 挨星 | 排龙诀 | 排龍訣 |")
        good = good.replace("（在此填写，替换本行）",
            "本章讲述玄空理气基础与排龙水法要诀，强调以水口为主顺逆排龙。")
        good = good.replace("| Q1 |  | 自检01·0001 |", "| Q1 | 玄空之学首重什么？ | 自检01·0001 |")
        # 带 BOM 写入，验证导回端 utf-8-sig 容错
        (pdir / "回填" / "01_任务包.md").write_text("﻿" + good, encoding="utf-8")
        r1 = import_pack("自检书", "01", "", None, fake_kb)
        if not r1["通过"]:
            print(f"  ❌ 01 章导回失败：{r1['失败行']}")
            ok = False
        if not (book_dir / "章节摘要" / "01.md").exists():
            print("  ❌ 01 章节摘要未写入")
            ok = False
        sa1 = (book_dir / "章节摘要" / "01.md").read_text(encoding="utf-8")
        if "页码：p.1~p.3" not in sa1:
            print(f"  ❌ 01 章页码范围拼接错误（应为 p.1~p.3）：{sa1.split(chr(10))[1] if chr(10) in sa1 else sa1}")
            ok = False
        if "| 自检01·0002 | p.2~3 |" not in sa1:
            print("  ❌ 锚点表未透传区间标签 p.2~3")
            ok = False
        print(f"  ② 01 章导回（自动读回填文件）：{'✅' if r1['通过'] else '❌'}")

        # ②b #15 标注状态：01 已导回、02 未导回 → 进行中 1/2
        st_part = book_annotation_status("自检书", fake_kb)
        if (st_part["状态"], st_part["已导回"], st_part["总章数"]) != ("进行中", 1, 2):
            print(f"  ❌ #15 部分导回态错误：{st_part}")
            ok = False

        # ③ 再生成全书：01 跳过（已导回），02 重新生成
        r2 = generate_all_packs("自检书", fake_kb)
        if "01" not in r2["跳过"] or "02" not in r2["生成"]:
            print(f"  ❌ 幂等跳过失效：{r2}")
            ok = False
        print(f"  ③ 再次生成：{r2}")

        # ④ 伪造 02 回填 → 批量导回拦截
        bad = (pdir / "02_任务包.md").read_text(encoding="utf-8").replace("自检01·0004", "自检01·9999")
        (pdir / "回填" / "02_任务包.md").write_text(bad, encoding="utf-8")
        r3 = import_all("自检书", fake_kb)
        hit = any(ch == "02" and any("9999" in f or "不存在" in f for f in fails)
                  for ch, fails in r3["失败"])
        if not hit:
            print(f"  ❌ 02 章伪造块ID未被拦截：{r3['失败']}")
            ok = False
        if len(r3["成功"]) != 0:
            print(f"  ❌ 01 章应跳过而非重复成功：{r3['成功']}")
            ok = False
        print(f"  ④ 批量导回：{r3['成功']} 成功 / {r3['失败']} 失败 / {r3['跳过']} 跳过")

        # ⑤ 清单状态
        manifest = (pdir / "_清单.md").read_text(encoding="utf-8")
        if "🟢已导入" not in manifest or "🟡已填写" not in manifest:
            print("  ❌ 清单状态错误")
            ok = False
        print("  ⑤ 清单状态：✅")

        # ⑥ 删除单章
        remove_packs("自检书", "02", fake_kb)
        if (pdir / "02_任务包.md").exists() or (pdir / "回填" / "02_任务包.md").exists():
            print("  ❌ 单章删除失败")
            ok = False
        print("  ⑥ 单章删除：✅")

        # ⑦ 拆子包：自检书2（第01章 25 块 > 20 → 自动拆 01a/01b）
        book2 = fake_kb / "书籍" / "自检书2"
        book2.mkdir(parents=True)
        o2_lines = []
        for i in range(25):
            o2_lines.append(f"【自检02·{i + 1:04d}】p.{i + 1}")
            o2_lines.append(f"块{i + 1}内容：玄空理气水法阴阳五行排龙立向之第{i + 1}条")
            o2_lines.append("")
        (book2 / "原文.md").write_text("\n".join(o2_lines), encoding="utf-8")
        (book2 / "检索副本.md").write_text("\n".join(o2_lines), encoding="utf-8")
        (book2 / "目录骨架.md").write_text(
            "# 《自检书2》目录骨架\n书号：自检02 | 总块数：25\n\n| 章号 | 章名 | 起始块ID | 块数 |\n|---|---|---|---|\n"
            "| 01 | 唯一章 | 自检02·0001 | 25 |\n", encoding="utf-8")
        (book2 / "章节摘要").mkdir(exist_ok=True)
        (verify_dir / "自检书2_评测原句.txt").write_text(
            "自检02·0001 | ⌈验·乙一·子丑⌋ | 玄空理气水法阴阳五行\n"
            "自检02·0014 | ⌈验·乙二·寅卯⌋ | 排龙立向之第十四条\n", encoding="utf-8")
        p2dir = common.get_path("输出") / "任务包" / "自检书2"

        def fill_pack(text, summary, eval_map):
            out = []
            for ln in text.split("\n"):
                m = re.match(r"^\| (自检02·\d{4}) \| (\S+) \|  \|  \|  \|$", ln)
                if m:
                    bid, fp = m.group(1), m.group(2)
                    out.append(f"| {bid} | {fp} | 标签{bid.split('·')[1]} | "
                               f"术语{bid.split('·')[1]} | 别名{bid.split('·')[1]} |")
                    continue
                out.append(ln)
            text = "\n".join(out).replace("（在此填写，替换本行）", summary)
            for q, (ask, bid) in eval_map.items():
                text = text.replace(f"| {q} |  | {bid} |", f"| {q} | {ask} | {bid} |")
            return text

        w2 = generate_pack("自检书2", "01", fake_kb)
        names2 = [p.name for p in w2]
        if names2 != ["01a_任务包.md", "01b_任务包.md"]:
            print(f"  ❌ 拆子包文件名不符：{names2}")
            ok = False
        pa = (p2dir / "01a_任务包.md").read_text(encoding="utf-8")
        pb = (p2dir / "01b_任务包.md").read_text(encoding="utf-8")
        if "块14内容" in pa or "块13内容" in pb:
            print("  ❌ 子包切分越界（a 应含 块1~13，b 应含 块14~25）")
            ok = False
        if "> 上接块ID：无" not in pa or "> 下接块ID：自检02·0014" not in pa:
            print("  ❌ 01a 衔接行错误（朴素均分：上接无 / 下接 0014）")
            ok = False
        if "> 上接块ID：自检02·0013" not in pb or "> 下接块ID：无" not in pb:
            print("  ❌ 01b 衔接行错误（朴素均分：上接 0013 / 下接 无）")
            ok = False

        fa = fill_pack(pa, "子包甲摘要：本章前半讲理气水法阴阳五行。",
                       {"Q1": ("玄空理气首重什么？", "自检02·0001")})
        (p2dir / "回填" / "01a_任务包.md").write_text(fa, encoding="utf-8")
        ra = import_pack("自检书2", "01", "a", None, fake_kb)
        if not ra["通过"]:
            print(f"  ❌ 子包 a 导回失败：{ra['失败行']}")
            ok = False
        sa = (book2 / "章节摘要" / "01.md").read_text(encoding="utf-8")
        if "子包甲摘要" not in sa or "子包乙摘要" in sa or sa.count("子包甲摘要") != 1:
            print("  ❌ 子包 a 导回后摘要未写或重复")
            ok = False

        fb = fill_pack(pb, "子包乙摘要：本章后半讲排龙立向逐条要诀。",
                       {"Q1": ("排龙立向第十四条如何？", "自检02·0014")})
        (p2dir / "回填" / "01b_任务包.md").write_text(fb, encoding="utf-8")
        rb = import_pack("自检书2", "01", "b", None, fake_kb)
        if not rb["通过"]:
            print(f"  ❌ 子包 b 导回失败：{rb['失败行']}")
            ok = False
        sb = (book2 / "章节摘要" / "01.md").read_text(encoding="utf-8")
        if "子包甲摘要" not in sb or "子包乙摘要" not in sb or sb.count("子包乙摘要") != 1:
            print("  ❌ 子包 b 导回后未合并 a/b 摘要或重复")
            ok = False
        if "标签0001" not in sb or "标签0025" not in sb:
            print("  ❌ 子包合并标签不全")
            ok = False

        # 重复导回子包 b → 应跳过（不重复追加评测题）
        r_dup = import_pack("自检书2", "01", "b", None, fake_kb)
        if not r_dup.get("已存在"):
            print("  ❌ 子包重复导回未跳过")
            ok = False
        eval_path2 = fake_kb / "评测集.md"
        eval_text2 = eval_path2.read_text(encoding="utf-8") if eval_path2.exists() else ""
        if eval_text2.count("排龙立向第十四条如何") != 1 or eval_text2.count("玄空理气首重什么") != 1:
            print("  ❌ 评测题重复追加或缺失")
            ok = False
        manifest2 = (p2dir / "_清单.md").read_text(encoding="utf-8")
        if "01a_任务包.md" not in manifest2 or "🟢已导入" not in manifest2:
            print("  ❌ 子包清单文件名或状态错误")
            ok = False
        print("  ⑦ 拆子包（生成/导回合并/幂等/评测去重）：✅")

        # ⑦c #15 标注状态：子包 a/b 全部导回 → 已完成 1/1
        st_done = book_annotation_status("自检书2", fake_kb)
        if (st_done["状态"], st_done["已导回"], st_done["总章数"]) != ("已完成", 1, 1):
            print(f"  ❌ #15 已完成态错误：{st_done}")
            ok = False

        # ⑦d #56 删书联动清评测题：按书号删行 + 题号重连 + 幂等
        ev56 = tmp / "ev56"
        ev56.mkdir()
        (ev56 / "评测集.md").write_text(
            "# 评测集\n\n| 题号 | 提问 | 答案块ID | 密文 | 命中 | token |\n|---|---|---|---|---|---|\n"
            "\n| Q1 | 甲书题一 | 甲01·0001 |  |  |  |\n"
            "| Q2 | 甲书题二 | 甲01·0002 |  |  |  |\n"
            "\n| Q3 | 乙书题一 | 乙02·0001 |  |  |  |\n"
            "\n| Q4 | 丙书题一 | 丙03·0001 |  |  |  |\n", encoding="utf-8")
        rp = purge_book_evals("乙02", ev56)
        ev_text = (ev56 / "评测集.md").read_text(encoding="utf-8")
        if rp["删除题数"] != 1 or "乙02·" in ev_text:
            print(f"  ❌ #56 清评测题：未按书号删行 {rp}")
            ok = False
        if ("| Q1 | 甲书题一 |" not in ev_text or "| Q2 | 甲书题二 |" not in ev_text
                or "| Q3 | 丙书题一 | 丙03·0001 |" not in ev_text):
            print("  ❌ #56 题号重连错误（删中段后应连续为 Q1/Q2/Q3）")
            ok = False
        if purge_book_evals("乙02", ev56)["删除题数"] != 0:
            print("  ❌ #56 幂等失败：重复清理仍有删除")
            ok = False
        print("  ⑦d #56 删书清评测题（按书号删行/题号重连/幂等）：✅")

        # ⑦b 拆子包节边界优先（§4.7）：自检书3 —— 第01章 25 块，块8/块12 以节标题起头，
        #     a/b 应优先落「块12（第二节）」边界（在 a 的后半段内、最接近均分点），而非朴素 13 切
        book3 = fake_kb / "书籍" / "自检书3"
        book3.mkdir(parents=True)
        o3_lines = []
        for i in range(25):
            o3_lines.append(f"【自检03·{i + 1:04d}】p.{i + 1}")
            if i == 7:
                o3_lines.append("第一节 玄空总论")
            elif i == 11:
                o3_lines.append("第二节 排龙要诀")
            else:
                o3_lines.append(f"块{i + 1}内容：玄空理气水法阴阳五行排龙立向之第{i + 1}条")
            o3_lines.append("")
        (book3 / "原文.md").write_text("\n".join(o3_lines), encoding="utf-8")
        (book3 / "检索副本.md").write_text("\n".join(o3_lines), encoding="utf-8")
        (book3 / "目录骨架.md").write_text(
            "# 《自检书3》目录骨架\n书号：自检03 | 总块数：25\n\n| 章号 | 章名 | 起始块ID | 块数 |\n|---|---|---|---|\n"
            "| 01 | 唯一章 | 自检03·0001 | 25 |\n", encoding="utf-8")
        (book3 / "章节摘要").mkdir(exist_ok=True)
        (verify_dir / "自检书3_评测原句.txt").write_text(
            "自检03·0001 | ⌈验·丙一·子丑⌋ | 玄空理气水法阴阳五行\n"
            "自检03·0012 | ⌈验·丙二·寅卯⌋ | 第二节排龙要诀\n", encoding="utf-8")
        p3dir = common.get_path("输出") / "任务包" / "自检书3"
        w3 = generate_pack("自检书3", "01", fake_kb)
        p3a = (p3dir / "01a_任务包.md").read_text(encoding="utf-8")
        p3b = (p3dir / "01b_任务包.md").read_text(encoding="utf-8")
        if "块12内容" in p3a or "第一节 玄空总论" not in p3a:
            print("  ❌ 节边界优先：a 应在节边界前切分（含块1~11，起于第一节）")
            ok = False
        if "第二节 排龙要诀" not in p3b or "块11内容" in p3b:
            print("  ❌ 节边界优先：b 应从节边界块12起头")
            ok = False
        if "> 上接块ID：无" not in p3a or "> 下接块ID：自检03·0012" not in p3a:
            print("  ❌ 01a 衔接行错误（节边界切分：上接无 / 下接 0012）")
            ok = False
        if "> 上接块ID：自检03·0011" not in p3b or "> 下接块ID：无" not in p3b:
            print("  ❌ 01b 衔接行错误（节边界切分：上接 0011 / 下接 无）")
            ok = False
        print("  ⑦b 节边界优先切分＋包衔接两行：✅")

        # ⑧ 合并确定性与段标题：乱序收集顺序不变；整章摘要带「第n子包·m块」小标题且按子包序排（#21 M2 去区间）
        entries_ob = _collect_filled_subpacks("自检书2", "01",
                                              extra_file=p2dir / "回填" / "01b_任务包.md")
        if [e["名称"] for e in entries_ob] != ["01a_任务包.md", "01b_任务包.md"]:
            print(f"  ❌ 子包排序不确定：{[e['名称'] for e in entries_ob]}")
            ok = False
        if "**【第1子包·13块】**" not in sb or "**【第2子包·12块】**" not in sb:
            print("  ❌ 整章摘要缺「第n子包·m块」小标题")
            ok = False
        elif sb.index("**【第1子包·13块】**") > sb.index("**【第2子包·12块】**"):
            print("  ❌ 整章摘要段落顺序与子包序不一致")
            ok = False
        print("  ⑧ 合并确定性/段标题：✅")

        # ⑨ 页码三态拼接（4.1/4.9）：区间取起止、降级透传 PDF 页序，空章返回 -
        if _chapter_page_range([{"页码": "1"}, {"页码": "2~3"}]) != "p.1~p.3":
            print("  ❌ 区间章页码范围拼接错误")
            ok = False
        if _chapter_page_range([{"页码": "4"}, {"页码": "PDF.5"}]) != "p.4~p.PDF.5":
            print("  ❌ 降级章页码范围拼接错误")
            ok = False
        if _chapter_page_range([]) != "-":
            print("  ❌ 空章页码范围应为 -")
            ok = False
        print("  ⑨ 页码三态拼接：✅")

        # ⑩ 图表块不进包（4.6/4.9）：清单内块被 _extract_chapter_blocks 剔除，包内不出现
        book4 = fake_kb / "书籍" / "自检书4"
        book4.mkdir(parents=True)
        (book4 / "原文.md").write_text(
            "【自检04·0001】p.1\n玄空之学首重理气阴阳五行之气运行\n\n"
            "【自检04·0002】p.2\n纳音五行表甲子乙丑海中金丙寅丁卯炉中火\n\n", encoding="utf-8")
        (book4 / "检索副本.md").write_text(
            "【自检04·0001】p.1\n玄空之学首重理气阴阳五行之气运行\n\n"
            "【自检04·0002】p.2\n纳音五行表甲子乙丑海中金丙寅丁卯炉中火\n\n", encoding="utf-8")
        (book4 / "目录骨架.md").write_text(
            "# 《自检书4》目录骨架\n书号：自检04 | 总块数：2\n\n| 章号 | 章名 | 起始块ID | 块数 |\n|---|---|---|---|\n"
            "| 01 | 唯一章 | 自检04·0001 | 2 |\n", encoding="utf-8")
        (book4 / "章节摘要").mkdir(exist_ok=True)
        (book4 / "图表块清单.json").write_text(
            json.dumps({"图表块": ["自检04·0002"]}, ensure_ascii=False, indent=2), encoding="utf-8")
        rb4 = generate_pack("自检书4", "01", fake_kb)
        p4 = (common.get_path("输出") / "任务包" / "自检书4" / "01_任务包.md").read_text(encoding="utf-8")
        if "自检04·0002" in p4 or "纳音五行表" in p4:
            print("  ❌ 图表块未剔除出任务包")
            ok = False
        print("  ⑩ 图表块不进包：✅")

        # ⑪ #35 瘦输出协议：解析→四道闸→程序合成→原 import_pack 导回 全回路
        book5 = fake_kb / "书籍" / "自检书5"
        book5.mkdir(parents=True)
        (book5 / "原文.md").write_text(
            "【自检05·0001】p.1\n玄空之学首重理气阴阳五行之气运行\n\n"
            "【自检05·0002】p.2\n排龙之法以水口为主顺逆相推定其星曜\n", encoding="utf-8")
        (book5 / "检索副本.md").write_text(
            "【自检05·0001】p.1\n玄空之学首重理气阴阳五行之气运行\n\n"
            "【自检05·0002】p.2\n排龙之法以水口为主顺逆相推定其星曜\n", encoding="utf-8")
        (book5 / "目录骨架.md").write_text(
            "# 《自检书5》目录骨架\n书号：自检05 | 总块数：2\n\n| 章号 | 章名 | 起始块ID | 块数 |\n|---|---|---|---|\n"
            "| 01 | 唯一章 | 自检05·0001 | 2 |\n", encoding="utf-8")
        (book5 / "章节摘要").mkdir(exist_ok=True)
        (verify_dir / "自检书5_评测原句.txt").write_text(
            "自检05·0001 | ⌈验·丁一·子丑⌋ | 玄空之学首重理气\n", encoding="utf-8")
        p5dir = common.get_path("输出") / "任务包" / "自检书5"
        generate_pack("自检书5", "01", fake_kb)
        p5 = (p5dir / "01_任务包.md").read_text(encoding="utf-8")
        expected5 = [r["块ID"] for r in _parse_filled(p5)["标签行"]]
        eval_ph5 = _parse_filled(p5)["评测题行"]
        eval_src5 = {"自检05·0001": "玄空之学首重理气"}
        p5_norm = search_keyword.normalize(p5)

        # ⑪a 排盘幻影回归：原文区三列表（| 3<br>七 | … |）不得被误判为评测题行
        phantom = _parse_filled(p5 + "\n| 3<br>七 | 8<br>三 | 1<br>五 |\n")
        if len(phantom["评测题行"]) != 1 or phantom["评测题行"][0]["题号"] != "Q1":
            print(f"  ❌ ⑪ 评测题行解析异常（应只有真占位行 Q1）：{phantom['评测题行']}")
            ok = False

        thin_good = _parse_thin(
            "## 本段摘要\n本章讲述玄空理气基础与排龙水法要诀。\n\n"
            "## 块标签\n| 块ID | 标签 | 术语候选 | 别名候选 |\n|---|---|---|---|\n"
            "| 自检05·0001 | 理气, 阴阳, 五行 | 玄空, 理气 |  |\n"
            "| 自检05·0002 | 排龙, 水口, 挨星 | 排龙 |  |\n\n"
            "## 评测题\n| 题号 | 提问 | 答案块ID |\n|---|---|---|\n"
            "| Q1 | 玄空之学首重什么？ | 自检05·0001 |\n")
        v = _validate_thin(thin_good, expected5, eval_ph5, eval_src5)
        if v:
            print(f"  ❌ ⑪ 合格瘦输出被误拦：{v}")
            ok = False
        filled5 = _compose_filled(p5, thin_good, p5_norm)
        if "理气, 阴阳, 五行" not in filled5:
            print("  ❌ ⑪ 合成回填文件未填入标签")
            ok = False
        (p5dir / "回填" / "01_任务包.md").write_text(filled5, encoding="utf-8")
        r5 = import_pack("自检书5", "01", "", None, fake_kb)
        if not r5["通过"]:
            print(f"  ❌ ⑪ 合成回填导回失败：{r5['失败行']}")
            ok = False
        sa5 = (book5 / "章节摘要" / "01.md").read_text(encoding="utf-8")
        if "理气, 阴阳, 五行" not in sa5 or "本章讲述玄空理气基础" not in sa5:
            print("  ❌ ⑪ 合成导回后章节摘要缺标签或摘要")
            ok = False

        def _thin_with(tag_row2=None, eval_q="玄空之学首重什么？", drop2=False, term1="玄空, 理气"):
            rows = ["## 本段摘要", "摘要：本章讲述玄空理气与排龙水法。", "",
                    "## 块标签", "| 块ID | 标签 | 术语候选 | 别名候选 |", "|---|---|---|---|",
                    f"| 自检05·0001 | 理气, 阴阳 | {term1} |  |"]
            if not drop2:
                rows.append(tag_row2 or "| 自检05·0002 | 排龙, 水口 | 排龙 |  |")
            rows += ["", "## 评测题", "| 题号 | 提问 | 答案块ID |", "|---|---|---|",
                     f"| Q1 | {eval_q} | 自检05·0001 |"]
            return _parse_thin("\n".join(rows))

        if not any("缺块" in f for f in _validate_thin(
                _thin_with(drop2=True), expected5, eval_ph5, eval_src5)):
            print("  ❌ ⑪ 闸①缺块未拦截")
            ok = False
        # 软闸（原闸②）：包内原文没有的词不拦截，合成时自动标【存疑】
        thin_fab = _thin_with(term1="河图洛书")
        if _validate_thin(thin_fab, expected5, eval_ph5, eval_src5):
            print("  ❌ ⑪ 软闸不应拦截编造词（应改标存疑放行）")
            ok = False
        if "河图洛书【存疑】" not in _compose_filled(p5, thin_fab, p5_norm):
            print("  ❌ ⑪ 编造词未被自动标【存疑】")
            ok = False
        if not any("雷同" in f for f in _validate_thin(
                _thin_with(tag_row2="| 自检05·0002 | 理气, 阴阳 | 排龙 |  |"),
                expected5, eval_ph5, eval_src5)):
            print("  ❌ ⑪ 闸③标签雷同未拦截")
            ok = False
        if not any("退化" in f for f in _validate_thin(
                _thin_with(eval_q="玄空之学首重理气写在哪一块？"),
                expected5, eval_ph5, eval_src5)):
            print("  ❌ ⑪ 闸④退化提问未拦截")
            ok = False
        print("  ⑪ #35 瘦输出协议（合成导回＋硬闸拦截＋软闸存疑＋排盘幻影回归）：✅")

        # ⑫ #35 拆包双判据：2 块但包体 >30KB → 拆 01a/01b
        book6 = fake_kb / "书籍" / "自检书6"
        book6.mkdir(parents=True)
        big1 = "玄空理气" * 4000
        big2 = "排龙水法" * 4000
        (book6 / "原文.md").write_text(
            f"【自检06·0001】p.1\n{big1}\n\n【自检06·0002】p.2\n{big2}\n", encoding="utf-8")
        (book6 / "检索副本.md").write_text(
            f"【自检06·0001】p.1\n{big1}\n\n【自检06·0002】p.2\n{big2}\n", encoding="utf-8")
        (book6 / "目录骨架.md").write_text(
            "# 《自检书6》目录骨架\n书号：自检06 | 总块数：2\n\n| 章号 | 章名 | 起始块ID | 块数 |\n|---|---|---|---|\n"
            "| 01 | 唯一章 | 自检06·0001 | 2 |\n", encoding="utf-8")
        (book6 / "章节摘要").mkdir(exist_ok=True)
        w6 = generate_pack("自检书6", "01", fake_kb)
        if [p.name for p in w6] != ["01a_任务包.md", "01b_任务包.md"]:
            print(f"  ❌ ⑫ 字节阈值拆包未生效：{[p.name for p in w6]}")
            ok = False
        print("  ⑫ #35 拆包双判据（包体>30KB 拆子包）：✅")

        # ⑬ #21 M2 出生证制取块：章内块 ID 非连续/乱序（更新后新块尾部领号、物理插入书中段）
        #     → 按「起始块ID+块数」物理序取块仍正确；旧格式骨架（块ID区间列）兼容解析
        book7 = fake_kb / "书籍" / "自检书7"
        book7.mkdir(parents=True)
        o7 = (
            "【自检07·0001】p.1\n第一章首块内容\n\n"
            "【自检07·0002】p.2\n第一章次块内容\n\n"
            "【自检07·0117】p.3\n第二章首块为更新后新块\n\n"
            "【自检07·0003】p.4\n第二章次块为老块\n"
        )
        (book7 / "原文.md").write_text(o7, encoding="utf-8")
        (book7 / "检索副本.md").write_text(o7, encoding="utf-8")
        (book7 / "目录骨架.md").write_text(
            "# 《自检书7》目录骨架\n书号：自检07 | 总块数：4\n\n| 章号 | 章名 | 起始块ID | 块数 |\n|---|---|---|---|\n"
            "| 01 | 第一章 | 自检07·0001 | 2 |\n"
            "| 02 | 第二章 | 自检07·0117 | 2 |\n", encoding="utf-8")
        chs7 = _read_skeleton("自检书7", fake_kb)
        ids71 = [b["块ID"] for b in _extract_chapter_blocks("自检书7", chs7[0], fake_kb)]
        ids72 = [b["块ID"] for b in _extract_chapter_blocks("自检书7", chs7[1], fake_kb)]
        if ids71 != ["自检07·0001", "自检07·0002"] or ids72 != ["自检07·0117", "自检07·0003"]:
            print(f"  ❌ ⑬ 出生证制物理序取块错误：章1={ids71} 章2={ids72}")
            ok = False
        book8 = fake_kb / "书籍" / "自检书8"
        book8.mkdir(parents=True)
        (book8 / "目录骨架.md").write_text(
            "# 《自检书8》目录骨架\n书号：自检08 | 总块数：3\n\n| 章号 | 章名 | 块ID区间 |\n|---|---|---|\n"
            "| 01 | 第一章 | 自检08·0001~0002 |\n"
            "| 02 | 第二章 | 自检08·0003~0003 |\n", encoding="utf-8")
        chs8 = _read_skeleton("自检书8", fake_kb)
        if (chs8[0]["起始块ID"], chs8[0]["块数"], chs8[1]["起始块ID"], chs8[1]["块数"]) \
                != ("自检08·0001", 2, "自检08·0003", 1):
            print(f"  ❌ ⑬ 旧格式骨架兼容解析错误：{chs8}")
            ok = False
        print("  ⑬ #21 M2 物理序取块（乱序 ID）＋旧格式骨架兼容：✅")

        # ⑭ #42 术语自动配对：桩 LLM 配对→写表合并去重；编造词/空主词条/自指别名被硬校验拦截；
        #    未配置跳过；重跑幂等无重复行；孤词不写表；成功清空候选
        _saved_chat, _saved_conf = llm_client.chat, llm_client.is_configured
        cand_p = verify_dir / "自检书_术语候选.json"
        term_tbl = fake_kb / "术语同义词表.md"
        try:
            cand_p.write_text(json.dumps(
                [{"术语": t} for t in ("三元九运", "河图洛书", "排龙诀", "排龍訣", "挨星诀", "孤词测试")],
                ensure_ascii=False), encoding="utf-8")
            term_tbl.write_text(
                "# 术语同义词表\n\n| 主词条 | 别名/异体/派别异名 |\n|---|---|\n| 排龙诀 | 排龙 |\n",
                encoding="utf-8")
            llm_client.is_configured = lambda: True
            llm_client.chat = lambda *a, **k: ('{"词条":[{"主词条":"排龙诀","别名":["挨星诀"]},'
                                               '{"主词条":"三元九运","别名":["河图洛书"]}]}')
            r1 = pair_terms("自检书", fake_kb)
            tbl1 = term_tbl.read_text(encoding="utf-8")
            # 预置表已有「排龙诀」行 → 别名并入算更新，新增主词条数=1（三元九运）
            if r1["配对"] != 1 or r1["失败批数"] != 0:
                print(f"  ❌ ⑭ 合法配对未全通过：{r1}")
                ok = False
            elif "| 排龙诀 | 排龙, 挨星诀 |" not in tbl1 \
                    or "| 三元九运 | 河图洛书 |" not in tbl1 \
                    or "孤词测试" in tbl1 or "排龍訣" in tbl1:
                print("  ❌ ⑭ 写表合并/孤词剔除错误：\n" + tbl1)
                ok = False
            elif cand_p.exists():
                print("  ❌ ⑭ 配对成功后候选未清空")
                ok = False
            # 重跑幂等：候选已清 → 无操作；表无重复行
            r2 = pair_terms("自检书", fake_kb)
            if r2["配对"] != 0 or term_tbl.read_text(encoding="utf-8").count("| 排龙诀 |") != 1:
                print(f"  ❌ ⑭ 重跑幂等失败：{r2}")
                ok = False
            # 编造词 → 硬校验拦截，候选保留待重试，黄灯报告落盘
            cand_p.write_text(json.dumps(
                [{"术语": t} for t in ("三元九运", "河图洛书", "排龙诀", "排龍訣", "挨星诀", "孤词测试")],
                ensure_ascii=False), encoding="utf-8")
            llm_client.chat = lambda *a, **k: '{"词条":[{"主词条":"不存在词","别名":["排龙诀"]}]}'
            r3 = pair_terms("自检书", fake_kb)
            rp3 = common.report_dir() / "自检书_术语配对.md"
            if r3["失败批数"] != 1 or not cand_p.exists() or not rp3.exists():
                print(f"  ❌ ⑭ 编造词未被拦截/候选未保留/报告未落：{r3}")
                ok = False
            # 空主词条 / 自指别名 → 拦截
            cand_p.write_text(json.dumps([{"术语": "排龙诀"}], ensure_ascii=False), encoding="utf-8")
            llm_client.chat = lambda *a, **k: '{"词条":[{"主词条":"","别名":["排龙诀"]}]}'
            if pair_terms("自检书", fake_kb)["失败批数"] != 1:
                print("  ❌ ⑭ 空主词条未被拦截")
                ok = False
            llm_client.chat = lambda *a, **k: '{"词条":[{"主词条":"排龙诀","别名":["排龙诀"]}]}'
            if pair_terms("自检书", fake_kb)["失败批数"] != 1:
                print("  ❌ ⑭ 自指别名未被拦截")
                ok = False
            # 未配置标注 AI → 跳过不阻塞（不写报告）
            cand_p.write_text(json.dumps([{"术语": "排龙诀"}], ensure_ascii=False), encoding="utf-8")
            llm_client.is_configured = lambda: False
            r4 = pair_terms("自检书", fake_kb)
            if not r4.get("跳过") or r4["配对"] != 0:
                print(f"  ❌ ⑭ 未配置时未正确跳过：{r4}")
                ok = False
            common.clear_report("自检书_术语配对.md")
            print("  ⑭ #42 术语自动配对（桩 LLM/硬校验拦截/未配置跳过/幂等）：✅")
        finally:
            llm_client.chat, llm_client.is_configured = _saved_chat, _saved_conf
            if cand_p.exists():
                cand_p.unlink()
            common.clear_report("自检书_术语配对.md")

        # ⑮ #4 M3（R4）题干入倒排：写「题干」节、旧题跳过、重跑幂等
        inv_self = fake_kb / "书籍" / "自检书" / "倒排表.json"
        inv_self.write_text(json.dumps({"书号": "自检01", "构建时间": "2026-09-17",
                                        "条目": {"甲": ["自检01·0001"]}},
                                       ensure_ascii=False, indent=2), encoding="utf-8")
        rows15 = [
            {"题号": "Q1", "提问": "自检提问甲", "答案块ID": "自检01·0001", "密文": ""},
            {"题号": "Q2", "提问": "旧题提问", "答案块ID": "旧书·0001", "密文": ""},
        ]
        c1 = _append_question_grams(rows15, fake_kb)
        d1 = json.loads(inv_self.read_text(encoding="utf-8")).get("题干", {})
        c2 = _append_question_grams(rows15, fake_kb)  # 幂等重跑
        d2 = json.loads(inv_self.read_text(encoding="utf-8")).get("题干", {})
        if not d1 or c1 != ["自检书"] or c2 or d2 != d1:
            print(f"  ❌ ⑮ R4 题干入倒排异常：c1={c1} c2={c2} d1={d1}")
            ok = False
        elif not any("自检01·0001" in v for v in d1.values()):
            print(f"  ❌ ⑮ R4 题干词条未写入：{d1}")
            ok = False
        else:
            print("  ✅ ⑮ R4 题干入倒排：写入 1 书、旧题跳过、重跑幂等")
        d1["失效入口"] = ["自检01·9999"]
        d1["混合入口"] = ["自检01·0001", "自检01·9999"]
        data15 = json.loads(inv_self.read_text(encoding="utf-8"))
        data15["题干"] = d1
        inv_self.write_text(json.dumps(data15, ensure_ascii=False, indent=2), encoding="utf-8")
        maintenance15 = _prune_stale_question_grams(fake_kb)
        cleaned15 = json.loads(inv_self.read_text(encoding="utf-8"))["题干"]
        if "失效入口" not in cleaned15 and cleaned15["混合入口"] == ["自检01·0001"] \
                and sum(len(v) for v in maintenance15.values()) == 2:
            print("  ✅ #65 M2 题干失效引用清理：退休词条移除、活块保留")
        else:
            print(f"  ❌ #65 M2 题干失效引用清理异常：{maintenance15} {cleaned15}")
            ok = False

        # ⑯ #66 阶段 3：import_pack 持久化 块标签.json + 回收块标签 + 真实集删书联动清理
        bt_path = book_dir / "块标签.json"
        if not bt_path.exists():
            print("  ❌ #66 import_pack 未生成 块标签.json")
            ok = False
        else:
            bt_data = json.loads(bt_path.read_text(encoding="utf-8"))
            if "自检01·0001" not in bt_data.get("块", {}):
                print(f"  ❌ #66 块标签.json 缺少块条目：{bt_data}")
                ok = False
            else:
                bt_path.unlink()
                rec = recover_block_tags("自检书", fake_kb)
                if rec["回收块数"] < 2 or not bt_path.exists():
                    print(f"  ❌ #66 recover_block_tags 回收失败：{rec}")
                    ok = False
                else:
                    print(f"  ✅ #66 块标签持久化与回填无损回收：{rec['回收块数']} 块")
        real_eval = fake_kb / "评测集_真实提问.md"
        real_eval.write_text(
            "# 评测集 · 真实提问\n\n| 题号 | 提问（用户原话） | 限定书 | 答案块ID | 确认人 |\n"
            "|---|---|---|---|---|\n"
            "| R001 | 自检问 | 自检书 | 自检01·0001 | 测试 |\n"
            "| R002 | 他书问 | 他书 | 自检02·0001 | 测试 |\n",
            encoding="utf-8")
        pr_real = purge_book_evals("自检01", fake_kb, book_title="自检书")
        real_after = real_eval.read_text(encoding="utf-8")
        if pr_real["真实集删除题数"] != 1 or "自检01·0001" in real_after or "| R001 | 他书问 |" not in real_after:
            print(f"  ❌ #66 真实集删书联动清理异常：{pr_real}\n{real_after}")
            ok = False
        else:
            print("  ✅ #66 真实集删书联动清理与题号接续正常")

        print(f"  {'✅ 通过' if ok else '❌ 失败'}")
        sys.exit(0 if ok else 1)
    finally:
        llm_client.is_configured = _sc_llm_conf
        shutil.rmtree(tmp, ignore_errors=True)
        for f in verify_dir.glob("自检书*"):
            if f.is_file():
                f.unlink()
        for d in (common.get_path("输出") / "任务包" / "自检书",
                  common.get_path("输出") / "任务包" / "自检书2",
                  common.get_path("输出") / "任务包" / "自检书3",
                  common.get_path("输出") / "任务包" / "自检书4",
                  common.get_path("输出") / "任务包" / "自检书5",
                  common.get_path("输出") / "任务包" / "自检书6"):
            if d.exists():
                shutil.rmtree(d)
        # 若自检产生了 自检书 章节摘要（fake_kb 内，无残留）；书目总索引/评测集在 fake_kb 内


if __name__ == "__main__":
    if common.is_selfcheck():
        _selfcheck()
    else:
        common.run_with_write_lock(main, "标注")
