# -*- coding: utf-8 -*-
"""页码锚点拟合（设计文档 4.3/4.4）。被 A-03 clean_split 调用（写块 ID 行之前完成拟合）。

锚点统一取自隔离区缓存（输出\\提取缓存\\{书号}\\隔离区\\页_NNN.txt）：
- 优先级①：MinerU page_number 块（⟦page_number⟧ 行，mineru_client 按页落盘）；
- 优先级②：兜底/文本通道的纯页码行（⟦纯页码行⟧ 行，clean_split 防线裁下）；
- 优先级③④：MinerU 把页码误归为页眉/页脚类型块时，⟦footer⟧/⟦header⟧ 行中
  内容为纯数字者仍作候选（仍须过段内众数+稳定性统计门，噪声页码被过滤）。
算法（4.3 六步）：收割 → 过滤 ∈[1,2000]（罗马数字/装饰字符一期不识别）→
每页候选取众数、孤立跳变页服从众数不切段 → 偏移突变（偏移值变化）切段 →
段稳定性（段内锚点数≥3 且 众数票占比≥60%）→ 全书无已拟合段即降级
（p.PDF.N + 黄灯）→ 块标签三态落地（起止页跨段或其一未拟合 → p.PDF.）。

本模块无 CLI 主流程（仅 --自检 独立运行）；拟合与落表由 clean_split 内嵌调用。
退出码：0=自检通过，1=自检失败。红线：不读写 路径配置.json；路径一律由调用方传入。
"""

import re
import sys
import tempfile
import shutil
from collections import Counter
from pathlib import Path

import common
import page_format

_ISO_FILE_RE = re.compile(r"^页_(\d+)$")
_ISO_LINE_RE = re.compile(r"^⟦(.+?)⟧\s*(.*)$")
_ANCHOR_TYPES = ("page_number", "纯页码行", "footer", "header")
_CAND_MIN, _CAND_MAX = 1, 2000
_MIN_ANCHORS = 3
_MODE_RATIO = 0.60


def _to_candidate(content: str):
    """把隔离区行内容转为印刷页码候选：经 page_format 多格式解析，结果 ∈[1,2000]；否则 None。"""
    return page_format.parse_page_number(content)


def harvest_anchors(iso_dir: Path) -> list:
    """从隔离区收割锚点：返回 [(PDF页序 int, 候选印刷页码 int)]，按 PDF 页序升序。

    页内优先级（4.3 + 扫描书增强）：page_number → 纯页码行 → footer → header，
    高一级类型存在时忽略低级；MinerU 把页码误归为页眉/页脚类型块（⟦footer⟧/
    ⟦header⟧ 行内容为纯数字）仍可收割为候选，噪声由段内众数+稳定性统计门过滤。
    页内同值去重，避免同一页重复计票放大单页影响。隔离区缺失/为空 → []。"""
    if not iso_dir or not iso_dir.exists():
        return []
    anchors = []
    for f in sorted(iso_dir.glob("页_*.txt"), key=lambda p: (int(p.stem.split("_")[1]), p.name)):
        m = _ISO_FILE_RE.match(f.stem)
        if not m:
            continue
        pdf_page = int(m.group(1))
        by_type = {t: [] for t in _ANCHOR_TYPES}
        for raw in f.read_text(encoding="utf-8").splitlines():
            m2 = _ISO_LINE_RE.match(raw)
            if not m2:
                continue
            typ, content = m2.group(1), m2.group(2).strip()
            if typ not in by_type:
                continue
            cand = _to_candidate(content)
            if cand is not None:
                by_type[typ].append(cand)
        picked = []
        for typ in _ANCHOR_TYPES:
            if by_type[typ]:
                picked = by_type[typ]
                break
        for val in sorted(set(picked)):
            anchors.append((pdf_page, val))
    anchors.sort()
    return anchors


def _page_modes(anchors: list) -> list:
    """每页候选取页内众数（平局取较小候选），输出 [(PDF页序, 偏移)] 升序。
    单页锚点冲突不逐页判断，服从页内众数。"""
    page_counter = {}
    for page, cand in anchors:
        page_counter.setdefault(page, Counter())[cand] += 1
    events = []
    for page in sorted(page_counter):
        cnt = page_counter[page]
        cand_mode = max(cnt, key=lambda k: (cnt[k], -k))
        events.append((page, cand_mode - page))
    return events


