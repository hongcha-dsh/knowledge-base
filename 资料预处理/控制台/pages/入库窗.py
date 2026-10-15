# -*- coding: utf-8 -*-
"""「添加书籍」窗口（#25 M3）：原「入库页」改为按需弹出的 Toplevel 单例。

- 打开即扫描「待入库」目录并默认全选；「开始入库（n）」合并原「入库选中 / 全部入库」，文案随勾选数变化；
- 「刷新」按钮删除（每次打开自动扫描）；「添加文件…」保留；
- 进度条 + 当前步骤常显，「详细日志」默认折叠；完成后窗口内结果区显示红绿灯汇总 +「立即自动标注」；
- OCR 模式三选一保留在「高级选项」折叠区；
- **关窗 ≠ 取消**：后台任务只写全局 TaskCenter，关窗后任务继续、进度回主窗口底部状态条
  （设计文档 #25 §3.2 / §4.1；worker 线程不碰任何控件，故窗口销毁后仍安全跑完）。

#25 M3 / R4：删除原「生成任务包 → 自动标注」旧链——`annotate_pack.py --自动回填` 内部
首次会自动 `generate_all_packs`，无需前置生成任务包；结果区只保留「立即自动标注」。
"""

import queue
import shutil
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import ttk, messagebox, filedialog

import common
import llm_client
import mineru_client
import theme
from . import run_script, taskcenter

TEXT_EXTS = {".docx", ".doc", ".txt"}
PDF_PROBE_PAGES = 5  # PDF 探测只读前 5 页文本量


def _read_warn_lines(report) -> list:
    """读取单个报告文件的「⚠️ 黄灯」块内 "- " 行；无文件/无黄灯返回空列表。"""
    if not report.exists():
        return []
    warns = []
    try:
        in_warn_block = False
        for ln in report.read_text(encoding="utf-8").splitlines():
            if "⚠️ 黄灯" in ln:
                in_warn_block = True
                continue
            if in_warn_block:
                if ln.startswith("- "):
                    warns.append(ln[2:].strip())
                elif ln.strip():
                    break
    except Exception:
        return []
    return warns


