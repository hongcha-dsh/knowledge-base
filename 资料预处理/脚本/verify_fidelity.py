# -*- coding: utf-8 -*-
"""忠实性对账（设计 §六-1 红线①自证）：重切前后字符守恒校验。

原理：新管线把页眉/页脚/纯页码行/跨页重复行/目录同名行从正文**移入** 隔离区.md
（只移不删、不重排）。由此可下两个硬断言：
  ① 新原文.md＋隔离区.md 归一化后的**字符多重集**必须与 旧原文.md 一致（无丢字/多字）；
  ② 新原文.md 归一化**字符序列**必须是 旧原文.md 归一化序列的**子序列**（正文顺序未乱）。
块 ID 行（【书号·序号】p.…）与 隔离区.md 的标记行不参与对账（页码段允许三态变化）。

用法：
  python verify_fidelity.py --快照 --书名 玄空风水精论                 # 重切前留基线
  python verify_fidelity.py --书名 玄空风水精论                        # 重切后对账
  python verify_fidelity.py --书名 X --基线 <旧原文.md 路径>           # 指定基线
  python verify_fidelity.py --自检                                    # 门禁自检
退出码：0=一致，1=不一致（丢字/乱序），2=黄灯（仅多出隔离区内容，需人工确认）
"""
import io
import re
import shutil
import sys
import tempfile
from collections import Counter
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

BLOCK_ID_RE = re.compile(r"^【[^】]+】p\..*$")
ISO_MARK_RE = re.compile(r"^【隔离·[^】]+】p\..*$")
WS_RE = re.compile(r"[\s\u3000\u00a0]+")


def _norm(text: str) -> str:
    """归一化：去掉全部空白（含全角），其余字符原样保留（保留标点，比检索归一更严）。"""
    return WS_RE.sub("", text)


def _read_norm(path: Path, drop_iso_markers: bool = False) -> str:
    """读文本 → 去块 ID 行/隔离区标记行 → 归一化为字符序列。"""
    buf = []
    for ln in path.read_text(encoding="utf-8").split("\n"):
        s = ln.strip()
        if not s or BLOCK_ID_RE.match(s):
            continue
        if drop_iso_markers and (
            ISO_MARK_RE.match(s) or s.startswith("# 隔离区：") or s.startswith(">")
        ):
            continue
        buf.append(s)
    return _norm("".join(buf))


def _subseq_break(sub: str, seq: str) -> int:
    """sub 是否为 seq 的子序列；是则返回 -1，否则返回 sub 中断处的下标。"""
    i = 0
    for c in seq:
        if i < len(sub) and sub[i] == c:
            i += 1
            if i == len(sub):
                return -1
    return i


def compare(old_path: Path, new_path: Path, iso_path: Path) -> dict:
    """执行对账，返回指标字典（纯函数，供 main 与自检共用）。"""
    old = _read_norm(old_path)
    new = _read_norm(new_path)
    iso = _read_norm(iso_path, drop_iso_markers=True) if iso_path.exists() else ""
    combined = new + iso
    lost = Counter(old) - Counter(combined)
    extra = Counter(combined) - Counter(old)
    break_at = _subseq_break(new, old)
    return {
        "旧": old, "新": new, "隔离": iso, "合并": combined,
        "丢字": lost, "多字": extra, "子序列断点": break_at,
    }


def _ctx(seq: str, idx: int, width: int = 30) -> str:
    a = max(0, idx - width)
    return seq[a: idx + width]


def _report(book_title: str, r: dict) -> int:
    lost_n = sum(r["丢字"].values())
    extra_n = sum(r["多字"].values())
    break_at = r["子序列断点"]
    subseq_ok = break_at == -1
    subseq_line = ("- 断言②正文子序列：一致（顺序未乱）" if subseq_ok
                   else f"- 断言②正文子序列：断裂于新原文第 {break_at} 字")
    lines = [
        f"# 忠实性对账：《{book_title}》",
        "",
        f"- 旧原文归一化字符数：{len(r['旧'])}",
        f"- 新原文归一化字符数：{len(r['新'])}",
        f"- 隔离区归一化字符数：{len(r['隔离'])}",
        f"- 合并（新原文＋隔离区）字符数：{len(r['合并'])}",
        f"- 断言①字符多重集：丢字 {lost_n} 个，多字 {extra_n} 个",
        subseq_line,
    ]
    if lost_n:
        sample = "".join(list(r["丢字"].elements())[:40])
        lines.append(f"- 丢字样例：{sample}")
    if extra_n:
        sample = "".join(list(r["多字"].elements())[:40])
        lines.append(f"- 多字样例：{sample}（预期来自旧管线已删除、现改隔离的页眉/页脚/页码行）")
    if not subseq_ok:
        lines.append(f"- 断点上下文（旧原文）：…{_ctx(r['旧'], r['子序列断点'])}…")
    verdict = "🟢 一致" if (lost_n == 0 and extra_n == 0 and subseq_ok) else (
        "🔴 不一致" if (lost_n or not subseq_ok) else "🟡 需人工确认（仅多出隔离区内容）")
    lines.append("")
    lines.append(f"结论：{verdict}")
    print("\n".join(lines))
    # #14 全绿不留档：一致则删旧报告，不一致/需人工确认才落盘
    if lost_n or not subseq_ok:
        common.write_report(f"{book_title}_忠实性对账.md", "\n".join(lines) + "\n")
        return 1
    if extra_n:
        common.write_report(f"{book_title}_忠实性对账.md", "\n".join(lines) + "\n")
        return 2
    common.clear_report(f"{book_title}_忠实性对账.md")
    return 0


