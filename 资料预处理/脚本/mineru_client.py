# -*- coding: utf-8 -*-
"""MinerU Standard API OCR 客户端（MinerU OCR 接入-落地设计文档 M1+M2）。

M1：申请上传链接 → PUT 上传 PDF → 落盘小纸条（24h 续跑）→ 轮询 → 下载 zip → 解包。
    超过 MINERU_PAGE_LIMIT 页的书自动本地分卷：PyMuPDF 切 ≤200 页分卷，单批次多文件
    上传，逐卷下载 zip 后按 page_idx 偏移合并（图片冲突重命名、full.md 拼接）再转换。
    下载原子落盘（.part 临时文件 + 改名），坏 zip 缓存自动识别删除并重下（#31）。
M2：以 content_list.json 为唯一忠实来源转出 页_NNN.txt，图片跟书走，忠实度自检。

退出码：0=干净；1=任务失败/zip 缺 content_list/忠实度不符/配置缺失；2=空页清单或图数对账不符。
红线：路径经 common.load_config()；UTF-8；header/footer/page_number 块不删除，按页写入隔离区缓存
      （输出\\提取缓存\\{书号}\\隔离区\\页_NNN.txt，行前缀 ⟦类型⟧），主缓存只含正文。
"""

import hashlib
import json
import re
import shutil
import sys
import time
import zipfile
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path

import httpx

import common

API_BASE = "https://mineru.net/api/v4"
TOKEN_ENV = "MINERU_API_TOKEN"
CONFIG_TOKEN_KEY = "mineru_api_token"
STICKY_TTL_HOURS = 24
MINERU_PAGE_LIMIT = 200
POLL_INIT_SEC = 3
POLL_MAX_SEC = 30
DISCARD_TYPES = ("header", "footer", "page_number")
IMG_TAG_PATTERN = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
MD_TABLE_SEP_PATTERN = re.compile(r"(?m)^[ \t]*\|(?:[ \t]*:?-{2,}:?[ \t]*\|)+[ \t]*$")
HTML_TAG_PATTERN = re.compile(r"</?[^>]+>")
CELL_NUM_HAN_PATTERN = re.compile(r"^(\s*\d[\d\s]*?)\s*([一二三四五六七八九十])\s*$")
PAN_CELL_PATTERN = re.compile(r"^[.·,]*\d[\d\s,.]*\d[.·,]*\s*[一二三四五六七八九十]$")
PAN_TABLE_MIN_ROWS = 6
API_ERROR_HINTS = {
    "A0202": "MinerU Token 无效，请到控制台设置页重新填写并保存",
    "A0211": "MinerU Token 已过期，请到控制台设置页重新填写并保存",
}


def get_token() -> str:
    """B3：环境变量 MINERU_API_TOKEN 优先，回落 路径配置.json 的 api.mineru_api_token。"""
    env_val = __import__("os").environ.get(TOKEN_ENV, "").strip()
    if env_val:
        return env_val
    try:
        cfg = common.load_config()
        return str(cfg.get("api", {}).get(CONFIG_TOKEN_KEY, "") or "").strip()
    except Exception:
        return ""


def mask_key(key: str) -> str:
    if len(key) > 8:
        return f"{key[:4]}…{key[-2:]}"
    return "已设置" if key else "未设置"


def _err_hint(err_code) -> str:
    code = str(err_code)
    if code in API_ERROR_HINTS:
        return API_ERROR_HINTS[code]
    hints = {
        "-60018": "今日 MinerU 解析额度已用尽，明日再跑；重跑时小纸条会自动续上，已下载的 zip 不重复烧额度",
        "-60005": "文件超过 MinerU 单文件 200MB 上限，请拆分成多个 PDF 分别入库",
        "-60006": "文件页数超过 MinerU 单任务 200 页上限（正常应自动分卷，请反馈此现象）",
        "-60002": "MinerU 不支持该文件格式，请确认扩展名正确",
        "-60003": "文件读取失败，请检查文件是否损坏",
        "-60004": "文件为空，请上传有效文件",
        "-60010": "MinerU 解析失败，请稍后重试",
        "-10001": "MinerU 服务异常，请稍后重试",
    }
    return hints.get(code, f"MinerU 错误码 {err_code}，请稍后重试或联系技术支持")


def _request(method: str, url: str, token: str, json_body=None) -> dict:
    """统一 HTTP 请求；兼容业务信封与网关失败两种响应形状。"""
    headers = {"Authorization": f"Bearer {token}", "Accept": "*/*"}
    if json_body is not None:
        headers["Content-Type"] = "application/json"
    try:
        with httpx.Client(timeout=120) as client:
            resp = client.request(method, url, headers=headers, json=json_body)
    except httpx.TimeoutException:
        raise RuntimeError("网络请求超时，请检查网络/代理后重试")
    except httpx.HTTPError as e:
        raise RuntimeError(f"网络请求失败：{e}")
    try:
        data = resp.json()
    except Exception:
        raise RuntimeError(f"MinerU 返回非 JSON（HTTP {resp.status_code}）")
    if isinstance(data, dict) and data.get("success") is False:
        msg_code = data.get("msgCode", "")
        if msg_code:
            raise RuntimeError(_err_hint(msg_code))
    if data.get("code") not in (0, None):
        raise RuntimeError(_err_hint(data.get("code")))
    return data


def _sticky_dir(book_id: str) -> Path:
    return common.get_path("输出") / "提取缓存" / book_id / "mineru"


def _sticky_path(book_id: str) -> Path:
    return _sticky_dir(book_id) / "batch.json"


def _load_sticky(book_id: str) -> dict:
    p = _sticky_path(book_id)
    if not p.exists():
        return {}
    try:
        sticky = json.loads(p.read_text(encoding="utf-8"))
        age_h = (time.time() - float(sticky.get("提交时间戳", 0))) / 3600
        if age_h > STICKY_TTL_HOURS:
            return {}
        return sticky
    except Exception:
        return {}


def _save_sticky(book_id: str, sticky: dict):
    sticky.setdefault("提交时间戳", time.time())
    p = _sticky_path(book_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(sticky, ensure_ascii=False, indent=2), encoding="utf-8")


def _clear_sticky(book_id: str):
    p = _sticky_path(book_id)
    if p.exists():
        p.unlink()


def _last_page_count(pdf_path: Path) -> int:
    try:
        import fitz
    except ImportError:
        raise RuntimeError("缺少 pymupdf 依赖，请重新双击 启动控制台.vbs（缺依赖会自动安装）")
    with fitz.open(str(pdf_path)) as doc:
        return len(doc)


