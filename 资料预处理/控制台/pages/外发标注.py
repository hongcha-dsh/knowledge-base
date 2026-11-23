# -*- coding: utf-8 -*-
"""书库主页·高级区「外发标注」子面板（#25 M2 迁入；#28 流程化收敛）。

作用对象＝主页选中行，任务包清单随选中书联动。主行收敛为四个按钮：
「生成任务包」检测标注状态自动决定生成/补缺，成功后自动打开文件夹并复制引导提示词；
「导回标注」预扫回填文件夹后全书导回（.done 幂等跳过已导回章），成功状态行提示、
失败弹窗带【复制报错给 AI】/【看日志】；「打开任务包文件夹」「删除全部任务包」摊平直放。
任务包生成/导回/删除仍走 annotate_pack.py（#42 起导回后由脚本自动跑术语配对，无弹窗）；子进程输出统一记入全局 TaskCenter。
"""

import os
import re
import tkinter as tk
from tkinter import ttk, messagebox

import annotate_pack
import common
from . import run_script, taskcenter


class 外发标注面板(ttk.LabelFrame):
    """外发标注高级子面板。

    get_book(): 返回主页当前选中书名或 None；on_changed(): 操作成功后回调（主页刷新）；
    set_status(text, color): 写主页状态行（可选）。
    """

    def __init__(self, master, get_book, on_changed=None, set_status=None):
        super().__init__(master, text="外部 AI 标注（把任务包发给您常用的 AI 填写）")
        self._get_book = get_book
        self._on_changed = on_changed
        self._set_status = set_status
        self._last_task_id = None
        self._last_out = ""
        self._build()

    def set_tree_rows(self, rows):
        """#41：设置任务包表格的显示行数（空间紧张时压缩表格——表格可滚动，按钮不可被压扁）。

        调用方：`App.fit_to_content()`（屏幕装不下时按 4→3→2 逐步压缩；容量恢复后还原 5）。"""
        try:
            self.tree.config(height=max(2, int(rows)))
        except tk.TclError:
            pass

    # ---------- 界面 ----------

    def _build(self):
        cols = ("章号", "章名", "状态", "文件名")
        self.tree = ttk.Treeview(self, columns=cols, show="headings", height=5)
        for c in cols:
            self.tree.heading(c, text=c)
        for c, w in (("章号", 60), ("章名", 240), ("状态", 100), ("文件名", 130)):
            self.tree.column(c, width=w, anchor="w")
        self.tree.pack(fill="x", padx=8, pady=4)

        row = ttk.Frame(self, style="Card.TFrame")
        row.pack(fill="x", padx=8, pady=2)
        ttk.Button(row, text="① 生成任务包", command=self.gen_all).pack(side="left", padx=4)
        ttk.Button(row, text="② 导入标注", command=self.import_all).pack(side="left", padx=4)
        # #28 返工：边缘项不足三个就不收菜单，直接摊平（用户拍板）
        ttk.Button(row, text="打开任务包文件夹", command=self.open_pack_dir).pack(side="left", padx=4)
        ttk.Button(row, text="删除全部任务包", style="Danger.TButton", command=self.del_all_packs).pack(side="left", padx=4)

    # ---------- 联动刷新 ----------

    def refresh(self):
        """主页选中书变化 / 操作完成后调用：刷新任务包清单。"""
        self.refresh_packs()

    def refresh_packs(self):
        self.tree.delete(*self.tree.get_children())
        book = self._get_book()
        if not book:
            return
        path = common.get_path("输出") / "任务包" / book / "_清单.md"
        if not path.exists():
            return
        for line in path.read_text(encoding="utf-8").split("\n"):
            line = line.strip()
            if not line.startswith("|") or line.startswith("|---") or line.startswith("| 章号"):
                continue
            cells = [c.strip() for c in line.strip("|").split("|")]
            if len(cells) >= 4:
                self.tree.insert("", "end", values=(cells[0], cells[1], cells[2], cells[3]))

    # ---------- 基础 ----------

    def _status(self, text, color="black"):
        if self._set_status:
            self._set_status(text, color)

    def _changed(self):
        if self._on_changed:
            self._on_changed()

    def _finish_task(self, tid, code, ok_text, fail_text):
        taskcenter.finish(tid, code == 0, ok_text if code == 0 else fail_text)

    def _start_task(self, kind, title):
        """#65 M1：启动写任务；写锁被占时人话提示并返回 0（调用点不得再跑子进程）。"""
        tid = taskcenter.start(kind, title)
        if not tid:
            self._status(f"🟡 另一个写库任务正在进行中，本次「{kind}」没有开始（书库未改动）", "orange")
            messagebox.showinfo("提示", taskcenter.lock_block_text(kind))
            return 0
        self._last_task_id = tid
        return tid

    def _run(self, *args, ok_text="完成", fail_text="失败"):
        """同步跑一次 annotate_pack 子进程；输出逐行记入 TaskCenter。

        #65 M1：写锁被占时返回 None（已提示），调用点不要把 None 当业务失败再弹窗。"""
        book = self._get_book()
        tid = self._start_task("标注", f"《{book}》外部 AI 标注" if book else "外部 AI 标注")
        if not tid:
            return None
        try:
            code, out, err = run_script("annotate_pack.py", *args)
        except Exception as e:
            taskcenter.log(tid, f"后台执行异常：{e}")
            self._finish_task(tid, 1, ok_text, fail_text)
            self._status(f"🔴 {fail_text}", "red")
            return False
        self._last_out = out or err
        for ln in self._last_out.split("\n"):
            if ln.strip():
                taskcenter.log(tid, ln)
        self._finish_task(tid, code, ok_text, fail_text)
        self.refresh()
        self._changed()
        return code == 0

    # ---------- 智能生成 / 智能导回 ----------

    def gen_all(self):
        """智能生成：全书已标注完成则提示无需生成；否则生成全书任务包
        （脚本自动跳过已导回章、补齐缺的章），成功后自动打开文件夹并复制引导提示词。"""
        book = self._get_book()
        if not book:
            messagebox.showwarning("提示", "请先在上方列表选择一本书")
            return
        try:
            st = annotate_pack.book_annotation_status(book)
        except Exception:
            st = {}
        if st.get("状态") == "已完成":
            self._status(f"🟢 《{book}》全书标注已完成，无需生成任务包", "green")
            return
        ok = self._run("--生成全书", "--书名", book,
                       ok_text=f"任务包已生成（{book}）", fail_text="任务包生成失败")
        if ok is None:      # 写锁被占：已提示，不再报“生成失败”
            return
        if not ok:
            self._status("🔴 任务包生成失败", "red")
            self._show_fail_dialog(
                "生成失败",
                "任务包没能生成。失败原因已记入任务日志，可复制给 AI 排查。")
            return
        self._copy_guide(book, silent=True)
        self.open_pack_dir()
        m = re.search(r"生成 (\d+) 章，跳过 (\d+) 章", self._last_out)
        if m:
            self._status(f"🟢 已生成 {m.group(1)} 章任务包（跳过 {m.group(2)} 章已导入）："
                         "文件夹已打开、引导词已复制，直接粘贴给 AI", "green")
        else:
            self._status("🟢 任务包已生成：文件夹已打开、引导词已复制，直接粘贴给 AI", "green")
        messagebox.showinfo(
            "任务包已生成",
            f"《{book}》的任务包已生成，任务包文件夹已打开。\n\n"
            "引导提示词已复制好，按下面两步操作：\n"
            "第一步：开启 AI 新对话，工作区选择知识库；\n"
            "第二步：直接粘贴（Ctrl+V）发给 AI，即可开始填写。\n\n"
            "AI 全部填完后，回到这里点「② 导入标注」即可导入书库。\n\n"
            "如果之后复制丢了，再点一次「① 生成任务包」就会重新复制。")

    def import_all(self):
        """智能导回：预扫 回填\\ 文件夹，空则提示先放文件；否则全书导回
        （.done 幂等，已导回章自动跳过）。全部成功自动弹术语确认；
        有失败弹窗带【复制报错给 AI】/【看日志】。"""
        book = self._get_book()
        if not book:
            messagebox.showwarning("提示", "请先在上方列表选择一本书")
            return
        back = common.get_path("输出") / "任务包" / book / "回填"
        files = self._filled_files(book)
        if not files:
            messagebox.showinfo(
                "提示",
                f"还没有可导入的文件。\n\n请把 AI 填好的标注文件放进这个文件夹：\n{back}")
            return
        tid = self._start_task("标注", f"《{book}》外部 AI 标注")
        if not tid:
            return
        try:
            code, out, err = run_script("annotate_pack.py", "--导回全书", "--书名", book)
        except Exception as e:
            taskcenter.log(tid, f"后台执行异常：{e}")
            code, out, err = 1, "", str(e)
        self._last_out = out or err
        for ln in self._last_out.split("\n"):
            if ln.strip():
                taskcenter.log(tid, ln)
        self._finish_task(tid, code, "导入全部成功", "导入全部失败")
        self.refresh()
        self._changed()
        if code == 0:
            self._status("🟢 导入全部成功", "green")
            return
        m = re.search(r"成功 (\d+) 章，失败 (\d+) 章", self._last_out)
        if code == 2 and m:
            self._status("🟡 导入：部分章节失败（详见日志）", "orange")
            head = f"部分章节导入失败（成功 {m.group(1)} 章，失败 {m.group(2)} 章）。"
        else:
            self._status("🔴 导入失败（详见日志）", "red")
            head = "导入失败。"
        self._show_fail_dialog(
            "导入未全部成功",
            head + "\n\n失败原因已记入任务日志，可复制给 AI 修正文件后重新导入"
            "（已成功的章不会重复导入）。")
    # ---------- 失败弹窗 / 日志出口 ----------

    def _filled_files(self, book):
        """回填文件夹里的可导回文件清单（无目录/无文件 → 空列表）。"""
        back = common.get_path("输出") / "任务包" / book / "回填"
        return list(back.glob("*_任务包.md")) if back.exists() else []

    def _show_fail_dialog(self, title, summary):
        """失败弹窗：人话结论 +【复制报错给 AI】+【看日志】+【关闭】
        （#28 口径：每处报警都要给用户一个"能复制去问 AI"的出口）。"""
        win = tk.Toplevel(self)
        win.title(title)
        win.transient(self.winfo_toplevel())
        ttk.Label(win, text=summary, wraplength=460, justify="left").pack(padx=12, pady=(12, 8))
        btns = ttk.Frame(win)
        btns.pack(pady=(0, 12))
        ttk.Button(btns, text="复制报错给 AI", command=self._copy_last_log).pack(side="left", padx=4)
        ttk.Button(btns, text="看日志", command=self._open_log).pack(side="left", padx=4)
        ttk.Button(btns, text="关闭", command=win.destroy).pack(side="left", padx=4)

    def _copy_last_log(self):
        """复制最近一次外发操作的日志（发回给 AI 修正回填文件）。"""
        text = taskcenter.log_text(self._last_task_id) if self._last_task_id else ""
        if not text.strip():
            self._status("暂无可复制的日志（可先执行一次生成/导入）", "black")
            return
        self.clipboard_clear()
        self.clipboard_append(text)
        self._status("🟢 日志内容已复制（可直接发给 AI 修正填好的文件）", "green")

    def _open_log(self):
        """打开主窗口的任务日志窗口（与书库页「查看问题」同一接法：winfo_toplevel 拿 App._open_log）。"""
        opener = getattr(self.winfo_toplevel(), "_open_log", None)
        if callable(opener):
            opener()

    # ---------- 更多菜单：删除 / 辅助 ----------

    def del_all_packs(self):
        book = self._get_book()
        if not book:
            messagebox.showwarning("提示", "请先在上方列表选择一本书")
            return
        if not messagebox.askyesno("确认", f"删除《{book}》全部任务包（含回填文件夹）？\n（已经导入的章节摘要不受影响）"):
            return
        ok = self._run("--删除", "--书名", book,
                       ok_text="已删除本书全部任务包", fail_text="删除失败")
        if ok is None:      # 写锁被占：已提示，不再把状态行改写成“删除失败”
            return
        self._status("🟢 已删除本书全部任务包" if ok else "🔴 删除失败",
                     "green" if ok else "red")

    def _guide_text(self, book):
        d = common.get_path("输出") / "任务包" / book
        if not d.exists():
            return None
        return (f"请你按《{d}\\_任务说明.md》的要求处理该文件夹里的所有任务包；"
                "全部回填完成后，按说明末尾的指引运行导回命令，直至全部通过。")

    def _copy_guide(self, book, silent=False):
        text = self._guide_text(book)
        if not text:
            if not silent:
                messagebox.showinfo("提示", "该书尚未生成任务包（可先点「① 生成任务包」）")
            return False
        self.clipboard_clear()
        self.clipboard_append(text)
        return True

    def open_pack_dir(self):
        book = self._get_book()
        if not book:
            messagebox.showwarning("提示", "请先在上方列表选择一本书")
            return
        d = common.get_path("输出") / "任务包" / book
        if not d.exists():
            messagebox.showinfo("提示", "该书尚未生成任务包")
            return
        try:
            os.startfile(str(d))
        except Exception as e:
            self._status(f"打开失败：{e}", "red")
