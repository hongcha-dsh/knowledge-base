# -*- coding: utf-8 -*-
r"""书籍更新（#21 · 方案丁·块出生证制）· 卡 A-09（M1 dry-run ＋ M3 手术执行器）。

用法：
  python update_book.py --书号 0003 --新版 "路径\\修订版.docx" --dry-run   # 预演（不动库）
  python update_book.py --书号 0003 --新版 "路径\\修订版.docx"             # 手术（真写库）

M1（纯只读）：新版文件 → 沙箱重跑 A-01/A-02/A-03 产新块序列 →
block_diff 四分类（保留/退休/新生/修改对）→ 产《更新方案报告》。

M3（手术，设计 §3.2 ⑤⑥）：整书快照备份（输出\迁移备份_YYYYMMDD\{书名}\）→
段级 diff → build_surgery_plan 装配（保留块原文原样搬运 / 变化区域重组领号 /
退休块移除）→ 重写 原文.md + 重生成 检索副本.md + 写 退休块.json → 重跑 A-04 →
**双路径互证对账**（路径 B＝沙箱内完整管线全新入库产物；段级全文行比对 +
保留块零触碰证明）→ 绿灯落库，红灯/异常整体回滚（库内不留半成品）。

对账粒度说明（M3 设计精化，同 §3.0 段级定案同理）：设计 §六.1 原定为**逐块**三重
一致，但块是「≤1500 字段聚合包」——插入/删除会推移贪心聚合边界，路径 B 重打包
与路径 A 保留块原样搬运在块级天然不等价（M1 已证聚合噪声）。故对账下沉为
**段级全文行比对**（剥块 ID 行整行后逐行字符级一致＝内容/顺序/wrap 全等价）＋
保留块原文切片逐字节零触碰证明；块级只断言领号合法（max+1 起连续）。

报告：《更新方案》始终落盘（#14 例外，用户确认凭据）；《更新对账》按 #14 口径
红灯才落盘（附销单指引），转绿自动清除同名旧报告。
运行中勿强杀进程（配置改写窗口内强杀会留下沙箱态配置）。

退出码：0=成功（或无变化全保留）；2=dry-run 有变化待确认；1=失败（已回滚）。
设计依据：项目文档\未来模块规划\2026-09-16-#21-书籍更新功能-design.md §3.2/§四/§六/§七。
"""

import json
import re
import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common
import block_diff

SANDBOX_KEYS = ["知识库", "待入库", "输出", "报告", "已入库"]
# #43：已入库 必须纳入沙箱键——控制台的残留自动清理会读 `已入库\_入库登记.json` 当名册
# （common.load_ingest_registry），若它指向真实目录而 知识库 指向沙箱，真实书会被判成
# 「登记在册但书库无目录」的残留 → 原件被「移回 待入库」（＝沙箱）→ 随沙箱销毁而丢失。
# ⚠️ 「书规则」**故意不入沙箱**（#21 M6 细致验收结论）：沙箱管线（A-01/A-03）必须读到
#   真实的 `书规则\{书名}.json` 才能忠实复现该书块结构（图块豁免/排盘规则）；运行时只读、
#   无写路径。若将来确要纳入沙箱，**必须先把该书规则文件复制进沙箱**，否则排盘书会误判。


# ---------------------------------------------------------------- 老块解析

def resolve_book_title(book_id: str, kb_root: Path = None) -> str:
    """书号 → 书名：扫各书 倒排表.json 头部「书号」字段（与控制台 _read_book_id 同源）。

    找不到时抛 ValueError（调用方按失败处理，退出码 1）。
    """
    if kb_root is None:
        kb_root = common.get_path("知识库")
    books_dir = kb_root / "书籍"
    if not books_dir.exists():
        raise ValueError(f"知识库书籍目录不存在：{books_dir}")
    for book_dir in books_dir.iterdir():
        inv = book_dir / "倒排表.json"
        if not inv.exists():
            continue
        try:
            head = json.loads(inv.read_text(encoding="utf-8"))
            if head.get("书号") == book_id:
                return book_dir.name
        except Exception:
            continue
    raise ValueError(f"书号 {book_id} 不在库内（扫过 {books_dir}）")


def load_blocks_with_lines(book_title: str, kb_root: Path = None, new_mode: bool = False) -> list:
    """解析书目录 原文.md+检索副本.md → 块序列（物理顺序，带双行流）。

    复用 build_index._parse_book_files；块结构含 原文行/副本行 切片，
    供 block_diff.expand_paras 展开段流（段级 diff 的输入）。
    new_mode=True 用于沙箱新块（键名「序号」，1 基物理序）。
    """
    import build_index
    block_map, orig_lines, copy_lines = build_index._parse_book_files(book_title, kb_root)
    blocks = sorted(block_map.values(), key=lambda b: b["起始行"])
    out = []
    for i, b in enumerate(blocks):
        s, e = b["起始行"], b["结束行"]
        item = {
            "页标签": b["页码"], "指纹": b["指纹"],
            "原文行": orig_lines[s + 1:e + 1],  # 跳过块 ID 行自身
            "副本行": copy_lines[s + 1:e + 1],
        }
        item["序号" if new_mode else "块ID"] = (i + 1) if new_mode else b["块ID"]
        out.append(item)
    return out


# ---------------------------------------------------------------- 资产预扫

def _ref_hits(pattern: re.Pattern, text: str) -> list:
    return sorted(set(pattern.findall(text)))


def scan_asset_refs(book_id: str, book_title: str, kb_root: Path = None) -> dict:
    """预扫库内块级引用（受影响资产统计，设计 §六.3）。

    返回 {"锚点": {块ID: 章号}, "评测题": [题号...], "学习记录": [文件相对路径...]}
    ——锚点按块归到章（待重标章预览），评测/学习只记引用存在的凭据。
    """
    if kb_root is None:
        kb_root = common.get_path("知识库")
    bid_re = re.compile(rf"{re.escape(book_id)}·\d{{4}}")
    result = {"锚点": {}, "评测题": [], "学习记录": []}

    # 章节摘要锚点表：块 ID → 所在章（文件名即章号）
    summary_dir = kb_root / "书籍" / book_title / "章节摘要"
    if summary_dir.exists():
        for f in sorted(summary_dir.glob("*.md")):
            m = re.match(r"(\d+)", f.stem)
            chapter = m.group(1) if m else f.stem
            for bid in _ref_hits(bid_re, f.read_text(encoding="utf-8")):
                result["锚点"].setdefault(bid, chapter)

    # 评测集：答案块 ID 引用（记录题号）
    eval_path = kb_root / "评测集.md"
    if eval_path.exists():
        for line in eval_path.read_text(encoding="utf-8").splitlines():
            m = re.match(r"\|\s*(Q\d+)\s*\|", line)
            if m and bid_re.search(line):
                result["评测题"].append(m.group(1))

    # 学习记录（目录可能不存在）
    study_dir = kb_root / "学习记录" / book_title
    if study_dir.exists():
        for f in study_dir.rglob("*.md"):
            if _ref_hits(bid_re, f.read_text(encoding="utf-8")):
                result["学习记录"].append(str(f.relative_to(kb_root)))
    return result


