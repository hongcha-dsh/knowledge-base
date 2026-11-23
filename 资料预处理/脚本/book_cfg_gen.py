# -*- coding: utf-8 -*-
"""卡 A-08 book_cfg_gen.py：#20 章节识别通用化·LLM 书级配置生成器（方案 F）。

角色＝配置生成器，非识别器：一次性把「结构信号清单」翻译成书级配置并**冻结落盘**为唯一
事实源；之后 build_index 只读配置、确定性消费，重跑不再调 LLM（保幂等）。

触发门槛（三者缺一不调）：
  ① 结构.json 存在且非空；
  ② 书配置.json 无「章级级别」（配置冻结 / 人工优先，永不被 LLM 覆盖）；
  ③ 自判（章级模式 build_index._match_chapter_title）过滤为空。

输入：结构.json 的「级别 + 标题」清单（去块ID，按级别分组、超量截断控 prompt 规模）。
LLM 返回契约：{"章级级别": int|null, "章级排除": [str], "章级补充": [str],
              "不确定": bool, "理由": str}。
硬校验：JSON 可解析；级别 ∈ 信号实际级别集合；排除/补充 ⊆ 信号标题集合；
        「不确定」=true 或级别为 null → 判失败。
成功：合并写入 书配置.json（含「章级配置来源」:"llm"），退出码 0，管线继续。
降级：未配置标注 AI / 调用失败 / 硬校验不过 → **不阻塞**，写报告黄灯 + 退出码 2，
      提示人工把「章级级别」手工填入 书配置.json（人工配置为最终兜底）。

红线：不碰原文层；不产 AI 生成内容进索引（LLM 产出只进书级配置）；路径经 common.get_path()；
      日志/报告不打印完整 API Key（llm_client 已脱敏，本脚本不回显）。
"""

import json
import re
import sys
from datetime import date
from pathlib import Path

import build_index
import common
import llm_client

MAX_PER_LEVEL = 40
MAX_TOTAL = 200
# LLM 输出预算：所配模型可能是**推理模型**（先产 reasoning_content 思考链再给正文），预算过小会被
# 思考链吃光、导致 content 为空且 finish_reason=length（实测 deepseek-flash 本任务需约 6.5k 思考 token）。
LLM_MAX_TOKENS = 16000
CFG_FILE = "书配置.json"
STRUCT_FILE = "结构.json"


def _snippet(text: str, n: int = 120) -> str:
    """报告用原始返回片段：压缩空白、截断，便于从黄灯报告复盘 LLM 到底回了什么。"""
    s = re.sub(r"\s+", " ", (text or "").strip())
    return s[:n] + ("…" if len(s) > n else "")

_SYSTEM_PROMPT = """你是「书籍章节结构配置」生成器。用户会给你一本书的「结构信号」清单\
（每条含级别与标题，来源为 OCR/解析器的标题块识别结果），需要你判断其中哪一个「级别」\
代表该书的**章**级标题，并给出需要剔除/补充的边角条目。

只输出一个 JSON 对象，不要输出任何解释文字、不要使用代码围栏。字段：
- "章级级别"：整数，必须是清单中出现过的级别；无法判断时输出 null。
- "章级排除"：字符串数组，该级别中其实是正文句/编号列表项/图题等非章条目，需剔除；
  标题须与清单**完全一致**；无则空数组。
- "章级补充"：字符串数组，不属于该级别但确为章条的标题，需补入；标题须与清单完全一致；
  无则空数组。
- "不确定"：布尔，无法判断章级时置 true。
- "理由"：字符串，一句话说明判断依据。

判断线索：级别越小越接近「章/部分」；书可能不套「第X章」命名法，此时凭标题语义、数量与\
在清单中的分布判断；正文句、编号列表项、图题会混入信号，注意剔除。"""


def _report_name(book_title: str) -> str:
    return f"{book_title}_书配置.md"


