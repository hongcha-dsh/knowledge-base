# -*- coding: utf-8 -*-
"""知识库 · 预处理控制台（#25 M4：1 主页 + 按需弹窗 + 全局状态条）。

#25 M4：取消页签导航——主窗口直接组合「书库主页 + 底部全局任务状态条」，入库/设置改为
按需弹出的 Toplevel 单例（`pages/入库窗.py` / `pages/设置窗.py`），标题栏右侧「⚙ 设置」；
首次运行（未配 MinerU Token 与标注 AI、且从未引导过）自动弹一次设置窗，标记写回 `路径配置.json`；
后台任务完成由状态条泵刷新主页（业务收尾挂 Task 级，不随弹窗销毁，R2）。

#40：任务日志窗口支持**按书定位**——`_open_log(书名)` 由书库页主按钮带「查看问题」按钮调用
（返工：原「需复查」双击入口用户判定不易发现，改按钮），定位该书最近一次「验证/重建」任务；
本次会话没有则显示一行「复制本句去问 AI」提示。

#41：**主窗口按内容自适应尺寸**——`fit_to_content()`（启动后 + 主页高级区展开/折叠后各调一次）：
窗口长到内容需求高、`minsize` 高抬到需求高（用户拖不小 → 内容永不被 Tk packer 压扁；
起因＝高级区展开后外发面板两行按钮被压成 1px）；屏幕装不下时先压缩外发面板表格行数兜底。
（曾试过把高级区搬成独立窗，用户否决「入口存在感太低」，故仍为主页内折叠区。）"""

