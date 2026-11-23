# -*- coding: utf-8 -*-
"""卡 A-05 verify_needles.py：密文检具（层① 全自动，验管线机械正确性）。

输入：知识库\\书籍\\{书名}\\原文.md + 检索副本.md
处理：① 复制原文到 输出\\验证\\{书名}_kb\\（临时副本）；
      ② 生成 5~8 根独特密文 ⌈验·{书号}{天干}{序号}·{地支}{地支}⌋（书号维度保证跨书唯一，
         先全文搜索确认零撞车），
         埋入 开头/中间/结尾/最长段 内的块（行内追加，行数不变，保持行对齐）；
      ③ 在副本上重建倒排表，调用 B-01 检索每根针，记录命中块与位置偏差；
      ④ 抽取针所在原句写入 输出\\验证\\{书名}_评测原句.txt；
      ⑤ 验证后删除副本目录；比对原文.md 哈希自证零改动。
输出：输出\\报告\\{书名}_密文验证.md；退出码：全中=0，有漏=1
"""

import hashlib
import json
import re
import shutil
import sys
from pathlib import Path

import common

# 引入 B-01 检索脚本（随「知识库」配置目录走；沙箱模式下也指向真实库，因为该目录不参与沙箱重定向）
sys.path.insert(0, str(common.PROJECT_ROOT / common.load_config()["知识库"] / "检索脚本"))
import search_keyword  # noqa: E402

TIANGAN = "甲乙丙丁戊己庚辛壬癸"
DIZHI = "子丑寅卯辰巳午未申酉戌亥"
CN_NUM = "一二三四五六七八九"
NEEDLE_COUNT = 6
BLOCK_ID_RE = re.compile(r"^【(.+?)·(\d{4})】p\.(.*)$")