def _split_pdf(pdf_path: Path, dest_dir: Path, chunk_pages: int = MINERU_PAGE_LIMIT) -> list:
    """把 PDF 切成 ≤chunk_pages 页的分卷。返回 [(分卷路径, 起始页偏移)]，按页序排列。"""
    try:
        import fitz
    except ImportError:
        raise RuntimeError("缺少 pymupdf 依赖，请重新双击 启动控制台.vbs（缺依赖会自动安装）")
    dest_dir.mkdir(parents=True, exist_ok=True)
    chunks = []
    with fitz.open(str(pdf_path)) as src:
        total = len(src)
        for start in range(0, total, chunk_pages):
            end = min(start + chunk_pages, total)
            out = dest_dir / f"分卷_{start + 1:04d}-{end:04d}.pdf"
            with fitz.open() as part:
                part.insert_pdf(src, from_page=start, to_page=end - 1)
                part.save(str(out))
            chunks.append((out, start))
    return chunks


def _dedup_arcname(name: str, used: dict) -> str:
    """zip 成员名冲突时生成新名：images/a.jpg → images/a_2.jpg（递增直到空闲）。"""
    stem, dot, suffix = name.rpartition(".")
    i = 2
    while True:
        cand = f"{stem}_{i}{dot}{suffix}" if dot else f"{name}_{i}"
        if cand not in used:
            return cand
        i += 1


def _merge_zips(chunk_zips: list, out_path: Path):
    """把多个分卷结果 zip 合并为一个：page_idx 加偏移、图片冲突重命名（同步改写
    img_path 与 full.md 引用）、full.md 顺序拼接。合并产物结构与单卷 zip 一致，
    交由 convert_zip 照常转换。"""
    merged = []
    full_parts = []
    name_hash = {}
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zout:
        for zip_path, offset in chunk_zips:
            with zipfile.ZipFile(zip_path) as zf:
                names = zf.namelist()
                cl_name = None
                md_name = None
                for n in names:
                    base = n.rsplit("/", 1)[-1]
                    if "content_list_v2" in base:
                        continue
                    if "content_list" in base and base.endswith(".json") and cl_name is None:
                        cl_name = n
                    elif base == "full.md":
                        md_name = n
                if cl_name is None:
                    raise RuntimeError(f"{zip_path.name} 中缺少 content_list.json，无法合并（退出码 1）")
                blocks = json.loads(zf.read(cl_name).decode("utf-8"))
                full_md = zf.read(md_name).decode("utf-8") if md_name else ""
                skip = {cl_name, md_name}
                rename = {}
                for n in names:
                    if n in skip or n.endswith("/"):
                        continue
                    base = n.rsplit("/", 1)[-1]
                    if "content_list" in base:
                        continue
                    data = zf.read(n)
                    h = hashlib.sha1(data).hexdigest()
                    if n in name_hash:
                        if name_hash[n] == h:
                            continue
                        new_name = _dedup_arcname(n, name_hash)
                        rename[n] = new_name
                        name_hash[new_name] = h
                        zout.writestr(new_name, data)
                    else:
                        name_hash[n] = h
                        zout.writestr(n, data)
                for b in blocks:
                    if not isinstance(b, dict):
                        merged.append(b)
                        continue
                    b = dict(b)
                    if b.get("page_idx") is not None:
                        b["page_idx"] = b["page_idx"] + offset
                    img = b.get("img_path", "")
                    if img and img in rename:
                        b["img_path"] = rename[img]
                    merged.append(b)
                for old, new in rename.items():
                    full_md = full_md.replace(f"]({old})", f"]({new})")
                full_parts.append(full_md)
        zout.writestr("content_list.json", json.dumps(merged, ensure_ascii=False))
        zout.writestr("full.md", "\n\n".join(full_parts))


class _HtmlTableParser(HTMLParser):
    """解析 MinerU 的 HTML 表格为行列文本；含 colspan/rowspan 或嵌套表则标 irregular。

    convert_charrefs=False：实体（&amp; 等）按原文保留，保证内容字符零增删。"""

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.rows = []
        self.irregular = False
        self._table_depth = 0
        self._row = None
        self._cell = None

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._table_depth += 1
            if self._table_depth > 1:
                self.irregular = True
        elif tag == "tr":
            if self._table_depth > 1 or self._cell is not None:
                self.irregular = True
            self._row = []
        elif tag in ("td", "th"):
            if self._table_depth > 1:
                self.irregular = True
            attrs = dict(attrs)
            if attrs.get("colspan") not in (None, "1") or attrs.get("rowspan") not in (None, "1"):
                self.irregular = True
            if self._row is None:
                self._row = []
            self._cell = []
        elif tag == "br" and self._cell is not None:
            self._cell.append("<br>")

    def handle_startendtag(self, tag, attrs):
        if tag == "br" and self._cell is not None:
            self._cell.append("<br>")

    def handle_endtag(self, tag):
        if tag == "tr":
            if self._row is not None:
                self.rows.append(self._row)
            self._row = None
        elif tag in ("td", "th"):
            if self._cell is not None:
                if self._row is None:
                    self._row = []
                self._row.append("".join(self._cell).strip())
            self._cell = None
        elif tag == "table":
            self._table_depth -= 1

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)

    def handle_entityref(self, name):
        if self._cell is not None:
            self._cell.append(f"&{name};")

    def handle_charref(self, name):
        if self._cell is not None:
            self._cell.append(f"&#{name};")


def _looks_like_html_table(body: str) -> bool:
    low = body.lower()
    return "<table" in low or "<tr" in low or "<td" in low or "<th" in low


def _format_cell(cell: str) -> str:
    """盘单元格观感对齐老通道（docx）：数字组与末尾汉字之间插 <br>（只会加 markup，内容字符零增删）。"""
    return CELL_NUM_HAN_PATTERN.sub(r"\1<br>\2", cell)


def _is_pan_table(rows: list) -> bool:
    """判「多盘合表」：行数 ≥6 且为 3 的倍数，且每个单元格都是盘式（≥2 位数字 + 末尾汉字）。

    MinerU 有时把相邻两/三个 3×3 盘并成一张 6/9 行表；命中则可拆回逐盘独立表。"""
    if len(rows) < PAN_TABLE_MIN_ROWS or len(rows) % 3 != 0:
        return False
    return all(PAN_CELL_PATTERN.match(c) for row in rows for c in row)


def _html_table_to_md(body: str, rules: dict = None):
    """规则 HTML 表（无合并单元格、行列整齐）→ Markdown 表；不规则/异常返回 None。

    `rules` 为按书特化开关（书规则\\{书名}.json）；缺省即全部关闭（通用行为）：
    - `排盘_单元格加换行`：单元格「数字组+末尾汉字」补 <br>；
    - `排盘_多盘合表拆分`：多盘式合表按每 3 行拆成多张表（空行分隔，各成独立段落/块）。"""
    rules = rules or {}
    add_br = bool(rules.get(common.RULE_CELL_BR))
    split_pan = bool(rules.get(common.RULE_PAN_SPLIT))
    parser = _HtmlTableParser()
    try:
        parser.feed(body)
        parser.close()
    except Exception:
        return None
    if parser.irregular or not parser.rows:
        return None
    width = len(parser.rows[0])
    if width < 1 or any(len(row) != width for row in parser.rows):
        return None
    cells = [c for row in parser.rows for c in row]
    if any("\n" in c or "|" in c for c in cells):
        return None
    fmt = _format_cell if add_br else (lambda c: c)
    groups = ([parser.rows[i:i + 3] for i in range(0, len(parser.rows), 3)]
              if (split_pan and _is_pan_table(parser.rows)) else [parser.rows])
    parts = []
    for rows in groups:
        md = ["| " + " | ".join(fmt(c) for c in rows[0]) + " |",
              "|" + "---|" * width]
        for row in rows[1:]:
            md.append("| " + " | ".join(fmt(c) for c in row) + " |")
        parts.append("\n".join(md))
    return "\n\n".join(parts)


