# -*- coding: utf-8 -*-
"""控制台 pages 包公共辅助：脚本调用、书籍列表、公共状态。"""

import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT / "资料预处理" / "脚本"))
import common  # noqa: E402

# Windows：子进程不另开控制台黑窗（控制台改以 pythonw 无控制台启动后，#34 防止子进程弹黑窗）
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# #35 M5：活跃子进程注册表——关主窗口时强杀，避免孤儿子进程
# （旧行为：daemon 线程随主窗口消亡，子进程成孤儿，下一次 print 撞 BrokenPipeError 才退出）
import threading as _threading

_active_procs = set()
_active_lock = _threading.Lock()


def kill_active_scripts():
    """强杀全部活跃脚本子进程（关窗前调用）。"""
    with _active_lock:
        procs = list(_active_procs)
    for p in procs:
        try:
            p.kill()
        except Exception:
            pass


def run_script(script_name: str, *args, on_line=None, timeout=3600) -> tuple:
    """以子进程运行 资料预处理\\脚本\\{script_name}，返回 (returncode, stdout, stderr)。

    Popen 流式读取（M4）：stderr 并入 stdout 逐行读取；
    on_line（可选）：每输出一行实时回调（供控制台逐行上屏）；
    timeout（秒）：超过则强杀子进程（继承旧版 subprocess.run 的 3600s 兜底）。"""
    import os
    import subprocess
    import threading
    import time
    script = common.get_path("脚本目录") / script_name
    cmd = [sys.executable, str(script), *args]
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    p = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=env,
        cwd=str(PROJECT_ROOT),
        creationflags=_NO_WINDOW,
    )
    with _active_lock:
        _active_procs.add(p)
    lines = []
    done = threading.Event()

    def _reader():
        try:
            for raw in p.stdout:
                line = raw.rstrip("\r\n")
                lines.append(line)
                if on_line:
                    try:
                        on_line(line)
                    except Exception:
                        pass
        finally:
            done.set()

    threading.Thread(target=_reader, daemon=True).start()
    deadline = time.monotonic() + timeout
    while not done.is_set():
        if time.monotonic() > deadline:
            p.kill()
            done.wait(timeout=10)
            break
        try:
            p.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
    p.wait()
    p.stdout.close()
    with _active_lock:
        _active_procs.discard(p)
    return p.returncode, "\n".join(lines), ""


def _read_book_id(idx_path) -> str:
    """只读 倒排表.json 头部取「书号」——倒排表数 MB，为一个字段全量解析会拖慢切页。
    头部未命中（异常格式）时退回全量解析。"""
    try:
        with idx_path.open("r", encoding="utf-8") as f:
            head = f.read(4096)
        m = re.search(r'"书号"\s*:\s*"([^"]*)"', head)
        if m:
            return m.group(1)
    except Exception:
        pass
    try:
        return json.loads(idx_path.read_text(encoding="utf-8")).get("书号", "")
    except Exception:
        return ""


def get_books() -> list:
    """扫描 知识库\\书籍\\，返回 [{书名, 书号, 有索引}]。

    #38：不再逐本全读 原文.md 数「块数」——该字段已无任何消费方（书库页四列不含块数），
    每次刷新白读整本原文（5 本约 1.6MB），也与 #25 M1-2「别为取字段全量解析大文件」口径相悖。"""
    books_dir = common.get_path("知识库") / "书籍"
    result = []
    if not books_dir.exists():
        return result
    for d in sorted(books_dir.iterdir()):
        if not d.is_dir():
            continue
        if not (d / "原文.md").exists():
            continue
        idx = d / "倒排表.json"
        book_id = _read_book_id(idx) if idx.exists() else ""
        result.append({"书名": d.name, "书号": book_id, "有索引": idx.exists()})
    return result


