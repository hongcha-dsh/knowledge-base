# -*- coding: utf-8 -*-
r"""段级 diff 引擎（#21 M1 · 方案丁·块出生证制 · 对齐粒度=段）。

粒度演进（2026-09-16 M1 实测定案，原设计 §四 契约为块级，实测发现聚合噪声后
经用户确认下沉为段级）：块是「≤1500 字的段聚合包」——在普通段流中插入/删除
内容会改变聚合边界，下游未变块被误报修改/退休（3 处注入误报 7 处变化）。
**聚合是打包策略，不是内容身份**；段（自然段/原子段）才是稳定单元。
方案丁的手术语义（§3.2「保留块原文原样搬运」）本就意味着保留块不重新聚合，
段级 diff 与之一致。

四分类（段级对齐 → 块级聚合）：
  保留块：块内全部段内容一致（顺序不变）——块 ID 不动、标注 100% 存活；
  修改块：块内部分段被改/增/删——老 ID 退休，变化区域重组新块领号；
  退休块：块内全部段被删——老 ID 显式失效；
  重组新块：修改块的新段 + 新生段，按 clean_split 同款聚合规则（1500 字 +
           原子段独立）在「保留块之间的间隙」重组——预演领号对象。

段等价判定＝段内检索副本行拼接文本完全一致（天然免疫 wrap 断行差异）；
段对齐键＝段首 12 字（与块指纹同思想）；修改段配对＝difflib ratio ≥ 阈值。
纯函数、零副作用、零第三方依赖，可独立自检。
设计依据：项目文档\未来模块规划\2026-09-16-#21-书籍更新功能-design.md §3.2/§四/§七。
"""

import sys
from difflib import SequenceMatcher

import common

# 修改段配对相似度阈值（设计 §十一.3：初拟 0.6，M1 沙箱调参后定）
DEFAULT_THRESHOLD = 0.6

# 聚合规则常量（与 clean_split.py 同款；独立定义避免 import 管线模块）
MAX_BLOCK_CHARS = 1500


def _similarity(a: str, b: str) -> float:
    """两段文本的相似度：difflib ratio，0~1。"""
    if not a and not b:
        return 1.0
    return SequenceMatcher(None, a, b, autojunk=False).ratio()


def _is_atomic(text: str) -> bool:
    """原子段判定：md 表格 / 代码块（与 clean_split._is_atomic_paragraph 同口径）。"""
    s = text.lstrip()
    return s.startswith("```text") or s.startswith("| ")


def expand_paras(blocks: list, *, new_mode: bool = False) -> list:
    """块序列 → 段流（物理序）。

    入参块结构（load 老块/新块的统一形态，正文=块内物理行，空行=段界）：
      老块 {"块ID", "原文行": [...], "副本行": [...]} 或
      新块 {"序号", "原文行": [...], "副本行": [...]}
    出参段结构：
      {"块ID"或"新块序号", "原文文本"(段内原文行拼接，含\n), "比对文本"(段内副本行拼接),
       "原子": bool, "页标签": 块页标签（手术新块推导块标签用，M3）}
    """
    paras = []
    for b in blocks:
        orig_lines = b.get("原文行", [])
        copy_lines = b.get("副本行", [])
        cur_orig, cur_copy = [], []
        for ol, cl in zip(orig_lines, copy_lines):
            if not ol.strip() and not cl.strip():  # 空行 = 段界
                if cur_orig:
                    paras.append(_mk_para(b, cur_orig, cur_copy, new_mode))
                    cur_orig, cur_copy = [], []
                continue
            cur_orig.append(ol)
            cur_copy.append(cl)
        if cur_orig:
            paras.append(_mk_para(b, cur_orig, cur_copy, new_mode))
    return paras


def _mk_para(block: dict, orig: list, copy: list, new_mode: bool) -> dict:
    orig_text = "\n".join(orig)
    return {
        "块ID" if not new_mode else "新块序号": block["块ID"] if not new_mode else block["序号"],
        "原文文本": orig_text,
        "比对文本": "".join(copy),
        "原子": _is_atomic(orig_text),
        "页标签": block.get("页标签", ""),
    }


