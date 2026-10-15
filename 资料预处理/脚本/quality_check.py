# -*- coding: utf-8 -*-
"""卡 A-02 quality_check.py：质量关卡（红绿灯）。

输入：输出\\提取缓存\\{书号}\\ 全部 txt
判定：① 平均每页字数 ≥30；② 乱码率 ≤1%；③ 【?】密度 ≤3/页（页通道才查）
     注：MinerU 输出不产生【?】，③对 MinerU 通道恒为 0（语义失效，仅保留）
输出：输出\\报告\\{书名}_质检.md（格式见 4.8），含失败页清单与空页清单；退出码 0/1
阈值读取：路径配置.json 的 阈值 节
"""

import json
import re
import sys
from pathlib import Path

import common


def _is_ocr_channel(dir_path: Path) -> bool:
    """通过文件名前缀判断是否为页通道（页_*.txt：文本层提取 / MinerU OCR）还是段通道。"""
    files = [f for f in dir_path.iterdir() if f.is_file() and f.suffix == ".txt"]
    return any(f.name.startswith("页_") for f in files)


def _garbled_ratio(text: str) -> float:
    """乱码率：非法字符/替换符占比。"""
    if not text:
        return 0.0
    bad = len(re.findall(r"[\ufffd]|[\x00-\x08\x0b\x0c\x0e-\x1f]", text))
    return bad / max(len(text), 1)


def quality_check(book_id: str) -> dict:
    """对一本书的提取缓存做质量判定。返回结果 dict。"""
    cache_dir = common.get_path("输出") / "提取缓存" / book_id
    thresholds = common.get_thresholds()
    min_chars = int(thresholds.get("每页字数下限", 30))
    max_garbled = float(thresholds.get("乱码率上限", 0.01))
    max_qmark = int(thresholds.get("不可辨识密度上限", 3))

    files = sorted(
        [f for f in cache_dir.iterdir() if f.is_file() and f.suffix == ".txt"],
        key=lambda f: f.name,
    )
    if not files:
        return {"通过": False, "原因": "提取缓存为空，无 txt 文件", "指标": {}}

    ocr = _is_ocr_channel(cache_dir)
    page_data = []
    for f in files:
        with open(f, "r", encoding="utf-8") as fh:
            text = fh.read()
        chars = len(text.replace("\n", "").replace(" ", ""))
        page_data.append({"文件": f.name, "字数": chars, "乱码率": _garbled_ratio(text)})
        if ocr:
            page_data[-1]["问号密度"] = text.count("【?】") / max(text.count("\n") + 1, 1)

    total_chars = sum(d["字数"] for d in page_data)
    avg_chars = total_chars / max(len(page_data), 1)
    max_garbled_actual = max(d["乱码率"] for d in page_data)
    failed_pages = []

    problems = []
    # 结构红灯（清单阶段一-0）：docx 通道含表格/文本框但转写为 0 → 静默丢失，必须停
    stats_path = cache_dir / "结构统计.json"
    struct_info = None
    if stats_path.exists():
        try:
            struct = json.loads(stats_path.read_text(encoding="utf-8"))
            struct_info = struct
            for key, done_key in (("表格", "表格转写"), ("文本框", "文本框转写")):
                total = int(struct.get(key, 0) or 0)
                done = int(struct.get(done_key, 0) or 0)
                if total > 0 and done == 0:
                    problems.append(f"结构丢失：docx 含 {key} {total} 个但转写 0，请检查提取层")
        except Exception:
            pass
    # "平均每页字数"指标仅适用于 OCR 页通道；段/表通道按自然段划分，不适用
    if ocr and avg_chars < min_chars:
        problems.append(f"平均每页字数 {avg_chars:.0f} < {min_chars}")
    if max_garbled_actual > max_garbled:
        problems.append(f"乱码率 {max_garbled_actual:.2%} > {max_garbled:.2%}")

    if ocr:
        # MinerU 输出不产生【?】，此红绿灯对 MinerU 通道恒不触发（语义失效，仅保留）
        for d in page_data:
            if d["问号密度"] > max_qmark:
                failed_pages.append(d["文件"])
        if failed_pages:
            problems.append(f"【?】密度超标页：{', '.join(failed_pages)}")
    else:
        for d in page_data:
            if d["字数"] == 0:
                failed_pages.append(d["文件"])
        if failed_pages:
            problems.append(f"空页/空段：{', '.join(failed_pages)}")

    passed = not problems
    reason = "；".join(problems) if problems else "全部指标达标"

    metrics = {
        "平均每页字数": round(avg_chars, 1),
        "乱码率": f"{max_garbled_actual:.2%}",
        "失败页数": len(failed_pages),
        "总文件数": len(page_data),
    }
    if ocr:
        # 页通道空页清单：仅呈现不亮红灯（M5；扫描空白页与识别失败由人工判断）
        empty_pages = [d["文件"] for d in page_data if d["字数"] == 0]
        metrics["空页清单"] = "、".join(empty_pages) if empty_pages else "无"
    if struct_info:
        metrics["结构统计"] = (
            f"表格 {struct_info.get('表格', 0)}/{struct_info.get('表格转写', 0)}，"
            f"文本框 {struct_info.get('文本框', 0)}/{struct_info.get('文本框转写', 0)}，"
            f"图片占位 {struct_info.get('图片占位', 0)}（{struct_info.get('图片', 0)}）"
        )
    if ocr:
        metrics["检查项"] = "页通道（含【?】密度；注：MinerU 输出不产生【?】，该项恒为 0，语义失效）"
    else:
        metrics["检查项"] = "直提/文本通道"

    return {
        "通过": passed,
        "原因": reason,
        "指标": metrics,
        "失败页清单": failed_pages,
        "OCR": ocr,
    }