def _render_block(block: dict, warnings: list, rules: dict = None) -> str:
    t = block.get("type", "")
    if t == "text":
        # #20：标题级别不再以 Markdown `#` 前缀混入原文层，级别信息改由 结构信号.jsonl 独立传递
        return block.get("text", "")
    if t == "table":
        # #17：规则 HTML 表转 Markdown 表（命中 clean_split 原子段/图表块体系）。
        # 输出顺序固定「表体 → 题注 → 脚注」：表体必须置于段首，段落（块）才以 `| ` 开头，
        # 方能命中 clean_split 的原子段判定与折行豁免。table 块不写【图】行（无人消费，
        # 且系"往原文层加行"，去掉更贴合原文层逐字转录红线）；图片引用仅 image/chart 分支保留。
        # 盘式特化（<br>、多盘拆表）由 rules 开关驱动，缺省关闭。
        out = []
        body = block.get("table_body", "")
        if body:
            md = _html_table_to_md(body, rules)
            if md is not None:
                out.append(md)
            else:
                if _looks_like_html_table(body):
                    snippet = body[:40] + ("…" if len(body) > 40 else "")
                    warnings.append(f"表格转 Markdown 失败或不规则，保留 HTML 原样：{snippet}")
                out.append(body)
        for cap in block.get("table_caption") or []:
            out.append(cap)
        for fn in block.get("table_footnote") or []:
            out.append(fn)
        return "\n".join(out)
    if t in ("image", "chart"):
        out = []
        img = block.get("img_path", "")
        if img:
            out.append(f"【图】图片/{Path(img).name}")
        out.extend(block.get("image_caption") or [])
        out.extend(block.get("image_footnote") or [])
        return "\n".join(out)
    if t == "equation":
        return block.get("text", "")
    if t in ("list", "code"):
        if t == "code":
            out = []
            for cap in block.get("code_caption") or []:
                out.append(cap)
            body = block.get("code_body", "")
            if body:
                out.append(body)
            for fn in block.get("code_footnote") or []:
                out.append(fn)
            return "\n".join(out)
        text = block.get("text", "")
        if not text:
            items = block.get("items") or block.get("list") or []
            if isinstance(items, list):
                text = "\n".join(str(i) for i in items)
        return text
    text = block.get("text", "")
    if text:
        return text
    warnings.append(f"跳过未知类型块：type={t}（字段结构待首日实测校准）")
    return ""


def _normalize_fidelity(text: str) -> str:
    # #20：产物已撤 `#` 前缀，full.md 仍带 Markdown 标题标记，比对前双侧对称剥除
    text = re.sub(r"(?m)^#{1,6}\s*", "", text)
    text = IMG_TAG_PATTERN.sub(lambda m: f"【图】图片/{Path(m.group(1)).name}", text)
    text = text.replace("【图】图片\\", "【图】图片/")
    # #17：渲染层把 HTML 表转 Markdown 表，与 full.md 的 markup 必然不同 → 双侧对称剥除
    # markup（md 表格分隔行与竖线、HTML 标签），只比内容字符
    text = MD_TABLE_SEP_PATTERN.sub("", text)
    text = HTML_TAG_PATTERN.sub("", text)
    text = text.replace("|", "")
    # #17：table 块补的【图】引用在 full.md 中无对应（MinerU 只给 image 块带图引用），
    # 且图片正确性由图对账/图文抽检保证 → 双侧对称剥除【图】引用，忠实度只比内容字符
    text = re.sub(r"【图】图片/[^\s\]]+", "", text)
    return re.sub(r"\s", "", text)