def affected_chapters(result: dict, retired_ids: list, modified_ids: list) -> list:
    """受影响章预览：退休/修改的老块经锚点表归章（#21 M2 起骨架为「起始块ID+块数」，不再做区间兜底）。"""
    chapters = set()
    for bid in retired_ids + modified_ids:
        ch = result["锚点"].get(bid)
        if ch:
            chapters.add(ch)
    return sorted(chapters, key=lambda s: (len(s), s))


# ---------------------------------------------------------------- 沙箱链

@contextmanager
def sandbox_ctx():
    """e2e 同款沙箱：临时改写 路径配置.json 沙箱键（`SANDBOX_KEYS`：#43 起含 已入库）
    → finally 恢复并销毁。

    with 块内 common.get_path()/子进程读到的都是沙箱路径；
    异常或强杀外层 KeyboardInterrupt 以外的正常异常都会走 finally 恢复。
    """
    cfg_path = common.PROJECT_ROOT / "路径配置.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    keys = [k for k in SANDBOX_KEYS if k in cfg]
    saved = {k: cfg[k] for k in keys}
    sandbox = Path(tempfile.mkdtemp(prefix="update_dry_"))
    for key in keys:
        (sandbox / key).mkdir(parents=True, exist_ok=True)
        cfg[key] = str(sandbox / key)
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        yield sandbox
    finally:
        cfg.update(saved)
        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        shutil.rmtree(sandbox, ignore_errors=True)


def _run_script(script_name: str, *args) -> tuple:
    """子进程跑管线脚本（cwd＝项目根）。返回 (退出码, stdout/stderr 尾部)。"""
    script = common.get_path("脚本目录") / script_name
    creation = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    p = subprocess.run([sys.executable, str(script), *args],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", cwd=str(common.PROJECT_ROOT),
                       timeout=600, creationflags=creation)
    tail = (p.stdout or p.stderr or "").strip()
    return p.returncode, tail


def _read_sandbox_reports(sandbox: Path, book_title: str) -> str:
    """失败时读取沙箱报告（质检/清洗分块/提取）关键内容，并入失败报告。"""
    parts = []
    report_dir = sandbox / "报告"
    if report_dir.exists():
        for f in sorted(report_dir.glob(f"{book_title}_*.md")):
            content = f.read_text(encoding="utf-8").strip()
            if content:
                parts.append(f"—— {f.name} ——\n{content}")
    return "\n\n".join(parts)


def _sandbox_run(new_file: Path, book_id: str, book_title: str, full: bool = False) -> tuple:
    """沙箱内跑管线产新块序列。full=True 时加跑 A-04 并读回沙箱 原文.md（双路径之 B）。

    返回 (新块序列, 沙箱原文行或 None, 沙箱日志摘要)。
    任一步红灯 → 抛 RuntimeError（消息含沙箱报告内容，由 main 落失败报告）。
    """
    log = []
    with sandbox_ctx() as sandbox:
        # 新版文件复制进沙箱待入库
        dst = sandbox / "待入库" / new_file.name
        shutil.copy2(new_file, dst)
        # 真实库 书配置.json 复制进沙箱书目录（页码拟合一致、避免 LLM 重跑）
        real_cfg = common.book_config_path(book_title)
        if real_cfg.exists():
            sd = sandbox / "知识库" / "书籍" / book_title
            sd.mkdir(parents=True, exist_ok=True)
            shutil.copy2(real_cfg, sd / "书配置.json")

        steps = [
            ("extract.py", ("--文件", new_file.name, "--书号", book_id),
             {0: "ok", 2: "ok"}, "提取"),
            ("quality_check.py", ("--书号", book_id, "--书名", book_title),
             {0: "ok"}, "质检"),
            ("clean_split.py", ("--书号", book_id, "--书名", book_title),
             {0: "ok", 2: "黄灯可继续"}, "切块"),
        ]
        if full:
            steps.append(("build_index.py", ("--书号", book_id, "--书名", book_title),
                          {0: "ok"}, "建索引"))
        for script, args, ok_codes, label in steps:
            code, tail = _run_script(script, *args)
            log.append(f"[{label}] 退出码 {code}")
            if tail:
                log.append("  " + tail.replace("\n", "\n  "))
            if code not in ok_codes:
                detail = _read_sandbox_reports(sandbox, book_title)
                raise RuntimeError(
                    f"沙箱 {label} 步骤失败（退出码 {code}）\n{tail}\n"
                    f"{'=' * 30} 沙箱报告 {'=' * 30}\n{detail}")

        # 新块序列解析（沙箱知识库；恢复配置前读入内存，带双行流供段级展开）
        kb_sandbox = sandbox / "知识库"
        new_blocks = load_blocks_with_lines(book_title, kb_sandbox, new_mode=True)
        pathb_lines = None
        if full:
            pathb_lines = (kb_sandbox / "书籍" / book_title / "原文.md").read_text(
                encoding="utf-8").split("\n")
    return new_blocks, pathb_lines, "\n".join(log)


def sandbox_dry_run(new_file: Path, book_id: str, book_title: str) -> tuple:
    """沙箱内跑 A-01/A-02/A-03 产新块序列。返回 (新块序列, 沙箱日志摘要)。"""
    new_blocks, _pathb, log = _sandbox_run(new_file, book_id, book_title, full=False)
    return new_blocks, log


# ---------------------------------------------------------------- 手术执行（M3）

BID_LINE_RE = re.compile(r"^【([^·]+·\d{4})】p\.")  # 块 ID 行（捕获块ID；隔离区 ⟦⟧ 行不匹配）


def _raw_block_slices(book_title: str, kb_root: Path) -> dict:
    """老块原文切片（含块 ID 行）：{块ID: [行...]}（去尾部空行，物理序）。"""
    import build_index
    block_map, orig_lines, _copy = build_index._parse_book_files(book_title, kb_root)
    out = {}
    for bid, b in block_map.items():
        seg = list(orig_lines[b["起始行"]:b["结束行"] + 1])
        while seg and not seg[-1].strip():
            seg.pop()
        out[bid] = seg
    return out


def _slice_blocks_from_lines(lines: list) -> dict:
    """原文行序列 → {块ID: [含 ID 行切片]}（去尾部空行）。对账重切用。"""
    out, cur_id, cur = {}, None, []
    for l in lines:
        m = BID_LINE_RE.match(l)
        if m:
            if cur_id is not None:
                while cur and not cur[-1].strip():
                    cur.pop()
                out[cur_id] = cur
            cur_id, cur = m.group(1), [l]
        elif cur_id is not None:
            cur.append(l)
    if cur_id is not None:
        while cur and not cur[-1].strip():
            cur.pop()
        out[cur_id] = cur
    return out


def _derive_label(segs: list) -> str:
    """重组新块的页标签推导：全段同标签 → 该标签；否则 首段起~末段止（区间式）。"""
    labels = [s.get("页标签", "") for s in segs]
    labels = [l for l in labels if l]
    if not labels:
        return "1"
    first = labels[0].split("~")[0]
    last = labels[-1].split("~")[-1]
    return first if first == last else f"{first}~{last}"


