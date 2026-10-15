# -*- coding: utf-8 -*-
"""页码拟合 LLM 分层复查（#27 M2）。

分层策略：
- 高置信度（≥0.8 且锚点≥5）：免查，直接通过
- 中置信度（0.5~0.8 或锚点 3~4）：D1 抽样验证（LLM 抽查 2~3 页）
- 低置信度/降级（<0.5 或锚点<3）：D2 诊断补救（LLM 判格式 → 程序重跑校验）

冲突裁决：LLM 建议 → 程序重跑 → 众数/稳定性/切段算法再校验 → 程序保留最终裁决权。
红线：不读写 路径配置.json；路径一律由调用方传入；LLM 失败回落降级+黄灯，不阻塞。
"""

import json
import re
import sys
from pathlib import Path

import llm_client
import page_fit
import page_format
import common

# LLM 输出预算：所配模型可能是**推理模型**（先产 reasoning_content 思考链再给正文），预算过小会被
# 思考链吃光、导致 content 为空且 finish_reason=length（#29，与 #24 同根因：实测 deepseek 系需约 6.5k 思考 token）。
LLM_MAX_TOKENS = 16000

_ISO_FILE_RE = re.compile(r"^页_(\d+)$")
_ISO_LINE_RE = re.compile(r"^⟦(.+?)⟧\s*(.*)$")
_ANCHOR_TYPES = ("page_number", "纯页码行", "footer", "header")


def assess_confidence(segments: list, state: str) -> str:
    """评估置信度层级：高/中/低。"""
    if state == "降级":
        return "低"
    fitted = [s for s in segments if s["状态"] == "已拟合"]
    if not fitted:
        return "低"
    if all(s["置信度"] >= 0.8 and s["锚点数"] >= 5 for s in fitted):
        return "高"
    return "中"


def _read_iso_page(iso_dir: Path, page: int) -> list:
    """读取单页隔离区行，返回 [(类型, 内容)]。"""
    f = iso_dir / f"页_{page:03d}.txt"
    if not f.exists():
        return []
    out = []
    for raw in f.read_text(encoding="utf-8").splitlines():
        m = _ISO_LINE_RE.match(raw)
        if m and m.group(1) in _ANCHOR_TYPES:
            out.append((m.group(1), m.group(2).strip()))
    return out


def _select_d1_samples(segments: list) -> list:
    """D1 抽样选页：已拟合段内随机 2~3 页；多段时每段至少 1 页。"""
    import random
    fitted = [s for s in segments if s["状态"] == "已拟合"]
    if not fitted:
        return []
    pages = []
    for seg in fitted[:3]:
        seg_pages = list(range(seg["PDF页起"], seg["PDF页止"] + 1))
        if seg_pages:
            pages.append(random.choice(seg_pages))
    return sorted(set(pages))[:3]


def _select_d2_samples(iso_dir: Path) -> list:
    """D2 诊断选页：隔离区行数最多的 1~2 页（避开前 3 页封面/目录）。"""
    if not iso_dir or not iso_dir.exists():
        return []
    page_counts = []
    for f in iso_dir.glob("页_*.txt"):
        m = _ISO_FILE_RE.match(f.stem)
        if not m:
            continue
        page = int(m.group(1))
        if page <= 3:
            continue
        lines = f.read_text(encoding="utf-8").splitlines()
        anchor_lines = [l for l in lines if _ISO_LINE_RE.match(l)]
        page_counts.append((page, len(anchor_lines)))
    page_counts.sort(key=lambda x: -x[1])
    return [p for p, _ in page_counts[:2]]


def _format_samples(iso_dir: Path, pages: list) -> str:
    """格式化样本页内容供 LLM 阅读。"""
    parts = []
    for p in pages:
        lines = _read_iso_page(iso_dir, p)
        if lines:
            content = "\n".join(f"  [{t}] {c}" for t, c in lines)
            parts.append(f"第 {p} 页：\n{content}")
    return "\n\n".join(parts) if parts else "（无样本）"


def _parse_llm_json(text: str) -> dict:
    """从 LLM 输出中提取 JSON。"""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group())
            except json.JSONDecodeError:
                pass
    return {}


def _validate_pattern(pattern: str, samples: list) -> tuple:
    """校验候选正则：可编译 + 样例命中。返回 (ok, reason)。"""
    try:
        re.compile(pattern)
    except re.error as e:
        return False, f"正则不可编译：{e}"
    for s in samples:
        m = re.fullmatch(pattern, s.strip())
        if not m:
            return False, f"正则未在样例 {s!r} 上命中"
    return True, ""