def _baseline_path(book_title: str) -> Path:
    return common.get_path("输出") / "对账基线" / f"{book_title}_原文.md"


def main():
    args = sys.argv[1:]
    book_title = None
    baseline = None
    do_snapshot = False
    for i, a in enumerate(args):
        if a == "--书名" and i + 1 < len(args):
            book_title = args[i + 1]
        elif a == "--基线" and i + 1 < len(args):
            baseline = Path(args[i + 1])
        elif a == "--快照":
            do_snapshot = True
    if not book_title:
        print("用法：python verify_fidelity.py --书名 <书名> [--基线 <路径>] [--快照]")
        sys.exit(1)

    new_path = common.get_path("知识库") / "书籍" / book_title / "原文.md"
    iso_path = common.get_path("知识库") / "书籍" / book_title / "隔离区.md"
    if do_snapshot:
        if not new_path.exists():
            print(f"找不到待快照的原文：{new_path}")
            sys.exit(1)
        bp = baseline or _baseline_path(book_title)
        bp.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(new_path, bp)
        print(f"已留基线：{bp}（{new_path.stat().st_size} 字节）")
        sys.exit(0)

    bp = baseline or _baseline_path(book_title)
    if not bp.exists():
        print(f"找不到基线：{bp}（重切前先跑 --快照）")
        sys.exit(1)
    if not new_path.exists():
        print(f"找不到新原文：{new_path}")
        sys.exit(1)
    sys.exit(_report(book_title, compare(bp, new_path, iso_path)))


def _selfcheck():
    """门禁自检：临时文件构造四态（一致/丢字/多字/乱序），断言判定与退出码口径。"""
    tmp = Path(tempfile.mkdtemp(prefix="verify_fidelity_selfcheck_"))
    ok = True
    try:
        old = tmp / "old.md"
        new = tmp / "new.md"
        iso = tmp / "iso.md"
        old.write_text("【X·0001】p.001\n玄空之学首重理气\n\n第七章\n", encoding="utf-8")

        # 态1：新原文＝旧原文去掉被隔离行，隔离区收该行 → 一致（多字 0，退出码 0）
        new.write_text("【X·0001】p.001\n玄空之学首重理气\n", encoding="utf-8")
        iso.write_text(
            "# 隔离区：《X》\n> 说明\n\n【隔离·跨页重复行】p.1\n第七章\n", encoding="utf-8"
        )
        r = compare(old, new, iso)
        if sum(r["丢字"].values()) or sum(r["多字"].values()) or r["子序列断点"] != -1:
            print(f"  ❌ 态1 应一致：{r['丢字']} / {r['多字']} / {r['子序列断点']}")
            ok = False

        # 态2：新原文丢字（旧有新无）→ 丢字，判定不一致
        new.write_text("【X·0001】p.001\n玄空之学首重理\n", encoding="utf-8")
        iso.write_text("# 隔离区：《X》\n\n【隔离·跨页重复行】p.1\n第七章\n", encoding="utf-8")
        r = compare(old, new, iso)
        if sum(r["丢字"].values()) != 1:
            print(f"  ❌ 态2 应丢 1 字：{r['丢字']}")
            ok = False

        # 态3：新原文多字（旧无新有，且不在隔离区）→ 多字，判定黄灯
        new.write_text(
            "【X·0001】p.001\n玄空之学首重理气补\n", encoding="utf-8"
        )
        iso.write_text("# 隔离区：《X》\n\n【隔离·跨页重复行】p.1\n第七章\n", encoding="utf-8")
        r = compare(old, new, iso)
        if sum(r["多字"].values()) != 1 or sum(r["丢字"].values()) != 0:
            print(f"  ❌ 态3 应多 1 字：{r['多字']} / {r['丢字']}")
            ok = False

        # 态4：正文乱序 → 子序列断裂
        new.write_text("【X·0001】p.001\n首重理气玄空之学\n", encoding="utf-8")
        iso.write_text("# 隔离区：《X》\n\n【隔离·跨页重复行】p.1\n第七章\n", encoding="utf-8")
        r = compare(old, new, iso)
        if r["子序列断点"] == -1:
            print("  ❌ 态4 乱序应被判为子序列断裂")
            ok = False

        print(f"  verify_fidelity 自检：{'✅ 通过' if ok else '❌ 失败'}")
        sys.exit(0 if ok else 1)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    if common.is_selfcheck():
        _selfcheck()
    else:
        main()