def diff_paragraphs(old_paras: list, new_paras: list,
                    threshold: float = DEFAULT_THRESHOLD) -> dict:
    """段级四分类对齐。

    返回 {
      "段保留": [(老块ID, 新段序号)],                  # 内容一致（新段序号=1 基物理序）
      "段修改": [(老块ID, 新段序号, 相似度)],           # 对齐上但内容变（一退一新）
      "段删除": [(老块ID, 原文文本)],
      "段插入": [(新段序号, 原文文本, 原子)],
    }
    """
    old_keys = [p["比对文本"][:12] for p in old_paras]
    new_keys = [p["比对文本"][:12] for p in new_paras]
    sm = SequenceMatcher(None, old_keys, new_keys, autojunk=False)

    keep, mods, dels, ins = [], [], [], []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            # 段首 12 字一致只是对齐证据，保留必须段文本逐字一致
            for i, j in zip(range(i1, i2), range(j1, j2)):
                if old_paras[i]["比对文本"] == new_paras[j]["比对文本"]:
                    keep.append((old_paras[i]["块ID"], j + 1))
                else:
                    sim = round(_similarity(old_paras[i]["比对文本"],
                                            new_paras[j]["比对文本"]), 3)
                    mods.append((old_paras[i]["块ID"], j + 1, sim))
        elif tag == "delete":
            for i in range(i1, i2):
                dels.append((old_paras[i]["块ID"], old_paras[i]["原文文本"]))
        elif tag == "insert":
            for j in range(j1, j2):
                ins.append((j + 1, new_paras[j]["原文文本"], new_paras[j]["原子"]))
        else:  # replace：段内相似度配对，≥ 阈值成「段修改」，其余拆 删除+插入
            pairs = []
            for i in range(i1, i2):
                for j in range(j1, j2):
                    sim = _similarity(old_paras[i]["比对文本"], new_paras[j]["比对文本"])
                    pairs.append((sim, i, j))
            pairs.sort(key=lambda t: -t[0])
            old_t, new_t = set(), set()
            for sim, i, j in pairs:
                if i in old_t or j in new_t:
                    continue
                if sim < threshold:
                    break
                mods.append((old_paras[i]["块ID"], j + 1, round(sim, 3)))
                old_t.add(i)
                new_t.add(j)
            for i in range(i1, i2):
                if i not in old_t:
                    dels.append((old_paras[i]["块ID"], old_paras[i]["原文文本"]))
            for j in range(j1, j2):
                if j not in new_t:
                    ins.append((j + 1, new_paras[j]["原文文本"], new_paras[j]["原子"]))
    return {"段保留": keep, "段修改": mods, "段删除": dels, "段插入": ins}


def _regroup(segs: list) -> list:
    """段序列按 clean_split 聚合规则重组为块（预演手术的重组行为）。

    segs: [{"原文文本", "原子"}, ...]（物理序）；返回 [ [段...] ]（每块=段列表）。
    规则：原子段独立成块；普通段累计 ≤MAX_BLOCK_CHARS 聚合（含 \n 计长，同 clean_split）。
    """
    groups, cur, chars = [], [], 0
    for seg in segs:
        if seg["原子"]:
            if cur:
                groups.append(cur)
                cur, chars = [], 0
            groups.append([seg])
            continue
        if cur and chars + len(seg["原文文本"]) > MAX_BLOCK_CHARS:
            groups.append(cur)
            cur, chars = [], 0
        cur.append(seg)
        chars += len(seg["原文文本"])
    if cur:
        groups.append(cur)
    return groups