def assemble_book(plan: list, old_raw: dict, new_ids: list) -> tuple:
    """按计划装配新 原文.md / 检索副本.md 行序列（格式与 clean_split 同款：
    块 ID 行 + 段（段间空行）、块间空行、全文无尾空行；副本＝ID 行原样 + 余行归一）。

    new_ids（[(预演序, 块ID)]）与 plan 中「新块」项按物理序一一对应。
    """
    orig, copy = [], []
    nid = iter(bid for _, bid in new_ids)

    def _emit_block(lines):
        if orig:
            orig.append("")
            copy.append("")
        orig.extend(lines)
        copy.extend(l if BID_LINE_RE.match(l) else common.normalize_for_search(l)
                    for l in lines)

    for item in plan:
        if item["类型"] == "保留":
            _emit_block(old_raw[item["块ID"]])
        else:
            lines = [f"【{next(nid)}】p.{_derive_label(item['段'])}"]
            for k, seg in enumerate(item["段"]):
                if k:
                    lines.append("")
                lines.extend(seg["原文文本"].split("\n"))
            _emit_block(lines)
    return orig, copy


def write_retire_registry(book_dir: Path, entries: list):
    """退休登记册（设计 §3.3）：追加写 退休块.json（{"退休块": [...]}）。

    条目＝{块ID, 日期, 原因(修改/删除), 后继, 相似度}；重建索引不影响它（出生/退休史）。"""
    path = book_dir / "退休块.json"
    data = {"退休块": []}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            data.setdefault("退休块", [])
        except Exception:
            data = {"退休块": []}
    data["退休块"].extend(entries)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _retire_entries(result: dict, seg_res: dict, cls: dict, plan: list, new_ids: list) -> list:
    """退休登记册条目组装：修改块（含打散）原因=修改+后继新块；退休块原因=删除+后继 null。"""
    today = date.today().isoformat()
    pos2bid = {}
    nid = iter(bid for _, bid in new_ids)
    for item in plan:
        if item["类型"] != "新块":
            continue
        bid = next(nid)
        for seg in item["段"]:
            pos2bid[seg["新段序号"]] = bid
    mod_pos = {}
    for b, j, s in seg_res["段修改"]:
        mod_pos.setdefault(b, []).append((j, s))
    entries = []
    for bid, sim in result["修改块"]:
        succ = None
        if mod_pos.get(bid):
            succ = pos2bid.get(max(mod_pos[bid], key=lambda t: t[1])[0])
        if succ is None:  # 打散块（无修改段）：取首个存活段所在新块
            for j in cls["段保留归块"].get(bid, []):
                if j in pos2bid:
                    succ = pos2bid[j]
                    break
        entries.append({"块ID": bid, "日期": today, "原因": "修改", "后继": succ, "相似度": sim})
    for bid in result["退休块"]:
        entries.append({"块ID": bid, "日期": today, "原因": "删除", "后继": None, "相似度": None})
    return entries


def reconcile(new_orig_lines: list, pathb_lines: list, keep_ids: list,
              old_raw: dict, new_ids: list, max_old_no: int) -> tuple:
    """双路径互证对账。返回 (通过, 明细列表)。

    ① 段级全文行比对：两路径剥块 ID 行整行后逐行字符级一致（内容/顺序/wrap 全等价）；
    ② 保留块零触碰：保留块老切片在新 原文.md 原位逐字节一致（重切新文件独立验证）；
    ③ 领号合法：新块 ID 自 max 序号+1 起连续。
    """
    details, ok = [], True
    a = [l for l in new_orig_lines if not BID_LINE_RE.match(l)]
    b = [l for l in pathb_lines if not BID_LINE_RE.match(l)]
    if a == b:
        details.append(f"① 段级全文行比对：一致（{len(a)} 行）")
    else:
        ok = False
        diff_at = next((i for i, (x, y) in enumerate(zip(a, b)) if x != y),
                       min(len(a), len(b)))
        la = a[diff_at] if diff_at < len(a) else "（EOF）"
        lb = b[diff_at] if diff_at < len(b) else "（EOF）"
        details.append(f"① 段级全文行比对：不一致（A {len(a)} 行 / B {len(b)} 行，"
                       f"首个分歧行 {diff_at + 1}）\n  A：{la}\n  B：{lb}")
    new_slices = _slice_blocks_from_lines(new_orig_lines)
    bad = [bid for bid in keep_ids if new_slices.get(bid) != old_raw[bid]]
    if bad:
        ok = False
        details.append(f"② 保留块零触碰：{len(bad)} 块被改动（{', '.join(bad[:5])}…）")
    else:
        details.append(f"② 保留块零触碰：{len(keep_ids)} 块逐字节一致")
    nos = [int(bid.split("·")[1]) for _, bid in new_ids]
    if nos == list(range(max_old_no + 1, max_old_no + 1 + len(nos))):
        details.append(f"③ 领号合法：{len(nos)} 个新块自 {max_old_no + 1:04d} 起连续")
    else:
        ok = False
        details.append(f"③ 领号非法：{[f'{n:04d}' for n in nos]}（应自 {max_old_no + 1:04d} 起连续）")
    return ok, details


def _snapshot_book(book_dir: Path, kb_root: Path, backup_base: Path = None) -> tuple:
    """整书快照 → 输出\\迁移备份_YYYYMMDD\\{书名}\\（同日重复加时分秒后缀）。

    返回 (快照目录, 书目总索引原文)——书目总索引在书目录外，随快照记文本、回滚时还原。"""
    if backup_base is None:
        backup_base = common.get_path("输出") / f"迁移备份_{datetime.now():%Y%m%d}"
    dst = backup_base / book_dir.name
    if dst.exists():
        dst = backup_base / f"{book_dir.name}_{datetime.now():%H%M%S}"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(book_dir, dst)
    catalog = kb_root / "书目总索引.md"
    cat_text = catalog.read_text(encoding="utf-8") if catalog.exists() else None
    return dst, cat_text


def _rollback(book_dir: Path, snapshot: Path, kb_root: Path, cat_text):
    """整体回滚：快照恢复书目录 + 书目总索引还原（库内不留半成品）。"""
    if book_dir.exists():
        shutil.rmtree(book_dir)
    shutil.copytree(snapshot, book_dir)
    if cat_text is not None:
        (kb_root / "书目总索引.md").write_text(cat_text, encoding="utf-8")


def _chapters_of_new_blocks(book_title: str, kb_root: Path, new_bids: list) -> set:
    """新块归章（M4）：读新骨架（起始块ID+块数），按原文物理序把新块归到章号集合。"""
    import annotate_pack
    chs = annotate_pack._read_skeleton(book_title, kb_root)
    orig = (kb_root / "书籍" / book_title / "原文.md").read_text(
        encoding="utf-8").split("\n")
    order = [m.group(1) for l in orig if (m := BID_LINE_RE.match(l))]
    pos = {bid: i for i, bid in enumerate(order)}
    targets = set(new_bids)
    out = set()
    for c in chs:
        start = pos.get(c["起始块ID"])
        if start is None:
            continue
        members = set(order[start:start + c["块数"]])
        if targets & members:
            out.add(c["章号"])
    return out