def get_chapters(book_title: str) -> list:
    """读目录骨架.md 的章号列表。"""
    sk = common.get_path("知识库") / "书籍" / book_title / "目录骨架.md"
    chapters = []
    if not sk.exists():
        return chapters
    for line in sk.read_text(encoding="utf-8").split("\n"):
        line = line.strip()
        if line.startswith("|") and not line.startswith("| 章号") and not line.startswith("|---"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            if len(cells) >= 2 and cells[0].isdigit():
                chapters.append((cells[0], cells[1]))
    return chapters


# ---------- 全局任务中心（#25 M1，机制先行） ----------
# 现状：每页各持 _log_q/_op_q + after 泵，队列归页面实例所有——切页后进度无处回显（#10 遗留缺口）。
# M1：任务与日志归全局 TaskCenter，主页底部状态条只是任务的"只读视图"；
# 四页现有页级队列保留不变（双写），保证四页功能零回归（R14）。


@dataclass
class Task:
    id: int
    kind: str                        # "入库" / "标注" / "验证" / "重建"
    title: str                       # 摘要主体，如 "《滴天髓》"
    total: int = 0
    current: int = 0
    step: str = ""                   # 当前步骤（当前书/当前动作）
    status: str = "running"          # running / done / failed
    summary: str = ""
    log_lines: list = field(default_factory=list)
    updated: float = 0.0
    started: float = 0.0             # #38：任务起始时刻（状态条显示「已用时」）
    result: dict = None              # #38：业务结果载荷（关窗重开后视图据此恢复结果区）


class TaskCenter:
    """全局唯一任务中心：worker 线程只调本类（内部加锁），不碰任何 UI 控件。"""

    LOG_CAP = 5000                   # 单任务日志缓冲封顶，防长任务内存膨胀

    def __init__(self):
        self._tasks = {}
        self._order = []
        self._next = 1
        self._lock = _threading.Lock()

    def start(self, kind: str, title: str, total: int = 0) -> int:
        """注册任务，返回 task_id。"""
        with self._lock:
            tid = self._next
            self._next += 1
            self._tasks[tid] = Task(id=tid, kind=kind, title=title, total=total,
                                    updated=time.time(), started=time.time())
            self._order.append(tid)
            return tid

    def progress(self, tid: int, current=None, step=None, title=None):
        """更新进度：current 可为 0（尚未完成一本）。"""
        with self._lock:
            t = self._tasks.get(tid)
            if not t:
                return
            if current is not None:
                t.current = current
            if step is not None:
                t.step = step
            if title is not None:
                t.title = title
            t.updated = time.time()

    def log(self, tid: int, line: str):
        """追加日志行；仅 running 任务接收（任务结束后到达的零散日志自动丢弃）。"""
        with self._lock:
            t = self._tasks.get(tid)
            if not t or t.status != "running":
                return
            t.log_lines.append(str(line))
            if len(t.log_lines) > self.LOG_CAP:
                del t.log_lines[:len(t.log_lines) - self.LOG_CAP]
            t.updated = time.time()

    def finish(self, tid: int, ok: bool, summary: str = "", result: dict = None):
        """标记任务结束（替代 __DONE_BATCH__ 等哨兵消息的进度语义）。
        result（#38）：业务收尾载荷——视图（尤其关窗重开后的新实例）据此恢复结果区，
        属 Task 级数据、不随窗口销毁（R2）。"""
        with self._lock:
            t = self._tasks.get(tid)
            if not t:
                return
            t.status = "done" if ok else "failed"
            t.summary = summary
            if result is not None:
                t.result = result
            t.updated = time.time()

    def snapshot(self) -> list:
        """返回只读快照（dict 列表），供 UI 泵轮询；不暴露可变 Task 对象。"""
        with self._lock:
            return [{"id": t.id, "kind": t.kind, "title": t.title, "total": t.total,
                     "current": t.current, "step": t.step, "status": t.status,
                     "summary": t.summary, "log_count": len(t.log_lines),
                     "updated": t.updated, "started": t.started}
                    for t in (self._tasks[i] for i in self._order)]

    def log_text(self, tid: int) -> str:
        with self._lock:
            t = self._tasks.get(tid)
            return "\n".join(t.log_lines) if t else ""

    def log_lines(self, tid: int) -> list:
        """日志行副本（#38：关窗重开后视图从 TaskCenter 重绘日志，而非依赖原实例队列）。"""
        with self._lock:
            t = self._tasks.get(tid)
            return list(t.log_lines) if t else []

    def result(self, tid: int) -> dict:
        """任务结果载荷副本（#38）；无则返回 None。"""
        with self._lock:
            t = self._tasks.get(tid)
            return dict(t.result) if t and t.result else None

    def running_task(self, kinds=("入库", "标注")) -> dict:
        """最近一个运行中、且 kind 属 kinds 的任务快照（#38：关窗重开后视图重新挂接）；无则 None。"""
        with self._lock:
            for tid in reversed(self._order):
                t = self._tasks[tid]
                if t.status == "running" and t.kind in kinds:
                    return {"id": t.id, "kind": t.kind, "title": t.title,
                            "total": t.total, "current": t.current, "step": t.step,
                            "started": t.started, "log_count": len(t.log_lines)}
        return None

    def latest(self, status: str = None) -> int:
        """最近一个任务 id（可按状态过滤）；无则返回 0。"""
        with self._lock:
            for tid in reversed(self._order):
                if status is None or self._tasks[tid].status == status:
                    return tid
        return 0

    def running(self, kind: str = None) -> int:
        """运行中任务数（可按 kind 过滤）；#25 M3/R6：跨窗口实例的同类互斥守卫
        （入窗关闭后重建的新实例据此仍能识别在跑的入库任务）。"""
        with self._lock:
            return sum(1 for tid in self._order
                       if self._tasks[tid].status == "running"
                       and (kind is None or self._tasks[tid].kind == kind))


taskcenter = TaskCenter()


# ---------- #28 定案③：警告/失败类弹窗统一形态 ----------

def show_issue_dialog(parent, title, summary, detail=""):
    """警告/失败弹窗：人话结论 +【复制给 AI】+【看日志】+【关闭】。

    summary＝D 型人话结论（事实+影响+行动）；detail＝可复制的技术细节（一般为任务日志尾部）。
    【看日志】经 winfo_toplevel() 拿 App._open_log（与书库页「查看问题」同一接法）。
    原则：每处报警都必须给用户一个"能复制去问 AI"的出口，不再指向 .md 报告。"""
    import tkinter as tk
    from tkinter import ttk
    win = tk.Toplevel(parent)
    win.title(title)
    win.transient(parent.winfo_toplevel())
    ttk.Label(win, text=summary, wraplength=460, justify="left").pack(padx=12, pady=(12, 8))
    btns = ttk.Frame(win)
    btns.pack(pady=(0, 12))

    def copy_for_ai():
        text = summary if not detail else f"{summary}\n\n—— 技术细节 ——\n{detail}"
        win.clipboard_clear()
        win.clipboard_append(text)
        copy_btn.config(text="已复制 ✓")

    copy_btn = ttk.Button(btns, text="复制给 AI", command=copy_for_ai)
    copy_btn.pack(side="left", padx=4)

    def open_log():
        opener = getattr(parent.winfo_toplevel(), "_open_log", None)
        if callable(opener):
            opener()

    ttk.Button(btns, text="看日志", command=open_log).pack(side="left", padx=4)
    ttk.Button(btns, text="关闭", command=win.destroy).pack(side="left", padx=4)
    return win