def _classify(old_blocks: list, old_paras: list, new_paras: list, seg_res: dict) -> dict:
    """段级结果 → 块级分类（报告与手术计划共用的唯一分类源）。

    M3 精化（手术正确性要求，聚合噪声在手术面的延伸）：
    - 保留块追加「段连续性」条件：块内段在新段流中的跨度内**无重组材料插入**；
      有新段插入块跨度中央 → 老块无法原样搬运（否则物理序错乱），打散为修改块
      （老 ID 退休、存活段进重组领号，退休登记册原因=修改）；
    - 重组材料补全：修改块（含打散块）的**存活段**同样是重组材料
      （M1 预览只收修改新段+插入段，会丢修改块的存活段——手术级必须全收）。
    """
    keep_by_block = {}   # 块ID → [新段序号]（段保留）
    for bid, j in seg_res["段保留"]:
        keep_by_block.setdefault(bid, []).append(j)
    mod_by_block = {}    # 块ID → [(新段序号, 相似度)]（段修改）
    for bid, j, sim in seg_res["段修改"]:
        mod_by_block.setdefault(bid, []).append((j, sim))
    del_by_block = {}    # 块ID → [段文本]（段删除）
    for bid, text in seg_res["段删除"]:
        del_by_block.setdefault(bid, []).append(text)
    insert_nos = {j for j, _, _ in seg_res["段插入"]}
    old_by_block = {}    # 块ID → [老段]
    for p in old_paras:
        old_by_block.setdefault(p["块ID"], []).append(p)

    keep_blocks, mod_blocks, retired_blocks = [], [], []
    for b in old_blocks:
        bid = b["块ID"]
        if bid not in mod_by_block and bid not in del_by_block:
            js = keep_by_block.get(bid, [])
            split = any(min(js) < x < max(js) for x in insert_nos) if js else False
            if js and not split:
                keep_blocks.append(bid)
                continue
            if js and split:
                # 段全保留但被插入段打散：老块无法原样搬运 → 打散为修改块（相似度 1.0）
                mod_blocks.append((bid, 1.0))
                continue
            retired_blocks.append(bid)  # 无任何段（病态兜底，正常管线不会发生）
            continue
        if bid in keep_by_block or bid in mod_by_block:
            # 修改块：老块全文 vs 新侧（存活段+修改新段）的相似度
            old_all = "".join(p["比对文本"] for p in old_by_block.get(bid, []))
            new_side = "".join(
                new_paras[j - 1]["比对文本"]
                for j in keep_by_block.get(bid, []) + [j for j, _ in mod_by_block.get(bid, [])])
            sim = round(_similarity(old_all, new_side), 3) if new_side else 0.0
            mod_blocks.append((bid, sim))
        else:
            retired_blocks.append(bid)

    pure_keep_nos = {j for bid in keep_blocks for j in keep_by_block.get(bid, [])}
    return {
        "保留": keep_blocks, "修改块": mod_blocks, "退休块": retired_blocks,
        "纯保留段": pure_keep_nos, "段保留归块": keep_by_block,
    }


def aggregate_to_blocks(old_blocks: list, old_paras: list, new_paras: list,
                        seg_res: dict) -> dict:
    """段级结果 → 块级四分类 + 重组新块预演。

    - 保留块：块内全部段保留（无删/改）且段跨度内无插入（M3 精化）——块 ID 不动
    - 修改块：有段删/改/被打散但仍有存活段——老 ID 退休（原因=修改），存活段+新段进重组
    - 退休块：全部段被删——老 ID 显式失效（原因=删除）
    - 重组新块：新段流中纯保留块锚点之间的间隙段（插入段 + 修改块新段 + 修改块存活段），
      按 _regroup 聚合 → 预演新块（领号对象）

    返回 {
      "保留": [块ID...],
      "修改块": [(块ID, 相似度)],          # 相似度=老块全文 vs 新侧（存活段+修改段）拼接
      "退休块": [块ID...],
      "新块预演": [ {"段": [原文文本...], "来源": str} ],
      "统计": {老块数, 新块数, 保留, 修改, 退休, 新块},
    }
    """
    cls = _classify(old_blocks, old_paras, new_paras, seg_res)
    pure_keep_nos = cls["纯保留段"]
    mod_new_nos = {j for _, j, _ in seg_res["段修改"]}

    # 重组材料＝新段流中一切非纯保留段（M3 补全：修改块存活段也在内，防内容丢失）
    groups_out, gap = [], []
    for j, p in enumerate(new_paras, start=1):
        if j in pure_keep_nos:
            if gap:
                groups_out.extend(_regroup(gap))
                gap = []
            continue
        gap.append({"原文文本": p["原文文本"], "原子": p["原子"],
                    "来源": "修改块" if j in mod_new_nos else "新增内容"})
    if gap:
        groups_out.extend(_regroup(gap))

    new_blocks = []
    for g in groups_out:
        srcs = {s.get("来源", "新增内容") for s in g}
        new_blocks.append({
            "段": [s["原文文本"] for s in g],
            "来源": "+".join(sorted(srcs)),
        })

    return {
        "保留": cls["保留"],
        "修改块": cls["修改块"],
        "退休块": cls["退休块"],
        "新块预演": new_blocks,
        "统计": {
            "老块数": len(old_blocks),
            "新块数": len(cls["保留"]) + len(new_blocks),
            "保留": len(cls["保留"]),
            "修改": len(cls["修改块"]),
            "退休": len(cls["退休块"]),
            "新块": len(new_blocks),
        },
    }


