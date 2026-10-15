# -*- coding: utf-8 -*-
"""卡 A-07 eval_gen.py：评测题生成 + 判分。

出题：由 A-06 导回时把"提问+答案块ID+密文"写入 评测集.md（本脚本不重复出题）。
判分：对评测集每条，检查 AI 回答引用的块是否含对应密文（或答案块ID），
      记录命中与 token 消耗，回写 评测集.md 的"命中/token"列。
验收：断言评测集表格可被解析、判分逻辑对内置样例给出正确 ✅/❌。
"""

import re
import sys
from pathlib import Path

import common

EVAL_HEADER = "| 题号 | 提问 | 答案块ID | 密文 | 命中 | token |"


def parse_evals(kb_root: Path = None) -> list:
    """解析 评测集.md。返回 [{题号, 提问, 答案块ID, 密文, 命中, token}]。"""
    if kb_root is None:
        kb_root = common.get_path("知识库")
    eval_path = kb_root / "评测集.md"
    rows = []
    if not eval_path.exists():
        return rows
    for line in eval_path.read_text(encoding="utf-8").split("\n"):
        line = line.strip()
        if not line.startswith("|") or line.startswith("| 题号") or line.startswith("|---"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 6:
            continue
        rows.append({
            "题号": cells[0],
            "提问": cells[1],
            "答案块ID": cells[2],
            "密文": cells[3],
            "命中": cells[4],
            "token": cells[5],
        })
    return rows


def judge_answer(answer_text: str, eval_row: dict) -> bool:
    """判分：回答引用的块是否含对应密文（或答案块ID）。

    - 回答中包含该条密文 → 命中（AI 引用了埋针块原文）；
    - 回答中包含答案块ID → 命中（AI 明确引用该块）；
    - 否则未命中。
    """
    needle = eval_row.get("密文", "").strip()
    block_id = eval_row.get("答案块ID", "").strip()
    if not answer_text:
        return False
    if needle and needle in answer_text:
        return True
    if block_id and block_id in answer_text:
        return True
    return False


def run_judge(answers: dict, kb_root: Path = None) -> dict:
    """对评测集逐条判分并回写。answers: {题号: 回答文本}。"""
    if kb_root is None:
        kb_root = common.get_path("知识库")
    eval_path = kb_root / "评测集.md"
    rows = parse_evals(kb_root)
    if not rows:
        raise RuntimeError("评测集为空，请先完成标注导回生成评测题")

    total = 0
    hit = 0
    for row in rows:
        q = row["题号"]
        if q in answers:
            answer = answers[q]
            total += 1
            is_hit = judge_answer(answer, row)
            if is_hit:
                hit += 1
            row["命中"] = "✅" if is_hit else "❌"
            row["token"] = str(max(len(answer), 1))

    lines = ["# 评测集",
             "",
             "> 判分方式：AI 按正常查询流程作答，检查其引用的块是否含对应密文。",
             "",
             EVAL_HEADER,
             "|---|---|---|---|---|---|"]
    for row in rows:
        lines.append(f"| {row['题号']} | {row['提问']} | {row['答案块ID']} | {row['密文']} | "
                     f"{row['命中']} | {row['token']} |")
    eval_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    return {
        "总数": len(rows),
        "已判分": total,
        "命中": hit,
        "命中率": round(hit / total, 4) if total else 0.0,
    }


def _parse_answers_file(path: str) -> dict:
    """回答文件格式：每行 `题号 | 回答内容`。"""
    answers = {}
    for line in Path(path).read_text(encoding="utf-8").split("\n"):
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) >= 2 and cells[0]:
            answers[cells[0]] = " | ".join(cells[1:])
    return answers


def main():
    args = sys.argv[1:]
    answers_file = None
    for i, a in enumerate(args):
        if a == "--判分" and i + 1 < len(args):
            answers_file = args[i + 1]
    if not answers_file:
        print("用法：python eval_gen.py --判分 <回答文件>\n"
              "回答文件每行：`| 题号 | 回答内容 |`")
        sys.exit(1)
    try:
        answers = _parse_answers_file(answers_file)
        if not answers:
            print("回答文件为空或格式错误")
            sys.exit(1)
        r = run_judge(answers)
        print(f"判分完成：{r['命中']}/{r['已判分']} 命中，命中率 {r['命中率']:.1%}，"
              f"评测集共 {r['总数']} 题")
        sys.exit(0)
    except Exception as e:
        print(f"错误：{e}")
        sys.exit(1)


def _selfcheck():
    """验收：断言评测集表格可被解析、判分逻辑对内置样例给出正确 ✅/❌。"""
    import tempfile
    import shutil

    tmp = Path(tempfile.mkdtemp(prefix="eval_selfcheck_"))
    fake_kb = tmp / "kb"
    fake_kb.mkdir(parents=True)
    eval_path = fake_kb / "评测集.md"
    eval_path.write_text(
        "# 评测集\n\n"
        "| 题号 | 提问 | 答案块ID | 密文 | 命中 | token |\n|---|---|---|---|---|---|\n"
        "| Q1 | 排龙以什么为主？ | 自检01·0002 | ⌈验·甲一·子丑⌋ |  |  |\n"
        "| Q2 | 理气讲什么？ | 自检01·0001 | ⌈验·乙二·寅卯⌋ |  |  |\n",
        encoding="utf-8",
    )

    ok = True
    try:
        rows = parse_evals(fake_kb)
        if len(rows) != 2:
            print(f"  ❌ 评测集解析异常：{len(rows)} 行")
            ok = False
        else:
            print(f"  ✅ 评测集解析：{len(rows)} 题")

        # 回答1：包含密文 → ✅；回答2：不相关 → ❌
        answers = {
            "Q1": "排龙以水口为主。⌈验·甲一·子丑⌋",
            "Q2": "完全无关的回答，没引用任何块。",
        }
        r = run_judge(answers, fake_kb)
        rows_after = parse_evals(fake_kb)
        hit1 = rows_after[0]["命中"] == "✅"
        hit2 = rows_after[1]["命中"] == "❌"
        if not (hit1 and hit2 and r["命中"] == 1):
            print(f"  ❌ 判分逻辑错误：{r}，回写：{[x['命中'] for x in rows_after]}")
            ok = False
        else:
            print(f"  ✅ 判分：{r['命中']}/{r['已判分']} 命中，回写 ✅/❌ 正确")

        print(f"  {'✅ 通过' if ok else '❌ 失败'}")
        sys.exit(0 if ok else 1)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    if common.is_selfcheck():
        _selfcheck()
    else:
        main()
