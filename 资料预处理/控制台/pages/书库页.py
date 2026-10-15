# -*- coding: utf-8 -*-
"""主页（原书库页，含原标注页并入）。

#25 M2：标注页并入本页——
- 表格四列化（书名/分类/标注进度/状态）+ 状态列三态推导（需重建索引/需复查/正常）；
- 主页主按钮「开始标注」＝原「继续标注」+ 标注页「API 自动标注」合并（作用选中行）；
- 外发标注十项功能移入高级区子面板 `pages/外发标注.py`；
- 健康记录默认折叠（#40 起整区删除）；「打开书籍目录」「重新验证」移入高级区（#40 起前者删除、后者并入重建索引）；
- 双击行执行该行状态对应动作（R8）；跨页跳转（on_navigate，原继续标注）废弃。

#25 M4：取消页签导航后本页即唯一主页——`on_show` 钩子删除（无切页场景），数据刷新由
`__init__`、`App._on_task_finished` 与各操作收尾触发；「开始标注」未配置标注 AI 的拦截
改为**打开设置窗并定位 ② 区**（不再口头"去设置页"）。

#40（主页瘦身与高级区重组，2026-09-15 定案七项）：
- 「设置分类」按钮删除 → **分类列双击行内编辑**（纯文本 Entry，无下拉；留空＝清除，读索引与写回口径不变）；
- 「排盘书·豁免图表块收纳」勾选控件移除（`书规则\\{书名}.json` 机制保留，改为手工配置）；
- 健康记录整区删除（评测成绩 + 验证记录列表 + 打开选中报告），**报告照常落盘仅供 AI 读**；
- 高级区按用途分两组（这本书出问题时 / 不用 API 的标注方式），删「打开书籍目录」；
- 「重新验证」并入「重建索引」（重建末尾自动跑 A-05 密文自检，结果汇入重建任务）；
- 按钮/标签命名的人话化（零术语替换）仍归 #28，本条只动结构与控件去留。

#40 验收返工（2026-09-15 用户拍板两项）：
- 分类填写引导改由**列标题「分类（可填写）」**承担（原「表格下方常驻提示行」撤掉——用户判定那种"底"不好用）；
- 「需复查」入口由**双击**改为**主按钮带「查看问题」按钮**（双击不易被发现；仅选中行状态为「需复查」时可用、其余置灰），双击该词条不再触发动作（分类格双击仍是编辑分类，「需重建索引」双击仍走重建）。

#41（高级区按钮被压成 1px 的修复，2026-09-15）：
- 高级折叠区**仍留在主页**（曾试过搬成右上角独立窗，用户否决：入口存在感太低）；
- 改为**主窗口按内容自适应**：`App.fit_to_content()` 在启动后与高级区展开/折叠后各调一次——
  窗口长到内容需求高 + `minsize` 高度抬到需求高（用户也拖不小 → 按钮永不被压扁）；
  屏幕装不下时先压缩外发面板的表格行数（表格可滚动、按钮不可压扁），容量恢复后自动还原。
"""

import json
import os
import queue
import re
import shutil
import threading
import tkinter as tk
from pathlib import Path
from tkinter import ttk, messagebox, filedialog

import annotate_pack
import build_index
import common
import llm_client
import theme
from . import get_books, get_chapters, run_script, show_issue_dialog, taskcenter
from .外发标注 import 外发标注面板


#: 分类列列名——#40 返工：由列标题本身承担「可填写」引导（原表下提示行已撤）
CAT_COL = "分类（可填写）"


class _StatusLabel(tk.Label):
    """状态行标签（#51）：fg 色名统一经 theme.color 翻译成语义令牌——
    各操作收尾的 config(fg="green"/"orange"/...) 调用点零改动。"""

    def configure(self, cnf=None, **kw):
        if "fg" in kw:
            kw["fg"] = theme.color(kw["fg"])
        return super().configure(cnf, **kw)

    config = configure


def _counted_rmtree(path, on_progress=None, batch=200) -> int:
    """分批删除目录树并回调进度，返回处理的文件数（#39：删书过程「已删 n/N 个文件」可感知）。

    on_progress(已删, 总数)：每 batch 个文件回调一次；单个文件删除失败静默跳过（与
    `shutil.rmtree(ignore_errors=True)` 同口径）；最后清掉空壳目录。"""
    p = Path(path)
    if not p.exists():
        return 0
    try:
        files = [f for f in p.rglob("*") if f.is_file()]
    except OSError:
        files = []
    total = len(files)
    for i, f in enumerate(files, 1):
        try:
            f.unlink()
        except OSError:
            pass
        if on_progress and (i % batch == 0 or i == total):
            try:
                on_progress(i, total)
            except Exception:
                pass
    shutil.rmtree(p, ignore_errors=True)
    return total