def rebase_structure_json(book_id: str, book_title: str, kb_root: Path,
                          cache_dir: Path = None) -> dict:
    """#21 M6-D1 修复：把 结构.json 的章锚点重定位到**术后**新块 ID。

    结构.json（A-03 产物，只供 build_index 章节识别候选）里的 块ID 是出生序快照；手术中
    变化区域整块退休+重组后，锚点会指向已不存在的块 → A-04 重建时该候选被静默丢弃 →
    章从骨架消失（骨架塌缩）→ 下游「受影响章」误判（保留章被误动、对已消失章号重标报错）。
    此处复用 clean_split 同源换算（`_match_signal_blocks`：标题文本 + 出现顺序）在术后块
    序列上重算锚点，与 A-03 每次入库所做的换算等价。

    返回 {"总数","条目","变更","丢失"}；无结构信号（无信号书）返回 None。
    """
    import clean_split
    if cache_dir is None:
        cache_dir = common.get_path("输出") / "提取缓存" / book_id
    signals = clean_split._load_structure_signals(cache_dir)
    if not signals:
        return None
    book_dir = kb_root / "书籍" / book_title
    block_ids, block_lines = [], []
    for ln in (book_dir / "原文.md").read_text(encoding="utf-8").split("\n"):
        m = BID_LINE_RE.match(ln)
        if m:
            block_ids.append(m.group(1))
            block_lines.append([])
        elif block_ids:
            block_lines[-1].append(ln)
    if not block_ids:
        return None
    entries, failed = clean_split._match_signal_blocks(
        signals, [(None, None, ["\n".join(b)]) for b in block_lines], book_id)
    out, changed = [], 0
    for e in entries:
        seq = int(e["块ID"].split("·")[1])
        real = block_ids[seq - 1] if 1 <= seq <= len(block_ids) else None
        if real is None:  # 术后块数少于匹配序位（异常）→ 宁缺勿错
            continue
        if real != e["块ID"]:
            changed += 1
        e["块ID"] = real
        e["匹配方式"] = "文本+顺序（更新后重定位）"
        out.append(e)
    struct_path = book_dir / "结构.json"
    if out:
        struct_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    elif struct_path.exists():
        struct_path.unlink()
    return {"总数": len(signals), "条目": len(out), "变更": changed, "丢失": len(failed)}


def remark_chapters_for_reannotation(book_title: str, kb_root: Path, chapters) -> tuple:
    """变化章「待重标」（设计 §3.4）：仅对**此前已标注过**的章（有 .done 或章节摘要）
    删 .done + 删该章旧回填/旧章包（引用退休块，已失效）+ 重新产包（只产变化章）。

    返回 (重标章列表, 未标注跳过章列表, 新骨架中已不存在的章列表)。.done 为唯一状态源
    （红线 8）：删除即回到「进行中」。#21 M6-D3：新骨架中已无此章号时**跳过而非抛错**
    （章标题块被改动等场景下，对不存在的章产包会中断已落库的手术收尾）。"""
    import annotate_pack
    book_dir = kb_root / "书籍" / book_title
    pdir = annotate_pack.pack_dir(book_title)
    back_dir = pdir / "回填"
    exist = {c["章号"] for c in annotate_pack._read_skeleton(book_title, kb_root)}
    redone, skipped, missing = [], [], []
    for ch in sorted(chapters):
        if ch not in exist:
            missing.append(ch)
            continue
        done_file = back_dir / f"{ch}.done"
        had_annot = done_file.exists() or (book_dir / "章节摘要" / f"{ch}.md").exists()
        if not had_annot:
            skipped.append(ch)
            continue
        if done_file.exists():
            done_file.unlink()
        for f in back_dir.glob(f"{ch}*_任务包.md"):
            f.unlink()
        for f in pdir.glob(f"{ch}*_任务包.md"):
            f.unlink()
        annotate_pack.generate_pack(book_title, ch, kb_root)
        redone.append(ch)
    return redone, skipped, missing


def execute(book_id: str, new_file: Path, threshold: float = block_diff.DEFAULT_THRESHOLD) -> int:
    """M3 手术主流程（真写库 + 双路径对账 + 红灯回滚 + M4 变化章待重标）。返回进程退出码。"""
    import build_index
    book_title = resolve_book_title(book_id)
    kb_root = common.get_path("知识库")
    book_dir = kb_root / "书籍" / book_title
    old_blocks = load_blocks_with_lines(book_title)
    old_raw = _raw_block_slices(book_title, kb_root)
    assets = scan_asset_refs(book_id, book_title)  # 术前锚点归章（M4 待重标用）

    new_blocks, pathb_lines, sandbox_log = _sandbox_run(new_file, book_id, book_title, full=True)

    old_paras = block_diff.expand_paras(old_blocks)
    new_paras = block_diff.expand_paras(new_blocks, new_mode=True)
    seg_res = block_diff.diff_paragraphs(old_paras, new_paras, threshold)
    result = block_diff.aggregate_to_blocks(old_blocks, old_paras, new_paras, seg_res)
    st = result["统计"]
    print(f"老 {st['老块数']} 块｜保留 {st['保留']}｜修改 {st['修改']}"
          f"｜退休 {st['退休']}｜重组新块 {st['新块']}")
    if not (st["修改"] or st["退休"] or st["新块"]):
        print("结论：无变化（全保留），库未改动")
        return 0

    new_ids = block_diff.assign_new_ids(old_blocks, result)
    plan = block_diff.build_surgery_plan(old_blocks, old_paras, new_paras, seg_res)
    cls = block_diff._classify(old_blocks, old_paras, new_paras, seg_res)
    max_old_no = max(int(b["块ID"].split("·")[1]) for b in old_blocks)

    snapshot, cat_text = _snapshot_book(book_dir, kb_root)
    print(f"整书快照：{snapshot}")
    try:
        orig_lines, copy_lines = assemble_book(plan, old_raw, new_ids)
        (book_dir / "原文.md").write_text("\n".join(orig_lines), encoding="utf-8")
        (book_dir / "检索副本.md").write_text("\n".join(copy_lines), encoding="utf-8")
        entries = _retire_entries(result, seg_res, cls, plan, new_ids)
        if entries:
            write_retire_registry(book_dir, entries)
            print(f"退休登记册：{len(entries)} 条（修改 {st['修改']} / 删除 {st['退休']}）")
        # M6-D1 修复：先把 结构.json 章锚点重基到术后块 ID，再重建索引
        # （否则锚点指向退休/改组块 → 章从骨架消失 → 受影响章误判）
        rb = rebase_structure_json(book_id, book_title, kb_root)
        if rb:
            print(f"结构锚点重基：{rb['条目']}/{rb['总数']} 条（其中位置变更 {rb['变更']}"
                  f"／匹配丢失 {rb['丢失']}）")
        info = build_index.build_index(book_id, book_title, kb_root)
        print(f"A-04 重建：{info['块数']} 块 / {info['章节数']} 章（章节识别：{info['章节识别']}）")
        ok, details = reconcile(orig_lines, pathb_lines, result["保留"],
                                old_raw, new_ids, max_old_no)
    except Exception as e:
        _rollback(book_dir, snapshot, kb_root, cat_text)
        common.write_report(
            f"{book_title}_更新对账.md",
            f"# 《{book_title}》更新对账\n结论：🔴 手术中途异常，已整体回滚（库内无半成品）\n"
            f"原因：{e}\n快照：{snapshot}\n")
        print(f"🔴 手术异常，已回滚：{e}")
        return 1

    for d in details:
        print(d)
    if ok:
        common.clear_report(f"{book_title}_更新对账.md")
        print("结论：🟢 双路径互证对账通过，更新已落库")
        # M4：变化章「待重标」（对账绿灯后执行，不参与回滚；只产变化章任务包）
        affected = set(affected_chapters(assets, list(result["退休块"]),
                                         [b for b, _ in result["修改块"]]))
        new_bids = [bid for _, bid in new_ids]
        if new_bids:
            affected |= _chapters_of_new_blocks(book_title, kb_root, new_bids)
        if affected:
            # M6-D3 修复：本步在「对账绿、已落库」之后，异常不得逃逸——否则控制台按失败收尾
            # 并弹出「已自动恢复原样」（与事实不符：库其实已更新）。异常 → 🟡 报告 + 仍退出 0。
            try:
                redone, skipped, missing = remark_chapters_for_reannotation(
                    book_title, kb_root, affected)
                if redone:
                    print(f"待重标章：{', '.join(redone)}（旧 .done/旧回填已清，新任务包已生成）")
                if skipped:
                    print(f"变化章此前未标注（跳过重标）：{', '.join(skipped)}")
                if missing:
                    print(f"🟡 变化章在新骨架中已不存在（跳过重标）：{', '.join(missing)}")
            except Exception as e:
                common.write_report(
                    f"{book_title}_更新对账.md",
                    f"# 《{book_title}》更新对账\n结论：🟡 手术与对账均已通过（**库已更新**），"
                    f"仅「变化章待重标」未完成\n原因：{e}\n"
                    f"待重标章：{', '.join(sorted(affected))}\n\n"
                    f"处理建议：重跑同一次更新（幂等，全保留直接退出 0 并自动清除本报告），"
                    f"或手动重新生成上述章的任务包后重新标注。\n")
                print(f"🟡 变化章待重标未完成（库已更新，详见 报告\\{book_title}_更新对账.md）：{e}")
        return 0
    _rollback(book_dir, snapshot, kb_root, cat_text)
    common.write_report(
        f"{book_title}_更新对账.md",
        f"# 《{book_title}》更新对账\n结论：🔴 对账不通过，已整体回滚（库内无半成品）\n\n"
        + "\n".join(f"- {d}" for d in details)
        + f"\n\n快照：{snapshot}\n\n## 沙箱运行日志\n\n```\n{sandbox_log}\n```\n")
    print("结论：🔴 对账不通过，已整体回滚（详见 报告\\更新对账）")
    return 1