def build_surgery_plan(old_blocks: list, old_paras: list, new_paras: list,
                       seg_res: dict) -> list:
    """手术装配计划（M3）：新 原文.md 的有序块序列。

    返回 [{"类型": "保留", "块ID": bid} / {"类型": "新块", "段": [段...]} ...]
    ——保留块原样搬运（调用方取老块原文切片）；新块段带 新段序号/页标签（领号与
    块标签推导用）。顺序＝新段流物理序（保留块在其首段位置整块插入）。
    """
    cls = _classify(old_blocks, old_paras, new_paras, seg_res)
    keep_first = {}   # 保留块首段位置 → 块ID
    keep_nos = {}     # 保留块段位置 → 块ID
    for bid in cls["保留"]:
        js = cls["段保留归块"].get(bid, [])
        if js:
            keep_first[min(js)] = bid
            for j in js:
                keep_nos[j] = bid

    plan, gap = [], []

    def _flush():
        nonlocal gap
        if gap:
            for g in _regroup(gap):
                plan.append({"类型": "新块", "段": g})
            gap = []

    for j, p in enumerate(new_paras, start=1):
        if j in keep_nos:
            _flush()
            if j in keep_first:
                plan.append({"类型": "保留", "块ID": keep_first[j]})
            continue
        gap.append({"原文文本": p["原文文本"], "原子": p["原子"],
                    "页标签": p["页标签"], "新段序号": j})
    _flush()
    return plan


def assign_new_ids(old_blocks: list, result: dict) -> list:
    """预演领号：重组新块按物理顺序从该书当前最大序号+1 起连续领号。

    返回 [(预演序, 预演块ID)]。
    """
    max_no = 0
    for b in old_blocks:
        parts = b["块ID"].split("·")
        if len(parts) == 2 and parts[1].isdigit():
            max_no = max(max_no, int(parts[1]))
    book_id = old_blocks[0]["块ID"].split("·")[0] if old_blocks else ""
    out = []
    for i in range(len(result["新块预演"])):
        max_no += 1
        out.append((i + 1, f"{book_id}·{max_no:04d}"))
    return out


def _mk_block(bid, orig_paras, copy_paras, new_mode=False):
    """构造测试块：orig_paras=[段原文文本]（段内 \n 表示多行），copy 同构。

    new_mode=True 时 bid 为新块序号（int），块结构用「序号」键。"""
    orig_lines, copy_lines = [], []
    for i, (o, c) in enumerate(zip(orig_paras, copy_paras)):
        if i:
            orig_lines.append("")
            copy_lines.append("")
        orig_lines.extend(o.split("\n"))
        copy_lines.extend(c.split("\n"))
    if new_mode:
        return {"序号": bid, "原文行": orig_lines, "副本行": copy_lines}
    return {"块ID": bid, "原文行": orig_lines, "副本行": copy_lines}