def _clear_report(book_title: str):
    """清理上一次运行留下的报告，避免陈旧黄灯误报（同书同名覆盖语义）。
    #14：统一走 common.clear_report（转绿清除公共实现）。"""
    common.clear_report(_report_name(book_title))


def _degrade(book_title: str, reason: str) -> tuple:
    """降级：写黄灯报告、不阻塞管线，退出码 2。"""
    report = (
        f"# 书级配置生成报告：《{book_title}》\n"
        f"结论：🟡 未自动生成（只警告不阻塞）\n\n"
        f"⚠️ 黄灯（只告警不阻塞，退出码 2）：\n"
        f"- 章节设置未能自动生成：{reason}。不影响入库，已按通用规则继续；"
        f"如需精调可手工填写后到书库页「重建索引」。\n"
        f"- 处置：已按通用规则继续（不阻塞）；如需精调章节识别，可手工把「章级级别」填入 "
        f"知识库\\书籍\\{book_title}\\{CFG_FILE} 后到书库页「重建索引」"
        f"（手工填写的内容优先，不会被自动生成的覆盖）\n")
    common.write_report(_report_name(book_title), report)
    print(f"🟡 章节设置未自动生成：{reason}")
    return 2, reason


def build_material(entries: list) -> tuple:
    """结构.json 条目 → (分组清单文本, 信号级别集合, 标题归一映射, 信号总数, 已列条数)。

    归一映射以「去空白」为键 → 结构.json 原始（净化后）标题，供校验时把 LLM 返回标题
    规范回结构.json 实际标题（容忍空格差异）；超量按级别与总量截断。
    """
    groups = {}
    title_map = {}
    total = 0
    for e in entries:
        if not isinstance(e, dict):
            continue
        title = build_index._clean_signal_title(e.get("标题", ""))
        if not title:
            continue
        total += 1
        title_map.setdefault(re.sub(r"\s+", "", title), title)
        groups.setdefault(e.get("级别"), [])
        if title not in groups[e.get("级别")]:
            groups[e.get("级别")].append(title)
    levels = set(groups.keys())
    lines = []
    shown = 0
    for lv in sorted(groups, key=lambda x: (x is None, x)):
        titles = groups[lv]
        cut = titles[:MAX_PER_LEVEL]
        if shown + len(cut) > MAX_TOTAL:
            cut = cut[: max(0, MAX_TOTAL - shown)]
        shown += len(cut)
        lines.append(f"[级别 {lv}]（清单 {len(titles)} 条，此处列 {len(cut)} 条）")
        lines.extend(f"- {t}" for t in cut)
    return "\n".join(lines), levels, title_map, total, shown


def _parse_llm_json(text: str) -> dict:
    """剥离代码围栏并解析出最外层 JSON 对象；失败抛 ValueError。"""
    s = (text or "").strip()
    s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
    s = re.sub(r"```\s*$", "", s).strip()
    m = re.search(r"\{.*\}", s, re.S)
    if not m:
        raise ValueError("未找到 JSON 对象")
    obj = json.loads(m.group(0))
    if not isinstance(obj, dict):
        raise ValueError("返回不是 JSON 对象")
    return obj


def _canon_title(t: str, title_map: dict):
    """把 LLM 返回标题规范回结构.json 实际标题（去空白匹配）；无法匹配返回 None。"""
    if not isinstance(t, str):
        return None
    return title_map.get(re.sub(r"\s+", "", t.strip()))


def validate_config(obj: dict, levels: set, title_map: dict) -> tuple:
    """硬校验 LLM 产出 → (规范化配置 dict 或 None, 失败原因)。"""
    if obj.get("不确定") is True:
        return None, "LLM 自报不确定"
    lv = obj.get("章级级别")
    if lv is None:
        return None, "未给出「章级级别」（null）"
    if isinstance(lv, bool) or not isinstance(lv, int):
        return None, f"「章级级别」非整数：{lv!r}"
    if lv not in levels:
        return None, f"「章级级别」{lv} 不在信号级别集合 {sorted(levels, key=lambda x: (x is None, x))}"
    conf = {"章级级别": lv}
    for key, out_key in (("章级排除", "章级排除"), ("章级补充", "章级补充")):
        raw = obj.get(key) or []
        if not isinstance(raw, list):
            return None, f"「{key}」非数组"
        canon = []
        for t in raw:
            c = _canon_title(t, title_map)
            if c is None:
                return None, f"「{key}」含非信号标题：{t!r}"
            if c not in canon:
                canon.append(c)
        conf[out_key] = canon
    return conf, ""


