# -*- coding: utf-8 -*-
"""公共模块：路径配置读取、报告写入、自检辅助。

设计文档 3.1 / 6.0 公共约束：
- 唯一起点配置文件 路径配置.json 位于项目根；
- 所有路径基于项目根目录推算，禁止绝对路径字面量；
- 文件读写一律 encoding='utf-8'；
- 退出码约定：0=成功/绿灯，1=失败/红灯，2=部分成功。
"""

import json
import sys
from pathlib import Path

PROJECT_ROOT: Path = None


def find_project_root() -> Path:
    """从本模块位置向上定位项目根目录（含 路径配置.json 的目录）。"""
    here = Path(__file__).resolve()
    for parent in [here] + list(here.parents):
        if (parent / "路径配置.json").exists():
            return parent
    raise RuntimeError("找不到 路径配置.json，请确认项目结构完整（脚本应位于 项目根\\资料预处理\\脚本 或 项目根\\知识库\\检索脚本 下）")


PROJECT_ROOT = find_project_root()

# 按书特化规则（书规则\{书名}.json）的键名，集中在此，避免各脚本各写一套
RULE_CELL_BR = "排盘_单元格加换行"       # 表格单元格「数字组+末尾汉字」补 <br>
RULE_PAN_SPLIT = "排盘_多盘合表拆分"     # 6/9 行盘式合表拆回逐盘独立表
RULE_CHART_EXEMPT = "豁免图表块收纳"      # 跳过图表块合并（盘逐盘独立成块、进任务包）


def load_config() -> dict:
    """读取唯一起点配置文件 路径配置.json。"""
    cfg_path = PROJECT_ROOT / "路径配置.json"
    with open(cfg_path, "r", encoding="utf-8") as f:
        return json.load(f)


def get_path(name: str):
    """按配置节名取绝对路径（配置内为相对项目根路径，这里解析为 Path）。"""
    cfg = load_config()
    raw = cfg[name]
    if isinstance(raw, str):
        return (PROJECT_ROOT / raw).resolve()
    return raw


def get_thresholds() -> dict:
    """读取质量关卡阈值（路径配置.json 的 阈值 节）。"""
    cfg = load_config()
    return cfg.get("阈值", {})


def block_fingerprint(block_lines: list) -> str:
    """块指纹：块内首个有效正文行的前 12 字。

    低信息行（整行分词均以"柱"结尾的排盘表头，如 年柱月柱日柱时柱）跳过；
    全块皆低信息行时回退首非空行。A-04 建索引、A-06 预填任务包、B-01 现场定位
    三处统一调用本函数，保证指纹同源。
    """
    fallback = ""
    for line in block_lines:
        s = line.strip()
        if not s:
            continue
        if not fallback:
            fallback = s
        tokens = s.split()
        if tokens and all(t.endswith("柱") for t in tokens):
            continue
        return s[:12]
    return fallback[:12]


def parse_page_tag(tag: str) -> dict:
    """解析块标签为页码数值（设计文档 4.1 契约，消费方统一入口）。

    标签三态：
    - "134"       → {"起始": 134, "结束": 134, "降级": False}  （单页）
    - "134~136"   → {"起始": 134, "结束": 136, "降级": False}  （区间，起止均含）
    - "PDF.156" / "PDF.156~158" → 对应数值 + {"降级": True}    （诚实降级）
    - "排_009" 等历史脏值 → {"起始": None, "结束": None, "降级": True}（不抛异常）

    凡需要数值页码的消费方（范围拼接、自检断言、排序校验）一律经本函数，
    禁止各自再写正则。
    """
    if not isinstance(tag, str):
        return {"起始": None, "结束": None, "降级": True}
    t = tag.strip()
    degraded = False
    upper = t.upper()
    if upper.startswith("PDF."):
        degraded = True
        t = t[4:]
    parts = t.split("~", 1)
    try:
        start = int(parts[0])
        end = int(parts[1]) if len(parts) == 2 else start
    except (ValueError, TypeError):
        return {"起始": None, "结束": None, "降级": True}
    return {"起始": start, "结束": end, "降级": degraded}