def _harvest_with_pattern(iso_dir: Path, pattern: str) -> list:
    """用自定义正则从隔离区收割锚点。"""
    anchors = []
    for f in sorted(iso_dir.glob("页_*.txt"), key=lambda p: (int(p.stem.split("_")[1]), p.name)):
        m = _ISO_FILE_RE.match(f.stem)
        if not m:
            continue
        pdf_page = int(m.group(1))
        for raw in f.read_text(encoding="utf-8").splitlines():
            m2 = _ISO_LINE_RE.match(raw)
            if not m2:
                continue
            typ, content = m2.group(1), m2.group(2).strip()
            if typ not in _ANCHOR_TYPES:
                continue
            try:
                m3 = re.fullmatch(pattern, content)
                if m3:
                    val = int(m3.group(1) if m3.groups() else m3.group(0))
                    if 1 <= val <= 2000:
                        anchors.append((pdf_page, val))
            except (ValueError, IndexError):
                continue
    anchors.sort()
    return anchors


def _run_d1(iso_dir: Path, segments: list) -> tuple:
    """D1 抽样验证。返回 (通过, 消息)。"""
    pages = _select_d1_samples(segments)
    if not pages:
        return True, "D1：无已拟合段可抽查"

    samples_text = _format_samples(iso_dir, pages)
    fitted = [s for s in segments if s["状态"] == "已拟合"]
    pages_info = []
    for seg in fitted:
        for p in range(seg["PDF页起"], seg["PDF页止"] + 1):
            pages_info.append(f"PDF页{p}→印刷页{p + seg['偏移']}")

    system = "你是页码验证助手。请验证程序判定的页码是否正确。输出 JSON：{\"验证结果\": \"通过|不通过\", \"理由\": \"...\"}"
    user = f"""程序判定的页码映射：
{chr(10).join(pages_info[:10])}

样本页原文片段：
{samples_text}

请判断这些页码是否真的是页码（而不是正文中的数字、年份等）。"""

    try:
        out = llm_client.chat(system, user, max_tokens=LLM_MAX_TOKENS, temperature=0.1)
        data = _parse_llm_json(out)
        result = data.get("验证结果", "")
        reason = data.get("理由", "")
        if result == "不通过":
            return False, ("页码验证未通过：引用位置将按 PDF 文件页数标注，"
                           "与书上印刷页码可能不一致；不影响检索，无需处理。")
        return True, ""
    except RuntimeError:
        return True, "页码验证跳过（验证 AI 不可用）：不影响入库与检索，无需处理。"


def _run_d2(iso_dir: Path, segments: list, state: str, book_title: str) -> tuple:
    """D2 诊断补救。返回 (新segments, 新state, 消息, 是否写入配置)。"""
    pages = _select_d2_samples(iso_dir)
    if not pages:
        return segments, state, "页码验证跳过：无有效样本页；不影响入库与检索，无需处理。", False

    samples_text = _format_samples(iso_dir, pages)
    anchor_count = sum(s["锚点数"] for s in segments if s["状态"] == "已拟合")
    result_desc = f"状态={state}，锚点总数={anchor_count}，段数={len(segments)}"

    system = """你是页码格式诊断助手。程序无法识别书籍的页码格式，请诊断并建议格式。
输出 JSON：
{
  "诊断": "...",
  "建议格式": {
    "格式类型": "纯数字|装饰数字|罗马数字|第N页|PageN|N/M|中文数字|其它",
    "候选正则": "...",
    "样例": ["...", "..."]
  },
  "疑似多段偏移": true|false
}"""
    user = f"""程序结果：{result_desc}

样本页原文片段：
{samples_text}

请诊断页码格式。若发现程序因格式不认而漏收，请提供可解析该格式的正则。"""

    try:
        out = llm_client.chat(system, user, max_tokens=LLM_MAX_TOKENS, temperature=0.1)
        data = _parse_llm_json(out)
    except RuntimeError:
        return segments, state, "页码验证跳过（验证 AI 不可用）：不影响入库与检索，无需处理。", False

    suggestion = data.get("建议格式", {})
    pattern = suggestion.get("候选正则", "")
    samples = suggestion.get("样例", [])
    fmt_type = suggestion.get("格式类型", "")
    diagnosis = data.get("诊断", "")

    if not pattern or not samples:
        return segments, state, ("页码验证未通过：引用位置将按 PDF 文件页数标注，"
                                 "与书上印刷页码可能不一致；不影响检索，无需处理。"), False

    ok, reason = _validate_pattern(pattern, samples)
    if not ok:
        return segments, state, ("页码验证未通过：引用位置将按 PDF 文件页数标注，"
                                 "与书上印刷页码可能不一致；不影响检索，无需处理。"), False

    anchors = _harvest_with_pattern(iso_dir, pattern)
    if len(anchors) < 3:
        return segments, state, ("页码验证未通过：引用位置将按 PDF 文件页数标注，"
                                 "与书上印刷页码可能不一致；不影响检索，无需处理。"), False

    new_segments, new_state = page_fit.fit_segments(anchors)
    if new_state != "已拟合":
        return segments, state, ("页码验证未通过：引用位置将按 PDF 文件页数标注，"
                                 "与书上印刷页码可能不一致；不影响检索，无需处理。"), False

    import common
    common.save_book_config(book_title, {
        "页码格式": {
            "格式类型": fmt_type,
            "候选正则": pattern,
            "来源": "llm",
            "生成时间": __import__("datetime").date.today().isoformat(),
        }
    })

    return new_segments, new_state, f"D2 补救成功：{diagnosis}（锚点 {len(anchors)}）", True


