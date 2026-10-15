# -*- coding: utf-8 -*-
"""端到端演示脚本：造样本 → 入库 → 密文验证 → 标注 → 检索 → 评测。

沙箱运行：启动时备份并临时改写 路径配置.json（知识库/待入库/输出/报告 指向临时目录），
全流程子进程读取同一份配置自然落入沙箱；结束后恢复配置并清理沙箱。
演示与真实知识库彻底隔离，可重复运行且零污染。
"""

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common


def run(script, *args):
    script = common.get_path("脚本目录") / script
    p = subprocess.run([sys.executable, str(script), *args],
                       capture_output=True, text=True, encoding="utf-8",
                       cwd=str(common.PROJECT_ROOT), timeout=600)
    tail = (p.stdout or p.stderr).strip()
    return p.returncode, tail


def search(*args):
    return run(common.get_path("知识库") / "检索脚本" / "search_keyword.py", *args)


def main():
    cfg_path = common.PROJECT_ROOT / "路径配置.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    keys = ["知识库", "待入库", "输出"] + (["报告"] if "报告" in cfg else [])
    saved = {k: cfg[k] for k in keys}
    sandbox = Path(tempfile.mkdtemp(prefix="e2e_sandbox_"))
    for key in keys:
        (sandbox / key).mkdir(parents=True, exist_ok=True)
        cfg[key] = str(sandbox / key)
    # 检索脚本随「知识库」走，沙箱重定向后子进程 import 找不到，需拷入沙箱
    real_kb = common.get_path("知识库")
    shutil.copytree(real_kb / "检索脚本", sandbox / "知识库" / "检索脚本")
    # search_keyword.py 以 parents[2] 反推项目根找 common.py，沙箱内拷贝保持解析
    (sandbox / "资料预处理" / "脚本").mkdir(parents=True, exist_ok=True)
    shutil.copy2(common.get_path("脚本目录") / "common.py",
                 sandbox / "资料预处理" / "脚本" / "common.py")
    # 沙箱内 common.py 的 find_project_root() 需找到 路径配置.json（沙箱根）
    sandbox_cfg = dict(cfg)
    sandbox_cfg["知识库"] = "知识库"
    sandbox_cfg["待入库"] = "待入库"
    sandbox_cfg["输出"] = "输出"
    sandbox_cfg["报告"] = "报告"
    (sandbox / "路径配置.json").write_text(
        json.dumps(sandbox_cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"沙箱：{sandbox}")

    ok = True
    try:
        # 0) 造样本 docx（沙箱待入库内）
        import docx
        sample = common.get_path("待入库") / "1--玄空风水--基础篇.docx"
        d = docx.Document()
        d.add_paragraph("第一章 玄空理气基础")
        paras = [
            "玄空之学，首重理气。阴阳五行之气运行于天地之间，随方位而变。",
            "挨星之法，以九星配九宫，察其生旺衰死，断其吉凶祸福。",
            "排龙之法，以水口为主，顺逆相推，定其星曜。",
            "坐山朝向，当以元运为纲，旺山旺向方为吉宅。",
            "三元九运，每运二十年，上元一二三四运，中元五六七运，下元八九运。",
            "罗盘二十四山，分阴阳二气，兼向须察消砂纳水。",
        ]
        for p in paras:
            d.add_paragraph(p)
        d.save(str(sample))
        print(f"造样本：{sample.name}")

        # 1) 入库
        code, out = run("extract.py", "--文件", "1--玄空风水--基础篇.docx", "--书号", "玄空01")
        print(f"[extract] {code} {out}")
        ok &= code in (0, 2)

        code, out = run("quality_check.py", "--书号", "玄空01")
        print(f"[quality_check] {code} {out}")
        ok &= code == 0

        code, out = run("clean_split.py", "--书号", "玄空01", "--书名", "玄空风水")
        print(f"[clean_split] {code} {out}")
        ok &= code == 0

        code, out = run("build_index.py", "--书号", "玄空01", "--书名", "玄空风水")
        print(f"[build_index] {code} {out}")
        ok &= code == 0

        # 2) 密文验证
        code, out = run("verify_needles.py", "--书名", "玄空风水", "--书号", "玄空01")
        print(f"[verify_needles] {code} {out}")
        ok &= code == 0

        # 3) 检索
        code, out = search("--query", "排龙")
        print(f"[search --query 排龙] {code}\n{out}")
        ok &= code == 0

        # 3b) 定位块 + 指纹验证
        code, out = search("--locate", "玄空01·0001")
        print(f"[search --locate] {code} {out}")
        ok &= code == 0
        code, out = search("--verify", "玄空01·0001", "第一章玄空理气基础")
        print(f"[search --verify] {code} {out}")
        ok &= code == 0

        # 4) 标注任务包生成
        code, out = run("annotate_pack.py", "--生成", "--书名", "玄空风水", "--章号", "01")
        print(f"[annotate 生成] {code} {out}")
        ok &= code == 0

        # 5) 模拟回填 → 导回 → 评测集闭环（层②评测）
        import re as _re
        pack_path = common.get_path("输出") / "任务包" / "玄空风水" / "01_任务包.md"
        filled = pack_path.read_text(encoding="utf-8")
        filled = filled.replace("（在此填写，替换本行）",
                                "本章讲述玄空理气基础与排龙水法要诀，强调以水口为主顺逆排龙。")
        filled = _re.sub(r"(\| 玄空01·0001 \|[^|]*\|)  \|  \|  \|",
                         r"\1 理气, 阴阳, 五行, 挨星, 排龙 | 玄空, 理气 | 玄空学 |", filled)
        q_idx = [0]

        def _fill_q(m):
            q_idx[0] += 1
            return f"| Q{q_idx[0]} | 玄空理气第{q_idx[0]}问 | 玄空01·0001 |"

        filled = _re.sub(r"\| Q\d+ \|  \| 玄空01·0001 \|", _fill_q, filled)
        filled_path = common.get_path("输出") / "任务包" / "玄空风水" / "回填" / "01_任务包.md"
        filled_path.write_text(filled, encoding="utf-8")
        code, out = run("annotate_pack.py", "--导回", "--书名", "玄空风水", "--章号", "01")
        print(f"[annotate 导回] {code} {out}")
        ok &= code == 0

        eval_path = common.get_path("知识库") / "评测集.md"
        eval_text = eval_path.read_text(encoding="utf-8") if eval_path.exists() else ""
        needles = _re.findall(r"⌈验·[^⌋]*⌋", eval_text)
        n_rows = len(_re.findall(r"^\| Q\d+ \|", eval_text, _re.M))
        print(f"[评测集] 存在={eval_path.exists()} 题数={n_rows} 密文数={len(needles)}")
        ok &= bool(needles)

        if needles:
            answers = common.get_path("输出") / "验证" / "e2e_回答.md"
            answers.parent.mkdir(parents=True, exist_ok=True)
            answers.write_text(
                f"| Q1 | 按原文作答：{needles[0]} |\n| Q2 | 无关回答，未引用任何块。 |\n",
                encoding="utf-8")
            code, out = run("eval_gen.py", "--判分", str(answers))
            print(f"[eval 判分] {code} {out}")
            ok &= (code == 0) and ("1/2 命中" in out or "2/3 命中" in out)

        print(f"\n端到端：{'✅ 全部通过' if ok else '❌ 有失败'}")
        sys.exit(0 if ok else 1)
    finally:
        cfg.update(saved)
        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        shutil.rmtree(sandbox, ignore_errors=True)


if __name__ == "__main__":
    main()