# ---------------------------------------------------------------- 报告渲染

def _keep_ranges(keep_ids: list) -> str:
    """保留块 ID 列表 → 连续序号区间压缩表示（报告可读性）。"""
    if not keep_ids:
        return "（无）"
    nos = sorted(int(bid.split("·")[1]) for bid in keep_ids if "·" in bid)
    if not nos:
        return f"{len(keep_ids)} 个"
    ranges, s, p = [], nos[0], nos[0]
    for n in nos[1:]:
        if n == p + 1:
            p = n
            continue
        ranges.append(f"{s:04d}" if s == p else f"{s:04d}~{p:04d}")
        s = p = n
    ranges.append(f"{s:04d}" if s == p else f"{s:04d}~{p:04d}")
    return f"{len(nos)} 个（序号 {', '.join(ranges)}）"


def build_plan_report(book_title: str, book_id: str, new_file: Path,
                      result: dict, new_ids: list, assets: dict,
                      affected: list, sandbox_log: str, threshold: float) -> str:
    """渲染《更新方案报告》（用户确认凭据；避免 🔴/🟡 字样以防误附销单指引）。

    result 为 block_diff.aggregate_to_blocks 的块级四分类（段级对齐产物）。"""
    st = result["统计"]
    lines = [
        f"# 《{book_title}》更新方案（dry-run 预演）",
        f"> 书号：{book_id} ｜ 新版文件：{new_file.name} ｜ 生成时间：{datetime.now():%Y-%m-%d %H:%M:%S}",
        f"> 对齐粒度：段（自然段/原子段，免疫重切块边界） ｜ 修改判定阈值：{threshold}",
        f"> **本报告为预演，未改动任何库内文件**",
        "",
        "## 一、总览",
        "",
        f"| 老块数 | 保留块 | 修改块 | 退休块 | 重组新块 |",
        f"|---|---|---|---|---|",
        f"| {st['老块数']} | {st['保留']} | {st['修改']} | {st['退休']} | {st['新块']} |",
        "",
        "- **保留块**：块内全部段内容一致——块 ID 不动、标注 100% 存活（手术时原文照搬）；",
        "- **修改块**：块内部分段被改/增/删——老 ID 退休（原因=修改），存活段进重组区段；",
        "- **退休块**：全部段被删——老 ID 显式失效（原因=删除）；",
        "- **重组新块**：修改块新段 + 新增段按原聚合规则（≤1500 字、表格/代码块独立）重组，"
        "从该书最大序号+1 起连续领号。",
        "",
    ]

    lines += ["## 二、预演领号段", ""]
    if new_ids:
        lo, hi = new_ids[0][1], new_ids[-1][1]
        lines += [f"重组新块共 {len(new_ids)} 个，预演领号：`{lo}` ~ `{hi}`"
                  f"（该书 max 序号 +1 起，连续不复用）", ""]
    else:
        lines += ["（无新块，无需领号——全部块 ID 原样保留）", ""]

    lines += ["## 三、保留块（零触碰，ID 不变）", "", _keep_ranges(result["保留"]), ""]

    lines += ["## 四、修改块（一退一新，老 ID 退休）", ""]
    if result["修改块"]:
        lines += ["| 老块ID（退休） | 内容相似度 |", "|---|---|"]
        for bid, sim in result["修改块"]:
            lines.append(f"| {bid} | {sim} |")
    else:
        lines.append("（无）")
    lines.append("")

    lines += ["## 五、退休块（新版已删除）", ""]
    lines.append("、".join(result["退休块"]) if result["退休块"] else "（无）")
    lines.append("")

    lines += ["## 六、重组新块（领号对象）", ""]
    if result["新块预演"]:
        nid_map = {i + 1: bid for i, bid in new_ids}
        lines += ["| 预演块ID | 段数 | 来源 | 首段预览 |", "|---|---|---|---|"]
        for i, nb in enumerate(result["新块预演"]):
            preview = (nb["段"][0][:20] + "…") if nb["段"] else ""
            preview = preview.replace("\n", " ")
            lines.append(f"| {nid_map.get(i + 1, '?')} | {len(nb['段'])} | {nb['来源']} | {preview} |")
    else:
        lines.append("（无）")
    lines.append("")

    # 受影响资产（设计 §六.3：多少锚点/评测题/学习记录引用将失效或退休）
    retired_ids = [b for b, _ in result["修改块"]] + list(result["退休块"])
    lost_anchor = [b for b in retired_ids if b in assets["锚点"]]
    lines += ["## 七、受影响资产统计（老块引用将失效）", ""]
    lines.append(f"- 章节摘要锚点：{len(lost_anchor)} 个块被锚点表引用"
                 + (f"（{', '.join(lost_anchor[:10])}{'…' if len(lost_anchor) > 10 else ''}）" if lost_anchor else ""))
    lines.append(f"- 评测题答案块引用：{len(assets['评测题'])} 题引用本书块"
                 + ("（涉及本次失效块的题需人工核对/迁移）" if assets["评测题"] else ""))
    lines.append(f"- 学习记录引用：{len(assets['学习记录'])} 个文件" if assets["学习记录"] else "- 学习记录引用：无")
    lines.append("")

    if affected:
        lines += ["## 八、受影响章（待重标预览）", "",
                  f"修改/退休块所在章：{', '.join(affected)}（手术后标「待重标」，"
                  f"任务包只生成变化章——M4 交付）", ""]
    else:
        lines += ["## 八、受影响章（待重标预览）", "", "（无）", ""]

    lines += ["## 九、沙箱运行日志", "", "```", sandbox_log, "```", "",
              "---", "确认执行：本报告为 dry-run 预演结果；确认后由手术执行器（M3 交付）"
              "按本方案实施（整书快照备份 → 保留块照搬 + 变化区域重组 → 双路径互证对账）。"]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- 主流程