import json
import sys
import time
import tkinter as tk
from tkinter import ttk, messagebox
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "资料预处理" / "脚本"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import common  # noqa: E402
import theme  # noqa: E402
from pages import kill_active_scripts, taskcenter  # noqa: E402
from pages.书库页 import 书库页  # noqa: E402


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("知识库 · 管理控制台")
        # #51：默认窗口改宽长方形比例；返工二轮用户实测「缩到不能缩正好」→ 默认即 minsize 宽 880
        self.geometry("880x560")
        self.minsize(880, 520)
        # #35 M5：关窗前强杀活跃脚本子进程，杜绝孤儿进程
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        # #25 M1：全局任务状态条状态
        self._log_win = None
        self._log_text = None
        self._log_shown = None
        self._log_focus = None      # #40：日志窗当前的「按书定位」目标（书名或 None）
        self._log_copy_btn = None   # #28：日志窗「复制全部」按钮
        self._cfg_mods = None
        self._cfg_missing = []
        self._tick = 0
        self._prog_mode = "determinate"
        # #25 M3：按需弹窗单例注册表 + 任务完成检测（业务收尾挂 Task 级）
        self._windows = {}
        self._seen_status = {}
        # #51：全局套用「浅色 · macOS 清透」主题（观感层，须在 _build 前）
        theme.apply(self)
        self._build()
        self._poll_tasks()
        self._first_run_check()
        # #41：启动后按内容自适应一次（窗口长到需求高 + minsize 抬高，内容永不被压扁）
        self.after(150, self.fit_to_content)
        # #48：启动前置到前台——vbs 以隐藏窗口启动 pythonw，Tk 主窗创建时未抢到前台焦点，
        # 会落到后台/最小化（用户要再去任务栏点一下）。启动后取消最小化 + 前置 + 短时置顶兜底。
        self.after(100, self._bring_to_front)

    def _bring_to_front(self):
        """把主窗取消最小化并前置到前台（#48）。

        Windows 前台有"锁定"机制（非用户点击启动的进程未必抢得到焦点），故先用短暂的
        -topmost 置顶兜底一次再放开，避免常驻置顶打扰用户使用其他窗口。失败静默。"""
        try:
            self.deiconify()
            self.lift()
            self.focus_force()
            self.attributes("-topmost", True)
            self.after(400, lambda: self.attributes("-topmost", False))
        except tk.TclError:
            pass  # 窗口已关闭等边界场景静默跳过

    def _on_close(self):
        """关窗前保护（#39）：有运行中任务时先确认——进程退出会硬杀 daemon worker，
        多步写盘操作（入库/删书/重建）被腰斩会留下不完整产物（#39 实测踩过）。"""
        running = []
        try:
            running = [t for t in taskcenter.snapshot() if t["status"] == "running"]
        except Exception:
            running = []
        if running:
            names = "、".join(f"{t['kind']}《{t['title']}》" for t in running[:3])
            if len(running) > 3:
                names += f" 等 {len(running)} 项"
            if not messagebox.askyesno(
                    "任务正在运行",
                    f"当前有任务正在进行：{names}\n\n"
                    "现在退出会中断它，可能留下不完整的数据（如删书删到一半、入库断在半途）。\n"
                    "建议等它跑完再退出。\n\n"
                    "仍要退出吗？"):
                return
        try:
            kill_active_scripts()
        except Exception:
            pass
        self.destroy()

    def _build(self):
        self._build_statusbar()

        # #25 M4：取消页签导航——主窗口＝书库主页（标题行右侧「⚙ 设置」入口）
        header = ttk.Frame(self)
        header.pack(side="top", fill="x")
        ttk.Label(header, text="知识库 · 管理控制台",
                  font=("Microsoft YaHei UI", 11, "bold")).pack(
            side="left", padx=10, pady=(8, 4))
        ttk.Button(header, text="⚙ 设置", width=10, command=self.open_settings).pack(
            side="right", padx=10, pady=(6, 2))

        self.container = ttk.Frame(self)
        self.container.pack(side="top", fill="both", expand=True)
        self.home = 书库页(self.container)
        self.home.pack(fill="both", expand=True)

    # ---------- #25 M3/M4：按需弹窗（单例注册表，§4.2/R3） ----------

    def fit_to_content(self, shrink=False):
        """按内容自适应窗口尺寸（#41；#51 返工二轮加 shrink 参数）。

        shrink=True（仅「折叠高级区」调用点传入）时窗口**自动收回到内容需求高**——
        展开时窗口被自适应拉长，折叠后若不收回会留下大段空白（用户实测反馈）。
        其他调用点不传 shrink：只在空间不足时把窗口拉长，绝不主动压缩用户手动拉大的窗口。

        为什么需要：主页无纵向滚动，一旦「内容需求高 ＞ 窗口实际给到的高度」，Tk packer 会
        **从最后打包的子件往前压**——高级区的外发面板里两行按钮（各 27px）曾被压成 **1px**
        （用户实测「看不到任何按钮」）。这里两手一起上：
          ① 窗口长到内容需求高（`cur_h < req_h` 时 `geometry` 加高）；
          ② `minsize` 的高也抬到需求高（当前内容装不下时用户也无法把窗口拖小 → 永不压扁）。
        屏幕装不下时（兜底）：先压缩外发面板的**表格行数**（表格可滚动，按钮不可压扁），
        再把窗口取到屏幕可用高；容量恢复后再自动还原表格行数。

        调用点：启动后一次（`after`）、主页高级区展开/折叠后（`书库页._toggle_adv`）。"""
        try:
            self.update_idletasks()
        except tk.TclError:
            return
        cap = max(560, self.winfo_screenheight() - 120)     # 屏幕可用高（留标题栏/任务栏余量）
        panel = getattr(getattr(self, "home", None), "export_panel", None)
        if panel is not None:                                # 先还原表格行数，再按需压缩
            try:
                panel.set_tree_rows(5)
                self.update_idletasks()
            except Exception:
                panel = None
        req_h = self.winfo_reqheight()
        if panel is not None and req_h > cap:
            for rows in (4, 3, 2):
                try:
                    panel.set_tree_rows(rows)
                    self.update_idletasks()
                except Exception:
                    break
                req_h = self.winfo_reqheight()
                if req_h <= cap:
                    break
        try:
            cur_w, cur_h = self.winfo_width(), self.winfo_height()
        except tk.TclError:
            return
        target = min(req_h + 8, cap)
        if cur_h < target or (shrink and cur_h > target):
            try:
                self.geometry(f"{max(cur_w, self.winfo_reqwidth())}x{target}")
            except tk.TclError:
                pass
        try:
            self.minsize(880, max(520, target))
        except tk.TclError:
            pass

    def open_window(self, key, factory, focus=None):
        """打开/置顶按需弹窗（单例）：已存在则 deiconify+lift 并调其 on_show()（视图自刷新）；
        focus 非空时若窗口实现 focus_section(section) 则定位到指定区（设置窗用）。
        `factory(master)` 须返回一个 Toplevel。窗口 <Destroy> 时自动从注册表注销。"""
        win = self._windows.get(key)
        if win is not None:
            try:
                if win.winfo_exists():
                    win.deiconify()
                    win.lift()
                    win.focus_force()
                    hook = getattr(win, "on_show", None)
                    if callable(hook):
                        hook()
                    if focus is not None and hasattr(win, "focus_section"):
                        win.focus_section(focus)
                    return win
            except tk.TclError:
                pass
        win = factory(self)
        self._windows[key] = win
        win.bind("<Destroy>",
                 lambda e, k=key, w=win: self._windows.pop(k, None) if e.widget is w else None,
                 add="+")
        if focus is not None and hasattr(win, "focus_section"):
            win.focus_section(focus)
        return win

    def open_ingest(self):
        """打开「添加书籍」入库窗（#25 M3：主页主按钮入口）。"""
        from pages.入库窗 import 入库窗
        return self.open_window("ingest", 入库窗)

    def open_settings(self, focus=None):
        """打开「设置」窗（#25 M4）；focus="mineru"/"annotate" 时定位并高亮对应区。"""
        from pages.设置窗 import 设置窗
        return self.open_window("settings", 设置窗, focus=focus)

    def _open_settings_from_cfg_label(self, _e=None):
        """状态条右侧配置小标签点击：开设置窗并定位到第一个缺失区。"""
        missing = list(self._cfg_missing or [])
        focus = None
        if any("MinerU" in m for m in missing):
            focus = "mineru"
        elif any("标注" in m for m in missing):
            focus = "annotate"
        self.open_settings(focus)

    def _first_run_check(self):
        """首次运行引导（R5/M4）：未配 MinerU Token 与标注 AI、且 路径配置.json 无「已引导」标记
        → 写标记并自动弹一次设置窗（只弹一次；用环境变量配置的用户不会被反复打扰）。"""
        try:
            cfg = common.load_config()
            if cfg.get("已引导"):
                return
            import llm_client
            import mineru_client
            if mineru_client.get_token() or llm_client.is_configured():
                return
            cfg["已引导"] = True
            cfg_path = common.PROJECT_ROOT / "路径配置.json"
            cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
            self.after(300, lambda: self.open_settings("mineru"))
        except Exception:
            pass

    # ---------- #25 M3：任务完成 → 刷新主页（业务收尾挂 Task 级，R2） ----------

    def _dispatch_finished(self, snap):
        """检测 running→done/failed 迁移，在主线程执行收尾（不随弹窗销毁）。"""
        for t in snap:
            prev = self._seen_status.get(t["id"])
            if prev == "running" and t["status"] != "running":
                self._on_task_finished(t["id"])
            self._seen_status[t["id"]] = t["status"]

    def _on_task_finished(self, tid):
        """任务结束：刷新主页书籍表（新入库书即时可见；弹窗已关也照常）。"""
        try:
            self.home.refresh()
        except tk.TclError:
            pass

    # ---------- #25 M1：全局任务状态条（主窗口底部通栏） ----------

    def _build_statusbar(self):
        # #51 布局 B：状态条白底卡片化（与工具条带同一面板语言）
        bar = ttk.Frame(self, style="Card.TFrame")
        bar.pack(side="bottom", fill="x")
        ttk.Separator(self, orient="horizontal").pack(side="bottom", fill="x")
        self.prog = ttk.Progressbar(bar, mode="determinate", length=150)
        self.prog.pack(side="left", padx=(8, 6), pady=4)
        self.status_lbl = ttk.Label(bar, text="就绪", anchor="w")
        self.status_lbl.pack(side="left", fill="x", expand=True)
        # #51 三轮返工：白状态条上的 tk.Label 需显式白底（option_add 全局是 CANVAS 灰，会出灰补丁）
        self.cfg_lbl = tk.Label(bar, text="", fg=theme.WARN, bg=theme.CARD, cursor="hand2")
        self.cfg_lbl.pack(side="right", padx=8)
        self.cfg_lbl.bind("<Button-1>", self._open_settings_from_cfg_label)
        self.log_btn = ttk.Button(bar, text="查看日志", width=10, command=self._open_log)
        self.log_btn.pack(side="right", padx=(6, 0))

    def _poll_tasks(self):
        """唯一消息泵：轮询 TaskCenter 快照刷新状态条（各页 worker 只写任务中心，不碰控件）。"""
        try:
            snap = taskcenter.snapshot()
        except Exception:
            snap = []
        self._dispatch_finished(snap)
        running = [t for t in snap if t["status"] == "running"]
        if running:
            task = running[-1]
            text = f"正在{task['kind']}：{task['title']}"
            if task["total"]:
                text += f"（{task['current']}/{task['total']}）"
            if task["step"]:
                text += f" — {task['step']}"
            # #38：显示「已用时」——即使步骤长时间不变，秒表在动，人就不会误以为卡死
            started = task.get("started") or 0
            if started:
                el = max(0, int(time.time() - started))
                text += f"　已用时 {el // 60:02d}:{el % 60:02d}"
            if len(running) > 1:
                text += f"（共 {len(running)} 项进行中）"
            self.status_lbl.config(text=text)
            self._set_progress(task)
        elif snap:
            task = snap[-1]
            if task["status"] == "done":
                text = f"✅ {task['title']}{task['kind']}完成"
            else:
                text = f"❌ {task['title']}{task['kind']}未全部完成"
            if task["summary"]:
                text += f"（{task['summary']}）"
            self.status_lbl.config(text=text)
            self._set_progress(None)
        else:
            self.status_lbl.config(text="就绪")
            self._set_progress(None)

        self._tick += 1
        if self._tick % 10 == 0:
            self._refresh_cfg_label()
        self._refresh_log_window(snap)
        try:
            self.after(100, self._poll_tasks)
        except tk.TclError:
            pass

    def _set_progress(self, task):
        """有总量走确定进度条（#38：total 已知即确定——current=0 也显示 0/N，不再退化成循环滚动条，
        原先判据含 current 使"入库刚开始"看起来像卡住的循环条）；无总量走往返滚动条；空闲复位。"""
        if task is not None and task["total"]:
            if self._prog_mode != "determinate":
                self.prog.stop()
                self.prog.config(mode="determinate")
                self._prog_mode = "determinate"
            self.prog.config(maximum=task["total"], value=task["current"])
            return
        if task is not None:
            if self._prog_mode != "indeterminate":
                self.prog.config(mode="indeterminate")
                self.prog.start(80)
                self._prog_mode = "indeterminate"
            return
        if self._prog_mode != "determinate":
            self.prog.stop()
            self.prog.config(mode="determinate")
            self._prog_mode = "determinate"
        self.prog.config(value=0)

    def _refresh_cfg_label(self):
        """状态条右侧配置小标签：缺 MinerU Token / 标注 AI 时提示，点击开设置窗并定位（M4）。"""
        if self._cfg_mods is None:
            try:
                import llm_client
                import mineru_client
                self._cfg_mods = (llm_client, mineru_client)
            except Exception:
                self._cfg_mods = ()
        if not self._cfg_mods:
            self.cfg_lbl.config(text="")
            return
        llm_client, mineru_client = self._cfg_mods
        missing = []
        try:
            if not mineru_client.get_token():
                missing.append("MinerU Token")
            if not llm_client.is_configured():
                missing.append("标注 AI")
        except Exception:
            missing = []
        self._cfg_missing = missing
        self.cfg_lbl.config(text=f"⚠️ {'、'.join(missing)}未配置（点击配置）" if missing else "")

    # ---------- #25 M1：任务日志窗口 ----------

    def _open_log(self, focus_book=None):
        """打开任务日志窗口（#40：focus_book＝«书名»，供书库页主按钮「查看问题」按书定位）。

        focus_book 非空时**定位**该书最近一次「验证/重建」任务的日志；本次会话没有这类任务时，
        日志区顶部显示一行可复制提示（让用户把它发给 AI），而不是打开用户看不懂的 .md 报告。"""
        if self._log_win is None or not self._log_win.winfo_exists():
            win = tk.Toplevel(self)
            win.title("任务日志")
            win.geometry("720x420")
            win.transient(self)
            win.protocol("WM_DELETE_WINDOW", self._close_log)
            self._log_text = tk.Text(win, wrap="word")
            self._log_text.pack(fill="both", expand=True, padx=6, pady=6)
            bar = ttk.Frame(win)
            bar.pack(fill="x", padx=6, pady=(0, 6))
            self._log_copy_btn = ttk.Button(bar, text="复制全部", command=self._copy_log_all)
            self._log_copy_btn.pack(side="left")
            self._log_win = win
        else:
            self._log_win.deiconify()
            self._log_win.lift()
        self._log_focus = focus_book
        self._log_shown = None
        self._refresh_log_window(taskcenter.snapshot())

    def _focus_task(self, snap):
        """按书号（书名）在任务快照里找该书最近一次「验证/重建」任务；无则 None。"""
        book = self._log_focus
        if not book:
            return None
        want = f"《{book}》"
        for t in reversed(snap):
            if t["title"] == want and t["kind"] in ("验证", "重建"):
                return t
        return None

    def _close_log(self):
        if self._log_win is not None:
            self._log_win.destroy()
        self._log_win = None
        self._log_text = None
        self._log_focus = None
        self._log_copy_btn = None

    def _copy_log_all(self):
        """复制日志窗当前显示的全部文本（#28：日志区一键复制，粘贴给 AI 排查）。"""
        if self._log_text is None:
            return
        try:
            text = self._log_text.get("1.0", "end").strip()
        except tk.TclError:
            return
        if not text:
            return
        self.clipboard_clear()
        self.clipboard_append(text)
        try:
            self._log_copy_btn.config(text="已复制 ✓")
            self.after(1500, self._reset_log_copy_btn)
        except tk.TclError:
            pass

    def _reset_log_copy_btn(self):
        try:
            if self._log_copy_btn is not None and self._log_copy_btn.winfo_exists():
                self._log_copy_btn.config(text="复制全部")
        except tk.TclError:
            pass

    def _refresh_log_window(self, snap):
        if self._log_win is None:
            return
        try:
            if not self._log_win.winfo_exists():
                self._log_win = None
                return
        except tk.TclError:
            self._log_win = None
            return
        task = self._focus_task(snap) if self._log_focus else (snap[-1] if snap else None)
        key = (task["id"] if task else 0, task["log_count"] if task else 0, self._log_focus)
        if key == self._log_shown:
            return
        self._log_shown = key
        self._log_text.delete("1.0", "end")
        if self._log_focus and task is None:
            self._log_text.insert(
                "end", f"《{self._log_focus}》上次验证没通过"
                       "（复制本句去问 AI，报这本书名即可）\n\n")
        if task is None:
            return
        self._log_text.insert("end", f"# 【{task['kind']}】{task['title']}（任务 {task['id']}）\n")
        self._log_text.insert("end", taskcenter.log_text(task["id"]))
        self._log_text.see("end")


if __name__ == "__main__":
    try:
        app = App()
        app.mainloop()
    except Exception:
        # #34：控制台改为无控制台（pythonw）启动后，stderr 不可见——启动异常改弹窗，避免静默失败
        import traceback
        from tkinter import messagebox
        messagebox.showerror(
            "知识库 · 启动失败",
            "控制台启动出错（可把下面内容发给开发者）：\n\n" + traceback.format_exc())