def _drop_isolated(events: list) -> list:
    """孤立跳变页服从众数：某页偏移与前后页都不同 → 视为噪声丢弃（不切段）。
    前言/正文/附录的大幅重排是连续区间（≥2 锚点一致），不会被误丢。"""
    out = []
    n = len(events)
    for i, (page, off) in enumerate(events):
        prev_off = events[i - 1][1] if i > 0 else off
        next_off = events[i + 1][1] if i < n - 1 else off
        if prev_off != off and next_off != off:
            continue
        out.append((page, off))
    return out


def fit_segments(anchors: list) -> tuple:
    """锚点 → (分段列表, 全书状态)。

    分段：按 PDF 页序游程切段，偏移值变化即"偏移突变"；每段偏移为段内众数。
    段稳定性：段内锚点数≥3 且 众数票占比≥60% → 已拟合。
    全书状态：至少一段已拟合 → 已拟合；否则 → 降级。
    段字段（4.2 契约）：PDF页起/PDF页止/状态/印刷页起/偏移/锚点数/置信度。"""
    if not anchors:
        return [], "降级"
    events = _drop_isolated(_page_modes(anchors))

    segments = []
    cur = None
    for page, off in events:
        if cur is None or off != cur["_off"]:
            if cur:
                segments.append(cur)
            cur = {"PDF页起": page, "PDF页止": page, "_off": off, "_pages": [], "_votes": Counter()}
        cur["PDF页止"] = page
        cur["_pages"].append(page)
        cur["_votes"][off] += 1
    if cur:
        segments.append(cur)

    fitted = False
    for seg in segments:
        mode_off, mode_votes = max(seg["_votes"].items(), key=lambda kv: (kv[1], -kv[0]))
        n = len(seg["_pages"])
        seg.pop("_votes")
        seg.pop("_pages")
        seg["偏移"] = mode_off
        seg["印刷页起"] = seg["PDF页起"] + mode_off
        seg["锚点数"] = n
        seg["置信度"] = round(mode_votes / n, 2) if n else 0.0
        seg["状态"] = "已拟合" if (n >= _MIN_ANCHORS and mode_votes / n >= _MODE_RATIO) else "未拟合"
        if seg["状态"] == "已拟合":
            fitted = True
    return segments, ("已拟合" if fitted else "降级")


def _find_fitted(block_start: int, block_end: int, segments: list):
    """块起止 PDF 页均落在同一已拟合段内 → 返回该段；否则 None（跨段/未拟合 → 降级）。"""
    for seg in segments:
        if seg["状态"] == "已拟合" and seg["PDF页起"] <= block_start and block_end <= seg["PDF页止"]:
            return seg
    return None


def block_label(block_start: int, block_end: int, segments: list, state: str) -> str:
    """块起止 PDF 页 → 三态标签（4.1）。

    起止页同在已拟合段 → 印刷页码区间（单页只写数字）；否则/全书降级 → p.PDF.。
    非分页书不调用本函数（标签维持提取序号语义）。"""
    if state == "降级" or not segments:
        return f"PDF.{block_start}" if block_start == block_end else f"PDF.{block_start}~{block_end}"
    seg = _find_fitted(block_start, block_end, segments)
    if seg is None:
        return f"PDF.{block_start}" if block_start == block_end else f"PDF.{block_start}~{block_end}"
    start_p = block_start + seg["偏移"]
    end_p = block_end + seg["偏移"]
    return str(start_p) if start_p == end_p else f"{start_p}~{end_p}"


def build_page_map(book_id: str, state: str, segments: list) -> dict:
    """组装 页码映射.json 结构（4.2 契约）：书号/生成时间/状态/分段。

    段按 PDF 页序排列；未拟合段不写 印刷页起/偏移/置信度 之外的推论字段
    （印刷页起仅对已拟合段有含义，未拟合段由 PDF 页序 + 偏移 反查）。"""
    segs_out = []
    for seg in segments:
        row = {"PDF页起": seg["PDF页起"], "PDF页止": seg["PDF页止"],
               "状态": seg["状态"], "锚点数": seg["锚点数"]}
        if seg["状态"] == "已拟合":
            row.update({"印刷页起": seg["印刷页起"], "偏移": seg["偏移"], "置信度": seg["置信度"]})
        segs_out.append(row)
    import datetime
    return {
        "书号": book_id,
        "生成时间": datetime.date.today().isoformat(),
        "状态": state,
        "分段": segs_out,
    }