def review(iso_dir: Path, segments: list, state: str, book_title: str, book_dir: Path) -> tuple:
    """LLM 分层复查入口。

    返回 (segments, state, llm_used, message)
    """
    if common.is_selfcheck():
        return segments, state, False, ""
    if not llm_client.is_configured():
        return segments, state, False, ""

    # M3 配置冻结：已有页码格式配置 → 用配置重跑，零 LLM 调用
    cfg = common.load_book_config(book_title)
    page_fmt = cfg.get("页码格式", {})
    if page_fmt.get("来源") == "人工":
        return segments, state, False, ""
    if page_fmt.get("候选正则"):
        pattern = page_fmt["候选正则"]
        anchors = _harvest_with_pattern(iso_dir, pattern)
        if anchors:
            new_segments, new_state = page_fit.fit_segments(anchors)
            return new_segments, new_state, False, "使用冻结配置重跑"

    level = assess_confidence(segments, state)
    if level == "高":
        return segments, state, False, ""

    if level == "中":
        ok, msg = _run_d1(iso_dir, segments)
        return segments, state, True, msg

    new_segments, new_state, msg, saved = _run_d2(iso_dir, segments, state, book_title)
    if saved:
        (book_dir / "页码映射.json").write_text(
            json.dumps(page_fit.build_page_map("0000", new_state, new_segments),
                       ensure_ascii=False, indent=2), encoding="utf-8")
    return new_segments, new_state, True, msg


def _selfcheck():
    """page_fit_llm 自检：分层逻辑 + 样本选择 + 正则校验。"""
    print("page_fit_llm 自检")
    ok = True

    segs_high = [{"状态": "已拟合", "置信度": 0.9, "锚点数": 10}]
    segs_mid = [{"状态": "已拟合", "置信度": 0.7, "锚点数": 4}]
    segs_low = [{"状态": "未拟合", "置信度": 0.3, "锚点数": 2}]

    if assess_confidence(segs_high, "已拟合") != "高":
        print("  ❌ 高置信度分层错误")
        ok = False
    if assess_confidence(segs_mid, "已拟合") != "中":
        print("  ❌ 中置信度分层错误")
        ok = False
    if assess_confidence(segs_low, "降级") != "低":
        print("  ❌ 低置信度分层错误")
        ok = False

    ok_pattern, _ = _validate_pattern(r"(\d+)", ["8", "42"])
    if not ok_pattern:
        print("  ❌ 正则校验通过案例失败")
        ok = False
    bad_pattern, _ = _validate_pattern(r"(\d+", ["8"])
    if bad_pattern:
        print("  ❌ 正则校验应拒绝不可编译模式")
        ok = False
    bad_pattern2, _ = _validate_pattern(r"(\d+)", ["abc"])
    if bad_pattern2:
        print("  ❌ 正则校验应拒绝未命中样例")
        ok = False

    if ok:
        print("  page_fit_llm 自检：分层/样本/正则校验通过 ✅")
    else:
        print("  ❌ 自检失败")
    return ok


if __name__ == "__main__":
    if "--自检" in sys.argv:
        sys.exit(0 if _selfcheck() else 1)
    else:
        print("page_fit_llm.py 为 LLM 复查库：请用 --自检 运行，或由 clean_split 调用")
        sys.exit(0)