def _selfcheck():
    """自检：段级四分类全路径 + 块级聚合 + 重组预演 + 领号。"""
    # 造三块老书：块1 两段、块2 一段、块3 一段（块2 用独立小块）
    o1a, o1b = "玄空之学首重理气，阴阳五行运行于天地之间随方位而变", "挨星之法以九星配九宫察其生旺衰死"
    o2 = "排龙之法以水口为主顺逆相推定其星曜"
    o3 = "三元九运每运二十年上元一二三四运中元五六七运"
    old = [
        _mk_block("0003·0001", [o1a, o1b], [o1a, o1b]),
        _mk_block("0003·0002", [o2], [o2]),
        _mk_block("0003·0003", [o3], [o3]),
    ]
    old_paras = expand_paras(old)
    assert len(old_paras) == 4, len(old_paras)
    assert old_paras[0]["块ID"] == "0003·0001" and old_paras[0]["原子"] is False

    # ① 全保留：同段流重切（新块用「序号」键，包装 _mk_new）
    def _mk_new(no, o, c):
        return _mk_block(no, o, c, new_mode=True)

    new = [_mk_new(1, [o1a, o1b, o2, o3], [o1a, o1b, o2, o3])]  # 全并成一块也无所谓：段级
    new_paras = expand_paras(new, new_mode=True)
    sr = diff_paragraphs(old_paras, new_paras)
    assert len(sr["段保留"]) == 4 and not sr["段修改"] and not sr["段删除"] and not sr["段插入"]
    ar = aggregate_to_blocks(old, old_paras, new_paras, sr)
    assert ar["统计"]["保留"] == 3 and ar["统计"]["修改"] == 0 and ar["统计"]["新块"] == 0
    print("  ① 全保留：4 段 4 中，3 块全保留（重切块边界无关）")

    # ② 删块2 的段 → 块2 退休（全段删除）
    new2 = [_mk_new(1, [o1a, o1b, o3], [o1a, o1b, o3])]
    np2 = expand_paras(new2, new_mode=True)
    sr = diff_paragraphs(old_paras, np2)
    assert len(sr["段删除"]) == 1 and sr["段删除"][0][0] == "0003·0002"
    ar = aggregate_to_blocks(old, old_paras, np2, sr)
    assert ar["统计"]["保留"] == 2 and ar["退休块"] == ["0003·0002"] and ar["统计"]["新块"] == 0
    print("  ② 删段：块2 退休（老 ID 显式失效），保留 2")

    # ③ 块1 后插新段 → 新生（独立重组块）
    ins = "全新增补的章节内容段落与库内任何内容都不相同"
    new3 = [_mk_new(1, [o1a, o1b, ins, o2, o3], [o1a, o1b, ins, o2, o3])]
    np3 = expand_paras(new3, new_mode=True)
    sr = diff_paragraphs(old_paras, np3)
    assert len(sr["段插入"]) == 1 and sr["段插入"][0][1] == ins
    ar = aggregate_to_blocks(old, old_paras, np3, sr)
    assert ar["统计"]["保留"] == 3 and ar["统计"]["新块"] == 1
    assert ar["新块预演"][0]["段"] == [ins]
    print("  ③ 插段：3 块全保留 + 重组新块 1（含插入段）")

    # ④ 改块2 的段（equal 路径：段首同、正文异）→ 修改块（一退一新）
    o2m = "排龙之法以水口为主顺逆相推定其星曜矣"
    new4 = [_mk_new(1, [o1a, o1b, o2m, o3], [o1a, o1b, o2m, o3])]
    np4 = expand_paras(new4, new_mode=True)
    sr = diff_paragraphs(old_paras, np4)
    assert len(sr["段修改"]) == 1 and sr["段修改"][0][0] == "0003·0002"
    ar = aggregate_to_blocks(old, old_paras, np4, sr)
    assert ar["统计"]["修改"] == 1 and ar["修改块"][0][0] == "0003·0002"
    assert ar["统计"]["新块"] == 1  # 修改块的新段重组 1 块
    print(f"  ④ 改段：块2 修改（相似度 {ar['修改块'][0][1]}）→ 重组新块 1")

    # ⑤ 块2 全换（replace 路径：段首变、低于阈值）→ 退休+插入（不配对）
    o2x = "截然不同的全新论述内容毫无关联"
    new5 = [_mk_new(1, [o1a, o1b, o2x, o3], [o1a, o1b, o2x, o3])]
    np5 = expand_paras(new5, new_mode=True)
    sr = diff_paragraphs(old_paras, np5)
    assert len(sr["段删除"]) == 1 and len(sr["段插入"]) == 1 and not sr["段修改"]
    ar = aggregate_to_blocks(old, old_paras, np5, sr)
    assert ar["退休块"] == ["0003·0002"] and ar["统计"]["新块"] == 1
    print("  ⑤ 换段：退休块2 + 插入段（低于阈值不配对）")

    # ⑥ wrap 免疫：同段内断行不同（80 字重折）、段边界一致 → 段保留
    def _split_line(t, n):
        return t[:n] + "\n" + t[n:]
    o1a_w = _split_line(o1a, 12)
    o2_w = _split_line(o2, 10)
    new6 = [_mk_new(1, [o1a_w, o1b, o2_w, o3], [o1a_w, o1b, o2_w, o3])]
    np6 = expand_paras(new6, new_mode=True)
    assert len(np6) == 4, np6  # 段数不变（段边界一致）
    sr = diff_paragraphs(old_paras, np6)
    assert len(sr["段保留"]) == 4 and not sr["段修改"], (sr["段保留"], sr["段修改"])
    ar = aggregate_to_blocks(old, old_paras, np6, sr)
    assert ar["统计"]["保留"] == 3
    print("  ⑥ wrap 免疫：段内断行不同、内容一致 → 全保留")

    # ⑦ 原子段独立重组：插入一个表格段 + 紧邻普通段 → 表格独立成块
    #    （注意：_mk_block 的原文/副本行数须一致，否则 expand 时 zip 截断）
    tbl = "| 甲 | 乙 |\n|---|---|\n| 1 | 2 |"
    tbl_copy = "甲乙\n表格分隔行\n12"  # 三行对三行
    ins7 = "普通新增段落文字内容"
    new7 = [_mk_new(1, [o1a, o1b, tbl, ins7, o2, o3],
                    [o1a, o1b, tbl_copy, ins7, o2, o3])]
    np7 = expand_paras(new7, new_mode=True)
    assert len(np7) == 6 and np7[2]["原子"] is True, (len(np7), np7[2])
    sr = diff_paragraphs(old_paras, np7)
    ar = aggregate_to_blocks(old, old_paras, np7, sr)
    assert ar["统计"]["新块"] == 2, ar["统计"]  # 表格独立 1 块 + 普通段 1 块
    assert ar["新块预演"][0]["段"] == [tbl]
    print("  ⑦ 原子段：表格独立成块 + 普通段聚块（重组规则同 clean_split）")

    # ⑧ 领号预演：max=3 → 新块 0004 起
    ar8 = {"新块预演": [{}, {}, {}]}
    ids = assign_new_ids(old, ar8)
    assert ids == [(1, "0003·0004"), (2, "0003·0005"), (3, "0003·0006")]
    print("  ⑧ 领号预演：max 序号+1 连续")

    # ⑨ 边界：空新段流 → 全退休
    sr9 = diff_paragraphs(old_paras, [])
    assert len(sr9["段删除"]) == 4
    print("  ⑨ 边界：空新段流全删除")

    # ⑩ M3 精化：段插入块跨度中央 → 保留块打散为修改块（sim=1.0）、存活段进重组不丢失；
    #    手术计划：新块在前（含存活段+插入段）、保留块原样在后
    ins10 = "插入块一中央的全新段落内容"
    new10 = [_mk_new(1, [o1a, ins10, o1b, o2, o3], [o1a, ins10, o1b, o2, o3])]
    np10 = expand_paras(new10, new_mode=True)
    sr10 = diff_paragraphs(old_paras, np10)
    ar10 = aggregate_to_blocks(old, old_paras, np10, sr10)
    assert ar10["统计"]["保留"] == 2 and ar10["统计"]["修改"] == 1, ar10["统计"]
    assert ar10["修改块"][0] == ("0003·0001", 1.0), ar10["修改块"]
    assert ar10["新块预演"][0]["段"] == [o1a, ins10, o1b], ar10["新块预演"]
    plan10 = build_surgery_plan(old, old_paras, np10, sr10)
    assert [it["类型"] for it in plan10] == ["新块", "保留", "保留"], plan10
    assert plan10[1]["块ID"] == "0003·0002" and plan10[2]["块ID"] == "0003·0003"
    assert [s["原文文本"] for s in plan10[0]["段"]] == [o1a, ins10, o1b]
    print("  ⑩ M3 精化：块内插入打散为修改 + 存活段不丢 + 手术计划顺序")

    print("block_diff 自检通过：段级四分类 + 块级聚合 + 重组预演 + 领号 + 手术计划 ×10 组用例")


if __name__ == "__main__":
    common.run_or_selfcheck(lambda: None, _selfcheck)