def convert_zip(zip_path: Path, *, images_dir: Path, pdf_page_count: int = None,
                book_id: str = None, book_title: str = None, rules: dict = None) -> dict:
    """把 MinerU 结果 zip 转为 页_NNN 文本（内存产物 + 图片落盘）。

    header/footer/page_number 块不丢弃，按页渲染为隔离区缓存（book_id 提供时
    写 输出\\提取缓存\\{book_id}\\隔离区\\页_NNN.txt，每行带 ⟦类型⟧ 前缀）。

    rules：按书特化开关；缺省按 book_title 从 `书规则\\{书名}.json` 读取
    （无书名/无文件 → 空 dict = 全部特化关闭）。

    返回 {code, products:[(文件名,文本)], report:dict}。
    """
    if rules is None:
        rules = common.load_book_rules(book_title or "")
    report = {"页数": 0, "空页清单": [], "隔离辅助块": 0, "隔离辅助块页数": 0, "图对账": {}, "忠实度": {}, "警告": []}
    try:
        with zipfile.ZipFile(zip_path) as zf:
            infos = zf.infolist()
            cl_name = None
            md_name = None
            for info in infos:
                name = info.filename
                base = name.rsplit("/", 1)[-1]
                if "content_list_v2" in base:
                    continue
                if "content_list" in base and base.endswith(".json") and cl_name is None:
                    cl_name = name
                elif base == "full.md":
                    md_name = name
            if cl_name is None:
                raise RuntimeError("zip 中缺少 content_list.json，无法转换（退出码 1）")
            content = json.loads(zf.read(cl_name).decode("utf-8"))
            full_md = zf.read(md_name).decode("utf-8") if md_name else ""
    except zipfile.BadZipFile:
        # #31 人话兜底：正常路径已被 _zip_ok 拦截，此处防命令行 --从zip 传入坏文件等漏网场景
        raise RuntimeError(f"{zip_path.name} 不是有效的解析结果文件（文件损坏或下载不完整）") from None

    if not isinstance(content, list):
        raise RuntimeError("content_list.json 结构异常：应为块数组")
    pages = {}
    discard_pages = {}
    max_page = -1
    prev_page = -1
    declared_imgs = []   # (文件名, 类型)
    isolated = 0
    for idx, block in enumerate(content):
        if not isinstance(block, dict):
            continue
        t = block.get("type", "")
        if t in DISCARD_TYPES:
            isolated += 1
            pid = block.get("page_idx")
            if pid is not None:
                discard_pages.setdefault(pid, []).append(block)
            continue
        pid = block.get("page_idx")
        if pid is None:
            report["警告"].append(f"第 {idx} 块缺少 page_idx，已跳过（待实测校准）")
            continue
        if pid < prev_page:
            raise RuntimeError(f"content_list 页号单调性断言失败：{prev_page} → {pid}")
        prev_page = pid
        pages.setdefault(pid, []).append(block)
        if pid > max_page:
            max_page = pid
        img = block.get("img_path", "")
        if img:
            declared_imgs.append((Path(img).name, t))

    if pdf_page_count is not None and max_page + 1 > pdf_page_count:
        raise RuntimeError(
            f"页号断言失败：content_list 最大页号 {max_page + 1} 大于 PDF 总页数 {pdf_page_count}"
        )

    if images_dir.exists():
        shutil.rmtree(images_dir)
    images_dir.mkdir(parents=True, exist_ok=True)
    products = []
    empty_pages = []
    isolated_files = []
    for pid in range(0, max_page + 1):
        iso_blocks = discard_pages.get(pid, [])
        if iso_blocks:
            iso_lines = []
            for b in iso_blocks:
                rendered = _render_block(b, report["警告"], rules)
                if rendered.strip():
                    iso_lines.append(f"⟦{b.get('type', '')}⟧ {rendered}".rstrip())
            if iso_lines:
                isolated_files.append((f"页_{pid + 1:03d}.txt", "\n".join(iso_lines)))
        blocks = pages.get(pid, [])
        if not blocks:
            empty_pages.append(pid + 1)
            continue
        text_parts = []
        for block in blocks:
            img = block.get("img_path", "")
            if img:
                src = _find_zip_file(zip_path, img)
                if src is None:
                    report["图对账"].setdefault("缺图", []).append(img)
                else:
                    shutil.copy(src, images_dir / Path(img).name)
            rendered = _render_block(block, report["警告"], rules)
            if rendered:
                text_parts.append(rendered)
        # #17：块之间以空行分隔 —— clean_split 按空行切段落，块=段落方可让 md 表成为"原子段"
        # （用单换行会把整页粘成一段，表既认不出、又会被 80 字折行劈开）
        page_text = "\n\n".join(text_parts)
        if not page_text.strip():
            empty_pages.append(pid + 1)
            continue
        products.append((f"页_{pid + 1:03d}.txt", page_text))

    # #20 结构信号（原料级）：标题块级别/文本走独立通道，供 clean_split 换算为 结构.json
    signals = []
    for pid in range(0, max_page + 1):
        for order, block in enumerate(pages.get(pid, []), start=1):
            if block.get("type") != "text":
                continue
            level = int(block.get("text_level") or 0)
            if level <= 0:
                continue
            title = block.get("text", "")
            if not title.strip():
                continue
            signals.append({"级别": level, "标题": title,
                            "来源文件": f"页_{pid + 1:03d}.txt", "来源序号": order})

    report["页数"] = len(pages)
    report["空页清单"] = empty_pages
    report["隔离辅助块"] = isolated
    report["隔离辅助块页数"] = len(isolated_files)
    received_img_count = len([p for p in images_dir.rglob("*") if p.is_file()])
    report["图对账"] = {
        "声明图数": len(declared_imgs),
        "实收图数": received_img_count,
        "缺图": report["图对账"].get("缺图", []),
    }

    prod_text = "\n".join(t for _, t in products)
    prod_norm = _normalize_fidelity(prod_text)
    full_norm = _normalize_fidelity(full_md)
    fidelity_ok = prod_norm == full_norm
    fidelity_mode = "逐字"
    if not fidelity_ok and Counter(prod_norm) == Counter(full_norm):
        fidelity_ok = True
        fidelity_mode = "多重集（题注重排容差）"
    report["忠实度"] = {
        "通过": fidelity_ok,
        "方式": fidelity_mode,
        "产物字符数": len(prod_norm),
        "full.md字符数": len(full_norm),
    }

    code = 0
    if report["空页清单"] or report["图对账"]["缺图"]:
        code = 2
    if not fidelity_ok:
        code = 1
    if book_id and isolated_files:
        iso_dir = common.get_path("输出") / "提取缓存" / book_id / "隔离区"
        iso_dir.mkdir(parents=True, exist_ok=True)
        for name, text in isolated_files:
            (iso_dir / name).write_text(text, encoding="utf-8")
    # #20 结构信号落盘（原料级，随提取缓存）：有信号才产文件；本次无信号时清理旧文件防陈旧
    if book_id:
        sig_dir = common.get_path("输出") / "提取缓存" / book_id
        sig_path = sig_dir / "结构信号.jsonl"
        if signals:
            sig_dir.mkdir(parents=True, exist_ok=True)
            sig_path.write_text(
                "\n".join(json.dumps(s, ensure_ascii=False) for s in signals) + "\n",
                encoding="utf-8")
        elif sig_path.exists():
            sig_path.unlink()
    report["结构信号数"] = len(signals)
    return {"code": code, "products": products, "report": report}


def _find_zip_file(zip_path: Path, member: str) -> Path:
    """把 zip 成员安全解压到 已解包 目录并返回路径，杜绝路径逃逸。"""
    with zipfile.ZipFile(zip_path) as zf:
        names = {i.filename for i in zf.infolist()}
        if member not in names:
            return None
        out_dir = zip_path.parent / "已解包"
        target = (out_dir / member).resolve()
        if not str(target).startswith(str(out_dir.resolve())):
            raise RuntimeError(f"zip 成员路径越界：{member}")
    out_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extract(member, out_dir)
    return target


def _download(url: str, token: str, dest: Path):
    """流式下载。原子落盘（#31）：先写 .part 临时文件、完成后改名，
    下载中断只留下 .part 半成品，不会留下"看似完整"的坏 zip 骗过续跑机制。"""
    headers = {"Authorization": f"Bearer {token}", "Accept": "*/*"}
    tmp = dest.with_name(dest.name + ".part")
    try:
        with httpx.Client(timeout=300) as client:
            with client.stream("GET", url, headers=headers) as resp:
                resp.raise_for_status()
                dest.parent.mkdir(parents=True, exist_ok=True)
                with open(tmp, "wb") as f:
                    for chunk in resp.iter_bytes(chunk_size=1 << 16):
                        f.write(chunk)
        tmp.replace(dest)
    finally:
        if tmp.exists():
            tmp.unlink()


def _zip_ok(path: Path) -> bool:
    """缓存 zip 有效性校验（#31）：存在 + 是合法 zip + 内含 content_list.json。
    半截下载/损坏文件一律判无效，由调用方删除后重下（重下载不烧解析额度）。"""
    if not path.exists():
        return False
    try:
        with zipfile.ZipFile(path) as zf:
            for n in zf.namelist():
                base = n.rsplit("/", 1)[-1]
                if "content_list" in base and "content_list_v2" not in base and base.endswith(".json"):
                    return True
        return False
    except (zipfile.BadZipFile, OSError):
        return False