def _load_entries(book_dir: Path) -> list:
    return build_index._load_structure(book_dir)


def _load_existing_cfg(book_dir: Path) -> dict:
    return build_index._load_book_cfg(book_dir)


def generate(book_id: str, book_title: str, book_dir: Path = None) -> tuple:
    """运行书级配置生成。返回 (退出码, 说明)；退出码 0=无需处理/成功，2=降级。"""
    if book_dir is None:
        book_dir = common.get_path("知识库") / "书籍" / book_title
    entries = _load_entries(book_dir)
    if not entries:
        _clear_report(book_title)
        print("无结构信号（结构.json 缺失/为空）：不纳入 LLM 配置生成范围，走启发式兜底，跳过")
        return 0, ""
    existing = _load_existing_cfg(book_dir)
    if existing.get("章级级别") is not None:
        _clear_report(book_title)
        src = existing.get("章级配置来源", "人工")
        print(f"书配置.json 已有「章级级别」={existing.get('章级级别')}（来源：{src}）：跳过 LLM（配置冻结）")
        return 0, ""
    if not build_index.signal_selfjudge_empty(book_dir):
        _clear_report(book_title)
        print("自判（章级模式）已命中候选：无需 LLM 配置，跳过")
        return 0, ""

    if not llm_client.is_configured():
        return _degrade(book_title, "标注 AI 未配置（设置页「标注 AI 配置」或环境变量 "
                                    "ANNOTATE_API_KEY）：回落启发式")

    material, levels, title_map, total, shown = build_material(entries)
    if not levels or not title_map:
        return _degrade(book_title, "结构.json 无可用的「级别+标题」决策材料")
    note = "" if shown >= total else f"\n注：信号量较大已截断，共 {total} 条、此处列 {shown} 条。"
    user = (f"书籍：《{book_title}》\n"
            f"结构信号（按级别分组）：\n{material}{note}\n\n请只输出 JSON。")
    try:
        out = llm_client.chat(_SYSTEM_PROMPT, user, max_tokens=LLM_MAX_TOKENS, temperature=0.1)
    except RuntimeError as e:
        return _degrade(book_title, f"LLM 调用失败：{e}")
    try:
        obj = _parse_llm_json(out)
    except Exception as e:
        return _degrade(book_title,
                        f"LLM 返回非合法 JSON：{e}（原始返回前 120 字：{_snippet(out)}）")
    conf, err = validate_config(obj, levels, title_map)
    if conf is None:
        return _degrade(book_title,
                        f"硬校验不过：{err}（LLM 原始 JSON：{_snippet(json.dumps(obj, ensure_ascii=False))}）")
    if not build_index._select_signal_starts(
            entries, conf["章级级别"], conf["章级排除"], conf["章级补充"]):
        return _degrade(book_title, "按 LLM 配置过滤后无候选（配置不可用）")

    merged = dict(existing)
    merged.update(conf)
    merged["章级配置来源"] = "llm"
    merged["章级配置日期"] = date.today().isoformat()
    book_dir.mkdir(parents=True, exist_ok=True)
    (book_dir / CFG_FILE).write_text(
        json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    _clear_report(book_title)
    print(f"✅ LLM 书级配置已写入 {CFG_FILE}：章级级别={conf['章级级别']}，"
          f"排除 {len(conf['章级排除'])} 条，补充 {len(conf['章级补充'])} 条（来源：llm）")
    return 0, ""


def main():
    args = sys.argv[1:]
    book_id = book_title = None
    for i, a in enumerate(args):
        if a == "--书号" and i + 1 < len(args):
            book_id = args[i + 1]
        elif a == "--书名" and i + 1 < len(args):
            book_title = args[i + 1]
    if not book_id or not book_title:
        print("用法：python book_cfg_gen.py --书号 <书号> --书名 <书名>")
        sys.exit(1)
    try:
        code, _ = generate(book_id, book_title)
    except Exception as e:
        code, _ = _degrade(book_title, f"生成过程异常：{e}")
    sys.exit(code)


def _selfcheck():
    """离线断言（不联网）：触发门槛判定、硬校验、标题规范化、截断策略。"""
    import shutil
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="book_cfg_gen_selfcheck_"))
    book_dir = tmp / "书籍" / "自检书"
    book_dir.mkdir(parents=True)
    ok = True
    try:
        # ① 无结构.json → 不纳入范围（退出码 0，不产配置）
        code, _ = generate("自检01", "自检书", book_dir)
        if code != 0 or (book_dir / CFG_FILE).exists():
            print("  ❌ 无结构信号时未正确跳过")
            ok = False
        # ② 自判命中（含「第X章」）→ 跳过，不产配置
        (book_dir / STRUCT_FILE).write_text(json.dumps([
            {"级别": 2, "标题": "第一章 玄空要旨", "块ID": "自检01·0001", "匹配方式": "文本+顺序"},
            {"级别": 1, "标题": "上部", "块ID": "自检01·0002", "匹配方式": "文本+顺序"},
        ], ensure_ascii=False, indent=2), encoding="utf-8")
        code, _ = generate("自检01", "自检书", book_dir)
        if code != 0 or (book_dir / CFG_FILE).exists():
            print("  ❌ 自判命中时未正确跳过")
            ok = False
        # ③ 自判全挡 + 已有「章级级别」→ 幂等跳过（不联网）
        (book_dir / STRUCT_FILE).write_text(json.dumps([
            {"级别": 1, "标题": "玩家行动轮", "块ID": "自检01·0001", "匹配方式": "文本+顺序"},
            {"级别": 1, "标题": "伤害与治疗", "块ID": "自检01·0002", "匹配方式": "文本+顺序"},
        ], ensure_ascii=False, indent=2), encoding="utf-8")
        (book_dir / CFG_FILE).write_text(json.dumps(
            {"章级级别": 1, "章级配置来源": "人工"}, ensure_ascii=False, indent=2), encoding="utf-8")
        code, _ = generate("自检01", "自检书", book_dir)
        cfg_after = json.loads((book_dir / CFG_FILE).read_text(encoding="utf-8"))
        if code != 0 or cfg_after.get("章级配置来源") != "人工":
            print("  ❌ 已有配置未正确幂等跳过（人工配置被覆盖？）")
            ok = False
        # ④ 决策材料：分组 + 截断 + 标题归一映射
        entries = [
            {"级别": 1, "标题": "第一章 玄空要旨", "块ID": "自检01·0001"},
            {"级别": 1, "标题": "第一章 玄空要旨", "块ID": "自检01·0002"},
            {"级别": 2, "标题": "排龙诀", "块ID": "自检01·0003"},
        ]
        material, levels, title_map, total, shown = build_material(entries)
        if levels != {1, 2} or total != 3 or "第一章 玄空要旨" not in material:
            print(f"  ❌ 决策材料分组异常：levels={levels} total={total}")
            ok = False
        if _canon_title("第一章玄空要旨", title_map) != "第一章 玄空要旨":
            print("  ❌ 标题规范化（容忍空格差异）未生效")
            ok = False
        # ⑤ 硬校验：合法 / 级别越界 / 例外含非信号标题 / 不确定 / 级别 null
        good, err = validate_config(
            {"章级级别": 1, "章级排除": ["排龙 诀"], "章级补充": [], "不确定": False}, levels, title_map)
        if good is None or good["章级排除"] != ["排龙诀"]:
            print(f"  ❌ 合法配置被误拒或标题未规范化：{err} / {good}")
            ok = False
        if validate_config({"章级级别": 9}, levels, title_map)[0] is not None:
            print("  ❌ 级别越界未被拒")
            ok = False
        if validate_config({"章级级别": 1, "章级排除": ["不存在的标题"]}, levels, title_map)[0] is not None:
            print("  ❌ 例外清单含非信号标题未被拒")
            ok = False
        if validate_config({"章级级别": 1, "不确定": True}, levels, title_map)[0] is not None:
            print("  ❌ 不确定=true 未被拒")
            ok = False
        if validate_config({"章级级别": None}, levels, title_map)[0] is not None:
            print("  ❌ 级别 null 未被拒")
            ok = False
        # ⑥ JSON 提取：代码围栏与非 JSON 文本
        if _parse_llm_json('```json\n{"章级级别": 1}\n```') != {"章级级别": 1}:
            print("  ❌ 代码围栏剥离失败")
            ok = False
        try:
            _parse_llm_json("抱歉，我无法判断")
            print("  ❌ 非 JSON 文本未被拒")
            ok = False
        except ValueError:
            pass
        # ⑦ LLM 路径全流程（离线注入桩，不联网）：未配置 / 校验不过 → 降级（退出码 2 + 黄灯报告）；
        #    合法产出 → 写配置（退出码 0）
        (book_dir / CFG_FILE).unlink()
        _saved_chat = llm_client.chat
        _saved_conf = llm_client.is_configured
        try:
            llm_client.is_configured = lambda: False
            code, _ = generate("自检01", "自检书", book_dir)
            if code != 2 or (book_dir / CFG_FILE).exists():
                print("  ❌ 未配置标注 AI 时未正确降级")
                ok = False
            llm_client.is_configured = lambda: True
            llm_client.chat = lambda *a, **k: "这不是 JSON"
            code, _ = generate("自检01", "自检书", book_dir)
            rp = common.report_dir() / "自检书_书配置.md"
            if code != 2 or (book_dir / CFG_FILE).exists():
                print("  ❌ 产出非 JSON 时未正确降级")
                ok = False
            elif not rp.exists() or "原始返回前 120 字" not in rp.read_text(encoding="utf-8"):
                print("  ❌ 非 JSON 降级报告未回写 LLM 原始返回片段（不可复盘）")
                ok = False
            # 推理模型思考链吃满 max_tokens → content 为空：必须走降级而非静默成功
            def _raise_trunc(*a, **k):
                raise RuntimeError("标注 AI 输出被长度截断（finish_reason=length）且正文为空")
            llm_client.chat = _raise_trunc
            code, _ = generate("自检01", "自检书", book_dir)
            if code != 2 or (book_dir / CFG_FILE).exists():
                print("  ❌ 空正文/截断时未正确降级")
                ok = False
            llm_client.chat = lambda *a, **k: '{"章级级别": 1, "章级排除": [], "章级补充": []}'
            code, _ = generate("自检01", "自检书", book_dir)
            cfg_llm = json.loads((book_dir / CFG_FILE).read_text(encoding="utf-8"))
            if code != 0 or cfg_llm.get("章级级别") != 1 or cfg_llm.get("章级配置来源") != "llm":
                print(f"  ❌ 合法产出未写入配置：{cfg_llm}")
                ok = False
        finally:
            llm_client.chat = _saved_chat
            llm_client.is_configured = _saved_conf
        print(f"  book_cfg_gen 自检：门槛/校验/规范化/截断/降级全流程 全部断言完成")
        print(f"  {'✅ 通过' if ok else '❌ 失败'}")
        sys.exit(0 if ok else 1)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        _clear_report("自检书")


if __name__ == "__main__":
    if common.is_selfcheck():
        _selfcheck()
    else:
        common.run_with_write_lock(main, "书配置")
