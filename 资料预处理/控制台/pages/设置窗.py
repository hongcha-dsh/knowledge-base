# -*- coding: utf-8 -*-
"""「设置」窗口（#25 M4）：原「设置页」改为按需弹出的 Toplevel 单例。

- 顶部「配置状态总览」行：MinerU ✅/❌、标注 AI ✅/❌；**未配置的区边框高亮**引导；
- ① MinerU 区 / ② 标注 AI 区原样搬入；「附加参数」「并发路数」（#35）收进标注 AI 区内小折叠「高级」（留空即默认）；
- ③ 阈值折叠区（#40 起含「MinerU 版面优选」，原在 ① 区）/ ④ 环境区原样（③ 折叠，④ 常显）；
- 内容超高可纵向滚动，窗口打开时按内容自适应尺寸；`focus_section("mineru" | "annotate")`
  定位并高亮对应区（前置检查拦截 / 状态条配置标签点击时用）；
- 连通性测试**保留本窗自有泵**（`_result_q` + `after`）——R9 拍板：不收编全局 TaskCenter；
- 打开时机（#25 M4）：主页标题栏「⚙ 设置」、首次运行自动弹一次、前置检查拦截时弹出定位、
  状态条右侧配置小标签点击；
- 保存生效机制不变（写 `路径配置.json` api 节，环境变量优先，无需重启）。
"""

import json
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import ttk, messagebox

import common
import llm_client
import mineru_client
import theme

# Windows：子进程不另开控制台黑窗（控制台以 pythonw 无控制台启动后，#34 防止子进程弹黑窗）
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_EDGE_IDLE = theme.HAIRLINE      # 区边框常态色
_EDGE_WARN = theme.WARN      # 未配置 / 被定位时的高亮色