def _selfcheck():
    """common 自检：验证 parse_page_tag 三态契约与脏值容错。"""
    cases = [
        ("134", {"起始": 134, "结束": 134, "降级": False}),
        ("134~136", {"起始": 134, "结束": 136, "降级": False}),
        ("PDF.156", {"起始": 156, "结束": 156, "降级": True}),
        ("PDF.156~158", {"起始": 156, "结束": 158, "降级": True}),
        (" 134 ", {"起始": 134, "结束": 134, "降级": False}),
        ("排_009", {"起始": None, "结束": None, "降级": True}),
        ("", {"起始": None, "结束": None, "降级": True}),
        (None, {"起始": None, "结束": None, "降级": True}),
        ("序", {"起始": None, "结束": None, "降级": True}),
    ]
    for tag, want in cases:
        got = parse_page_tag(tag)
        assert got == want, f"parse_page_tag({tag!r}) → {got}，应为 {want}"
    print(f"common 自检通过：parse_page_tag 三态契约 ×{len(cases)}")

    # #15 阶段③：书号＝纯数字自增 ID（旧缩写书号不参与取号）
    id_cases = [
        ([], 1),
        (["玄空01", "梁湘01"], 1),                      # 旧缩写全不参与 → 从 0001 起
        (["0001", "玄空01", "0003"], 4),                # 纯数字取最大 +1
        (["0007", "0002"], 8),
    ]
    for ids, want in id_cases:
        got = _next_book_num(ids)
        assert got == want, f"_next_book_num({ids!r}) → {got}，应为 {want}"
    assert f"{_next_book_num([]):04d}" == "0001", "书号须 4 位补零（0001）"
    print(f"common 自检通过：gen_book_id 纯数字取号 ×{len(id_cases)}")


def report_dir() -> Path:
    """报告目录（#14：项目根\\报告）。优先配置「报告」键，旧配置缺键时回落 输出\\报告。"""
    rel = load_config().get("报告")
    return (PROJECT_ROOT / rel).resolve() if rel else get_path("输出") / "报告"


def write_report(name: str, content: str) -> Path:
    """把内容写入 报告目录\\{name}，UTF-8，返回路径。
    #14：报告目录＝待办清单。内容含 🔴/🟡（非绿）时统一附销单指引行（AI 工单制）。"""
    rd = report_dir()
    rd.mkdir(parents=True, exist_ok=True)
    if ("🔴" in content or "🟡" in content) and "问题解决后请删除本文件" not in content:
        if not content.endswith("\n"):
            content += "\n"
        content += ("\n---\n本报告为待处理问题：问题解决后请删除本文件，"
                    "或重跑对应步骤验证为绿后由程序自动删除。\n")
    path = rd / name
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


def clear_report(name: str):
    """转绿清除：删除 报告目录\\{name} 旧报告（存在才删，异常吞掉）。"""
    p = report_dir() / name
    if p.exists():
        try:
            p.unlink()
        except Exception:
            pass


def is_selfcheck() -> bool:
    """判断是否带 --自检 参数。"""
    return "--自检" in sys.argv


def norm_rel(path_str: str) -> Path:
    """把项目内相对路径（如 知识库\\书籍\\xxx）解析为绝对路径。"""
    return (PROJECT_ROOT / path_str).resolve()


def normalize_for_search(line: str) -> str:
    """检索副本同款归一：简繁归一 + 去空白与标点（闸门①字符 diff 双侧同款处理）。

    先从 clean_split 移入公共层；风险①：先剥 HTML 标签，防止 `85<br>六`
    被归一化成垃圾检索词 `85br六`。"""
    import re
    try:
        from opencc import OpenCC
        cc = OpenCC("t2s")
        s = cc.convert(line)
    except ImportError:
        s = line
    s = re.sub(r"<[^>]+>", "", s)
    s = re.sub(r"[\s\u3000\u00a0]+", "", s)
    s = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", s)
    return s


def clean_book_title(filename: str) -> str:
    """从文件名提取书名：去扩展名、去编号前缀与页码后缀，取主要汉字部分。

    例：1--玄空风水--503.docx -> 玄空风水
        5763.梁湘润《命理传承班笔记》180页.pdf -> 梁湘润命理传承班笔记
    """
    import re
    name = filename
    for ext in (".docx", ".doc", ".pdf", ".txt", ".xlsx", ".xls"):
        if name.lower().endswith(ext):
            name = name[: -len(ext)]
            break
    name = name.replace("《", "").replace("》", "")
    han = re.findall(r"[\u4e00-\u9fff]+", name)
    title = "".join(han)
    if not title:
        title = name.replace(" ", "").strip()
    if not title:
        title = "未命名书籍"
    return title


def load_ingest_registry() -> list:
    """读 已入库\\_入库登记.json（入库原始文件 ↔ 书号映射，供删除时精确找回）。

    返回 [{文件名, 书名, 书号}]。文件缺失/损坏返回空列表。"""
    p = get_path("已入库") / "_入库登记.json"
    if p.exists():
        try:
            rows = json.loads(p.read_text(encoding="utf-8"))
            return rows if isinstance(rows, list) else []
        except Exception:
            return []
    return []