def run_ocr(pdf_path: Path, book_id: str, book_title: str, *, heartbeat=None,
            model_version: str = "vlm", is_ocr: bool = True, enable_table: bool = True,
            language: str = "ch", page_ranges: str = None) -> dict:
    """M1+M2 全流程。返回 {code, products, report}。heartbeat：进度行回调。
    超过 MINERU_PAGE_LIMIT 页的书自动本地分卷上传合并，对上层透明。"""
    def say(msg):
        if heartbeat:
            heartbeat(msg)
        else:
            print(msg)

    token = get_token()
    if not token:
        raise RuntimeError("未配置 MinerU Token：请设置环境变量 MINERU_API_TOKEN 或到控制台设置页填写保存")

    page_count = _last_page_count(pdf_path)
    sticky = _load_sticky(book_id)
    if sticky and page_count > MINERU_PAGE_LIMIT and not sticky.get("分卷"):
        say("[纸条作废] 旧纸条为整卷提交（超云端单任务页数上限），改走分卷重提")
        _clear_sticky(book_id)
        sticky = {}
    sticky_dir = _sticky_dir(book_id)
    chunk_files = None

    if sticky:
        batch_id = sticky["batch_id"]
        chunks_meta = sticky.get("分卷") or [{"文件": sticky.get("文件", pdf_path.name),
                                              "偏移": 0, "data_id": book_id}]
        targets = [(m.get("data_id", book_id), sticky_dir / _zip_name(len(chunks_meta), i))
                   for i, m in enumerate(chunks_meta)]
        for _, z in targets:
            if z.exists() and not _zip_ok(z):
                say(f"[缓存损坏] {z.name} 不是完整的解析结果（疑似上次下载中断），"
                    f"已删除坏文件，重新下载（不重复烧解析额度）")
                z.unlink()
        if all(z.exists() for _, z in targets):
            say("[纸条命中] 已有解析结果 zip，直接转换")
        else:
            say(f"[纸条命中] 续跑 batch {batch_id}，直接轮询")
            _poll_and_fetch(batch_id, token, targets, say)
        offsets = [int(m.get("偏移", 0)) for m in chunks_meta]
    else:
        if page_count > MINERU_PAGE_LIMIT:
            n = (page_count + MINERU_PAGE_LIMIT - 1) // MINERU_PAGE_LIMIT
            say(f"[分卷] 全书 {page_count} 页，超 MinerU 单任务 {MINERU_PAGE_LIMIT} 页上限，"
                f"本地切 {n} 卷（单批次上传合并，无需手工拆书）")
            chunk_files = _split_pdf(pdf_path, sticky_dir)
        else:
            chunk_files = [(pdf_path, 0)]
        multi = len(chunk_files) > 1
        say(f"[1/4] 申请上传链接（{page_count} 页" + (f"，{len(chunk_files)} 卷）" if multi else "）"))
        files = [{"name": p.name, "is_ocr": is_ocr,
                  "data_id": f"{book_id}#{i + 1:02d}" if multi else book_id}
                 for i, (p, _) in enumerate(chunk_files)]
        body = {
            "files": files,
            "model_version": model_version,
            "enable_table": enable_table,
            "language": language,
            "extra_formats": [],
        }
        if page_ranges and not multi:
            body["files"][0]["page_ranges"] = page_ranges
        data = _request("POST", f"{API_BASE}/file-urls/batch", token, body)
        batch_id = data["data"]["batch_id"]
        file_urls = data["data"]["file_urls"]
        if len(file_urls or []) < len(files):
            raise RuntimeError("MinerU 未返回足够的上传链接，请稍后重试")
        chunks_meta = [{"文件": p.name, "偏移": off, "data_id": f["data_id"]}
                       for (p, off), f in zip(chunk_files, files)]
        _save_sticky(book_id, {"batch_id": batch_id, "书号": book_id, "书名": book_title,
                               "文件": pdf_path.name, "页数": page_count,
                               "分卷": chunks_meta, "状态": "uploaded"})
        say("[2/4] 上传 PDF 到云端" + (f"（共 {len(files)} 卷）" if multi else ""))
        with httpx.Client(timeout=600) as client:
            for i, ((p, _), url) in enumerate(zip(chunk_files, file_urls)):
                if multi:
                    say(f"  上传分卷 {i + 1}/{len(files)}：{p.name}")
                with open(p, "rb") as f:
                    up = client.put(url, content=f.read())
                if up.status_code >= 400:
                    raise RuntimeError(f"{p.name} 上传失败（HTTP {up.status_code}），请重试")
        say("[3/4] 上传完成，等待云端解析（系统自动提交任务）")
        targets = [(m["data_id"], sticky_dir / _zip_name(len(chunks_meta), i))
                   for i, m in enumerate(chunks_meta)]
        _poll_and_fetch(batch_id, token, targets, say)
        offsets = [off for _, off in chunk_files]

    zips = [z for _, z in targets]
    if any(not z.exists() for z in zips):
        raise RuntimeError("解析结果 zip 未下载成功")

    images_dir = common.get_path("知识库") / "书籍" / book_title / "图片"
    say("[4/4] 解析完成，转换页文件")
    if len(zips) > 1:
        src_zip = sticky_dir / "结果_合并.zip"
        _merge_zips(list(zip(zips, offsets)), src_zip)
    else:
        src_zip = zips[0]
    result = convert_zip(src_zip, images_dir=images_dir, pdf_page_count=page_count,
                         book_id=book_id, book_title=book_title)
    result["report"]["书号"] = book_id
    result["report"]["书名"] = book_title
    result["report"]["分卷数"] = len(zips)
    if chunk_files and len(chunk_files) > 1:
        for p, _ in chunk_files:
            try:
                p.unlink()
            except OSError:
                pass
    done_sticky = {"batch_id": batch_id, "书号": book_id, "书名": book_title,
                   "文件": pdf_path.name, "页数": page_count, "状态": "done"}
    if len(zips) > 1:
        done_sticky["分卷"] = [{"文件": m["文件"], "偏移": m["偏移"], "data_id": m["data_id"]}
                               for m in chunks_meta]
    _save_sticky(book_id, done_sticky)
    return result


def _zip_name(total: int, idx: int) -> str:
    return "结果.zip" if total == 1 else f"结果_{idx + 1:02d}.zip"