def dry_run(book_id: str, new_file: Path, threshold: float = block_diff.DEFAULT_THRESHOLD) -> int:
    """M1 dry-run 主流程（段级对齐）。返回进程退出码。"""
    book_title = resolve_book_title(book_id)
    old_blocks = load_blocks_with_lines(book_title)
    assets = scan_asset_refs(book_id, book_title)

    new_blocks, sandbox_log = sandbox_dry_run(new_file, book_id, book_title)

    old_paras = block_diff.expand_paras(old_blocks)
    new_paras = block_diff.expand_paras(new_blocks, new_mode=True)
    seg_res = block_diff.diff_paragraphs(old_paras, new_paras, threshold)
    result = block_diff.aggregate_to_blocks(old_blocks, old_paras, new_paras, seg_res)
    new_ids = block_diff.assign_new_ids(old_blocks, result)
    affected = affected_chapters(assets, list(result["退休块"]),
                                 [b for b, _ in result["修改块"]])

    report = build_plan_report(book_title, book_id, new_file, result, new_ids,
                               assets, affected, sandbox_log, threshold)
    path = common.write_report(f"{book_title}_更新方案.md", report)
    print(f"更新方案已生成：{path}")
    st = result["统计"]
    print(f"老 {st['老块数']} 块｜保留 {st['保留']}｜修改 {st['修改']}"
          f"｜退休 {st['退休']}｜重组新块 {st['新块']}")
    if st["修改"] or st["退休"] or st["新块"]:
        print("结论：有变化待确认（见更新方案报告）")
        return 2
    print("结论：无变化（全保留）")
    return 0


def main():
    args = sys.argv[1:]
    book_id = new_file = None
    dry = "--dry-run" in args
    for i, a in enumerate(args):
        if a == "--书号" and i + 1 < len(args):
            book_id = args[i + 1]
        elif a == "--新版" and i + 1 < len(args):
            new_file = args[i + 1]

    if not book_id or not new_file:
        print("用法：python update_book.py --书号 <书号> --新版 <文件路径> [--dry-run]")
        sys.exit(1)

    nf = Path(new_file)
    if not nf.is_absolute():
        nf = (Path.cwd() / nf).resolve()
    if not nf.exists():
        print(f"新版文件不存在：{nf}")
        sys.exit(1)

    try:
        sys.exit(dry_run(book_id, nf) if dry else execute(book_id, nf))
    except Exception as e:
        # M6-D4 修复：报告命名与其余报告统一用书名；书号反查失败（本就是异常成因之一）
        # 时才回落书号，保证任何情况下都有落盘位置
        try:
            title = resolve_book_title(book_id)
        except Exception:
            title = ""
        report_name = f"{title or book_id}_更新{'方案' if dry else '对账'}.md"
        common.write_report(report_name,
                            f"# 更新{'方案（dry-run）' if dry else '对账（手术）'}报告\n"
                            f"结论：🔴 失败\n原因：{e}\n")
        print(f"🔴 {'dry-run' if dry else '手术'}失败：{e}")
        sys.exit(1)


# ---------------------------------------------------------------- 自检