class 入库窗(tk.Toplevel):
    """「添加书籍」窗口：扫描待入库 → 类型探测 → 批量入库 → 结果区引导自动标注。"""

    def __init__(self, master):
        super().__init__(master)
        self.title("添加书籍")
        self.geometry("740x560")
        self.minsize(660, 440)
        self.transient(master)
        self._log_q = queue.Queue()
        self._running = False
        self._task_id = None
        self._probe = {}            # 文件名 -> 类型标签
        self._ok_books = []         # 本次成功入库的书（供结果区「立即自动标注」）
        self._prog_mode = "determinate"
        self._log_open = True       # #38 返工：按用户口径「详细日志」默认展开
        self._last_line = ""        # #38：最近一条输出（常显「最近输出」行，补折叠日志后的进度感知）
        self._task_started = 0.0    # #38：当前任务起始时刻（步骤行显示「已用时」）
        self._adopted = False       # #38：视图是否由「关窗重开」重新挂接（日志改从 TaskCenter 渲染）
        self._log_shown = -1        # adapted 路径已渲染到的日志行数
        self._result_shown = False
        self._build()
        self.refresh()
        self._adopt_running_task()
        self.after(100, self._poll_ui)

    # ---------- 界面 ----------

    def _build(self):
        ttk.Label(self, text="待入库文件（默认全选，可多选）：", anchor="w").pack(fill="x", padx=8, pady=(8, 0))

        top = ttk.Frame(self)
        top.pack(fill="x", padx=8, pady=4)
        self.ingest_btn = ttk.Button(top, text="开始入库", style="Primary.TButton", command=self.run_ingest)
        self.ingest_btn.pack(side="left")
        ttk.Button(top, text="添加文件…", command=self.add_files).pack(side="left", padx=6)

        cols = ("文件", "大小", "类型")
        self.tree = ttk.Treeview(self, columns=cols, show="headings",
                                 selectmode="extended", height=6)
        self.tree.heading("文件", text="文件")
        self.tree.heading("大小", text="大小")
        self.tree.heading("类型", text="类型（自动探测）")
        self.tree.column("文件", width=340)
        self.tree.column("大小", width=80, anchor="e")
        self.tree.column("类型", width=240)
        self.tree.pack(fill="x", padx=8, pady=2)
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._sync_ingest_btn())

        # 高级选项（OCR 模式，默认自动判断，新手无需关心）
        self._adv_open = False
        self.adv_toggle = ttk.Button(self, text="▸ 高级选项（OCR 模式，默认自动判断）",
                                     command=self._toggle_adv)
        self.adv_toggle.pack(anchor="w", padx=8, pady=(2, 0))
        self.adv_frame = ttk.Frame(self)
        self.ocr_mode = tk.StringVar(value="auto")
        ttk.Radiobutton(self.adv_frame, text="自动判断（推荐）", variable=self.ocr_mode,
                        value="auto").pack(side="left", padx=8)
        ttk.Radiobutton(self.adv_frame, text="强制启用 OCR", variable=self.ocr_mode,
                        value="on").pack(side="left", padx=8)
        ttk.Radiobutton(self.adv_frame, text="强制关闭 OCR", variable=self.ocr_mode,
                        value="off").pack(side="left", padx=8)

        # 进度条 + 当前步骤（常显）
        self.pbar = ttk.Frame(self)
        self.pbar.pack(fill="x", padx=8, pady=(6, 0))
        self.prog = ttk.Progressbar(self.pbar, mode="determinate", length=220)
        self.prog.pack(side="left")
        self.step_lbl = ttk.Label(self.pbar, text="就绪", anchor="w")
        self.step_lbl.pack(side="left", fill="x", expand=True, padx=8)

        # #38：常显「最近输出」行——详细日志默认折叠后，靠这行动态文字让人感知进度（长步骤也不再像卡死）
        self.tail_lbl = ttk.Label(self, text="", anchor="w", foreground=theme.INK3)
        self.tail_lbl.pack(fill="x", padx=8, pady=(0, 2))

        # 结果区（完成后出现；#25 M3 / R4：只留「立即自动标注」+ 关闭）
        self.result_frame = ttk.LabelFrame(self, text="本次结果")
        self.result_lbl = tk.Label(self.result_frame, text="", anchor="w", justify="left",
                                   wraplength=720)
        self.result_lbl.pack(anchor="w", padx=8, pady=(4, 2))
        rrow = ttk.Frame(self.result_frame)
        rrow.pack(fill="x", padx=8, pady=(0, 6))
        self.annotate_now_btn = ttk.Button(rrow, text="立即自动标注", command=self._annotate_now)
        self.goto_settings_btn = ttk.Button(
            rrow, text="去设置", command=lambda: self._open_settings("annotate"))
        self.annotate_hint = ttk.Label(
            rrow, text="未配置标注 AI：点「去设置」补配置，或到主页展开「高级 → 手动 AI 标注」，把任务包发给任意 AI 填写",
            foreground=theme.INK3, wraplength=560)
        ttk.Button(rrow, text="关闭", command=self.destroy).pack(side="right")

        # 详细日志（#38 返工：默认展开——用户口径「弹窗偏大、日志默认打开」）
        self.log_toggle = ttk.Button(self, text="▾ 详细日志", command=self._toggle_log)
        self.log_toggle.pack(anchor="w", padx=8, pady=(6, 0))
        self.log = tk.Text(self, height=11)
        self.log.pack(fill="both", expand=True, padx=8, pady=4)

    def on_show(self):
        """窗口再次置顶显示（单例 deiconify）：重扫 待入库\\ 并重新挂接运行中任务（#38）。"""
        self.refresh()
        self._adopt_running_task()

    def _adopt_running_task(self):
        """关窗 ≠ 取消：重开后把视图重新挂到正在跑的入库/标注任务上（否则进度无从显示，#38 返工）。
        日志/结果改从 TaskCenter 渲染（原实例的 _log_q 已随窗口销毁）。
        只有「入库」任务才占用 _running（异类可并行，R6）。"""
        if self._task_id:
            return
        try:
            task = taskcenter.running_task(("入库", "标注"))
        except Exception:
            task = None
        if not task:
            return
        self._task_id = task["id"]
        self._task_started = task.get("started") or time.monotonic()
        self._adopted = True
        self._running = task["kind"] == "入库"
        self._log_shown = -1
        self._result_shown = False
        self.result_frame.pack_forget()
        self.step_lbl.config(text="已接续正在运行的任务…")
        self._sync_ingest_btn()

    def _toggle_adv(self):
        if self._adv_open:
            self.adv_frame.pack_forget()
            self.adv_toggle.config(text="▸ 高级选项（OCR 模式，默认自动判断）")
        else:
            self.adv_frame.pack(fill="x", padx=8, pady=2, after=self.adv_toggle)
            self.adv_toggle.config(text="▾ 高级选项（OCR 模式，默认自动判断）")
        self._adv_open = not self._adv_open

    def _toggle_log(self):
        if self._log_open:
            self.log.pack_forget()
            self.log_toggle.config(text="▸ 详细日志")
        else:
            self.log.pack(fill="both", expand=True, padx=8, pady=4)
            self.log_toggle.config(text="▾ 详细日志")
        self._log_open = not self._log_open

    def _sync_ingest_btn(self):
        """主按钮文案随勾选数变化（开始入库（n））；入库中禁用。"""
        n = len(self.tree.selection())
        if self._running:
            self.ingest_btn.state(["disabled"])
            return
        self.ingest_btn.state(["!disabled"])
        self.ingest_btn.config(text=f"开始入库（{n}）" if n else "开始入库")

    # ---------- 文件列表与探测 ----------

    def add_files(self):
        in_dir = common.get_path("待入库")
        in_dir.mkdir(parents=True, exist_ok=True)
        paths = filedialog.askopenfilenames(
            title="选择要入库的文件",
            filetypes=[("支持的文件", "*.docx *.doc *.txt *.pdf"), ("所有文件", "*.*")])
        if not paths:
            return
        for p in paths:
            shutil.copy2(p, in_dir / Path(p).name)
        self.refresh()

    def refresh(self):
        in_dir = common.get_path("待入库")
        in_dir.mkdir(parents=True, exist_ok=True)
        files = sorted(p for p in in_dir.iterdir() if p.is_file())
        has_token = bool(mineru_client.get_token())
        mode = self.ocr_mode.get()
        self._probe = {}
        self.tree.delete(*self.tree.get_children())
        for p in files:
            kind = self._probe_file(p)
            self._probe[p.name] = kind
            if self._needs_mineru(p.name, mode, self._probe) and not has_token:
                kind += " ✗未配Token"
            self.tree.insert("", "end", iid=p.name,
                             values=(p.name, self._fmt_size(p.stat().st_size), kind))
        if files:  # 默认全选
            self.tree.selection_set(self.tree.get_children())
        self._sync_ingest_btn()

    @staticmethod
    def _fmt_size(n: int) -> str:
        return f"{n / 1048576:.1f}MB" if n >= 1048576 else f"{n / 1024:.0f}KB"

    @staticmethod
    def _probe_file(path: Path) -> str:
        """类型探测：文本类直提；PDF 读前几页文本量判断是否扫描版。"""
        ext = path.suffix.lower()
        if ext in TEXT_EXTS:
            return "文本类·直接读取"
        if ext == ".pdf":
            try:
                import fitz
            except ImportError:
                return "PDF（缺 pymupdf 无法探测，按直接读取处理）"
            try:
                with fitz.open(str(path)) as doc:
                    n = min(PDF_PROBE_PAGES, len(doc))
                    if n == 0:
                        return "扫描版·需 OCR 识别"
                    chars = sum(len(doc[i].get_text().strip()) for i in range(n))
                limit = float(common.get_thresholds().get("每页字数下限", 30))
                return "扫描版·需 OCR 识别" if chars / n < limit else "文字层 PDF·直接读取"
            except Exception:
                return "PDF（探测失败，按直接读取处理）"
        return "未知格式·尝试直接读取"

    @staticmethod
    def _ocr_decision(filename: str, mode: str) -> str:
        """按 OCR 模式返回三态（4.8 矩阵）：on/off 强制胜负；auto 下 PDF 交
        extract 矩阵决策（有文本层走文本层，无文本层看 mineru_版面优选）。
        R13：mode 由批开始时的快照传入，worker 不读 UI 变量。"""
        if not filename.lower().endswith(".pdf"):
            return "off"
        if mode == "on":
            return "on"
        if mode == "off":
            return "off"
        return "auto"

    @staticmethod
    def _needs_mineru(filename: str, mode: str, probe: dict) -> bool:
        """本次入库是否真会走到 MinerU 通道（Token 前置检查 + 列表未配Token标记用）。"""
        if not filename.lower().endswith(".pdf"):
            return False
        if mode == "on":
            return True
        if mode == "off":
            return False
        if not bool(common.load_config().get("mineru_版面优选", True)):
            return False
        return "需 OCR 识别" in probe.get(filename, "")

    # ---------- 日志（线程安全，窗口销毁后仍安全） ----------

    @staticmethod
    def _read_gate_warns(book_title: str) -> list:
        """读取《书名》黄灯行：闸门报告 + 书级配置报告（#20）；只读，无则空列表。"""
        report_dir = common.report_dir()
        warns = []
        for name in (f"{book_title}_闸门.md", f"{book_title}_书配置.md"):
            warns.extend(_read_warn_lines(report_dir / name))
        return warns

    def _log(self, msg):
        """线程安全：只入队，实际渲染由主线程 _poll_ui 完成；#25 同时写全局任务中心。
        （仅用 queue + TaskCenter，不碰控件——故窗口销毁后 worker 仍可安全调用。）"""
        self._log_q.put(msg)
        if isinstance(msg, str):
            self._last_line = msg      # #38：「最近输出」行数据源
        if self._task_id:
            taskcenter.log(self._task_id, msg)

    def _poll_ui(self):
        """窗口视图泵：本实例视图渲自 `_log_q`；接续视图（关窗重开）改从 TaskCenter 重绘；
        两者都从 TaskCenter 同步进度条与步骤。"""
        try:
            if not self.winfo_exists():   # 窗口已销毁：停止泵（worker 继续只写 TaskCenter）
                return
        except tk.TclError:
            return
        if not self._adopted:
            try:
                while True:
                    msg = self._log_q.get_nowait()
                    if isinstance(msg, tuple) and msg[0] == "__DONE_BATCH__":
                        self._on_batch_done(msg[1], msg[2], msg[3])
                    elif isinstance(msg, tuple) and msg[0] == "__FILL_DONE__":
                        self._on_fill_done(msg[1])
                    else:
                        self.log.insert("end", str(msg) + "\n")
                        self.log.see("end")
            except queue.Empty:
                pass
            except tk.TclError:
                pass
        self._render_state()
        self._sync_progress()
        try:
            if self.winfo_exists():
                self.after(100, self._poll_ui)
        except tk.TclError:
            pass

    def _render_state(self):
        """刷新「最近输出」；接续视图（_adopted）额外从 TaskCenter 重绘日志，并在任务结束后收尾。"""
        try:
            if not self._adopted:
                self.tail_lbl.config(
                    text=("最近输出：" + self._last_line)[:150] if self._last_line else "")
                return
            tid = self._task_id
            if not tid:
                return
            task = next((t for t in taskcenter.snapshot() if t["id"] == tid), None)
            if task is None:
                return
            if task["log_count"] != self._log_shown:
                self._log_shown = task["log_count"]
                lines = taskcenter.log_lines(tid)
                show = lines[-800:]
                self.log.delete("1.0", "end")
                self.log.insert("end", "\n".join(show) + ("\n" if show else ""))
                self.log.see("end")
                self._last_line = lines[-1] if lines else ""
                self.tail_lbl.config(
                    text=("最近输出：" + self._last_line)[:150] if self._last_line else "")
            if task["status"] != "running" and not self._result_shown:
                self._result_shown = True
                self._adopted = False     # 交回收尾
                self._finish_by_result(task)
        except tk.TclError:
            pass

    def _finish_by_result(self, task):
        """按 Task 结果载荷收尾（接续视图在任务结束后走这里；本实例视图走 __DONE_BATCH__ 哨兵）。"""
        r = taskcenter.result(task["id"]) or {}
        mode = r.get("模式")
        if mode == "入库":
            self._on_batch_done(r.get("成功") or [], r.get("失败") or [], r.get("黄灯") or [])
        elif mode == "标注":
            self._on_fill_done(r.get("完成") or [])
        else:
            self._running = False
            self._sync_ingest_btn()
            self._reset_progress()
            self.refresh()

    def _sync_progress(self):
        """进度条 + 当前步骤行：数据源＝全局 TaskCenter（与主窗口状态条同源）。"""
        if not self._task_id:
            return
        try:
            task = next((t for t in taskcenter.snapshot() if t["id"] == self._task_id), None)
        except Exception:
            return
        if task is None or task["status"] != "running":
            return
        if task["total"]:
            if self._prog_mode != "determinate":
                self.prog.stop()
                self.prog.config(mode="determinate")
                self._prog_mode = "determinate"
            self.prog.config(maximum=task["total"], value=task["current"])
        elif self._prog_mode != "indeterminate":
            self.prog.config(mode="indeterminate")
            self.prog.start(80)
            self._prog_mode = "indeterminate"
        text = f"正在{task['kind']}：{task['title']}"
        if task["total"]:
            text += f"（{task['current']}/{task['total']}）"
        if task["step"]:
            text += f" — {task['step']}"
        if self._task_started:   # #38：已用时秒表（步骤长时间不变时也让人看到"活着"）
            el = max(0, int(time.monotonic() - self._task_started))
            text += f"　已用时 {el // 60:02d}:{el % 60:02d}"
        self.step_lbl.config(text=text)

    def _reset_progress(self):
        try:
            self.prog.stop()
            self.prog.config(mode="determinate", value=0)
        except tk.TclError:
            pass
        self._prog_mode = "determinate"

    # ---------- 入库主流程 ----------

    def run_ingest(self):
        if self._running or taskcenter.running("入库"):
            messagebox.showinfo("提示", "已有入库任务进行中，请等待完成")
            return
        files = [self.tree.item(i, "values")[0] for i in self.tree.selection()]
        if not files:
            messagebox.showwarning("提示", "请先在列表中选中要入库的文件（打开时已默认全选）")
            return
        mode = self.ocr_mode.get()
        # Token 前置检查：本次真会走 MinerU 通道（扫描版 PDF + 版面优选开）但未配 Token → 拦下
        if not mineru_client.get_token():
            blocked = [f for f in files if self._needs_mineru(f, mode, self._probe)]
            if blocked:
                messagebox.showwarning(
                    "缺少 MinerU Token",
                    "以下文件需要在线识别（扫描版 PDF），但还没有配置 MinerU Token：\n  "
                    + "\n  ".join(blocked)
                    + "\n\n设置窗已打开并定位到 ① MinerU 区，填入 Token 并保存即可。\n"
                      "（或者关掉 MinerU 版面分析 / 改强制关闭 OCR 模式）")
                self._open_settings("mineru")
                return
        self._running = True
        self._sync_ingest_btn()
        self.result_frame.pack_forget()
        self._ok_books = []
        self._reset_progress()
        self.step_lbl.config(text="入库中…（大文件 OCR 可能较久；可关闭本窗口，进度见主窗口底部状态条）")
        self.log.delete("1.0", "end")
        self._task_id = taskcenter.start("入库", f"共 {len(files)} 本", total=len(files))
        self._task_started = time.monotonic()
        self._last_line = ""
        threading.Thread(target=self._ingest_worker,
                         args=(files, self._task_id, mode, dict(self._probe)),
                         daemon=True).start()

    def _ingest_worker(self, files, task_id, ocr_mode, probe):
        """后台线程：逐本跑完整管线，绿灯后自动密文自检；全部结束发批次信号。
        #25：只向 TaskCenter 汇报进度（状态条/窗口进度条可见），不碰任何 UI 控件。"""
        ok_books, fail_books, warn_books = [], [], []
        in_dir = common.get_path("待入库")
        for idx, filename in enumerate(files, 1):
            book_id = common.gen_book_id(filename)
            book_title = common.clean_book_title(filename)
            try:
                ocr = self._ocr_decision(filename, ocr_mode)
                taskcenter.progress(task_id, title=f"《{book_title}》", step="提取")
                self._log(f"===== 《{book_title}》（{filename}）=====")
                self._log(f"① 提取：→ {book_id}（OCR 模式：{ocr}）")
                code, _, _ = run_script("extract.py", "--文件", filename, "--书号", book_id,
                                        "--ocr", ocr, on_line=self._log, timeout=14400)
                if code not in (0, 2):
                    self._log("❌ 提取失败，本书已停止入库。详见提取报告。")
                    fail_books.append((book_title, "提取失败"))
                    taskcenter.progress(task_id, current=idx)
                    continue
                if code == 2:
                    self._log("  🟡 提取完成但有警告：在线识别存在空页/缺图，或内容比对未达标。详见提取报告与检查报告。")

                self._log("② 质量检查：")
                taskcenter.progress(task_id, step="质量检查")
                code, _, _ = run_script("quality_check.py", "--书号", book_id, "--书名", book_title,
                                        on_line=self._log)
                if code != 0:
                    self._log("❌ 质量检查未通过，本书已停止入库。原因见质量检查报告。")
                    fail_books.append((book_title, "质量检查未通过"))
                    taskcenter.progress(task_id, current=idx)
                    continue

                self._log("③ 整理正文：")
                taskcenter.progress(task_id, step="整理正文")
                code, _, _ = run_script("clean_split.py", "--书号", book_id, "--书名", book_title,
                                        on_line=self._log)
                if code not in (0, 2):
                    self._log("❌ 整理正文失败，本书已停止入库。详见检查报告。")
                    fail_books.append((book_title, "整理正文失败"))
                    taskcenter.progress(task_id, current=idx)
                    continue
                if code == 2:
                    self._log("  🟡 整理正文完成但有警告（不影响使用），详见检查报告。")

                # #20 独立步骤：clean_split 后、build_index 前，LLM 书级配置生成（不阻塞）
                self._log("③.5 章节设置（自动识别不出章节时请 AI 生成一次）：")
                taskcenter.progress(task_id, step="识别章节")
                code, _, _ = run_script("book_cfg_gen.py", "--书号", book_id, "--书名", book_title,
                                        on_line=self._log)
                if code == 2:
                    self._log("  🟡 章节设置未能自动生成：不影响入库，已按通用规则继续；如需精调章节识别，可手工填写后到书库页「重建索引」。")
                elif code != 0:
                    self._log("  ⚠️ 章节设置步骤异常，已忽略并继续入库（不影响）。")

                self._log("④ 建立索引：")
                taskcenter.progress(task_id, step="建立索引")
                code, _, _ = run_script("build_index.py", "--书号", book_id, "--书名", book_title,
                                        on_line=self._log)
                if code != 0:
                    self._log("❌ 建立索引失败")
                    fail_books.append((book_title, "建立索引失败"))
                    taskcenter.progress(task_id, current=idx)
                    continue

                self._log("⑤ 移入已入库…")
                taskcenter.progress(task_id, step="归档")
                out_dir = common.get_path("已入库")
                out_dir.mkdir(parents=True, exist_ok=True)
                shutil.move(str(in_dir / filename), str(out_dir / filename))
                # 登记 原始文件名↔书号（供书库页删除时按书号精确找回移回待入库）
                try:
                    rows = common.load_ingest_registry()
                    rows = [r for r in rows if r.get("文件名") != filename]
                    rows.append({"文件名": filename, "书名": book_title, "书号": book_id})
                    common.save_ingest_registry(rows)
                except Exception:
                    self._log("  ⚠️ 已入库登记写入失败（删除时将按书名回退匹配）")

                self._log("⑥ 自动验证：")
                taskcenter.progress(task_id, step="自动验证")
                code, _, _ = run_script("verify_needles.py", "--书名", book_title,
                                        "--书号", book_id, on_line=self._log)
                if code != 0:
                    self._log("  ⚠️ 自动验证未全部通过：AI 检索本书时可能有个别内容定位不到。不影响入库，可到书库页「重建索引（含自动验证）」复查。")
                self._log(f"🟢 《{book_title}》入库完成")
                ok_books.append(book_title)
                warns = self._read_gate_warns(book_title)
                if warns:
                    warn_books.append((book_title, warns))
            except Exception as e:
                self._log(f"❌ 异常：{e}")
                fail_books.append((book_title, str(e)))
            taskcenter.progress(task_id, current=idx)
        taskcenter.finish(task_id, not fail_books,
                          f"成功 {len(ok_books)} 本／失败 {len(fail_books)} 本",
                          result={"模式": "入库", "成功": ok_books, "失败": fail_books,
                                  "黄灯": warn_books})
        self._log_q.put(("__DONE_BATCH__", ok_books, fail_books, warn_books))

    def _on_batch_done(self, ok_books, fail_books, warn_books):
        """主线程收尾：复位按钮/进度、刷新列表、窗口内结果区汇总 +「立即自动标注」。"""
        self._running = False
        try:
            self._sync_ingest_btn()
            self.refresh()
            self._reset_progress()
            parts = []
            if ok_books:
                parts.append(f"🟢 成功 {len(ok_books)} 本")
            if warn_books:
                parts.append(f"🟡 有警告 {len(warn_books)} 本")
            if fail_books:
                parts.append(f"🔴 失败 {len(fail_books)} 本")
            text = "、".join(parts) or "本次无文件入库"
            if ok_books:
                text += "：" + "、".join(f"《{t}》" for t in ok_books)
            if fail_books:
                text += "\n失败明细：\n" + "\n".join(f"  《{t}》：{why}" for t, why in fail_books)
            if warn_books:
                rows = []
                for t, warns in warn_books:
                    rows.append(f"  《{t}》")
                    rows.extend(f"    - {w}" for w in warns)
                n_warn = sum(len(w) for _t, w in warn_books)
                text += f"\n⚠️ 有 {n_warn} 条警告（不影响使用，详见检查报告）：\n" + "\n".join(rows)
            self.result_lbl.config(text=text)
            self._ok_books = list(ok_books)
            self._show_result()
            self.step_lbl.config(text=text.split("\n", 1)[0])
        except tk.TclError:
            pass

    def _show_result(self):
        """结果区可见性 + 主按钮：已配置标注 AI →「立即自动标注」；未配置 → 「去设置」入口（M4）。"""
        if not self.result_frame.winfo_ismapped():
            self.result_frame.pack(fill="x", padx=8, pady=(6, 0), before=self.log_toggle)
        self.annotate_now_btn.pack_forget()
        self.goto_settings_btn.pack_forget()
        self.annotate_hint.pack_forget()
        if not self._ok_books:
            return
        if llm_client.is_configured():
            self.annotate_now_btn.pack(side="left")
        else:
            self.goto_settings_btn.pack(side="left")
            self.annotate_hint.pack(side="left", padx=6)

    def _open_settings(self, section):
        """打开「设置」窗并定位到指定区（#25 M4：前置检查拦截不再只说"去设置页"，直接弹窗定位）。
        注意：本窗自身即 Toplevel，`winfo_toplevel()` 返回自己，故先找 master（顶层 App）。"""
        for cand in (self.master, self.winfo_toplevel()):
            opener = getattr(cand, "open_settings", None)
            if callable(opener):
                opener(section)
                return

    # ---------- 结果区：立即自动标注（R4：直接走 --自动回填，内部自动生成任务包） ----------

    def _annotate_now(self):
        books = list(self._ok_books)
        if not books:
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
        if taskcenter.running("标注"):
            messagebox.showinfo("提示", "已有自动标注任务进行中，请等待完成")
            return
        cfg = llm_client.get_annotate_config()
        if not messagebox.askyesno(
                "自动标注",
                f"将用标注 AI（{cfg['model']}）为这 {len(books)} 本书逐章整理标注，并自动核对导入书库。\n\n"
                "这会消耗接口额度。继续吗？"):
            return
        title = f"《{books[0]}》" if len(books) == 1 else f"{len(books)} 本"
        self._task_id = taskcenter.start("标注", title, total=len(books))
        self._task_started = time.monotonic()
        self._last_line = ""
        taskcenter.progress(self._task_id, step="调用标注 AI 填写")
        self.step_lbl.config(text=f"正在标注：{title}")
        self.result_frame.pack_forget()
        threading.Thread(target=self._fill_worker, args=(books, self._task_id),
                         daemon=True).start()

    def _fill_worker(self, books, task_id):
        """后台线程：逐本 API 自动回填（annotate_pack --自动回填，内部自动生成任务包）。"""
        done = []
        for idx, t in enumerate(books, 1):
            self._log(f"===== API 自动标注《{t}》=====")
            taskcenter.progress(task_id, title=f"《{t}》", step="调用标注 AI 填写")
            code, _, _ = run_script("annotate_pack.py", "--自动回填", "--书名", t,
                                    on_line=self._log, timeout=86400)
            if code == 0:
                done.append(t)
            else:
                self._log(f"⚠️ 《{t}》自动标注未全部通过（详见日志）。")
            taskcenter.progress(task_id, current=idx)
        taskcenter.finish(task_id, len(done) == len(books), f"成功 {len(done)}／{len(books)} 本",
                          result={"模式": "标注", "完成": done})
        self._log_q.put(("__FILL_DONE__", done))

    def _on_fill_done(self, books):
        try:
            self._reset_progress()
            if books:
                names = "、".join(f"《{t}》" for t in books)
                self.step_lbl.config(text=f"✅ 自动标注完成：{names}")
                messagebox.showinfo(
                    "自动标注完成",
                    names + " 已完成自动标注。\n请到主页确认术语后，知识库即可正式使用。")
        except tk.TclError:
            pass