def _poll_and_fetch(batch_id: str, token: str, targets: list, say):
    """轮询批量任务直到全部 done 并下载 zip。targets: [(data_id, zip_path)]；
    已存在且有效（_zip_ok）的 zip 跳过（续跑不重复下载、不重复烧额度）；
    存在但损坏的 zip 删除后重下（#31）。"""
    def match(d_id):
        if len(targets) == 1:
            return items[0]
        for it in items:
            if it.get("data_id", "") == d_id:
                return it
        return None

    wait = POLL_INIT_SEC
    start = time.time()
    items = []
    remaining = []
    for d, z in targets:
        if z.exists() and not _zip_ok(z):
            say(f"[缓存损坏] {z.name} 不是完整的解析结果，重新下载")
            z.unlink()
        if not z.exists():
            remaining.append((d, z))
    while remaining:
        data = _request("GET", f"{API_BASE}/extract-results/batch/{batch_id}", token)
        items = data.get("data", {}).get("extract_result") or []
        if not items:
            raise RuntimeError("批量结果为空，请稍后重试")
        for it in items:
            if it.get("state") == "failed":
                who = it.get("file_name") or it.get("data_id") or "?"
                raise RuntimeError(f"MinerU 解析失败（{who}）：{it.get('err_msg', '')}")
        states = {}
        undone = 0
        for d_id, _ in remaining:
            it = match(d_id)
            if it is None:
                raise RuntimeError(f"批量结果中找不到分卷 {d_id}，请重试（或删除小纸条后重提）")
            st = it.get("state", "")
            states[st] = states.get(st, 0) + 1
            if st != "done":
                undone += 1
        if undone == 0:
            break
        elapsed_min = int((time.time() - start) / 60)
        summary = "/".join(f"{k}×{v}" for k, v in states.items())
        say(f"[轮询] {summary}（共 {len(remaining)} 卷），已等 {elapsed_min} 分钟（每 {wait}s）")
        time.sleep(wait)
        wait = min(wait * 2, POLL_MAX_SEC)
    for d_id, z in remaining:
        it = match(d_id)
        zip_url = it.get("full_zip_url", "")
        if not zip_url:
            raise RuntimeError(f"任务完成但缺少 zip 下载链接（{d_id}）")
        say(f"[下载] {z.name}")
        _download(zip_url, token, z)


def _write_products(products: list, book_id: str) -> Path:
    out_dir = common.get_path("输出") / "提取缓存" / book_id
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, text in products:
        (out_dir / name).write_text(text, encoding="utf-8")
    return out_dir


def report_text(filename: str, book_id: str, result: dict) -> str:
    rp = result.get("report", {})
    code = result.get("code", 0)
    conclusion = {0: "🟢 完成", 1: "🔴 失败", 2: "🟡 部分成功"}.get(code, "🔴 未知")
    lines = [
        f"# MinerU 提取报告：{filename}",
        f"结论：{conclusion}",
        f"书号：{book_id}",
        f"页数：{rp.get('页数')}，空页：{len(rp.get('空页清单', []))}，隔离页眉页脚块：{rp.get('隔离辅助块')}（{rp.get('隔离辅助块页数')} 页）",
    ]
    if rp.get("分卷数", 1) > 1:
        lines.append(f"分卷：{rp['分卷数']} 卷（超云端单任务 {MINERU_PAGE_LIMIT} 页上限，本地自动分卷合并）")
    lines += [
        f"图对账：声明 {rp.get('图对账', {}).get('声明图数')}，实收 {rp.get('图对账', {}).get('实收图数')}，缺图 {rp.get('图对账', {}).get('缺图')}",
        f"忠实度自检：{'通过' if rp.get('忠实度', {}).get('通过') else '不通过'}（方式 {rp.get('忠实度', {}).get('方式', '逐字')}；产物 {rp.get('忠实度', {}).get('产物字符数')} 字 / full.md {rp.get('忠实度', {}).get('full.md字符数')} 字）",
    ]
    if rp.get("空页清单"):
        lines.append(f"空页清单：{rp['空页清单']}（扫描空白页或识别失败，需人工判断）")
    for w in rp.get("警告", []):
        lines.append(f"警告：{w}")
    lines.append(f"\n产物目录：{common.get_path('输出') / '提取缓存' / book_id}\n")
    return "\n".join(lines)


def main():
    args = sys.argv[1:]
    filename = None
    book_id = None
    book_title = None
    zip_arg = None
    for i, a in enumerate(args):
        if a == "--文件" and i + 1 < len(args):
            filename = args[i + 1]
        elif a == "--书号" and i + 1 < len(args):
            book_id = args[i + 1]
        elif a == "--书名" and i + 1 < len(args):
            book_title = args[i + 1]
        elif a == "--从zip" and i + 1 < len(args):
            zip_arg = args[i + 1]

    if not zip_arg and not filename:
        print("用法：python mineru_client.py --文件 <待入库文件名> [--书号 <书号>] [--书名 <书名>]\n"
              "      或 python mineru_client.py --从zip <本地zip路径> [--书号 <书号>] [--书名 <书名>]")
        sys.exit(1)

    if not book_id and filename:
        book_id = common.gen_book_id(filename)
    if not book_title and filename:
        book_title = common.clean_book_title(filename)

    try:
        if zip_arg:
            zip_path = Path(zip_arg)
            if not zip_path.exists():
                raise FileNotFoundError(f"zip 不存在：{zip_path}")
            name = common.clean_book_title(zip_path.name)
            book_id = book_id or common.gen_book_id(f"{name}.pdf")
            book_title = book_title or name
            images_dir = common.get_path("知识库") / "书籍" / book_title / "图片"
            result = convert_zip(zip_path, images_dir=images_dir, book_id=book_id,
                                 book_title=book_title)
            result["report"]["书号"] = book_id
            result["report"]["书名"] = book_title
            _write_products(result["products"], book_id)
        else:
            result = run_ocr(common.get_path("待入库") / filename, book_id, book_title)
            _write_products(result["products"], book_id)
    except (FileNotFoundError, ValueError, RuntimeError) as e:
        print(f"错误：{e}")
        if filename:
            common.write_report(f"{common.clean_book_title(filename)}_提取.md",
                                f"# MinerU 提取报告：{filename}\n结论：🔴 失败\n原因：{e}\n")
        sys.exit(1)

    print(f"提取完成：{len(result['products'])} 个页文件；空页 {len(result['report'].get('空页清单', []))} 个；"
          f"忠实度自检{'通过' if result['report'].get('忠实度', {}).get('通过') else '不通过'}")
    if filename:
        # #14 全绿不留档：绿删旧报告，非绿写报告
        if result["code"] == 0:
            common.clear_report(f"{common.clean_book_title(filename)}_提取.md")
        else:
            common.write_report(f"{common.clean_book_title(filename)}_提取.md",
                                report_text(filename, book_id, result))
    sys.exit(result["code"])