def _selfcheck():
    """page_fit 自检：收割优先级/过滤/拟合切段/稳定性/降级/块标签 7 组用例。"""
    print("page_fit 自检")
    ok = True
    tmp = Path(tempfile.mkdtemp(prefix="page_fit_selfcheck_"))

    def iso_dir():
        d = tmp / "iso"
        shutil.rmtree(d, ignore_errors=True)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def write_iso(d, page, *lines):
        (d / f"页_{page:03d}.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")

    # 用例A：双段拟合（前言段落偏移 -3，正文段偏移 0）→ 状态 已拟合，2 段
    d = iso_dir()
    for p in (4, 5, 6):
        write_iso(d, p, f"⟦page_number⟧ {p - 3}")
    for p in (11, 12, 13):
        write_iso(d, p, f"⟦page_number⟧ {p}")
    anchors = harvest_anchors(d)
    segments, state = fit_segments(anchors)
    if state != "已拟合" or len(segments) != 2:
        print(f"  ❌ 用例A 双段拟合：state={state} 段数={len(segments)}")
        ok = False
    if not all(s["状态"] == "已拟合" and s["置信度"] == 1.0 for s in segments):
        print(f"  ❌ 用例A 段稳定性：{segments}")
        ok = False
    if block_label(4, 6, segments, state) != "1~3":
        print(f"  ❌ 用例A 块标签区间：{block_label(4, 6, segments, state)}")
        ok = False
    if block_label(11, 11, segments, state) != "11":
        print(f"  ❌ 用例A 块标签单页：{block_label(11, 11, segments, state)}")
        ok = False
    if block_label(4, 11, segments, state) != "PDF.4~11":
        print(f"  ❌ 用例A 跨段降级：{block_label(4, 11, segments, state)}")
        ok = False
    pm = build_page_map("自检A", state, segments)
    if pm["状态"] != "已拟合" or pm["分段"][0]["印刷页起"] != 1:
        print(f"  ❌ 用例A 页码映射.json 结构：{pm}")
        ok = False

    # 用例B：锚点不足（2 <3）→ 全书降级，块标签 p.PDF.
    d = iso_dir()
    write_iso(d, 1, "⟦page_number⟧ 1")
    write_iso(d, 2, "⟦page_number⟧ 2")
    segments, state = fit_segments(harvest_anchors(d))
    if state != "降级":
        print(f"  ❌ 用例B 锚点不足应降级：{state}")
        ok = False
    if block_label(1, 2, segments, state) != "PDF.1~2":
        print(f"  ❌ 用例B 降级标签：{block_label(1, 2, segments, state)}")
        ok = False

    # 用例C：page_number 优先于纯页码行，且页内同值去重
    d = iso_dir()
    for p in (7, 8, 9):
        write_iso(d, p, f"⟦page_number⟧ {p + 130}", f"⟦纯页码行⟧ {p + 999}",
                  f"⟦纯页码行⟧ {p + 130}")
    segments, state = fit_segments(harvest_anchors(d))
    if state != "已拟合" or block_label(7, 9, segments, state) != "137~139":
        print(f"  ❌ 用例C page_number 优先/去重：state={state} 标签={block_label(7, 9, segments, state)}")
        ok = False

    # 用例D：过滤（超界 2026、非数字"序"）
    d = iso_dir()
    write_iso(d, 1, "⟦page_number⟧ 2026", "⟦纯页码行⟧ 序")
    if harvest_anchors(d):
        print(f"  ❌ 用例D 过滤：{harvest_anchors(d)}")
        ok = False
    segments, state = fit_segments([])
    if state != "降级" or block_label(1, 1, segments, state) != "PDF.1":
        print(f"  ❌ 用例D 无锚降级：{state}")
        ok = False

    # 用例E：孤立跳变页服从众数（页 5 偏移 4 为噪声，前后页偏移 0）→ 不切段
    d = iso_dir()
    for p in (3, 4, 5, 6, 7):
        cand = p if p != 5 else p + 4
        write_iso(d, p, f"⟦page_number⟧ {cand}")
    segments, state = fit_segments(harvest_anchors(d))
    if state != "已拟合" or len(segments) != 1:
        print(f"  ❌ 用例E 孤立噪声服从众数：state={state} 段数={len(segments)}")
        ok = False
    if block_label(3, 7, segments, state) != "3~7":
        print(f"  ❌ 用例E 噪声页不降级：{block_label(3, 7, segments, state)}")
        ok = False

    # 用例F：footer/header 纯数字行兜底（MinerU 把页码误归为页眉页脚类型时仍可收割）
    d = iso_dir()
    for p in (1, 2, 3, 4, 5, 6):
        write_iso(d, p, f"⟦footer⟧ {p}", f"⟦header⟧ 魔法骑士规则说明书")
    segments, state = fit_segments(harvest_anchors(d))
    if state != "已拟合" or block_label(1, 6, segments, state) != "1~6":
        print(f"  ❌ 用例F footer 兜底：state={state} 标签={block_label(1, 6, segments, state)}")
        ok = False

    # 用例G：page_number 仍优先于 footer（footer 噪声页码被忽略）
    d = iso_dir()
    for p in (7, 8, 9):
        write_iso(d, p, f"⟦footer⟧ {p + 100}", f"⟦page_number⟧ {p}")
    segments, state = fit_segments(harvest_anchors(d))
    if state != "已拟合" or block_label(7, 9, segments, state) != "7~9":
        print(f"  ❌ 用例G 优先级：state={state} 标签={block_label(7, 9, segments, state)}")
        ok = False

    # 用例H：装饰数字 `- N -`（标志书场景，PDF页9→印刷1，偏移 -8）
    d = iso_dir()
    for p in (9, 10, 11, 12, 13):
        write_iso(d, p, f"⟦纯页码行⟧ - {p - 8} -")
    segments, state = fit_segments(harvest_anchors(d))
    if state != "已拟合" or len(segments) != 1:
        print(f"  ❌ 用例H 装饰数字拟合：state={state} 段数={len(segments)}")
        ok = False
    if block_label(9, 13, segments, state) != "1~5":
        print(f"  ❌ 用例H 装饰数字标签：{block_label(9, 13, segments, state)}")
        ok = False

    # 用例I：罗马数字（i, ii, iii, iv）
    d = iso_dir()
    romans = ["i", "ii", "iii", "iv"]
    for p, r in enumerate(romans, start=1):
        write_iso(d, p, f"⟦page_number⟧ {r}")
    segments, state = fit_segments(harvest_anchors(d))
    if state != "已拟合" or block_label(1, 4, segments, state) != "1~4":
        print(f"  ❌ 用例I 罗马数字：state={state} 标签={block_label(1, 4, segments, state)}")
        ok = False

    # 用例J：多段偏移（前言罗马数字段 + 正文装饰数字段）
    d = iso_dir()
    for p, r in enumerate(romans, start=1):
        write_iso(d, p, f"⟦page_number⟧ {r}")
    for p in (9, 10, 11):
        write_iso(d, p, f"⟦纯页码行⟧ - {p - 8} -")
    segments, state = fit_segments(harvest_anchors(d))
    if state != "已拟合" or len(segments) != 2:
        print(f"  ❌ 用例J 多段偏移：state={state} 段数={len(segments)}")
        ok = False
    if block_label(1, 4, segments, state) != "1~4":
        print(f"  ❌ 用例J 前言段标签：{block_label(1, 4, segments, state)}")
        ok = False
    if block_label(9, 11, segments, state) != "1~3":
        print(f"  ❌ 用例J 正文段标签：{block_label(9, 11, segments, state)}")
        ok = False

    # 用例K：第N页 与 Page N 与 N/M
    d = iso_dir()
    write_iso(d, 5, "⟦纯页码行⟧ 第 5 页")
    write_iso(d, 6, "⟦纯页码行⟧ Page 6")
    write_iso(d, 7, "⟦纯页码行⟧ 7 / 503")
    segments, state = fit_segments(harvest_anchors(d))
    if state != "已拟合" or block_label(5, 7, segments, state) != "5~7":
        print(f"  ❌ 用例K 混合格式：state={state} 标签={block_label(5, 7, segments, state)}")
        ok = False

    if ok:
        print("  page_fit 自检：收割/切段/稳定性/降级/块标签/多格式 11 组用例通过 ✅")
    else:
        print("  ❌ 自检失败")
    shutil.rmtree(tmp, ignore_errors=True)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    if common.is_selfcheck():
        _selfcheck()
    else:
        print("page_fit.py 为拟合库：请用 --自检 运行，或由 A-03 clean_split 内嵌调用")
        sys.exit(0)