def _render_report(title: str, result: dict) -> str:
    lines = [
        f"# 入库质检报告：《{title}》",
        f"结论：{'🟢 通过' if result['通过'] else '🔴 不通过'}",
        f"原因（一行）：{result['原因']}",
        "",
        "| 指标 | 结果 |",
        "|---|---|",
    ]
    for k, v in result["指标"].items():
        lines.append(f"| {k} | {v} |")
    if result["失败页清单"]:
        lines.append("")
        lines.append("失败页清单：" + "、".join(result["失败页清单"]))
    lines.append("")
    lines.append("建议：把识别失败的页（或整本书）用其他工具转成文字，存成 .txt 放进「待入库」重新入库。")
    return "\n".join(lines)


def main():
    args = sys.argv[1:]
    book_id = book_title = None
    for i, a in enumerate(args):
        if a == "--书号" and i + 1 < len(args):
            book_id = args[i + 1]
        elif a == "--书名" and i + 1 < len(args):
            book_title = args[i + 1]
    if not book_id:
        print("用法：python quality_check.py --书号 <书号> [--书名 <书名>]")
        sys.exit(1)

    result = quality_check(book_id)
    # 报告名书名优先（#23：本步骤在建索引之前，反查倒排表必然落空）；
    # 未传 --书名 时退回反查书名目录，仍无则按书号命名。
    title = book_title or book_id
    if not book_title:
        books_dir = common.get_path("知识库") / "书籍"
        if books_dir.exists():
            for d in books_dir.iterdir():
                if d.is_dir() and (d / "倒排表.json").exists():
                    try:
                        idx = json.loads((d / "倒排表.json").read_text(encoding="utf-8"))
                        if idx.get("书号") == book_id:
                            title = d.name
                            break
                    except Exception:
                        pass
    # #14 全绿不留档：质检通过删旧报告，不通过才落盘
    if result["通过"]:
        common.clear_report(f"{title}_质检.md")
    else:
        path = common.write_report(f"{title}_质检.md", _render_report(title, result))
        print(f"质检报告：{path}")
    print(f"结论：{'🟢 通过' if result['通过'] else '🔴 不通过'}（{result['原因']}）")
    sys.exit(0 if result["通过"] else 1)


def _selfcheck():
    """验收：内置好/坏/结构坏样本，断言退出码 0 与 1；好样本含空页验证空页清单仅呈现不亮红灯。"""
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="qc_selfcheck_"))
    good_dir = tmp / "good"
    bad_dir = tmp / "bad"
    struct_bad_dir = tmp / "struct_bad"
    good_dir.mkdir()
    bad_dir.mkdir()
    struct_bad_dir.mkdir()

    good_text = "玄空之学，首重理气。" * 50
    bad_text = "玄空之学，首重理气。" + "【?】" * 100

    (good_dir / "页_001.txt").write_text(good_text, encoding="utf-8")
    (good_dir / "页_002.txt").write_text(good_text, encoding="utf-8")
    (good_dir / "页_003.txt").write_text("", encoding="utf-8")
    (bad_dir / "页_001.txt").write_text(bad_text, encoding="utf-8")
    # 结构红灯样本：文本正常，但结构统计显示表格 2 个、转写 0（静默丢失）
    (struct_bad_dir / "段_001.txt").write_text(good_text, encoding="utf-8")
    import json as _json
    (struct_bad_dir / "结构统计.json").write_text(
        _json.dumps({"表格": 2, "表格转写": 0, "文本框": 3, "文本框转写": 3,
                     "图片": 1, "图片占位": 1}, ensure_ascii=False), encoding="utf-8")

    import shutil
    cache_root = common.get_path("输出") / "提取缓存"
    cache_root.mkdir(parents=True, exist_ok=True)
    good_target = cache_root / "质检好样本"
    bad_target = cache_root / "质检坏样本"
    struct_bad_target = cache_root / "质检结构坏样本"
    if good_target.exists():
        shutil.rmtree(good_target)
    if bad_target.exists():
        shutil.rmtree(bad_target)
    if struct_bad_target.exists():
        shutil.rmtree(struct_bad_target)
    shutil.copytree(good_dir, good_target)
    shutil.copytree(bad_dir, bad_target)
    shutil.copytree(struct_bad_dir, struct_bad_target)

    try:
        good = quality_check("质检好样本")
        bad = quality_check("质检坏样本")
        struct_bad = quality_check("质检结构坏样本")
        ok = good["通过"] and not bad["通过"] and not struct_bad["通过"]
        ok = ok and "结构丢失" in struct_bad["原因"]
        ok = ok and good["指标"].get("空页清单") == "页_003.txt"
        print(f"  好样本：{'🟢' if good['通过'] else '🔴'}（{good['原因']}；空页清单：{good['指标'].get('空页清单')}）")
        print(f"  坏样本：{'🔴' if not bad['通过'] else '🟢'}（{bad['原因']}）")
        print(f"  结构坏样本：{'🔴' if not struct_bad['通过'] else '🟢'}（{struct_bad['原因']}）")
        print(f"  {'✅ 通过' if ok else '❌ 失败'}")
        sys.exit(0 if ok else 1)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(good_target, ignore_errors=True)
        shutil.rmtree(bad_target, ignore_errors=True)
        shutil.rmtree(struct_bad_target, ignore_errors=True)


if __name__ == "__main__":
    if common.is_selfcheck():
        _selfcheck()
    else:
        main()