class 书库页(tk.Frame):
    def __init__(self, master):
        super().__init__(master)
        self._op_q = queue.Queue()   # 后台操作 → 主线程消息泵（#30）
        self._busy = False           # 重建索引/重新验证/删除书籍 排障互斥（防重入，#30/R6/#38）
        self._annot_running = False  # 「开始标注」进行中标志（与排障互斥分开，同类互斥）
        self._task_id = None         # #25 M1：当前全局任务 id（双写 TaskCenter）
        self._annot_cache = {}       # #25 M1-2：书名 → (输入签名, book_annotation_status 结果)
        self._book_ids = {}          # 书名 → 书号（书号列已隐藏，内部逻辑仍需要）
        self._auto_clean_running = False   # #39：自动残留清理是否在跑（防重入）
        self._auto_cleaned = ()            # 已自动清理过的残留指纹（同一批不重复自动跑）
        self._build()
        self.after(100, self._poll_op)

    # ---------- 界面 ----------

    def _build(self):
        # #51 布局 B（分组面板）：主按钮收进白色工具条带（卡片）
        strip = tk.Frame(self, bg=theme.CARD, highlightbackground=theme.HAIRLINE,
                         highlightthickness=1, bd=0)
        strip.pack(fill="x", padx=10, pady=(8, 10))
        bar = ttk.Frame(strip, style="Card.TFrame")
        bar.pack(fill="x", padx=8, pady=6)
        ttk.Button(bar, text="＋ 添加书籍", style="Strip.TButton",
                   command=self.open_ingest).pack(side="left", padx=(0, 4))
        self.annotate_btn = ttk.Button(bar, text="开始标注", style="Primary.TButton",
                                       command=self.start_annotate)
        self.annotate_btn.pack(side="left")
        # #44：更新入口提为一级主按钮（用户口径：不要收进高级区；文案定稿「书籍更新」）
        ttk.Button(bar, text="书籍更新", style="Strip.TButton",
                   command=self.update_book).pack(side="left", padx=4)
        # #40 返工：原「需复查」双击不易被发现 → 主按钮带常显按钮，仅选中行需复查时可用
        self.problem_btn = ttk.Button(bar, text="查看问题", style="Strip.TButton",
                                      command=self.open_book_log)
        self.problem_btn.pack(side="left", padx=4)
        ttk.Button(bar, text="删除书籍", style="StripDanger.TButton",
                   command=self.delete_book).pack(side="left", padx=4)
        # #28 返工：日志入口从状态条/高级区提为主按钮（用户口径：不要藏那么深）
        ttk.Button(bar, text="查看任务日志", style="Strip.TButton",
                   command=self.open_task_log).pack(side="left", padx=4)

        cols = ("书名", CAT_COL, "标注进度", "状态")
        self.tree = ttk.Treeview(self, columns=cols, show="headings", height=8)
        for c in cols:
            self.tree.heading(c, text=c)
        # #51：主窗加宽为长方形比例，列宽同步加宽吃掉横向空间
        for c, w in (("书名", 260), (CAT_COL, 170), ("标注进度", 150), ("状态", 160)):
            self.tree.column(c, width=w)
        self.tree.pack(fill="x", padx=10, pady=4)
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._on_select())
        self.tree.bind("<Double-1>", self._on_double)
        self._cat_col = f"#{cols.index(CAT_COL) + 1}"   # #40：双击按列分派用（分类列）

        self.status = _StatusLabel(self, text="就绪", anchor="w",
                                   font=("Microsoft YaHei UI", 11))
        self.status.pack(fill="x", padx=12)
        # #39：残留提示行（常驻，无残留时为空——不占用 status 行，避免盖掉操作结果）
        self.residue_lbl = tk.Label(self, text="", anchor="w", fg=theme.WARN, justify="left")
        self.residue_lbl.pack(fill="x", padx=12)

        # 高级操作（默认折叠）——#40：按用途分两组（「入库异常处理」/「手动 AI 标注」，#28 定稿）
        # #51 布局 B：折叠入口面板化（白卡片条 + 内嵌无边框按钮）
        self._adv_open = False
        self.adv_wrap = tk.Frame(self, bg=theme.CARD, highlightbackground=theme.HAIRLINE,
                                 highlightthickness=1, bd=0)
        self.adv_toggle = ttk.Button(self.adv_wrap, text="▸ 高级（入库异常处理 / 手动 AI 标注）",
                                     style="Strip.TButton", command=self._toggle_adv)
        self.adv_toggle.pack(side="left", padx=6, pady=4)
        self.adv_wrap.pack(fill="x", padx=10, pady=(10, 0))
        self.adv_frame = ttk.Frame(self)

        # 组①：入库异常处理（重建索引含自动验证 + 清理残留；#44 起「更新这本书的内容…」提为主按钮）
        ttk.Label(self.adv_frame, text="入库异常处理",
                  font=("Microsoft YaHei UI", 10, "bold")).pack(
            anchor="w", padx=8, pady=(4, 0))
        fix_row = ttk.Frame(self.adv_frame)
        fix_row.pack(fill="x", padx=16, pady=2)
        ttk.Button(fix_row, text="重建索引（含自动验证）",
                   command=self.rebuild).pack(side="left", padx=(0, 8))
        # #39：登记在册但书库无目录的残留（多步删除被中断后的收尾入口）
        self.cleanup_btn = ttk.Button(fix_row, text="清理残留", command=self.clean_residue)
        self.cleanup_btn.pack(side="left", padx=(0, 8))

        # 组②：手动 AI 标注（外发标包子面板整块搬迁，内部逻辑零改动）
        ttk.Label(self.adv_frame, text="手动 AI 标注",
                  font=("Microsoft YaHei UI", 10, "bold")).pack(
            anchor="w", padx=8, pady=(8, 0))
        self.export_panel = 外发标注面板(self.adv_frame, get_book=self._current_title,
                                        on_changed=self.refresh, set_status=self._set_status)
        self.export_panel.pack(fill="x", padx=16, pady=(2, 4))

        self.refresh()

    def open_ingest(self):
        """打开「添加书籍」入库窗（#25 M3：主页主入口，Toplevel 单例）。"""
        top = self.winfo_toplevel()
        opener = getattr(top, "open_ingest", None)
        if callable(opener):
            opener()

    def _open_settings(self, section=None):
        """打开「设置」窗并定位到指定区（#25 M4：未配置拦截不再只说"去设置页"）。"""
        top = self.winfo_toplevel()
        opener = getattr(top, "open_settings", None)
        if callable(opener):
            opener(section)

    def _toggle_adv(self):
        """展开/折叠高级区（#41：改动后按内容自适应主窗口，保证按钮不被 Tk packer 压扁；
        #51 返工二轮：折叠时传 shrink=True，窗口随内容自动收回，不留空白）。"""
        collapsing = self._adv_open
        if collapsing:
            self.adv_frame.pack_forget()
            self.adv_toggle.config(text="▸ 高级（入库异常处理 / 手动 AI 标注）")
        else:
            self.adv_frame.pack(fill="x", padx=10, pady=2, after=self.adv_wrap)
            self.adv_toggle.config(text="▾ 高级（入库异常处理 / 手动 AI 标注）")
        self._adv_open = not self._adv_open
        fit = getattr(self.winfo_toplevel(), "fit_to_content", None)
        if callable(fit):
            fit(shrink=collapsing)

    def _set_status(self, text, color="black"):
        try:
            self.status.config(text=text, fg=color)
        except tk.TclError:
            pass

    # ---------- 消息泵 ----------

    def _poll_op(self):
        """后台操作消息泵（#30）：worker 线程只入队，控件刷新全部在主线程此处完成。"""
        try:
            while True:
                msg = self._op_q.get_nowait()
                if msg[0] == "status":
                    self.status.config(text=msg[1], fg=msg[2])
                    if self._task_id:
                        taskcenter.progress(self._task_id, step=msg[1])
                elif msg[0] == "rebuild_done":
                    self._on_rebuild_done(*msg[1:])
                elif msg[0] == "update_dry_done":
                    self._on_update_dry_done(*msg[1:])
                elif msg[0] == "update_done":
                    self._on_update_done(*msg[1:])
                elif msg[0] == "delete_done":
                    self._on_delete_done(*msg[1:])
                elif msg[0] == "cleanup_done":
                    self._on_cleanup_done(*msg[1:])
                elif msg[0] == "fill_done":
                    self._on_fill_done(*msg[1:])
        except queue.Empty:
            pass
        try:
            if self.winfo_exists():
                self.after(100, self._poll_op)
        except tk.TclError:
            pass

    # ---------- 选中行 / 状态列 ----------

    def _on_select(self):
        """选中行变化：同步「查看问题」按钮态、标注按钮态、外发子面板（章号 + 任务包清单）。"""
        self._sync_problem_btn()
        self._sync_annotate()
        self.export_panel.refresh()

    def _on_double(self, event):
        """双击按列分派（#40）：「分类」列 → 行内编辑（纯文本，无下拉）；
        其他列维持 #25 R8 的状态路由——「需重建索引」→ 重建。
        （#40 返工：「需复查」已改由主按钮带「查看问题」按钮承担，双击不再触发。）"""
        row = self.tree.identify_row(event.y)
        if not row:
            return
        if self.tree.identify_column(event.x) == self._cat_col:
            self._edit_category_cell(row)
            return
        values = self.tree.item(row, "values")
        if values[3] == "需重建索引":
            self.tree.selection_set(row)
            self.rebuild()

    def _sync_problem_btn(self):
        """「查看问题」按钮态：仅选中行状态为「需复查」时可用（同「开始标注」的置灰逻辑）。"""
        enable = False
        title = self._selected_title(quiet=True)
        if title and self.tree.exists(title):
            enable = self.tree.item(title, "values")[3] == "需复查"
        self.problem_btn.state(["!disabled"] if enable else ["disabled"])

    def open_book_log(self):
        """「查看问题」（#40 返工）：开任务日志窗口并定位选中书——本次会话有该书验证/重建任务
        则定位到它，没有则日志区顶部给一行「复制本句去问 AI」提示。"""
        title = self._selected_title()
        if not title:
            return
        self._open_log_for(title)

    def _open_log_for(self, title):
        """调主窗口的任务日志窗口（`App._open_log(书名)`）；不再用系统程序打开 .md 报告。"""
        top = self.winfo_toplevel()
        opener = getattr(top, "_open_log", None)
        if callable(opener):
            opener(title)

    def open_task_log(self):
        """「查看任务日志」主按钮（#28 返工）：开任务日志窗口看最新任务（不按书定位）。"""
        top = self.winfo_toplevel()
        opener = getattr(top, "_open_log", None)
        if callable(opener):
            opener()

    _CAT_PLACEHOLDER = "输入分类，回车确认（留空清除）"

    def _edit_category_cell(self, row):
        """分类列行内编辑（#40）：在单元格 bbox 上覆盖**纯文本 Entry**（无下拉、带占位提示），
        Enter / 焦点离开提交、Esc 取消；留空＝清除（#15 口径不变，写 书目总索引.md「分类」列）。"""
        title = self.tree.item(row, "values")[0]
        book_id = self._book_ids.get(title, "")
        if not book_id:
            self._set_status(f"《{title}》尚未建立索引，请先重新入库", "red")
            return
        bbox = self.tree.bbox(row, self._cat_col)
        if not bbox:
            return
        x, y, w, h = bbox
        cur = self._category(book_id)
        st = {"done": False, "ph": False}
        var = tk.StringVar(value=cur)
        entry = tk.Entry(self.tree, textvariable=var)
        entry.place(x=x, y=y, width=w, height=h)
        if not cur:                     # 空值：先用灰字占位提示，首次敲键即清掉
            st["ph"] = True
            var.set(self._CAT_PLACEHOLDER)
            entry.config(fg=theme.INK3)
        entry.focus_set()
        entry.select_range(0, "end")

        def _clear_ph():
            if st["ph"]:
                st["ph"] = False
                var.set("")
                entry.config(fg=theme.INK)

        def _finish(save):
            if st["done"]:
                return
            st["done"] = True
            text = "" if st["ph"] else var.get().strip()
            try:
                entry.destroy()
            except tk.TclError:
                pass
            if not save or text == cur:
                return
            if build_index.catalog_set_category(book_id, text):
                self.tree.set(row, CAT_COL, text)      # 就地刷新该行，不整表重载
                self._set_status(f"《{title}》分类已设为「{text or '（已清除）'}」", "green")
            else:
                self._set_status(f"《{title}》未在书目总索引中找到登记行，分类未保存", "red")

        entry.bind("<Key>", lambda _e: _clear_ph())
        entry.bind("<Return>", lambda _e: _finish(True))
        entry.bind("<Escape>", lambda _e: _finish(False))
        entry.bind("<FocusOut>", lambda _e: _finish(True))

    def _selected_title(self, quiet=False):
        sel = self.tree.selection()
        if not sel:
            if not quiet:
                messagebox.showwarning("提示", "请先在列表选择一本书")
            return None
        return self.tree.item(sel[0], "values")[0]

    def _selected(self, quiet=False):
        title = self._selected_title(quiet)
        if not title:
            return None
        return {"书名": title, "书号": self._book_ids.get(title, "")}

    def _current_title(self):
        """当前选中书名（静默，无选中返回 None）——供外发子面板读取，避免切页/刷新时误弹提示。"""
        return self._selected_title(quiet=True)

    def _annot_status(self, title):
        """书级标注状态（#25 M1-2）：按输入文件签名记忆化——避免每次切页/点行逐章重读原文。
        缓存只是 book_annotation_status 现算结果的记忆，任一输入变化即失效重算，不新增状态源。"""
        sig = self._annot_sig(title)
        cached = self._annot_cache.get(title)
        if cached is not None and cached[0] == sig:
            return cached[1]
        try:
            st = annotate_pack.book_annotation_status(title)
        except Exception:
            st = None
        self._annot_cache[title] = (sig, st)
        return st

    @staticmethod
    def _annot_sig(title):
        """标注状态的输入签名：任务包存在性 + 回填 .done + 原文/检索副本/目录骨架/图表块清单。"""
        book_dir = common.get_path("知识库") / "书籍" / title
        pack = common.get_path("输出") / "任务包" / title

        def _st(p):
            try:
                s = p.stat()
                return (s.st_mtime_ns, s.st_size)
            except OSError:
                return None

        done = []
        if pack.exists():
            back = pack / "回填"
            if back.exists():
                try:
                    done = sorted((f.name, _st(f)) for f in back.glob("*.done"))
                except OSError:
                    done = []
        return (pack.exists(), tuple(done), _st(book_dir / "原文.md"),
                _st(book_dir / "检索副本.md"), _st(book_dir / "目录骨架.md"),
                _st(book_dir / "图表块清单.json"))

    def _annot_text(self, title):
        """书级标注状态显示文本：未开始 / 进行中 x/n / 已完成（.done 现算）。"""
        st = self._annot_status(title)
        if not st:
            return "—"
        if st.get("状态") == "进行中":
            return f"进行中 {st.get('已导回', 0)}/{st.get('总章数', 0)}"
        return st.get("状态", "—")

    def _category(self, book_id):
        if not book_id:
            return ""
        try:
            return build_index.catalog_get_category(book_id)
        except Exception:
            return ""

    @staticmethod
    def _annotating_book():
        """当前是否有该书正在跑「开始标注」（与状态条同源，取 TaskCenter 快照）。"""
        try:
            for t in taskcenter.snapshot():
                if t["status"] == "running" and t["kind"] == "标注":
                    m = re.fullmatch(r"《(.+)》", t["title"])
                    if m:
                        return m.group(1)
        except Exception:
            pass
        return None

    @staticmethod
    def _needs_recheck(title):
        """最新密文验证报告结论行非全绿 → 需复查（结论行锚点：`结论：✅ 全部命中`）。"""
        p = common.report_dir() / f"{title}_密文验证.md"
        if not p.exists():
            return False
        try:
            for ln in p.read_text(encoding="utf-8").splitlines():
                s = ln.strip()
                if s.startswith("结论："):
                    return "✅ 全部命中" not in s
        except Exception:
            return False
        return False

    def _status_text(self, b):
        """状态列：标注中… / 需重建索引 / 需复查 / 正常。"""
        if self._annotating_book() == b["书名"]:
            return "标注中…"
        if not b["有索引"]:
            return "需重建索引"
        if self._needs_recheck(b["书名"]):
            return "需复查"
        return "正常"

    # ---------- 数据刷新 ----------

    def refresh(self):
        keep = self._selected_title(quiet=True)
        for i in self.tree.get_children():
            self.tree.delete(i)
        self._book_ids = {}
        books = get_books()
        for b in books:
            self._book_ids[b["书名"]] = b["书号"]
            self.tree.insert("", "end", iid=b["书名"],
                             values=(b["书名"], self._category(b["书号"]),
                                     self._annot_text(b["书名"]), self._status_text(b)))
        if keep and self.tree.exists(keep):
            self.tree.selection_set(keep)
        self._sync_problem_btn()
        self._sync_annotate()
        self.export_panel.refresh()
        self._refresh_residue(books)     # #39：残留提示行 +「清理残留（N）」按钮态
        self._maybe_auto_clean(books)    # #39：残留自动清理（安全前提下，无需事前确认）

    # ---------- 开始标注（原「继续标注」+ 标注页「API 自动标注」合并） ----------

    def _sync_annotate(self):
        """「开始标注」：已完成/未选中 → 置灰（状态源＝.done 现算）。"""
        enable = False
        title = self._selected_title(quiet=True)
        if title:
            st = self._annot_status(title)
            enable = bool(st) and st.get("状态") != "已完成"
        self.annotate_btn.state(["!disabled"] if enable else ["disabled"])

    def start_annotate(self, book=None):
        # #46：允许显式指定要标注的书（更新后「立即标注」用），默认取当前选中行
        if not book:
            book = self._selected_title()
        if not book:
            return
        if self._annot_running:
            messagebox.showinfo("提示", "已有自动标注任务进行中，请等待完成")
            return
        if not llm_client.is_configured():
            messagebox.showwarning(
                "未配置标注 AI",
                "还没有配置标注 AI。\n\n"
                "「自动标注」需要一个 AI 接口才能帮你逐章整理标注。\n\n"
                "设置窗已打开并定位到 ② 标注 AI 区，把 Base URL / API Key / 模型填上并保存即可。\n"
                "（也可以在主页「高级 → 手动 AI 标注」里把任务包发给任意 AI 填写，不消耗额度）")
            self._open_settings("annotate")
            return
        cfg = llm_client.get_annotate_config()
        n_ch = len(get_chapters(book))
        if not messagebox.askyesno(
                "自动标注",
                f"将用标注 AI（{cfg['model']}）为《{book}》全部 {n_ch} 章整理标注，并自动核对导入书库。\n"
                "个别没过的章会自动重试 3 次。\n\n"
                "这会消耗接口额度。继续吗？"):
            return
        self._annot_running = True
        self.annotate_btn.state(["disabled"])
        self._set_status(f"API 自动标注中…（《{book}》逐章调用大模型，界面可正常操作）", "orange")
        self._task_id = taskcenter.start("标注", f"《{book}》", total=n_ch)
        taskcenter.progress(self._task_id, step="调用标注 AI 填写")
        threading.Thread(target=self._auto_fill_worker, args=(book, self._task_id),
                         daemon=True).start()

    def _auto_fill_worker(self, book, task_id):
        """后台线程：整本 API 自动回填 + 导回；只入队消息/写 TaskCenter，不碰控件。"""
        try:
            code, out, err = run_script("annotate_pack.py", "--自动回填", "--书名", book,
                                        on_line=lambda ln: taskcenter.log(task_id, ln), timeout=86400)
        except Exception as e:
            taskcenter.log(task_id, f"后台执行异常：{e}")
            code = 1
        taskcenter.finish(task_id, code == 0, "全部章填写完成" if code == 0 else "部分章未通过")
        self._op_q.put(("fill_done", book, code))

    def _on_fill_done(self, book, code):
        """完成收尾（R10）：书名来自任务上下文，不依赖主页当前选中行。"""
        self._annot_running = False
        try:
            self.annotate_btn.state(["!disabled"])
            self.refresh()
            if code == 0:
                self._set_status(f"🟢 《{book}》API 自动标注全部完成（术语已自动配对）", "green")
            elif code == 2:
                self._set_status(f"🟡 《{book}》部分章自动标注失败（详见日志）", "orange")
                show_issue_dialog(self, "部分章没标完",
                                  f"《{book}》有部分章没通过检查，已标好的章不受影响。\n\n"
                                  "可以在主页「高级 → 手动 AI 标注」补齐，或复制给 AI 排查。")
            else:
                self._set_status(f"🔴 《{book}》API 自动标注失败（详见日志）", "red")
        except tk.TclError:
            pass

    # ---------- 排障：重建索引（#30 后台化 + #25 M1 任务中心；#40 起含自动验证） ----------

    def rebuild(self):
        """重建索引：重跑 A-03 清洗分块 + A-04 建索引（复用提取缓存，不重烧 OCR），
        末尾自动跑一次 A-05 密文自检（#40：原独立「重新验证」并入本流程）。"""
        b = self._selected()
        if not b:
            return
        if not b["书号"]:
            messagebox.showerror("错误", "该书尚未建立索引，请先重新入库")
            return
        if self._busy:
            messagebox.showinfo("提示", "已有排障操作进行中，请等待完成")
            return
        if not messagebox.askyesno(
                "确认重建",
                f"将重新整理《{b['书名']}》的正文并重新建立搜索（沿用已有的识别结果，不会重新在线识别）。\n\n"
                "注意：重建后正文核对码会变化，以前发给外部 AI 的标注文件将无法导入；已导入的章节摘要不受影响。\n\n"
                "确定继续吗？"):
            return
        self._busy = True
        self.status.config(text=f"正在重建《{b['书名']}》索引…（界面可正常操作）", fg="orange")
        self._task_id = taskcenter.start("重建", f"《{b['书名']}》")
        taskcenter.progress(self._task_id, step="整理正文")
        threading.Thread(target=self._rebuild_worker,
                         args=(b["书名"], b["书号"], self._task_id), daemon=True).start()

    def _rebuild_worker(self, title, book_id, task_id):
        """后台线程：清洗分块 → 书级配置 → 建索引 → 自动密文自检（#40）；
        只入队消息，不碰任何控件。"""
        q = self._op_q.put
        try:
            code, out, err = run_script("clean_split.py", "--书号", book_id, "--书名", title)
        except Exception as e:
            taskcenter.finish(task_id, False, "整理正文异常")
            q(("rebuild_done", title, False, "整理正文异常", f"后台执行异常：{e}"))
            return
        if code not in (0, 2):
            taskcenter.finish(task_id, False, "整理正文失败")
            q(("rebuild_done", title, False, "整理正文失败",
               (out or err)[-800:] + "\n\n提示：若识别缓存已被清理，需重新入库。"))
            return
        if code == 2:
            q(("status", "🟡 整理正文有警告（不影响），继续重建…", "orange"))
        # #20 独立步骤（不阻塞）：自判过滤为空时由 LLM 产书级配置；失败回落自判/启发式
        try:
            taskcenter.progress(task_id, step="识别章节")
            run_script("book_cfg_gen.py", "--书号", book_id, "--书名", title)
            taskcenter.progress(task_id, step="建立索引")
            code, out, err = run_script("build_index.py", "--书号", book_id, "--书名", title)
        except Exception as e:
            taskcenter.finish(task_id, False, "建立索引异常")
            q(("rebuild_done", title, False, "建立索引异常", f"后台执行异常：{e}"))
            return
        if code != 0:
            taskcenter.finish(task_id, False, "建立索引失败")
            q(("rebuild_done", title, False, "建立索引失败", (out or err)[-800:]))
            return
        # #40：末尾自动跑一次密文自检（原独立「重新验证」并入本流程；失败不影响索引本身）
        taskcenter.progress(task_id, step="自动验证")
        try:
            vcode, vout, verr = run_script("verify_needles.py", "--书名", title, "--书号", book_id)
        except Exception as e:
            vcode, vout, verr = 1, "", f"后台执行异常：{e}"
        if vcode == 0:
            taskcenter.finish(task_id, True, "重建完成，自动验证通过")
            q(("rebuild_done", title, True, "", out[-500:]))
        else:
            taskcenter.finish(task_id, False, "重建完成，但自动验证有未命中")
            q(("rebuild_done", title, False, "自动验证有未命中", (vout or verr)[-800:]))

    def _on_rebuild_done(self, title, ok, stage, tail):
        self._busy = False
        if ok:
            self.status.config(text=f"🟢 《{title}》重建完成，自动验证通过：AI 能准确检索到本书内容。",
                               fg="green")
            messagebox.showinfo("完成", f"重建完成，自动验证通过：AI 能准确检索到本书内容。")
        elif stage == "自动验证有未命中":
            self.status.config(
                text=f"🟡 《{title}》重建完成，但自动验证有未命中（可点「重建」再试）。",
                fg="orange")
            show_issue_dialog(self, "自动验证有未命中",
                              f"《{title}》重建完成，但自动验证有个别内容没定位到，AI 检索本书时可能查不准。\n\n"
                              "可再点一次「重建索引（含自动验证）」，或复制给 AI 排查。",
                              detail=tail)
        else:
            self.status.config(text=f"🔴 {stage}", fg="red")
            show_issue_dialog(self, stage,
                              f"《{title}》{stage}。原书库内容不受影响；可复制给 AI 排查。",
                              detail=tail)
        self.refresh()

    # ---------- 更新书籍（#21 M5：dry-run 确认 → 手术两段式，后台执行） ----------

    def update_book(self):
        """更新选中书籍的内容：选新版文件 → 后台比对（预演，不动书库）→ 用户确认 →
        后台手术（自动备份＋机器双路径校验，不对就自动恢复原样）。"""
        b = self._selected()
        if not b:
            return
        if not b["书号"]:
            messagebox.showerror("错误", "该书尚未建立索引，请先重新入库")
            return
        if self._busy:
            messagebox.showinfo("提示", "已有排障操作进行中，请等待完成")
            return
        path = filedialog.askopenfilename(
            title=f"选择《{b['书名']}》的新版文件",
            filetypes=[("支持的资料", "*.docx *.pdf *.txt *.md *.xlsx"), ("所有文件", "*.*")])
        if not path:
            return
        self._busy = True
        self.status.config(text=f"正在比对《{b['书名']}》新旧版本…（界面可正常操作）", fg="orange")
        self._task_id = taskcenter.start("更新", f"《{b['书名']}》")
        taskcenter.progress(self._task_id, step="比对新旧版本（预演，不动书库）")
        taskcenter.log(self._task_id, f"新版文件：{path}")
        threading.Thread(target=self._update_dry_worker,
                         args=(b["书名"], b["书号"], path, self._task_id), daemon=True).start()

    def _update_dry_worker(self, title, book_id, path, task_id):
        """后台线程：dry-run 预演。只入队消息，不碰控件。"""
        q = self._op_q.put
        try:
            code, out, err = run_script("update_book.py", "--书号", book_id, "--新版", path,
                                        "--dry-run",
                                        on_line=lambda ln: taskcenter.log(task_id, ln),
                                        timeout=3600)
        except Exception as e:
            taskcenter.finish(task_id, False, "比对异常")
            q(("update_dry_done", title, book_id, path, 1, f"后台执行异常：{e}"))
            return
        taskcenter.finish(task_id, code != 1,
                          "比对完成，有变化待确认" if code == 2 else
                          ("比对完成：内容一致" if code == 0 else "比对失败"))
        q(("update_dry_done", title, book_id, path, code, (out or err or "")[-800:]))

    def _on_update_dry_done(self, title, book_id, path, code, tail):
        """dry-run 收尾（主线程）：无变化→提示；有变化→确认后启动手术；失败→报警弹窗。"""
        if code == 0:
            self._busy = False
            self.status.config(text=f"🟢 《{title}》新旧版本内容一致，无需更新。", fg="green")
            messagebox.showinfo("无需更新", f"《{title}》新旧版本内容一致，书库没有改动。")
            self.refresh()
            return
        if code == 1:
            self._busy = False
            self.status.config(text=f"🔴 《{title}》比对失败", fg="red")
            show_issue_dialog(self, "比对失败",
                              f"《{title}》新旧版本没能正常比对，书库没有改动。\n\n可复制给 AI 排查。",
                              detail=tail)
            self.refresh()
            return
        # code == 2：有变化待确认
        summary = next((ln for ln in tail.split("\n") if "保留" in ln and "退休" in ln), "")
        detail = (common.get_path("报告") / f"{title}_更新方案.md")
        ok = messagebox.askyesno(
            "确认更新",
            f"《{title}》新版有一些内容变化：\n\n{summary}\n\n"
            "更新会这样处理：没变的段落原样保留、变了或删了的老段落收档留底、新增内容接着往后排。\n"
            "更新前自动整本备份，更新后自动核验一遍，通不过会恢复原样。\n"
            f"（待更新清单已存好：{detail.name}）\n\n确定更新吗？")
        if not ok:
            self._busy = False
            self.status.config(text="已取消更新（书库未改动）。", fg="gray")
            self.refresh()
            return
        self.status.config(text=f"正在更新《{title}》…（界面可正常操作）", fg="orange")
        self._task_id = taskcenter.start("更新", f"《{title}》")
        taskcenter.progress(self._task_id, step="执行更新（自动备份＋校验）")
        threading.Thread(target=self._update_exec_worker,
                         args=(title, book_id, path, self._task_id), daemon=True).start()

    def _update_exec_worker(self, title, book_id, path, task_id):
        """后台线程：手术执行（快照＋对账＋红灯回滚都在 update_book 内完成）。"""
        q = self._op_q.put
        try:
            code, out, err = run_script("update_book.py", "--书号", book_id, "--新版", path,
                                        on_line=lambda ln: taskcenter.log(task_id, ln),
                                        timeout=3600)
        except Exception as e:
            taskcenter.finish(task_id, False, "更新异常")
            q(("update_done", title, 1, f"后台执行异常：{e}"))
            return
        taskcenter.finish(task_id, code == 0,
                          "更新完成" if code == 0 else "更新未通过校验，已恢复原样")
        q(("update_done", title, code, (out or err or "")[-800:]))

    def _on_update_done(self, title, code, tail):
        """手术收尾（主线程）。"""
        self._busy = False
        if code == 0:
            # #46：更新若有变化章进入「待重标」，弹窗提示并提供【立即标注】入口
            if "待重标章" in tail:
                note = f"；有 {self._reannotate_count(tail)} 章待重新标注"
                self.status.config(text=f"🟢 《{title}》更新完成，系统核验通过{note}。", fg="green")
                self._prompt_reannotate(title, tail)
            else:
                self.status.config(
                    text=f"🟢 《{title}》更新完成，系统核验通过。", fg="green")
                messagebox.showinfo("更新完成",
                                    f"《{title}》更新完成，系统核验通过，书库内容已是最新版。")
        else:
            self.status.config(text=f"🔴 《{title}》更新未通过核验，已自动恢复原样。", fg="red")
            show_issue_dialog(self, "更新未能通过核验",
                              f"《{title}》更新后系统核验没通过，书库已自动恢复原样。\n\n"
                              f"可复制给 AI 排查。", detail=tail)
        self.refresh()

    @staticmethod
    def _reannotate_count(tail: str) -> int:
        """从更新日志 tail 解析待重标章数量（#46）。update_book 输出形如
        「待重标章：01, 02（旧 .done/旧回填已清，新任务包已生成）」；解析不到返回 0。"""
        import re as _re
        m = _re.search(r"待重标章：([0-9,，\s]+)", tail)
        if not m:
            return 0
        return len([s for s in _re.split(r"[,，\s]+", m.group(1)) if s])

    def _prompt_reannotate(self, title: str, tail: str):
        """#46 更新成功且有变化章待重标的弹窗：提示章数＋【立即标注】/【稍后然后再手动标注】。

        【立即标注】关闭本弹窗后调用 start_annotate（API 自动标注，与主页「开始标注」同入口）；
        选【稍后再标】仅关闭，提示用户之后手动点主页「开始标注」。"""
        n = self._reannotate_count(tail) or 1
        win = tk.Toplevel(self)
        win.title("更新完成")
        try:
            win.transient(self.winfo_toplevel())
        except Exception:
            pass
        ttk.Label(
            win,
            text=f"《{title}》更新完成，系统核验通过。\n\n"
                 f"本次有 {n} 章内容发生变化，已进入「待重标」：这几章的旧标注已清掉，重新生成了标注文件。\n\n"
                 f"现在就用标注 AI 立即标注这些变化章吗？\n\n"
                 f"（如果选稍后，之后到主页上方点「开始标注」选中这本书即可。）",
            wraplength=480, justify="left").pack(padx=14, pady=(14, 10))
        btns = ttk.Frame(win)
        btns.pack(pady=(0, 14))

        def do_annotate():
            win.destroy()
            self.start_annotate(title)

        ttk.Button(btns, text="立即标注", command=do_annotate).pack(side="left", padx=4)
        ttk.Button(btns, text="稍后再标", command=win.destroy).pack(side="left", padx=4)
        return win

    # ---------- 删除书籍 ----------

    def delete_book(self):
        """删除选中书籍（#38：改为后台线程执行——原先 rmtree 大提取缓存会冻结界面 0.3~1s）。

        确认后在 worker 里跑「书库目录 + 提取缓存 + 相关报告 + 任务包 + 移回原件 + 书目总索引」，
        进度经 TaskCenter 上报（状态条「正在删除…（n/6）」可见），完成后 _poll_op 收尾。"""
        b = self._selected()
        if not b:
            return
        title = b["书名"]
        book_dir = common.get_path("知识库") / "书籍" / title
        if not book_dir.exists():
            messagebox.showerror("错误", f"书籍目录不存在：{book_dir}")
            return
        if self._busy:
            messagebox.showinfo("提示", "已有排障操作进行中，请等待完成")
            return

        if not messagebox.askyesno(
            "确认删除",
            f"将彻底删除《{title}》：\n\n"
            f"• 书库里的书（原文、搜索、章节摘要）\n"
            f"• 识别缓存\n"
            f"• 检查与自动验证报告\n"
            f"• 评测集里这本书的评测题（其余题号自动接续）\n"
            f"• 书库登记清单里的这一条\n\n"
            f"原始文件会移回 待入库（原件保留）。\n\n"
            f"确定继续吗？"):
            return

        self._busy = True
        self.status.config(text=f"正在删除《{title}》…（界面可正常操作）", fg="orange")
        self._task_id = taskcenter.start("删除", f"《{title}》", total=6)
        taskcenter.progress(self._task_id, current=0, step="清理书库目录")
        threading.Thread(target=self._delete_worker, args=(title, b["书号"], self._task_id),
                         daemon=True).start()

    def _purge_book_products(self, title, book_id, report=None, fine=None):
        """清理某书的全部产物（即删除的 ②~⑥ 步），返回清理明细列表。

        #39：删书与「残留清理」共用同一份实现（同一规则只写一处）；各步幂等、可重复调用——
        中断后再跑一遍就能续完，残留清理正是建立在此之上。
        report(step_text)：步骤级进度（推进进度条）；fine(step_text)：细粒度文本（不推条）。"""
        def _r(step):
            if report:
                report(step)

        def _f(text):
            if fine:
                fine(text)

        removed = []
        # ① 提取缓存（按书号）——可能近万文件，用计数式删除给出可感知进度（#39-C）
        cache = None
        if book_id:
            cache = common.get_path("输出") / "提取缓存" / book_id
        if cache is not None and cache.exists():
            n = _counted_rmtree(
                cache,
                on_progress=(lambda done, total: _f(f"清理识别缓存（已删 {done}/{total} 个文件）"))
                if fine else None)
            removed.append(f"识别缓存（{n} 个文件）")
        # ② 该书全部报告（按书名/书号）+ 验证目录产物
        _r("清理报告")
        report_dir = common.report_dir()
        for stem in {title, book_id} - {""}:
            for p in sorted(report_dir.glob(f"{stem}_*.md")):
                try:
                    p.unlink()
                    removed.append(f"报告 {p.name}")
                except Exception:
                    pass
        verify_dir = common.get_path("输出") / "验证"
        for name in (f"{title}_术语候选.json", f"{title}_评测原句.txt"):
            p = verify_dir / name
            if p.exists():
                try:
                    p.unlink()
                    removed.append(f"验证 {name}")
                except Exception:
                    pass
        # ③ 任务包（新结构按书分目录：输出\任务包\{书名}\）
        _r("清理任务包")
        pack_sub = common.get_path("输出") / "任务包" / title
        if pack_sub.exists():
            _counted_rmtree(pack_sub)
            removed.append("任务包目录")
        # ④ 已入库原始文件 → 移回 待入库（按书号登记精确匹配；旧数据回退按书名）
        _r("移回原始文件")
        moved = self._move_back_ingested(title, book_id)
        if moved:
            removed.append(f"原件移回待入库：{'、'.join(moved)}")
        # ⑤ 书目总索引删行 + 入库登记删条目（按书号；缺失回退按书名）——兜住「原件已丢失」的残留
        _r("清理登记")
        catalog = common.get_path("知识库") / "书目总索引.md"
        if catalog.exists() and book_id:
            lines = catalog.read_text(encoding="utf-8").split("\n")
            keep = [l for l in lines if f"| {book_id} |" not in l]
            if len(keep) != len(lines):
                catalog.write_text("\n".join(keep) + "\n", encoding="utf-8")
                removed.append("书目总索引登记行")
        try:
            rows = common.load_ingest_registry()
            keep = [r for r in rows
                    if not ((book_id and r.get("书号") == book_id) or r.get("书名") == title)]
            if len(keep) != len(rows):
                common.save_ingest_registry(keep)
                removed.append("入库登记条目")
        except Exception:
            pass
        # ⑥ 评测集删该书题行并题号重连（#56：书号复用串书根因——按书号收回该书评测题钥匙）
        _r("清理评测集")
        pr = annotate_pack.purge_book_evals(book_id)
        if pr["删除题数"]:
            removed.append(f"评测集 {pr['删除题数']} 题（其余题号接续）")
        return removed

    def _delete_worker(self, title, book_id, task_id):
        """后台线程：删书全流程（① 书库目录 → _purge_book_products ②~⑥）；
        只入队结果/写 TaskCenter，不碰任何控件。"""
        removed = []
        cnt = {"n": 0}

        def _step(text):
            cnt["n"] = min(6, cnt["n"] + 1)
            taskcenter.progress(task_id, current=cnt["n"] - 1, step=text)

        def _fine(text):
            taskcenter.progress(task_id, step=text)

        try:
            _step("清理书库目录")
            shutil.rmtree(common.get_path("知识库") / "书籍" / title, ignore_errors=True)
            removed.append("书库目录")
            removed.extend(self._purge_book_products(title, book_id, report=_step, fine=_fine))
            taskcenter.progress(task_id, current=6)
            taskcenter.finish(task_id, True, "删除完成")
            self._op_q.put(("delete_done", title, True, "、".join(removed)))
        except Exception as e:
            taskcenter.finish(task_id, False, "删除出错")
            self._op_q.put(("delete_done", title, False, f"{e}\n已清理：{'、'.join(removed)}"))

    def _on_delete_done(self, title, ok, detail):
        self._busy = False
        try:
            if ok:
                self._annot_cache.pop(title, None)
                self.status.config(text=f"🟢 《{title}》已删除", fg="green")
                messagebox.showinfo("删除完成", f"《{title}》已删除。\n清理：{detail}")
            else:
                self.status.config(text=f"🔴 《{title}》删除失败", fg="red")
                show_issue_dialog(self, "删除失败",
                                  f"《{title}》删除过程中出了问题，书库内容可能处于半删除状态。\n\n"
                                  "可复制给 AI 排查。", detail=detail)
        except tk.TclError:
            pass
        self.refresh()

    # ---------- #39：残留检测与一键清理 ----------

    def _residual_books(self, books=None):
        """检测「登记在册但书库无目录」的残留书（**只读**，不改任何产物）。

        名册＝`书目总索引.md`（书名/书号）+ `已入库\\_入库登记.json`（文件名/书名/书号）；
        残留＝名册中有该书名/书号、但 `知识库\\书籍\\{书名}\\原文.md` 不存在；
        另附孤儿缓存（`输出\\提取缓存\\{书号}` 存在但名册与存量都没有它）。"""
        kb = common.get_path("知识库")
        out = {}      # 书号（或书名）-> {书名, 书号, 文件名}
        catalog = kb / "书目总索引.md"
        if catalog.exists():
            try:
                for line in catalog.read_text(encoding="utf-8").split("\n"):
                    s = line.strip()
                    if not s.startswith("|") or s.startswith("|---"):
                        continue
                    cells = [c.strip() for c in s.strip("|").split("|")]
                    if len(cells) >= 2 and cells[0] and cells[0] != "书名" and cells[1]:
                        out.setdefault(cells[1], {"书名": cells[0], "书号": cells[1], "文件名": ""})
            except Exception:
                pass
        try:
            for r in common.load_ingest_registry():
                bid = str(r.get("书号") or "")
                title = str(r.get("书名") or "")
                fname = str(r.get("文件名") or "")
                if bid:
                    d = out.setdefault(bid, {"书名": title, "书号": bid, "文件名": ""})
                    d["书名"] = d["书名"] or title
                    d["文件名"] = d["文件名"] or fname
                elif title:
                    out.setdefault(title, {"书名": title, "书号": "", "文件名": fname})
        except Exception:
            pass
        # 现存的（有 原文.md 的）书不算残留；其书号也要记下，供孤儿缓存判定
        alive, alive_ids = set(), set()
        for b in (books if books is not None else get_books()):
            alive.add(b["书名"])
            if b.get("书号"):
                alive_ids.add(b["书号"])
        resid = [v for v in out.values() if v["书名"] and v["书名"] not in alive]
        cache_root = common.get_path("输出") / "提取缓存"
        if cache_root.exists():
            known = set(out.keys()) | alive | alive_ids
            try:
                for d in cache_root.iterdir():
                    if d.is_dir() and d.name not in known:
                        resid.append({"书名": f"（未知书·缓存目录 {d.name}）",
                                      "书号": d.name, "文件名": ""})
            except OSError:
                pass
        resid.sort(key=lambda x: str(x.get("书号") or x.get("书名")))
        return resid

    def _refresh_residue(self, books=None):
        """刷新残留提示行与「清理残留（N）」按钮态（只读）。"""
        try:
            resid = self._residual_books(books)
        except Exception:
            resid = []
        names = "、".join(f"《{r['书名']}》" for r in resid[:3]) + ("…" if len(resid) > 3 else "")
        try:
            self.residue_lbl.config(
                text=(f"⚠️ 检测到 {len(resid)} 本书的残留数据（书已不在书库，但有遗留文件和登记）："
                      f"{names}　可用「高级 → 清理残留」收尾") if resid else "")
            self.cleanup_btn.config(text=f"清理残留（{len(resid)}）" if resid else "清理残留")
            self.cleanup_btn.state(["!disabled"] if resid else ["disabled"])
        except tk.TclError:
            pass

    def _maybe_auto_clean(self, books=None):
        """#39（2026-09-14 用户口径）：残留**自动**后台清理，无需事前确认。

        为何可自动化：残留按定义＝「书库目录已不存在」＝该书已不可用，清理不毁可用数据；
        且**不删原件**（原件移回 `待入库` 保留，可重新入库）。防误判：仅在无写盘任务（入库/删除/
        重建/其他清理）在跑时才动手——入库中的 `提取缓存\\{书号}`、删除中「目录已删登记未清」的
        中间态都会被这几道闸挡住。同一批残留只自动跑一次（失败不反复重试），执行留痕见任务日志。"""
        if self._auto_clean_running or self._busy:
            return
        if (taskcenter.running("入库") or taskcenter.running("删除")
                or taskcenter.running("重建") or taskcenter.running("标注")
                or taskcenter.running("更新") or taskcenter.running("清理")):
            return          # 有其它写盘任务在跑：等安静时再说（避免把中间态误判为残留）
        try:
            resid = self._residual_books(books)
        except Exception:
            return
        keys = tuple(sorted(f"{r['书号']}|{r['书名']}" for r in resid))
        if not resid or keys == self._auto_cleaned:
            return
        self._auto_cleaned = keys
        self._auto_clean_running = True
        self._busy = True
        self._task_id = taskcenter.start("清理", f"共 {len(resid)} 本", total=len(resid))
        taskcenter.progress(self._task_id, current=0, step="自动清理残留")
        taskcenter.log(self._task_id, f"检测到 {len(resid)} 本书的残留数据（书已不在书库，但有遗留文件和登记），自动清理："
                                      + "、".join(f"《{r['书名']}》" for r in resid))
        self.status.config(text=f"检测到 {len(resid)} 本残留，正在自动清理…（界面可正常操作）", fg="orange")
        threading.Thread(target=self._cleanup_worker, args=(resid, self._task_id, True),
                         daemon=True).start()

    def clean_residue(self):
        """#39：一键清理「登记在册但书库无目录」的残留（补做完删除的 ②~⑥ 步）。"""
        resid = self._residual_books()
        if not resid:
            messagebox.showinfo("清理残留", "未检测到残留记录，无需清理。")
            return
        if self._busy:
            messagebox.showinfo("提示", "已有排障操作进行中，请等待完成")
            return
        lines = "\n".join(
            f"  • 《{r['书名']}》" + (f"（书号 {r['书号']}）" if r["书号"] else "")
            for r in resid[:20])
        more = f"\n  …共 {len(resid)} 本" if len(resid) > 20 else ""
        if not messagebox.askyesno(
                "确认清理残留",
                f"检测到 {len(resid)} 本书的残留数据（书已不在书库，但还留了一些文件和登记）：\n"
                f"{lines}{more}\n\n"
                "会清掉识别缓存、报告、核验产物、任务包和登记记录；"
                "已入库的原始文件会移回 待入库（原件保留）。\n\n确定继续吗？"):
            return
        self._busy = True
        self.status.config(text=f"正在清理 {len(resid)} 本残留…（界面可正常操作）", fg="orange")
        self._task_id = taskcenter.start("清理", f"共 {len(resid)} 本", total=len(resid))
        taskcenter.progress(self._task_id, current=0, step="开始清理")
        threading.Thread(target=self._cleanup_worker, args=(resid, self._task_id),
                         daemon=True).start()

    def _cleanup_worker(self, resid, task_id, auto=False):
        """后台线程：逐本清理残留（只写 TaskCenter/入队，不碰控件）。"""
        done, failed = [], []
        for idx, r in enumerate(resid, 1):
            title, bid = r["书名"], r["书号"]
            taskcenter.progress(task_id, current=idx - 1,
                                step=f"清理《{title}》（{idx}/{len(resid)}）")
            try:
                removed = self._purge_book_products(
                    title, bid,
                    report=lambda t, ti=title: taskcenter.progress(task_id, step=f"《{ti}》{t}"),
                    fine=lambda t, ti=title: taskcenter.progress(task_id, step=f"《{ti}》{t}"))
                done.append(title)
                taskcenter.log(task_id, f"✅ 《{title}》残留已清理：{'、'.join(removed) or '无'}")
            except Exception as e:
                failed.append((title, str(e)))
                taskcenter.log(task_id, f"❌ 《{title}》清理出错：{e}")
            taskcenter.progress(task_id, current=idx)
        taskcenter.finish(task_id, not failed, f"清理 {len(done)} 本／失败 {len(failed)} 本")
        self._op_q.put(("cleanup_done", done, failed, auto))

    def _on_cleanup_done(self, done, failed, auto=False):
        self._busy = False
        self._auto_clean_running = False
        try:
            if failed:
                self.status.config(
                    text=f"🔴 残留清理部分失败（{len(failed)} 本）——详见任务日志，可点「高级 → 清理残留」重试",
                    fg="red")
                if not auto:
                    detail = "\n".join(f"  《{t}》：{why}" for t, why in failed)
                    messagebox.showwarning("部分清理失败", f"这几本残留没能清干净：\n{detail}")
            elif done:
                self.status.config(
                    text=f"🟢 已清理 {len(done)} 本残留（原件已移回 待入库，详见任务日志）", fg="green")
                if not auto:
                    messagebox.showinfo("清理完成", f"已清理 {len(done)} 本残留记录。")
            elif not auto:
                self.status.config(text="未检测到残留记录", fg="gray")
        except tk.TclError:
            pass
        self.refresh()

    def _move_back_ingested(self, title, book_id):
        """把 已入库\ 里属于该书的原始文件移回 待入库\。返回移回的文件名列表。"""
        in_dir = common.get_path("已入库")
        todo_dir = common.get_path("待入库")
        if not in_dir.exists():
            return []
        todo_dir.mkdir(parents=True, exist_ok=True)
        names = []
        if book_id:
            names = [r["文件名"] for r in common.load_ingest_registry()
                     if r.get("书号") == book_id and r.get("文件名")]
        if not names:
            for f in in_dir.iterdir():
                if f.is_file() and f.name != "_入库登记.json":
                    if common.clean_book_title(f.name) == title:
                        names.append(f.name)
        moved = []
        for name in names:
            src = in_dir / name
            if not src.exists():
                continue
            dst = todo_dir / name
            if dst.exists():  # 待入库已有同名 → 追加序号避免覆盖
                stem, ext = os.path.splitext(name)
                i = 2
                while (todo_dir / f"{stem}（{i}）{ext}").exists():
                    i += 1
                dst = todo_dir / f"{stem}（{i}）{ext}"
            shutil.move(str(src), str(dst))
            moved.append(dst.name)
        if moved:
            rows = [r for r in common.load_ingest_registry() if r.get("文件名") not in names]
            common.save_ingest_registry(rows)
        return moved