def _needle(tiangan_idx: int, dizhi_pair_idx: int, book_id: str) -> str:
    """生成密文：⌈验·{书号}{天干}{序号}·{地支}{地支}⌋（书号维度保证跨书唯一）"""
    t = TIANGAN[tiangan_idx % len(TIANGAN)]
    n = CN_NUM[(tiangan_idx // len(TIANGAN)) % len(CN_NUM)]
    d1 = DIZHI[dizhi_pair_idx % len(DIZHI)]
    d2 = DIZHI[(dizhi_pair_idx + 1) % len(DIZHI)]
    return f"⌈验·{book_id}{t}{n}·{d1}{d2}⌋"


def _scan_blocks(orig_lines: list, copy_lines: list) -> list:
    """解析块结构。返回 [{块ID, 起始行, 结束行, 首正文行, 指纹, 页}]（0基行号）。"""
    blocks = []
    cur = None
    for i, (ol, cl) in enumerate(zip(orig_lines, copy_lines)):
        m = BLOCK_ID_RE.match(ol.strip())
        if m:
            if cur:
                cur["结束行"] = i - 1
            cur = {
                "块ID": f"{m.group(1)}·{m.group(2)}",
                "起始行": i,
                "结束行": None,
                "页码": m.group(3),
                "首正文行": None,
                "指纹": "",
            }
            blocks.append(cur)
        elif cur is not None:
            if cl.strip() and cur["首正文行"] is None:
                cur["首正文行"] = i
                cur["指纹"] = cl.strip()[:12]
    if cur:
        cur["结束行"] = len(orig_lines) - 1
    return blocks


def _pick_needle_blocks(blocks: list) -> list:
    """选 5~8 个块：开头/中间/结尾/最长段/均匀分布（块数不足时允许重复使用块）。"""
    if len(blocks) <= 1:
        return blocks[:1] * NEEDLE_COUNT
    picks = {0, len(blocks) - 1, len(blocks) // 2}
    longest = max(range(len(blocks)), key=lambda i: blocks[i]["结束行"] - blocks[i]["起始行"])
    picks.add(longest)
    step = len(blocks) / NEEDLE_COUNT
    for k in range(NEEDLE_COUNT):
        picks.add(min(len(blocks) - 1, int(k * step)))
    picks = sorted(list(picks))[:NEEDLE_COUNT]
    result = []
    for i in range(NEEDLE_COUNT):
        result.append(blocks[picks[i % len(picks)]])
    return result


def _plan_needle_rows(block: dict, needle_idx: int) -> int:
    """确定某块内埋第 needle_idx 根针的行（轮换正文行，避免冲突）。"""
    rows = list(range(block["起始行"] + 1, block["结束行"] + 1))
    if not rows:
        return block["起始行"] + 1
    return rows[needle_idx % len(rows)]


def _build_inverted(book_id: str, copy_lines: list, blocks: list) -> dict:
    """在副本检索副本上构建 n-gram 倒排表（密文已埋入）。"""
    inverted = {}
    for blk in blocks:
        text = "".join(copy_lines[blk["起始行"] + 1 : blk["结束行"] + 1])
        chars = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", text)
        for n in (2, 3):
            for i in range(len(chars) - n + 1):
                inverted.setdefault(chars[i : i + n], [])
                if blk["块ID"] not in inverted[chars[i : i + n]]:
                    inverted[chars[i : i + n]].append(blk["块ID"])
    return {"书号": book_id, "构建时间": "", "条目": inverted}


def verify_book(book_title: str, book_id: str, needle_count: int = NEEDLE_COUNT) -> dict:
    """对一本书执行密文自检。返回结果 dict。"""
    books_dir = common.get_path("知识库") / "书籍"
    book_dir = books_dir / book_title
    orig_path = book_dir / "原文.md"
    copy_path = book_dir / "检索副本.md"
    if not orig_path.exists() or not copy_path.exists():
        raise FileNotFoundError(f"缺少 原文.md/检索副本.md：{book_dir}")

    # 红线：验证前后原文哈希比对
    def _hash(p: Path) -> str:
        return hashlib.sha256(p.read_bytes()).hexdigest()

    orig_hash_before = _hash(orig_path)

    orig_lines = orig_path.read_text(encoding="utf-8").split("\n")
    copy_lines = copy_path.read_text(encoding="utf-8").split("\n")
    if len(orig_lines) != len(copy_lines):
        raise RuntimeError("原文.md 与 检索副本.md 行数不一致")

    blocks = _scan_blocks(orig_lines, copy_lines)
    if not blocks:
        raise RuntimeError("未解析到任何块")

    # M3 图表块（4.6）：埋针避开图表块（清单内块不参与选块，避免盘面/表格被密文污染）
    chart_ids = set()
    chart_path = book_dir / "图表块清单.json"
    if chart_path.exists():
        try:
            chart_ids = set(json.loads(chart_path.read_text(encoding="utf-8")).get("图表块", []))
        except Exception:
            chart_ids = set()
    needle_candidates = [b for b in blocks if b["块ID"] not in chart_ids]
    if not needle_candidates:
        raise RuntimeError("无可用埋针块（全书均为图表块）")

    # 生成密文并确认零撞车（在真实原文+副本全文搜索）
    needles = []
    used = set()
    ti, di = 0, 0
    while len(needles) < needle_count and ti < 200:
        n = _needle(ti, di, book_id)
        norm_n = search_keyword.normalize(n)
        di += 1
        if di % 6 == 0:
            ti += 1
        if n in used or norm_n in used:
            continue
        full_text = "\n".join(orig_lines + copy_lines)
        if norm_n in full_text:
            continue
        used.add(n)
        used.add(norm_n)
        needles.append(n)
    if not needles:
        raise RuntimeError("无法生成不与正文撞车的密文")

    # 选块（图表块已排除）
    pick_blocks = _pick_needle_blocks(needle_candidates)

    # 埋针：在块内某正文行末尾追加密文（行数不变，行对齐保持）
    copy_orig = list(orig_lines)
    copy_copy = list(copy_lines)
    needle_targets = []  # [(密文, 块ID, 行号0基, 原句)]
    for idx, (needle, blk) in enumerate(zip(needles, pick_blocks)):
        insert_at = _plan_needle_rows(blk, idx)
        if insert_at > blk["结束行"] or insert_at <= blk["起始行"]:
            insert_at = blk["起始行"] + 1
        original_line = copy_orig[insert_at]
        copy_orig[insert_at] = original_line + needle
        copy_copy[insert_at] = copy_copy[insert_at] + search_keyword.normalize(needle)
        needle_targets.append((needle, blk["块ID"], insert_at, original_line))

    # 写临时副本知识库
    verify_dir = common.get_path("输出") / "验证"
    verify_dir.mkdir(parents=True, exist_ok=True)
    fake_kb = verify_dir / f"{book_title}_kb"
    fake_book = fake_kb / "书籍" / book_title
    if fake_kb.exists():
        shutil.rmtree(fake_kb)
    fake_book.mkdir(parents=True, exist_ok=True)
    (fake_book / "原文.md").write_text("\n".join(copy_orig), encoding="utf-8")
    (fake_book / "检索副本.md").write_text("\n".join(copy_copy), encoding="utf-8")

    # 副本倒排表
    fake_blocks = _scan_blocks(copy_orig, copy_copy)
    inverted = _build_inverted(book_id, copy_copy, fake_blocks)
    (fake_book / "倒排表.json").write_text(
        json.dumps(inverted, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # 检索每根针
    report_rows = []
    hit_all = True
    for needle, target_bid, insert_line, original_line in needle_targets:
        results = search_keyword.query_keywords(fake_kb, needle)
        hit = any(r[1] == target_bid for r in results)
        if not hit:
            hit_all = False
        # 位置偏差：命中块实际行号与预期埋针行号之差
        try:
            loc = search_keyword.locate_block(fake_kb, target_bid)
            actual_line = loc["起始行"] - 1
            deviation = abs(actual_line - insert_line)
        except ValueError:
            deviation = -1
        report_rows.append({
            "密文": needle,
            "埋针块": target_bid,
            "命中": hit,
            "命中块": ", ".join(r[1] for r in results[:3]) if results else "-",
            "埋针行": insert_line + 1,
            "实际行": actual_line + 1 if deviation >= 0 else "-",
            "偏差": deviation,
            "原句": original_line,
        })

    # 评测原句.txt（供 A-07 出题：块ID | 密文 | 原句）
    with open(verify_dir / f"{book_title}_评测原句.txt", "w", encoding="utf-8") as f:
        for row in report_rows:
            f.write(f"{row['埋针块']} | {row['密文']} | {row['原句']}\n")

    # 清理副本
    shutil.rmtree(fake_kb, ignore_errors=True)

    # 红线：原文哈希未变
    orig_hash_after = _hash(orig_path)
    hash_unchanged = orig_hash_before == orig_hash_after

    report = _render_report(book_title, book_id, report_rows, hit_all, hash_unchanged)
    common.write_report(f"{book_title}_密文验证.md", report)

    return {
        "书名": book_title,
        "书号": book_id,
        "总针数": len(report_rows),
        "命中数": sum(1 for r in report_rows if r["命中"]),
        "全部命中": hit_all,
        "原文哈希未变": hash_unchanged,
        "位置偏差列表": [r["偏差"] for r in report_rows],
    }


def _render_report(book_title, book_id, rows, hit_all, hash_unchanged) -> str:
    lines = [
        f"# 密文验证报告：《{book_title}》",
        f"结论：{'✅ 全部命中' if hit_all else '❌ 有漏针'}",
        f"总针数：{len(rows)} | 命中：{sum(1 for r in rows if r['命中'])} | "
        f"原文零改动：{'✅' if hash_unchanged else '❌'}",
        "",
        "| 密文 | 埋针块 | 命中 | 命中块 | 埋针行 | 实际行 | 偏差 |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(f"| {r['密文']} | {r['埋针块']} | {'✅' if r['命中'] else '❌'} | "
                     f"{r['命中块']} | {r['埋针行']} | {r['实际行']} | {r['偏差']} |")
    return "\n".join(lines) + "\n"


def main():
    args = sys.argv[1:]
    book_title = book_id = None
    for i, a in enumerate(args):
        if a == "--书名" and i + 1 < len(args):
            book_title = args[i + 1]
        elif a == "--书号" and i + 1 < len(args):
            book_id = args[i + 1]
    if not book_title or not book_id:
        print("用法：python verify_needles.py --书名 <书名> --书号 <书号>")
        sys.exit(1)
    try:
        r = verify_book(book_title, book_id)
        print(f"自动验证：《{r['书名']}》 命中 {r['命中数']}/{r['总针数']}，"
              f"原文零改动：{'✅' if r['原文哈希未变'] else '❌'}")
        sys.exit(0 if r["全部命中"] else 1)
    except Exception as e:
        common.write_report(f"{book_title or book_id}_密文验证.md",
                            f"# 密文验证报告\n结论：🔴 失败\n原因：{e}\n")
        print(e)
        sys.exit(1)


def _selfcheck():
    """验收：断言副本已删、原文哈希未变、报告已生成。"""
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="needle_selfcheck_"))
    fake_kb = tmp / "kb"
    book_dir = fake_kb / "书籍" / "自检书"
    book_dir.mkdir(parents=True)

    orig_lines = []
    copy_lines = []
    for b in range(1, 9):
        orig_lines.append(f"【自检01·{b:04d}】p.{b}")
        copy_lines.append(f"【自检01·{b:04d}】p.{b}")
        body = f"第{b}章玄空理气阴阳五行运行于天地之间" + "水口排龙挨星" * 20
        orig_lines.append(body)
        copy_lines.append(body)
        orig_lines.append("")
        copy_lines.append("")

    (book_dir / "原文.md").write_text("\n".join(orig_lines), encoding="utf-8")
    (book_dir / "检索副本.md").write_text("\n".join(copy_lines), encoding="utf-8")

    # 临时把知识库指向 fake_kb
    import json
    cfg_path = common.PROJECT_ROOT / "路径配置.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    saved_kb = cfg["知识库"]
    cfg["知识库"] = str(fake_kb)
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    # M3 图表块（4.6）：块 0004 标记为图表块，埋针应避开
    (book_dir / "图表块清单.json").write_text(
        json.dumps({"图表块": ["自检01·0004"]}, ensure_ascii=False, indent=2), encoding="utf-8")
    ok = True
    try:
        r = verify_book("自检书", "自检01")
        fake_kb_after = fake_kb / "书籍" / "自检书" / "_kb_placeholder"
        verify_dir = common.get_path("输出") / "验证"
        leftover = [p for p in verify_dir.glob("*") if p.is_dir() and p.name.endswith("_kb")]
        report = common.report_dir() / "自检书_密文验证.md"
        if leftover:
            print(f"  ❌ 副本未清理：{leftover}")
            ok = False
        if not r["原文哈希未变"]:
            print("  ❌ 原文被改动")
            ok = False
        if not report.exists():
            print("  ❌ 报告未生成")
            ok = False
        # M3 图表块（4.6）：埋针块不得落在图表块 0004
        if report.exists():
            report_txt = report.read_text(encoding="utf-8")
            if any("| 自检01·0004 |" in ln for ln in report_txt.split("\n")):
                print("  ❌ 埋针落到了图表块 0004")
                ok = False
        print(f"  密文自检：《{r['书名']}》 命中 {r['命中数']}/{r['总针数']}，原文零改动："
              f"{'✅' if r['原文哈希未变'] else '❌'}，报告：{'✅' if report.exists() else '❌'}")
        print(f"  {'✅ 通过' if ok else '❌ 失败'}")
        sys.exit(0 if ok else 1)
    finally:
        cfg["知识库"] = saved_kb
        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        shutil.rmtree(tmp, ignore_errors=True)
        # 清理自检残留
        verify_dir = common.get_path("输出") / "验证"
        for p in verify_dir.glob("自检书*"):
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            else:
                p.unlink(missing_ok=True)
        rpt = common.report_dir() / "自检书_密文验证.md"
        if rpt.exists():
            rpt.unlink()


if __name__ == "__main__":
    if common.is_selfcheck():
        _selfcheck()
    else:
        common.run_with_write_lock(main, "密文验证")