def _selfcheck_offline() -> bool:
    """离线用例：分卷切割 → 伪造分卷 zip → 合并 → convert_zip 转换全链（沙箱，零污染）。"""
    import tempfile
    ok = True
    tmp = Path(tempfile.mkdtemp(prefix="mineru_selfcheck_"))
    try:
        try:
            import fitz
        except ImportError:
            print("  [FAIL] 缺少 pymupdf 依赖")
            return False
        src_pdf = tmp / "源书.pdf"
        with fitz.open() as doc:
            for i in range(3):
                page = doc.new_page()
                page.insert_text((72, 72), f"第{i + 1}页")
            doc.save(str(src_pdf))
        chunks = _split_pdf(src_pdf, tmp / "分卷", chunk_pages=2)
        if len(chunks) != 2 or chunks[0][1] != 0 or chunks[1][1] != 2:
            print(f"  [FAIL] 分卷切割：{[(p.name, o) for p, o in chunks]}")
            ok = False
        else:
            counts = []
            for p, _ in chunks:
                with fitz.open(str(p)) as d:
                    counts.append(len(d))
            if counts != [2, 1]:
                print(f"  [FAIL] 分卷页数：{counts}（期望 [2, 1]）")
                ok = False
            else:
                print(f"  [OK] 分卷切割：3 页 → 2 卷（{counts}）")

        def fake_zip(path, blocks, full_md, images):
            with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
                z.writestr("content_list.json", json.dumps(blocks, ensure_ascii=False))
                z.writestr("full.md", full_md)
                for name, data in images.items():
                    z.writestr(name, data)

        z1 = tmp / "结果_01.zip"
        z2 = tmp / "结果_02.zip"
        fake_zip(z1,
                 [{"type": "text", "text": "第一页内容", "page_idx": 0},
                  {"type": "text", "text": "第二页内容", "page_idx": 1},
                  {"type": "image", "img_path": "images/a.jpg", "page_idx": 1}],
                 "第一页内容\n\n第二页内容\n\n![](images/a.jpg)",
                 {"images/a.jpg": b"img-bytes-1"})
        fake_zip(z2,
                 [{"type": "text", "text": "第三页内容", "page_idx": 0},
                  {"type": "image", "img_path": "images/a.jpg", "page_idx": 0}],
                 "第三页内容\n\n![](images/a.jpg)",
                 {"images/a.jpg": b"img-bytes-2-different"})
        merged = tmp / "结果_合并.zip"
        _merge_zips([(z1, 0), (z2, 2)], merged)
        result = convert_zip(merged, images_dir=tmp / "图片", pdf_page_count=3)
        names = [n for n, _ in result["products"]]
        fid = result["report"]["忠实度"]
        imgs = sorted(p.name for p in (tmp / "图片").glob("*"))
        if names != ["页_001.txt", "页_002.txt", "页_003.txt"]:
            print(f"  [FAIL] 合并转换页文件：{names}")
            ok = False
        elif not fid["通过"]:
            print(f"  [FAIL] 合并忠实度：{fid}")
            ok = False
        elif imgs != ["a.jpg", "a_2.jpg"]:
            print(f"  [FAIL] 图片冲突重命名：{imgs}")
            ok = False
        elif "【图】图片/a_2.jpg" not in result["products"][2][1]:
            print(f"  [FAIL] 重命名后块内引用：{result['products'][2][1]!r}")
            ok = False
        elif result["products"][1][1] != "第二页内容\n\n【图】图片/a.jpg":
            print(f"  [FAIL] 块间未以空行分隔（clean_split 段落=块的前提）：{result['products'][1][1]!r}")
            ok = False
        else:
            print("  [OK] 分卷合并：页偏移/图片重命名/块间空行/忠实度自检全过")

        # 题注重排容差：full.md 题注在表体前、渲染产物在表体后（真实分歧）→
        # 逐字不符但字符多重集一致 → 应判通过且方式=多重集
        z3 = tmp / "重排.zip"
        fake_zip(z3,
                 [{"type": "table", "table_caption": ["(图一)"],
                   "table_body": "<table><tr><td>三</td></tr></table>", "page_idx": 0}],
                 "(图一)\n\n<table><tr><td>三</td></tr></table>",
                 {})
        r3 = convert_zip(z3, images_dir=tmp / "图片3", pdf_page_count=1)
        f3 = r3["report"]["忠实度"]
        if not f3["通过"] or not f3["方式"].startswith("多重集"):
            print(f"  [FAIL] 题注重排多重集容差：{f3}")
            ok = False
        else:
            print("  [OK] 题注重排容差：逐字不符 → 多重集判过")

        # #20 结构信号与原文分离：标题块不再写 `#` 前缀 → 产 结构信号.jsonl；忠实度双侧对称比对仍过
        z4 = tmp / "标题.zip"
        fake_zip(z4,
                 [{"type": "text", "text": "第一章 测试章", "text_level": 2, "page_idx": 0},
                  {"type": "text", "text": "正文内容甲", "page_idx": 0}],
                 "## 第一章 测试章\n\n正文内容甲",
                 {})
        r4 = convert_zip(z4, images_dir=tmp / "图片4", pdf_page_count=1, book_id="自检M1")
        sig_path = common.get_path("输出") / "提取缓存" / "自检M1" / "结构信号.jsonl"
        sig_rows = ([json.loads(x) for x in sig_path.read_text(encoding="utf-8").splitlines() if x.strip()]
                    if sig_path.exists() else [])
        prod4 = r4["products"][0][1] if r4["products"] else ""
        if "#" in prod4:
            print(f"  [FAIL] #20 标题块仍带 # 前缀：{prod4!r}")
            ok = False
        elif not r4["report"]["忠实度"]["通过"]:
            print(f"  [FAIL] #20 撤 # 后忠实度不通过：{r4['report']['忠实度']}")
            ok = False
        elif len(sig_rows) != 1 or sig_rows[0].get("级别") != 2 or sig_rows[0].get("标题") != "第一章 测试章":
            print(f"  [FAIL] #20 结构信号.jsonl 内容异常：{sig_rows}")
            ok = False
        else:
            print("  [OK] #20 结构信号：撤 # 前缀 + 结构信号.jsonl（忠实度仍过）")

        # #17 V1/V2/V3：规则盘（3×3 无合并单元格）→ Markdown 表；table 带 img_path → 出【图】引用；
        # 忠实度改内容级（双侧剥 markup）后仍逐字通过（字符零增删）
        z5 = tmp / "排盘.zip"
        pan = ("<table><tr><td>7 4九</td><td>2 9五</td><td>5 1三</td></tr>"
               "<tr><td>1 6四</td><td>9 3六</td><td>8 7二</td></tr>"
               "<tr><td>3 8一</td><td>4 2八</td><td>6 5七</td></tr></table>")
        fake_zip(z5,
                 [{"type": "table", "table_caption": ["(挨星盘)"], "table_body": pan,
                   "img_path": "images/p1.jpg", "page_idx": 0}],
                 pan + "\n\n![](images/p1.jpg)\n\n(挨星盘)",
                 {"images/p1.jpg": b"pan-bytes"})
        r5 = convert_zip(z5, images_dir=tmp / "图片5", pdf_page_count=1,
                         rules={common.RULE_CELL_BR: True})
        f5 = r5["report"]["忠实度"]
        prod5 = r5["products"][0][1] if r5["products"] else ""
        if not (prod5.startswith("| 7 4<br>九 | 2 9<br>五 | 5 1<br>三 |") and "\n|---|" in prod5
                and prod5.endswith("(挨星盘)")):
            print(f"  [FAIL] 排盘转 md 表（表体置段首/单元格 <br>）异常：{prod5!r}")
            ok = False
        elif "【图】" in prod5:
            print(f"  [FAIL] table 块仍写出【图】行：{prod5!r}")
            ok = False
        elif not f5["通过"] or f5["方式"] != "逐字":
            print(f"  [FAIL] 排盘转 md 后忠实度（内容级）：{f5}")
            ok = False
        else:
            print("  [OK] 排盘转 md 表：表体置段首 + 单元格 <br> + 内容级忠实度逐字通过")

        # #17 V1/V2：含 colspan 的不规则表 → 回落 HTML 原样 + warnings 记一笔（不阻塞）；
        # 无 img_path 的 table 不多出【图】行
        z6 = tmp / "不规则表.zip"
        irregular = ('<table><tr><td colspan="2">合并格</td></tr>'
                     '<tr><td>甲</td><td>乙</td></tr></table>')
        fake_zip(z6,
                 [{"type": "table", "table_body": irregular, "page_idx": 0}],
                 irregular,
                 {})
        r6 = convert_zip(z6, images_dir=tmp / "图片6", pdf_page_count=1)
        prod6 = r6["products"][0][1] if r6["products"] else ""
        warns6 = r6["report"]["警告"]
        if "| " in prod6 or irregular not in prod6:
            print(f"  [FAIL] 不规则表未回落 HTML 原样：{prod6!r}")
            ok = False
        elif "【图】" in prod6:
            print(f"  [FAIL] 无 img_path 的 table 多出【图】行：{prod6!r}")
            ok = False
        elif not any("Markdown" in w for w in warns6):
            print(f"  [FAIL] 不规则表未记 warnings：{warns6}")
            ok = False
        elif not r6["report"]["忠实度"]["通过"]:
            print(f"  [FAIL] 不规则表忠实度不通过：{r6['report']['忠实度']}")
            ok = False
        else:
            print("  [OK] 不规则表回落 HTML：warnings 记录 + 忠实度通过（无【图】多行）")

        # #17：盘式特化由「书规则」开关驱动 —— 开关开：6 行盘式表拆回两张；开关关：保持通用行为（一张、无 <br>）
        z7 = tmp / "双盘合表.zip"
        pan2 = ("<table><tr><td>1 1一</td><td>2 2二</td><td>3 3三</td></tr>"
                "<tr><td>4 4四</td><td>5 5五</td><td>6 6六</td></tr>"
                "<tr><td>7 7七</td><td>8 8八</td><td>9 9九</td></tr>"
                "<tr><td>2 2二</td><td>3 3三</td><td>4 4四</td></tr>"
                "<tr><td>5 5五</td><td>6 6六</td><td>7 7七</td></tr>"
                "<tr><td>8 8八</td><td>9 9九</td><td>1 1一</td></tr></table>")
        fake_zip(z7, [{"type": "table", "table_body": pan2, "page_idx": 0}], pan2, {})
        r7 = convert_zip(z7, images_dir=tmp / "图片7", pdf_page_count=1,
                         rules={common.RULE_PAN_SPLIT: True, common.RULE_CELL_BR: True})
        prod7 = r7["products"][0][1] if r7["products"] else ""
        r7b = convert_zip(z7, images_dir=tmp / "图片7b", pdf_page_count=1)
        prod7b = r7b["products"][0][1] if r7b["products"] else ""
        if prod7.count("|---|---|---|") != 2 or "\n\n| 2 2<br>二 |" not in prod7:
            print(f"  [FAIL] 双盘合表未拆分为两张独立表：{prod7!r}")
            ok = False
        elif not r7["report"]["忠实度"]["通过"]:
            print(f"  [FAIL] 双盘合表拆分后忠实度不通过：{r7['report']['忠实度']}")
            ok = False
        elif prod7b.count("|---|---|---|") != 1 or "<br>" in prod7b:
            print(f"  [FAIL] 开关缺省时未保持通用行为（应一张表、无 <br>）：{prod7b!r}")
            ok = False
        else:
            print("  [OK] 盘式特化由开关驱动：开→拆两张带 <br>；关→保持一张（忠实度仍过）")

        # #31 坏 zip 缓存识别：半截/垃圾文件判无效（续跑会删除重下），完整结果 zip 判有效
        bad_zip = tmp / "半截.zip"
        bad_zip.write_bytes(b"PK\x03\x04 truncated-garbage")
        good_zip = tmp / "完整.zip"
        fake_zip(good_zip, [{"type": "text", "text": "x", "page_idx": 0}], "x", {})
        bad_convert_msg = None
        try:
            convert_zip(bad_zip, images_dir=tmp / "图片坏", pdf_page_count=1)
        except RuntimeError as e:
            bad_convert_msg = str(e)
        except Exception as e:
            bad_convert_msg = f"非人话异常：{type(e).__name__}"
        if _zip_ok(bad_zip):
            print("  [FAIL] 半截 zip 未被判无效")
            ok = False
        elif not _zip_ok(good_zip):
            print("  [FAIL] 完整结果 zip 未被判有效")
            ok = False
        elif not bad_convert_msg or "不是有效的解析结果文件" not in bad_convert_msg:
            print(f"  [FAIL] 坏 zip 转换未出人话报错：{bad_convert_msg!r}")
            ok = False
        else:
            print("  [OK] #31 坏 zip 识别：半截判无效、完整判有效、转换人话报错")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(common.get_path("输出") / "提取缓存" / "自检M1", ignore_errors=True)
    return ok