def _selfcheck():
    """自检（零真实库依赖、零子进程、零沙箱）：
    书号反查 / 老块解析 / 资产预扫 / 报告渲染 / 沙箱配置往返。"""
    import tempfile

    # ① 书号反查：临时知识库造两本假书（倒排表.json 头部带书号）
    with tempfile.TemporaryDirectory() as td:
        kb = Path(td) / "知识库"
        for title, bid in (("甲书", "0098"), ("乙书", "0099")):
            d = kb / "书籍" / title
            d.mkdir(parents=True)
            (d / "倒排表.json").write_text(
                json.dumps({"书号": bid, "构建时间": "2026-09-16", "条目": {}},
                           ensure_ascii=False), encoding="utf-8")
        assert resolve_book_title("0099", kb) == "乙书"
        assert resolve_book_title("0098", kb) == "甲书"
        try:
            resolve_book_title("0100", kb)
            raise AssertionError("未抛出书号不存在异常")
        except ValueError:
            pass
        print("  ① 书号反查：命中 2 本 + 未命中报错")

        # ② 资产预扫：造假摘要锚点表/评测集/学习记录
        sm = kb / "书籍" / "乙书" / "章节摘要"
        sm.mkdir()
        (sm / "01.md").write_text(
            "| 块ID | 页 | 指纹 |\n|---|---|---|\n| 0099·0001 | p.1 | 甲子 |\n| 0099·0002 | p.2 | 乙丑 |\n",
            encoding="utf-8")
        (sm / "02.md").write_text(
            "| 块ID | 页 | 指纹 |\n|---|---|---|\n| 0099·0003 | p.3 | 丙寅 |\n",
            encoding="utf-8")
        (kb / "评测集.md").write_text(
            "| 题号 | 提问 | 答案块ID | 命中 |\n|---|---|---|---|\n"
            "| Q1 | 何谓甲子 | 0099·0001 | ✅ |\n| Q2 | 他书之问 | 0001·0005 | ❌ |\n",
            encoding="utf-8")
        lr = kb / "学习记录" / "乙书"
        lr.mkdir(parents=True)
        (lr / "学习进度.md").write_text("读了 0099·0002 的内容", encoding="utf-8")
        assets = scan_asset_refs("0099", "乙书", kb)
        assert assets["锚点"] == {"0099·0001": "01", "0099·0002": "01", "0099·0003": "02"}
        assert assets["评测题"] == ["Q1"]
        assert assets["学习记录"] == [str(Path("学习记录") / "乙书" / "学习进度.md")]
        # 他书块 ID（0001·0005）不误伤 ✅（评测题 Q2 未计入）
        chs = affected_chapters(assets, ["0099·0002"], ["0099·0003"])
        assert chs == ["01", "02"], chs
        print("  ② 资产预扫：锚点 3 / 评测 1 / 学习 1，章归位 01+02")

    # ③ 块解析（带双行流）：临时书目录造 原文.md + 检索副本.md
    with tempfile.TemporaryDirectory() as td:
        kb = Path(td) / "知识库"
        d = kb / "书籍" / "测试书"
        d.mkdir(parents=True)
        orig = ("【0007·0001】p.1\n甲块首行内容\n\n【0007·0002】p.2\n乙块首行内容\n第二行\n\n"
                "【0007·0003】p.3\n丙块首行内容\n")
        copy = orig  # 自检用同文（normalize 不影响块结构解析）
        (d / "原文.md").write_text(orig, encoding="utf-8")
        (d / "检索副本.md").write_text(copy, encoding="utf-8")
        blocks = load_blocks_with_lines("测试书", kb)
        assert [b["块ID"] for b in blocks] == ["0007·0001", "0007·0002", "0007·0003"]
        assert blocks[1]["原文行"] == ["乙块首行内容", "第二行", ""]
        assert blocks[1]["副本行"] == ["乙块首行内容", "第二行", ""]
        # 段流展开：块内空行分界
        paras = block_diff.expand_paras(blocks)
        assert len(paras) == 3
        assert paras[1]["比对文本"] == "乙块首行内容第二行"
        # 沙箱新块模式
        new_blocks = load_blocks_with_lines("测试书", kb, new_mode=True)
        assert [b["序号"] for b in new_blocks] == [1, 2, 3]
        print("  ③ 块解析：3 块物理序 + 双行流 + 段流展开（新块模式同构）")

    # ④ 报告渲染：段级 result 结构 + 领号段 + 资产统计 + 无销单指引
    result = {
        "保留": ["0007·0001", "0007·0002", "0007·0003"],
        "修改块": [], "退休块": [],
        "新块预演": [],
        "统计": {"老块数": 3, "新块数": 3, "保留": 3, "修改": 0, "退休": 0, "新块": 0},
    }
    new_ids = block_diff.assign_new_ids(
        [{"块ID": f"0007·{i:04d}"} for i in range(1, 4)], result)
    assets = {"锚点": {}, "评测题": [], "学习记录": []}
    report = build_plan_report("测试书", "0007", Path("x/新版.txt"), result,
                               new_ids, assets, [], "（沙箱日志）", 0.6)
    assert "保留块（零触碰" in report and "3" in report
    assert "未改动任何库内文件" in report
    assert "问题解决后请删除本文件" not in report  # 非问题清单，不带销单指引
    # 有变化场景：修改块表 + 重组新块表 + 受影响章
    r2 = {
        "保留": ["0007·0002"],
        "修改块": [("0007·0001", 0.9)],
        "退休块": ["0007·0003"],
        "新块预演": [{"段": ["修改后的段落文本…"], "来源": "修改块"}],
        "统计": {"老块数": 3, "新块数": 2, "保留": 1, "修改": 1, "退休": 1, "新块": 1},
    }
    old_ids = [{"块ID": f"0007·{i:04d}"} for i in range(1, 4)]
    ids2 = block_diff.assign_new_ids(old_ids, r2)
    rep2 = build_plan_report("测试书", "0007", Path("x/新版.txt"), r2, ids2,
                             {"锚点": {"0007·0001": "01"}, "评测题": [], "学习记录": []},
                             ["01"], "（沙箱日志）", 0.6)
    assert "0007·0004" in rep2  # 预演领号出现
    assert "待重标" in rep2 and "01" in rep2
    assert "0007·0003" in rep2 and "0.9" in rep2
    print("  ④ 报告渲染：总览/领号段/四分类/资产统计，无销单指引")

    # ⑤ 沙箱配置往返：SANDBOX_KEYS 全键改写 → 恢复 → 配置内容逐字节一致（不跑子进程）
    cfg_path = common.PROJECT_ROOT / "路径配置.json"
    before = cfg_path.read_text(encoding="utf-8")
    with sandbox_ctx():
        during = json.loads(cfg_path.read_text(encoding="utf-8"))
        for k in SANDBOX_KEYS:
            assert k in during, f"沙箱键 {k} 不在 路径配置.json（沙箱覆盖不全，#43）"
            assert str(during[k]).startswith(str(Path(tempfile.gettempdir()))), during[k]
    after = cfg_path.read_text(encoding="utf-8")
    assert before == after, "沙箱恢复后配置与改写前不一致"
    # 异常路径恢复：with 内抛异常也必须恢复
    try:
        with sandbox_ctx():
            raise RuntimeError("模拟中断")
    except RuntimeError:
        pass
    assert cfg_path.read_text(encoding="utf-8") == before, "异常退出后配置未恢复"
    print(f"  ⑤ 沙箱配置往返：{len(SANDBOX_KEYS)} 键改写（含 #43 新增 已入库）/恢复/异常恢复，配置零残留")

    # ⑥ M3 装配：保留块照搬 + 新块领号/页标签推导 + 格式与 clean_split 同款（块间空行、无尾空行）
    old_raw6 = {
        "0007·0001": ["【0007·0001】p.1", "甲块内容"],
        "0007·0002": ["【0007·0002】p.2", "乙块内容"],
    }
    plan6 = [
        {"类型": "保留", "块ID": "0007·0001"},
        {"类型": "新块", "段": [
            {"原文文本": "新段一\n折行二", "页标签": "2", "新段序号": 2, "原子": False},
            {"原文文本": "新段二", "页标签": "3", "新段序号": 3, "原子": False},
        ]},
        {"类型": "保留", "块ID": "0007·0002"},
    ]
    orig6, copy6 = assemble_book(plan6, old_raw6, [(1, "0007·0003")])
    assert orig6 == ["【0007·0001】p.1", "甲块内容", "",
                     "【0007·0003】p.2~3", "新段一", "折行二", "", "新段二", "",
                     "【0007·0002】p.2", "乙块内容"], orig6
    assert copy6[3] == "【0007·0003】p.2~3"  # ID 行副本原样
    assert copy6[4] == common.normalize_for_search("新段一")
    assert _derive_label([{"页标签": "5"}, {"页标签": "5"}]) == "5"
    assert _derive_label([{"页标签": "5~6"}, {"页标签": "7"}]) == "5~7"
    assert _derive_label([{"页标签": ""}]) == "1"
    print("  ⑥ 装配：保留照搬 + 领号 + 页标签推导 + clean_split 同款格式")

    # ⑦ M3 对账：一致通过 / 篡改行检出 / 保留块零触碰验证 / 领号非法检出
    keep7 = ["0007·0001", "0007·0002"]
    ok7, det7 = reconcile(orig6, orig6, keep7, old_raw6, [(1, "0007·0003")], 2)
    assert ok7, det7
    bad_b = list(orig6)
    bad_b[1] = "甲块内容被篡改"
    ok7b, det7b = reconcile(orig6, bad_b, keep7, old_raw6, [(1, "0007·0003")], 2)
    assert not ok7b and "分歧行 1" in det7b[0], det7b
    bad_keep = list(orig6)
    bad_keep[10] = "乙块内容被改动"
    ok7c, det7c = reconcile(bad_keep, bad_keep, keep7, old_raw6, [(1, "0007·0003")], 2)
    assert not ok7c and "零触碰" in det7c[1] and "0007·0002" in det7c[1], det7c
    ok7d, det7d = reconcile(orig6, orig6, keep7, old_raw6, [(1, "0007·0004")], 2)
    assert not ok7d and "领号非法" in det7d[2], det7d
    print("  ⑦ 对账：三重一致通过 + 篡改/触碰/领号异常各被检出")

    # ⑧ M3 退休登记册：新建 + 追加合并
    with tempfile.TemporaryDirectory() as td:
        bd = Path(td) / "乙书"
        bd.mkdir()
        write_retire_registry(bd, [{"块ID": "0099·0002", "日期": "2026-09-16",
                                    "原因": "修改", "后继": "0099·0005", "相似度": 0.9}])
        write_retire_registry(bd, [{"块ID": "0099·0003", "日期": "2026-09-16",
                                    "原因": "删除", "后继": None, "相似度": None}])
        reg = json.loads((bd / "退休块.json").read_text(encoding="utf-8"))
        assert len(reg["退休块"]) == 2 and reg["退休块"][0]["后继"] == "0099·0005"
        assert reg["退休块"][1]["原因"] == "删除" and reg["退休块"][1]["后继"] is None
    print("  ⑧ 退休登记册：新建 + 追加合并")

    # ⑨ M3 快照/回滚：书目录 + 书目总索引整体往返
    with tempfile.TemporaryDirectory() as td:
        kb9 = Path(td) / "知识库"
        bd9 = kb9 / "书籍" / "测试书"
        bd9.mkdir(parents=True)
        (bd9 / "原文.md").write_text("原文甲", encoding="utf-8")
        (kb9 / "书目总索引.md").write_text("索引甲", encoding="utf-8")
        snap, cat = _snapshot_book(bd9, kb9, backup_base=Path(td) / "备份")
        assert (snap / "原文.md").read_text(encoding="utf-8") == "原文甲"
        (bd9 / "原文.md").write_text("原文乙", encoding="utf-8")
        (bd9 / "退休块.json").write_text("{}", encoding="utf-8")
        (kb9 / "书目总索引.md").write_text("索引乙", encoding="utf-8")
        _rollback(bd9, snap, kb9, cat)
        assert (bd9 / "原文.md").read_text(encoding="utf-8") == "原文甲"
        assert not (bd9 / "退休块.json").exists()
        assert (kb9 / "书目总索引.md").read_text(encoding="utf-8") == "索引甲"
    print("  ⑨ 快照/回滚：书目录+书目总索引整体往返（新增文件被清除）")

    # ⑩ M4 变化章待重标：已标注章 → .done/旧回填/旧章包清除 + 新包只含当前块；未标注章跳过
    with tempfile.TemporaryDirectory() as td:
        kb10 = Path(td) / "知识库"
        bd10 = kb10 / "书籍" / "自检更新书"
        bd10.mkdir(parents=True)
        (bd10 / "原文.md").write_text(
            "【0055·0001】p.1\n第一章内容甲\n\n【0055·0002】p.2\n第二章内容乙\n", encoding="utf-8")
        (bd10 / "检索副本.md").write_text(
            "【0055·0001】p.1\n第一章内容甲\n\n【0055·0002】p.2\n第二章内容乙\n", encoding="utf-8")
        (bd10 / "目录骨架.md").write_text(
            "# 《自检更新书》目录骨架\n书号：0055 | 总块数：2\n\n"
            "| 章号 | 章名 | 起始块ID | 块数 |\n|---|---|---|---|\n"
            "| 01 | 第一章 | 0055·0001 | 1 |\n| 02 | 第二章 | 0055·0002 | 1 |\n",
            encoding="utf-8")
        (bd10 / "章节摘要").mkdir()
        (bd10 / "章节摘要" / "01.md").write_text("# 01 第一章\n", encoding="utf-8")
        pdir10 = common.get_path("输出") / "任务包" / "自检更新书"
        try:
            back10 = pdir10 / "回填"
            back10.mkdir(parents=True)
            (back10 / "01.done").write_text("整章", encoding="utf-8")
            (back10 / "01_任务包.md").write_text("旧回填", encoding="utf-8")
            (pdir10 / "01_任务包.md").write_text("旧章包", encoding="utf-8")
            redone, skipped, missing = remark_chapters_for_reannotation(
                "自检更新书", kb10, {"01", "02"})
            assert redone == ["01"] and skipped == ["02"] and missing == [], (redone, skipped, missing)
            assert not (back10 / "01.done").exists(), ".done 未清除"
            assert not (back10 / "01_任务包.md").exists(), "旧回填未清除"
            new_pack = (pdir10 / "01_任务包.md").read_text(encoding="utf-8")
            assert "0055·0001" in new_pack and "旧章包" not in new_pack, "新包内容异常"
            assert (pdir10 / "_清单.md").exists(), "清单未刷新"
            chs10 = _chapters_of_new_blocks("自检更新书", kb10, ["0055·0002"])
            assert chs10 == {"02"}, chs10
            # M6-D3：新骨架中不存在的章号 → 跳过（不产包、不抛错）
            r3, s3, m3 = remark_chapters_for_reannotation("自检更新书", kb10, {"99"})
            assert r3 == [] and s3 == [] and m3 == ["99"], (r3, s3, m3)
        finally:
            shutil.rmtree(pdir10, ignore_errors=True)
    print("  ⑩ 变化章待重标：已标注章重置+只产变化章，未标注章跳过；新块归章正确；"
          "骨架中不存在的章号跳过不报错")

    # ⑪ M6-D1 修复：结构.json 章锚点重基（术后块 ID 有跳跃时按标题文本+出现顺序重定位）
    with tempfile.TemporaryDirectory() as td:
        kb11 = Path(td) / "知识库"
        cache11 = Path(td) / "提取缓存" / "0060"
        bd11 = kb11 / "书籍" / "自检重基书"
        bd11.mkdir(parents=True)
        (bd11 / "原文.md").write_text(
            "【0060·0001】p.1\n第一章 甲篇\n正文甲\n\n"
            "【0060·0004】p.2\n第二章 乙篇\n正文乙\n\n"
            "【0060·0005】p.3\n正文丙\n", encoding="utf-8")
        (bd11 / "结构.json").write_text(json.dumps([
            {"级别": 0, "标题": "第一章 甲篇", "块ID": "0060·0001", "匹配方式": "文本+顺序"},
            {"级别": 0, "标题": "第二章 乙篇", "块ID": "0060·0002", "匹配方式": "文本+顺序"},
        ], ensure_ascii=False, indent=2), encoding="utf-8")
        cache11.mkdir(parents=True)
        (cache11 / "结构信号.jsonl").write_text(
            json.dumps({"级别": 0, "标题": "第一章 甲篇"}, ensure_ascii=False) + "\n"
            + json.dumps({"级别": 0, "标题": "第二章 乙篇"}, ensure_ascii=False) + "\n",
            encoding="utf-8")
        info11 = rebase_structure_json("0060", "自检重基书", kb11, cache11)
        assert info11 == {"总数": 2, "条目": 2, "变更": 1, "丢失": 0}, info11
        after11 = {e["标题"]: e["块ID"] for e in json.loads(
            (bd11 / "结构.json").read_text(encoding="utf-8"))}
        assert after11 == {"第一章 甲篇": "0060·0001", "第二章 乙篇": "0060·0004"}, after11
        # 无结构信号（无信号书）→ 不动 结构.json
        (cache11 / "结构信号.jsonl").unlink()
        assert rebase_structure_json("0060", "自检重基书", kb11, cache11) is None
        assert {e["标题"]: e["块ID"] for e in json.loads(
            (bd11 / "结构.json").read_text(encoding="utf-8"))} == after11
    print("  ⑪ 结构锚点重基：术后块 ID 跳跃时按标题文本+顺序重定位（无信号书不动）")

    print("update_book 自检通过：反查/解析/预扫/渲染/沙箱/装配/对账/登记册/回滚/待重标/锚点重基 "
          "×11 组用例")


if __name__ == "__main__":
    common.run_or_selfcheck(main, _selfcheck)