def save_ingest_registry(rows: list):
    """写 已入库\\_入库登记.json，丢弃空行。"""
    rows = [r for r in rows if r and r.get("文件名")]
    p = get_path("已入库") / "_入库登记.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")


def book_rule_path(book_title: str) -> Path:
    """按书特化规则文件路径：{书规则}\\{书名}.json（一本一文件，按书名识别）。"""
    return get_path("书规则") / f"{book_title}.json"


def book_config_path(book_title: str) -> Path:
    """书级配置文件路径：{知识库}\\书籍\\{书名}\\书配置.json。"""
    return get_path("知识库") / "书籍" / book_title / "书配置.json"


def load_book_config(book_title: str) -> dict:
    """读书级配置 书配置.json；缺失/损坏/无书名 → 空 dict。"""
    if not book_title:
        return {}
    p = book_config_path(book_title)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_book_config(book_title: str, config: dict):
    """写书级配置文件（合并更新，不覆盖已有字段）。"""
    p = book_config_path(book_title)
    p.parent.mkdir(parents=True, exist_ok=True)
    existing = load_book_config(book_title)
    existing.update(config)
    p.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")


def load_book_rules(book_title: str) -> dict:
    """读按书特化规则；缺失/损坏/无书名 → 空 dict（即所有特化开关默认关闭）。

    规则为数据驱动：主程序不含任何按书名分支，只按本 dict 里的开关行事。"""
    if not book_title:
        return {}
    p = book_rule_path(book_title)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_book_rules(book_title: str, rules: dict):
    """写按书特化规则文件（供控制台勾选使用）。"""
    p = book_rule_path(book_title)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(rules, ensure_ascii=False, indent=2), encoding="utf-8")


def _collect_book_ids() -> list:
    """收集库内现有书号字符串：书目总索引「书号」列为主，回退扫各书 倒排表.json。

    只做采集（含旧缩写书号，供调用方过滤）；缺失/损坏一律静默跳过。"""
    ids = []
    kb = get_path("知识库")
    catalog = kb / "书目总索引.md"
    if catalog.exists():
        col = None
        data_started = False
        for ln in catalog.read_text(encoding="utf-8").split("\n"):
            s = ln.strip()
            if not s.startswith("|"):
                continue
            cells = [c.strip() for c in s.strip("|").split("|")]
            if col is None:
                if "书号" in cells:
                    col = cells.index("书号")
                continue
            if not data_started:
                if all(set(c) <= set("-: ") for c in cells):
                    data_started = True
                continue
            if 0 <= col < len(cells):
                ids.append(cells[col])
    books_dir = kb / "书籍"
    if books_dir.exists():
        for d in books_dir.iterdir():
            if not d.is_dir():
                continue
            idx = d / "倒排表.json"
            if not idx.exists():
                continue
            try:
                bid = json.loads(idx.read_text(encoding="utf-8")).get("书号", "")
                if bid:
                    ids.append(str(bid))
            except Exception:
                pass
    return ids


def _next_book_num(book_ids: list) -> int:
    r"""从书号集合取下一个纯数字号：只统计 `^\d+$`（旧缩写书号不参与取号）。"""
    import re
    nums = [int(s.strip()) for s in book_ids
            if isinstance(s, str) and re.fullmatch(r"\d+", s.strip())]
    return (max(nums) + 1) if nums else 1


def gen_book_id(filename: str) -> str:
    """书号＝纯数字自增 ID（4 位补零，取库内最大值 +1，号不复用）。

    旧缩写书号（玄空01 等）不参与取号；面向人的显示层一律用书名全称，
    书号只在明确标注为「书号」的机器键位置出现（映射唯一源＝书目总索引）。
    参数 filename 保留以兼容调用点签名，不再参与取号。
    """
    return f"{_next_book_num(_collect_book_ids()):04d}"


def run_or_selfcheck(main_func, selfcheck_func):
    """脚本入口统一分发：--自检 走自检，否则走 main_func。"""
    if is_selfcheck():
        try:
            selfcheck_func()
        except SystemExit as e:
            raise
        except Exception as e:
            print(f"自检失败：{e}")
            sys.exit(1)
    else:
        main_func()


if __name__ == "__main__":
    if is_selfcheck():
        _selfcheck()
    else:
        print("common.py 为公共模块：请用 --自检 运行，或作为依赖被各脚本 import")
        sys.exit(0)