class 设置窗(tk.Toplevel):
    """「设置」窗：MinerU Token、标注 AI、连通性测试、质量阈值。"""

    def __init__(self, master):
        super().__init__(master)
        self.title("设置")
        self.transient(master)
        self._result_q = queue.Queue()
        self._build()
        self._load_all()
        self._fit()
        self.after(100, self._poll_result)

    # ---------- 界面 ----------

    def _section(self, title):
        """配置区容器：tk.LabelFrame + 2px 边框，供未配置时高亮引导（tk 支持 highlightbackground）。"""
        return tk.LabelFrame(self.body, text=title, highlightthickness=2,
                             highlightbackground=_EDGE_IDLE, padx=2, pady=2)

    def _build(self):
        outer = ttk.Frame(self)
        outer.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(outer, highlightthickness=0)
        self.canvas.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(outer, orient="vertical", command=self.canvas.yview)
        sb.pack(side="right", fill="y")
        self.canvas.configure(yscrollcommand=sb.set)
        self.body = ttk.Frame(self.canvas)
        self._body_id = self.canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.body.bind("<Configure>", self._on_body_config)
        self.canvas.bind("<Configure>", self._on_canvas_config)
        self.bind("<MouseWheel>", self._on_wheel)   # Toplevel 在 bindtags 链上，子控件滚轮也能落到这里

        # 配置状态总览（#25 M4）
        ov = ttk.Frame(self.body)
        ov.pack(fill="x", padx=8, pady=(8, 2))
        ttk.Label(ov, text="配置状态：").pack(side="left")
        self.ov_mineru = tk.Label(ov, text="", foreground=theme.INK3)
        self.ov_mineru.pack(side="left", padx=(0, 14))
        self.ov_annot = tk.Label(ov, text="", foreground=theme.INK3)
        self.ov_annot.pack(side="left")
        self.ov_hint = ttk.Label(self.body, text="", foreground=theme.WARN)
        self.ov_hint.pack(fill="x", padx=8)

        # ① MinerU API 配置（可填写保存，环境变量优先）
        mineru_frame = self._section("① MinerU 配置（OCR 通道）")
        self.mineru_frame = mineru_frame
        mineru_frame.pack(fill="x", padx=8, pady=6)

        self.token_var = tk.StringVar()
        self.token_visible = tk.BooleanVar(value=False)

        row1 = ttk.Frame(mineru_frame)
        row1.pack(fill="x", padx=8, pady=2)
        ttk.Label(row1, text="API Token（密钥）：").pack(side="left")
        self.token_entry = ttk.Entry(row1, textvariable=self.token_var, width=46, show="*")
        self.token_entry.pack(side="left", padx=4)
        ttk.Checkbutton(row1, text="显示", variable=self.token_visible,
                        command=self._toggle_token_visible).pack(side="left")

        ttk.Label(mineru_frame,
                  text="从 MinerU 控制台（https://mineru.net）获取 API Token。\n"
                  "环境变量 MINERU_API_TOKEN 优先于此处；保存后写入 路径配置.json 的 api 节，无需重启即可生效。\n"
                  "⚠ 明文风险提示：Token 为明文存储，请勿分享 路径配置.json。",
                  foreground=theme.INK3, justify="left").pack(anchor="w", padx=8, pady=(0, 2))

        btns = ttk.Frame(mineru_frame)
        btns.pack(fill="x", padx=8, pady=2)
        ttk.Button(btns, text="保存 Token", command=self.save_token).pack(side="left", padx=(0, 6))
        self.test_btn = ttk.Button(btns, text="测试连通性", command=self.test_mineru)
        self.test_btn.pack(side="left", padx=6)

        self.mineru_result = tk.Text(mineru_frame, height=4)
        self.mineru_result.pack(fill="x", padx=8, pady=4)
        self.token_status = tk.Label(mineru_frame, text="", foreground=theme.INK3)
        self.token_status.pack(anchor="w", padx=8, pady=(0, 2))

        # ② 标注 AI 配置（API 自动回填用，OpenAI 兼容接口）
        ann_frame = self._section("② 标注 AI 配置（自动填写用，OpenAI 兼容接口）")
        self.ann_frame = ann_frame
        ann_frame.pack(fill="x", padx=8, pady=6)

        self.ann_base_var = tk.StringVar()
        self.ann_key_var = tk.StringVar()
        self.ann_model_var = tk.StringVar()
        self.ann_key_visible = tk.BooleanVar(value=False)

        brow = ttk.Frame(ann_frame)
        brow.pack(fill="x", padx=8, pady=2)
        ttk.Label(brow, text="Base URL：").pack(side="left")
        self.ann_base_entry = ttk.Entry(brow, textvariable=self.ann_base_var, width=52)
        self.ann_base_entry.pack(side="left", padx=4)

        krow = ttk.Frame(ann_frame)
        krow.pack(fill="x", padx=8, pady=2)
        ttk.Label(krow, text="API Key：").pack(side="left")
        self.ann_key_entry = ttk.Entry(krow, textvariable=self.ann_key_var, width=46, show="*")
        self.ann_key_entry.pack(side="left", padx=4)
        ttk.Checkbutton(krow, text="显示", variable=self.ann_key_visible,
                        command=self._toggle_ann_key_visible).pack(side="left")

        mrow = ttk.Frame(ann_frame)
        mrow.pack(fill="x", padx=8, pady=2)
        ttk.Label(mrow, text="模    型：").pack(side="left")
        ttk.Entry(mrow, textvariable=self.ann_model_var, width=52).pack(side="left", padx=4)

        # #35 附加请求参数 + 并发路数 → #25 M4 收进区内小折叠「高级」（留空即用默认）
        self._ann_adv_open = False
        self.ann_adv_toggle = ttk.Button(
            ann_frame, text="▸ 高级（附加参数 / 并发路数，留空即默认）",
            command=self._toggle_ann_adv)
        self.ann_adv_toggle.pack(anchor="w", padx=8, pady=(4, 0))
        self.ann_adv_frame = ttk.Frame(ann_frame)

        erow = ttk.Frame(self.ann_adv_frame)
        erow.pack(fill="x", padx=8, pady=2)
        ttk.Label(erow, text="附加参数：").pack(side="left")
        self.ann_extra_var = tk.StringVar()
        ttk.Entry(erow, textvariable=self.ann_extra_var, width=52).pack(side="left", padx=4)

        crow = ttk.Frame(self.ann_adv_frame)
        crow.pack(fill="x", padx=8, pady=2)
        ttk.Label(crow, text="并发路数：").pack(side="left")
        self.ann_conc_var = tk.StringVar()
        ttk.Entry(crow, textvariable=self.ann_conc_var, width=6).pack(side="left", padx=4)
        ttk.Label(crow, text="API 自动标注同时处理的包数（默认 8，填 1=逐包串行）",
                  foreground=theme.INK3).pack(side="left", padx=4)

        ttk.Label(ann_frame,
                  text="任意 OpenAI 兼容服务均可（火山方舟 / DeepSeek 等）；Base URL 与模型留空则用默认（火山方舟）。\n"
                  "环境变量 ANNOTATE_API_KEY 优先于此处 Key；保存后写入 路径配置.json api.annotate_api 节，无需重启即可生效。\n"
                  "附加参数（高级，留空即可）：JSON 对象，原样合并进请求体——如 DeepSeek 关思考模式填\n"
                  '{"thinking": {"type": "disabled"}}（参数名以你所用服务商文档为准，填错会导致调用报错）。\n'
                  "⚠ 明文风险提示：API Key 为明文存储，请勿分享 路径配置.json。",
                  foreground=theme.INK3, justify="left").pack(anchor="w", padx=8, pady=(0, 2))

        abtns = ttk.Frame(ann_frame)
        abtns.pack(fill="x", padx=8, pady=2)
        ttk.Button(abtns, text="保存配置", command=self.save_annotate).pack(side="left", padx=(0, 6))
        self.ann_test_btn = ttk.Button(abtns, text="测试连通性", command=self.test_annotate)
        self.ann_test_btn.pack(side="left", padx=6)

        self.ann_result = tk.Text(ann_frame, height=4)
        self.ann_result.pack(fill="x", padx=8, pady=4)
        self.ann_status = tk.Label(ann_frame, text="", foreground=theme.INK3)
        self.ann_status.pack(anchor="w", padx=8, pady=(0, 2))

        # ③ 阈值（高级，默认折叠）——#40：「MinerU 版面优选」由 ① 区移入本区（收纳位置变化，行为零改动）
        self._th_open = False
        self.th_toggle = ttk.Button(self.body, text="▸ ③ 质量检查阈值（高级，一般不用动）",
                                    command=self._toggle_thresholds)
        self.th_toggle.pack(anchor="w", padx=8, pady=(6, 0))
        self.th_frame = ttk.LabelFrame(self.body, text="③ 质量检查阈值（路径配置.json）")
        self.th_vars = {}
        th = common.get_thresholds()
        for i, (k, v) in enumerate(th.items()):
            ttk.Label(self.th_frame, text=f"{k}：").grid(row=i, column=0, padx=8, pady=2, sticky="e")
            var = tk.StringVar(value=str(v))
            self.th_vars[k] = var
            ttk.Entry(self.th_frame, textvariable=var, width=10).grid(row=i, column=1, pady=2)
        # #40：版面优选（原 ① 区主行；th_frame 走 grid，故经 pref_host 内嵌后整体入列）
        pref_host = ttk.Frame(self.th_frame)
        pref_host.grid(row=len(th), column=0, columnspan=2, sticky="w", padx=8, pady=(6, 0))
        self.layout_pref_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(pref_host, text="MinerU 版面分析（默认开）",
                        variable=self.layout_pref_var,
                        command=self.save_layout_pref).pack(side="left")
        self.layout_pref_status = tk.Label(pref_host, text="", foreground=theme.INK3)
        self.layout_pref_status.pack(side="left", padx=(6, 0))
        ttk.Button(self.th_frame, text="保存阈值", command=self.save_thresholds).grid(
            row=len(th) + 1, column=0, columnspan=2, pady=6)

        # ④ 环境
        info = ttk.LabelFrame(self.body, text="④ 环境")
        info.pack(fill="x", padx=8, pady=6)
        ttk.Label(info, text=f"Python {sys.version.split()[0]} | 项目根：{common.PROJECT_ROOT}").pack(
            anchor="w", padx=8, pady=4)

    # ---------- 滚动 / 自适应 / 定位 ----------

    def _on_body_config(self, _e=None):
        try:
            self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        except tk.TclError:
            pass

    def _on_canvas_config(self, e):
        try:
            self.canvas.itemconfigure(self._body_id, width=e.width)
        except tk.TclError:
            pass

    def _on_wheel(self, e):
        try:
            self.canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")
        except tk.TclError:
            pass

    def _fit(self):
        """按内容请求尺寸自适应窗口（内容再高也有滚动条兜底）。"""
        try:
            self.update_idletasks()
            w = min(max(self.body.winfo_reqwidth() + 40, 760), 1000)
            h = min(max(self.body.winfo_reqheight() + 20, 560), 900)
            self.geometry(f"{w}x{h}")
            self.minsize(680, 480)
        except tk.TclError:
            pass

    def focus_section(self, section):
        """定位并高亮指定配置区（open_window 在已开窗置顶后调用，R3）。

        section: "mineru"（① 区）/ "annotate"（② 区）——前置检查拦截、配置小标签点击时用。"""
        target = {"mineru": (self.mineru_frame, self.token_entry),
                  "annotate": (self.ann_frame, self.ann_base_entry)}.get(section)
        if not target:
            return
        frame, entry = target
        try:
            self.update_idletasks()
            self.canvas.yview_moveto(max(0.0, (frame.winfo_y() - 10) / max(1, self.body.winfo_height())))
        except tk.TclError:
            pass
        for f in (self.mineru_frame, self.ann_frame):
            try:
                f.config(highlightbackground=_EDGE_IDLE)
            except tk.TclError:
                pass
        try:
            frame.config(highlightbackground=_EDGE_WARN)
            entry.focus_set()
        except tk.TclError:
            pass

    def _set_edge(self, frame, warn):
        try:
            frame.config(highlightbackground=_EDGE_WARN if warn else _EDGE_IDLE)
        except tk.TclError:
            pass

    # ---------- 数据装载 ----------

    def on_show(self):
        """窗口再次置顶显示（单例 deiconify / 重开）：重读配置并刷新总览。"""
        self._load_all()

    def _load_all(self):
        self._load_token_value()
        self._load_layout_pref()
        self._load_annotate_values()
        self._refresh_overview()

    def _refresh_overview(self):
        """配置状态总览行 + 未配置区高亮引导（#25 M4）。"""
        try:
            has_mineru = bool(mineru_client.get_token())
        except Exception:
            has_mineru = False
        try:
            has_annot = bool(llm_client.is_configured())
        except Exception:
            has_annot = False
        self.ov_mineru.config(text="MinerU ✅ 已配置" if has_mineru else "MinerU ❌ 未配置",
                              foreground=theme.OK if has_mineru else theme.ERR)
        self.ov_annot.config(text="标注 AI ✅ 已配置" if has_annot else "标注 AI ❌ 未配置",
                             foreground=theme.OK if has_annot else theme.ERR)
        self._set_edge(self.mineru_frame, not has_mineru)
        self._set_edge(self.ann_frame, not has_annot)
        self.ov_hint.config(
            text="" if (has_mineru and has_annot)
            else "高亮区尚未配置——补齐并点「保存」后对应功能才可用（未配置不影响已有书库的检索）。")

    def _toggle_thresholds(self):
        if self._th_open:
            self.th_frame.pack_forget()
            self.th_toggle.config(text="▸ ③ 质量检查阈值（高级，一般不用动）")
        else:
            self.th_frame.pack(fill="x", padx=8, pady=6, after=self.th_toggle)
            self.th_toggle.config(text="▾ ③ 质量检查阈值（高级，一般不用动）")
        self._th_open = not self._th_open
        self._fit()

    def _toggle_ann_adv(self):
        if self._ann_adv_open:
            self.ann_adv_frame.pack_forget()
            self.ann_adv_toggle.config(text="▸ 高级（附加参数 / 并发路数，留空即默认）")
        else:
            self.ann_adv_frame.pack(fill="x", padx=8, pady=2)
            self.ann_adv_toggle.config(text="▾ 高级（附加参数 / 并发路数，留空即默认）")
        self._ann_adv_open = not self._ann_adv_open
        self._fit()

    # ---------- ① MinerU ----------

    def _load_token_value(self):
        """回填 Token：环境变量 MINERU_API_TOKEN 优先，其次 路径配置.json api 节。"""
        cfg = common.load_config()
        api = cfg.get("api", {})
        env_token = os.environ.get("MINERU_API_TOKEN", "").strip()
        cfg_token = str(api.get("mineru_api_token", "") or "").strip()
        self.token_var.set(env_token or cfg_token)
        if env_token:
            masked = f"{env_token[:4]}…{env_token[-2:]}" if len(env_token) > 8 else "已设置"
            self.token_status.config(text=f"当前生效：环境变量 MINERU_API_TOKEN（{masked}）", fg=theme.OK)
        elif cfg_token:
            self.token_status.config(
                text="当前生效：路径配置.json api 节（环境变量 MINERU_API_TOKEN 未设置）", fg=theme.OK)
        else:
            self.token_status.config(
                text="❌ 未设置（MinerU 通道不可用：开版面分析时自动退回文字层；文字层 PDF/其他格式不受影响）",
                fg=theme.ERR)

    def _toggle_token_visible(self):
        self.token_entry.config(show="" if self.token_visible.get() else "*")

    def save_token(self):
        cfg_path = common.PROJECT_ROOT / "路径配置.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        cfg.setdefault("api", {})
        cfg["api"]["mineru_api_token"] = self.token_var.get().strip()
        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        self._load_all()
        messagebox.showinfo(
            "完成",
            "Token 已保存。\n"
            "提示：环境变量 MINERU_API_TOKEN 优先于这里的配置；Token 是明文存储，别把这个文件分享给别人。")

    def _load_layout_pref(self):
        """回填 MinerU 版面优选开关（路径配置.json 顶层键，缺省开）。"""
        pref = bool(common.load_config().get("mineru_版面优选", True))
        self.layout_pref_var.set(pref)
        self.layout_pref_status.config(
            text="开：无文字层的 PDF 自动走在线识别；识别不可用或失败时自动退回本地文字层。" if pref
            else "关：一律本地文字层通道，不消耗额度")

    def save_layout_pref(self):
        cfg_path = common.PROJECT_ROOT / "路径配置.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        cfg["mineru_版面优选"] = self.layout_pref_var.get()
        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        self.layout_pref_status.config(
            text="已保存：开（无文字层的 PDF 自动走在线识别）" if self.layout_pref_var.get()
            else "已保存：关（一律本地文字层通道，不消耗额度）")

    def test_mineru(self):
        """连通性测试：后台线程执行 mineru_client --自检，界面不冻结。"""
        self.mineru_result.delete("1.0", "end")
        self.mineru_result.insert("end", "⏳ 正在测试连通性…（上传链接申请不真正上传、不消耗解析额度）")
        self.test_btn.state(["disabled"])
        threading.Thread(target=self._test_mineru_worker, daemon=True).start()

    def _test_mineru_worker(self):
        script = common.get_path("脚本目录") / "mineru_client.py"
        try:
            env = dict(os.environ, PYTHONIOENCODING="utf-8")
            p = subprocess.run([sys.executable, str(script), "--自检"],
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace", env=env, cwd=str(common.PROJECT_ROOT),
                               timeout=90, creationflags=_NO_WINDOW)
            out = "\n".join(line for line in (p.stdout or "").splitlines() if line.strip())
            if not out:
                out = "（无输出）"
        except subprocess.TimeoutExpired:
            out = "❌ 测试超时（90 秒未返回）。\n请检查：①网络/代理 ②Token 是否正确有效"
        except Exception as e:
            out = f"❌ 测试失败：{e}"
        # 跨线程安全：仅放入队列，UI 由主线程 _poll_result 轮询更新
        self._result_q.put(("mineru", out))

    # ---------- ② 标注 AI ----------

    def _load_annotate_values(self):
        """回填标注 AI 配置：环境变量 ANNOTATE_API_KEY 优先于 路径配置.json api.annotate_api。"""
        cfg = common.load_config().get("api", {}).get("annotate_api", {}) or {}
        self.ann_base_var.set(str(cfg.get("base_url", "") or ""))
        self.ann_model_var.set(str(cfg.get("model", "") or ""))
        extra = cfg.get("附加参数")
        self.ann_extra_var.set(
            json.dumps(extra, ensure_ascii=False) if isinstance(extra, dict) and extra else "")
        self.ann_conc_var.set(str(cfg.get("并发", "") or ""))
        env_key = os.environ.get("ANNOTATE_API_KEY", "").strip()
        cfg_key = str(cfg.get("api_key", "") or "").strip()
        self.ann_key_var.set(env_key or cfg_key)
        if env_key:
            masked = f"{env_key[:4]}…{env_key[-2:]}" if len(env_key) > 8 else "已设置"
            self.ann_status.config(text=f"当前生效：环境变量 ANNOTATE_API_KEY（{masked}）", fg=theme.OK)
        elif cfg_key:
            self.ann_status.config(
                text="当前生效：路径配置.json api.annotate_api 节（环境变量 ANNOTATE_API_KEY 未设置）", fg=theme.OK)
        else:
            self.ann_status.config(text="未配置（仅影响 API 自动标注，外部 AI 标注与入库检索均不受影响）", fg=theme.INK3)

    def _toggle_ann_key_visible(self):
        self.ann_key_entry.config(show="" if self.ann_key_visible.get() else "*")

    def save_annotate(self):
        extra_text = self.ann_extra_var.get().strip()
        extra = {}
        if extra_text:
            try:
                extra = json.loads(extra_text)
            except json.JSONDecodeError:
                messagebox.showerror("错误", "附加参数不是合法的 JSON 格式，请检查后重试\n"
                                     '示例：{"thinking": {"type": "disabled"}}')
                return
            if not isinstance(extra, dict):
                messagebox.showerror("错误", "附加参数需要是 JSON 对象（用 {} 包起来的一串键值）")
                return
        conc_text = self.ann_conc_var.get().strip()
        conc = None
        if conc_text:
            try:
                conc = int(conc_text)
                if conc < 1:
                    raise ValueError
            except ValueError:
                messagebox.showerror("错误", "并发路数需要是 ≥1 的整数（留空则用默认 8）")
                return
        cfg_path = common.PROJECT_ROOT / "路径配置.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        cfg.setdefault("api", {})
        cfg["api"]["annotate_api"] = {
            "base_url": self.ann_base_var.get().strip(),
            "api_key": self.ann_key_var.get().strip(),
            "model": self.ann_model_var.get().strip(),
        }
        if extra:
            cfg["api"]["annotate_api"]["附加参数"] = extra
        if conc is not None:
            cfg["api"]["annotate_api"]["并发"] = conc
        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        self._load_all()
        messagebox.showinfo(
            "完成",
            "标注 AI 配置已保存。\n"
            "提示：环境变量 ANNOTATE_API_KEY 优先于这里的 Key；配置是明文存储，别把这个文件分享给别人。")

    def test_annotate(self):
        """连通性测试：后台线程执行 llm_client --自检（最小对话），界面不冻结。"""
        self.ann_result.delete("1.0", "end")
        self.ann_result.insert("end", "⏳ 正在测试连通性…（最小对话调用，几乎不消耗额度）")
        self.ann_test_btn.state(["disabled"])
        threading.Thread(target=self._test_annotate_worker, daemon=True).start()

    def _test_annotate_worker(self):
        script = common.get_path("脚本目录") / "llm_client.py"
        try:
            env = dict(os.environ, PYTHONIOENCODING="utf-8")
            p = subprocess.run([sys.executable, str(script), "--自检"],
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace", env=env, cwd=str(common.PROJECT_ROOT),
                               timeout=90, creationflags=_NO_WINDOW)
            out = "\n".join(line for line in (p.stdout or "").splitlines() if line.strip())
            if not out:
                out = "（无输出）"
        except subprocess.TimeoutExpired:
            out = "❌ 测试超时（90 秒未返回）。\n请检查：①网络/代理 ②Base URL 与模型名 ③API Key 是否有效"
        except Exception as e:
            out = f"❌ 测试失败：{e}"
        self._result_q.put(("annotate", out))

    def _poll_result(self):
        """连通性测试结果泵（R9：保留设置窗自有泵，不收编 TaskCenter）。"""
        try:
            while True:
                kind, out = self._result_q.get_nowait()
                self._on_test_done(kind, out)
        except queue.Empty:
            pass
        try:
            if self.winfo_exists():
                self.after(100, self._poll_result)
        except tk.TclError:
            pass

    def _on_test_done(self, kind, out):
        try:
            if kind == "mineru":
                self.test_btn.state(["!disabled"])
                self.mineru_result.delete("1.0", "end")
                self.mineru_result.insert("end", out)
            else:
                self.ann_test_btn.state(["!disabled"])
                self.ann_result.delete("1.0", "end")
                self.ann_result.insert("end", out)
        except tk.TclError:
            pass

    # ---------- ③ 阈值 ----------

    def save_thresholds(self):
        cfg_path = common.PROJECT_ROOT / "路径配置.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        try:
            for k, var in self.th_vars.items():
                cfg["阈值"][k] = float(var.get())
        except ValueError:
            messagebox.showerror("错误", "阈值必须是数字")
            return
        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        messagebox.showinfo("完成", "阈值已保存")