def _selfcheck():
    """自检：①离线分卷/合并/转换用例 ②token 配置存在性 ③上传链接申请连通（不真正上传，不烧解析额度）。"""
    print("mineru_client 自检")
    ok = _selfcheck_offline()
    token = get_token()
    if token:
        print(f"  [OK] Token 已配置：{mask_key(token)}（{'环境变量' if __import__('os').environ.get(TOKEN_ENV) else '路径配置.json api 节'}）")
    else:
        print("  [FAIL] Token 未配置（环境变量 MINERU_API_TOKEN 或 设置页保存）")
        ok = False
        print("  自检结论：[FAIL] 请先配置 Token 再重试")
        sys.exit(1)
    try:
        data = _request("POST", f"{API_BASE}/file-urls/batch", token,
                        {"files": [{"name": "selfcheck.pdf", "data_id": "selfcheck"}],
                         "model_version": "pipeline", "enable_table": False})
        if data.get("data", {}).get("file_urls"):
            print("  [OK] 上传链接申请连通（network + token 认证通过）")
        else:
            print("  [FAIL] 上传链接申请返回异常")
            ok = False
    except RuntimeError as e:
        print(f"  [FAIL] {e}")
        ok = False
    print(f"  自检结论：[{'OK' if ok else 'FAIL'}]")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    if common.is_selfcheck():
        _selfcheck()
    else:
        common.run_with_write_lock(main, "OCR")