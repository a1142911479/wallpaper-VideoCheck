# -*- coding: utf-8 -*-
"""
GUI 图形界面模块（tkinter / ttk）
=================================

定位：给「不想敲命令行」的用户提供操作面板与结果查看器。
内部**完全复用** main.py 的流水线（run_pipeline），本模块不重复实现任何业务逻辑。

布局（单窗口三段式）：
    ┌─────────────────────────────────────────────────────┐
    │ ① 参数配置：目录选择 + 关键阈值 + 运行模式         │
    │    [开始运行] [停止] [打开报告] [打开回收目录]      │
    ├─────────────────────────────────────────────────────┤
    │ ② 运行日志（实时滚动，与命令行日志同一内容）       │
    ├─────────────────────────────────────────────────────┤
    │ ③ 复核结果表（读取 CSV 报告；按处置决定着色 + 筛选）│
    │    + 结果统计（待删除 / 保留 / 唯一 计数）          │
    └─────────────────────────────────────────────────────┘
      状态栏

线程模型（重要）：
    - 流水线在后台守护线程执行，避免界面卡死；
    - 日志经 queue.Queue 中转，主线程用 root.after() 定时拉取；
    - tkinter 控件**只在主线程访问**（tkinter 非线程安全）。

取消机制：
    「停止」按钮置位 threading.Event；流水线在每个视频 / 每对候选处
    协作式检查（PipelineCancelled），已写入 SQLite 的缓存结果保留，
    下次运行可断点续跑。

运行方式：
    python3 gui.py          # 需要图形环境（本地桌面 / 远程桌面）
    python3 gui.py --demo   # 冒烟自测：加载示例报告后自动退出
"""

import csv
import dataclasses
import logging
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Dict, List, Optional

from config import DEFAULT_CONFIG
from main import run_pipeline
from utils import PipelineCancelled, setup_logging

APP_TITLE = "视频去重系统 - 操作与复核台"

# 结果表着色（处置决定 → 标签名）
_ACTION_TAGS = {
    "待删除": "delete",
    "疑似重复": "suspect",
    "保留": "keep",
    "唯一": "unique",
    "无法解码": "corrupt",
}
_ACTION_FILTERS = ["全部", "待删除", "疑似重复", "保留", "唯一", "无法解码"]


# =====================================================================
# 日志桥接：logging → queue → 主线程文本框
# =====================================================================

class QueueLogHandler(logging.Handler):
    """把日志记录格式化后投递到线程安全队列，供 GUI 主线程消费。"""

    def __init__(self, log_queue: "queue.Queue[str]") -> None:
        super().__init__()
        self.log_queue = log_queue

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.log_queue.put(self.format(record))
        except Exception:  # 日志绝不应让业务崩溃
            pass


def _open_with_system(path: str) -> None:
    """用系统默认程序打开文件 / 目录（跨平台）。"""
    try:
        if os.name == "nt":
            os.startfile(path)  # noqa: S606 仅 Windows 可用
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception as error:  # 打开失败不影响主流程
        messagebox.showwarning("打开失败", f"无法打开 {path}\n{error}")


# =====================================================================
# 主窗口
# =====================================================================

class DedupGuiApp(tk.Tk):
    """视频去重系统 GUI 主窗口。"""

    def __init__(self) -> None:
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1180x820")
        self.minsize(960, 640)

        # 并发控制
        self._log_queue: "queue.Queue[str]" = queue.Queue()
        self._worker: Optional[threading.Thread] = None
        self._cancel_event = threading.Event()
        self._queue_handler: Optional[QueueLogHandler] = None

        # 报告行缓存（供筛选重绘）
        self._report_headers: List[str] = []
        self._report_rows: List[List[str]] = []

        self._build_form()
        self._build_actions()
        self._build_log_panel()
        self._build_result_panel()
        self._build_statusbar()

        self._poll_log_queue()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------
    # ① 参数配置区
    # ------------------------------------------------------------------
    def _build_form(self) -> None:
        form = ttk.LabelFrame(self, text="① 参数配置", padding=10)
        form.pack(fill="x", padx=10, pady=(10, 4))

        # 目录 / 路径变量
        self.video_dir_var = tk.StringVar(value=DEFAULT_CONFIG.VIDEO_DIR)
        self.db_path_var = tk.StringVar(value=DEFAULT_CONFIG.DB_PATH)
        self.report_path_var = tk.StringVar(value=DEFAULT_CONFIG.REPORT_PATH)
        self.trash_dir_var = tk.StringVar(value=DEFAULT_CONFIG.TRASH_DIR)

        rows = [
            ("视频目录（NAS）:", self.video_dir_var, self._browse_video_dir),
            ("指纹缓存库（本地 SSD）:", self.db_path_var, self._browse_db_file),
            ("CSV 复核报告:", self.report_path_var, self._browse_report_file),
            ("回收目录（留空=直接删除）:", self.trash_dir_var, self._browse_trash_dir),
        ]
        for row_index, (label, var, browse_cmd) in enumerate(rows):
            ttk.Label(form, text=label).grid(row=row_index, column=0, sticky="e", padx=(0, 6), pady=3)
            ttk.Entry(form, textvariable=var, width=72).grid(row=row_index, column=1, sticky="ew", pady=3)
            ttk.Button(form, text="浏览...", command=browse_cmd, width=9).grid(
                row=row_index, column=2, padx=(6, 0), pady=3
            )

        # 数值参数
        self.interval_var = tk.DoubleVar(value=DEFAULT_CONFIG.FIXED_INTERVAL_SEC)
        self.scene_var = tk.DoubleVar(value=DEFAULT_CONFIG.SCENE_THRESHOLD)
        self.phash_var = tk.DoubleVar(value=DEFAULT_CONFIG.PHASH_SCREENING_THRESHOLD)
        self.final_var = tk.DoubleVar(value=DEFAULT_CONFIG.FINAL_SIMILARITY_THRESHOLD)
        self.workers_var = tk.IntVar(value=DEFAULT_CONFIG.WORKER_THREADS)
        self.md5_chunk_var = tk.DoubleVar(value=DEFAULT_CONFIG.MD5_CHUNK_SIZE / (1024 * 1024))

        param_row = 4
        ttk.Label(form, text="初筛间隔(秒):").grid(row=param_row, column=0, sticky="e", padx=(0, 6), pady=3)
        ttk.Spinbox(form, from_=5, to=300, increment=5, textvariable=self.interval_var, width=8).grid(
            row=param_row, column=1, sticky="w", pady=3
        )

        param_row = 5
        ttk.Label(form, text="场景阈值（越小越敏感）:").grid(row=param_row, column=0, sticky="e", padx=(0, 6), pady=3)
        ttk.Spinbox(form, from_=1, to=60, increment=0.5, textvariable=self.scene_var, width=8).grid(
            row=param_row, column=1, sticky="w", pady=3
        )

        param_row = 6
        ttk.Label(form, text="pHash 初筛线（0-100）:").grid(row=param_row, column=0, sticky="e", padx=(0, 6), pady=3)
        ttk.Spinbox(form, from_=50, to=100, increment=1, textvariable=self.phash_var, width=8).grid(
            row=param_row, column=1, sticky="w", pady=3
        )

        param_row = 7
        ttk.Label(form, text="融合判重线（0-100）:").grid(row=param_row, column=0, sticky="e", padx=(0, 6), pady=3)
        ttk.Spinbox(form, from_=50, to=100, increment=1, textvariable=self.final_var, width=8).grid(
            row=param_row, column=1, sticky="w", pady=3
        )

        # 文件级快速判重开关（增量快通道）
        self.fast_dedup_var = tk.BooleanVar(value=DEFAULT_CONFIG.FAST_DEDUP_ENABLED)
        ttk.Checkbutton(
            form,
            text="文件级快速判重（增量快通道：字节级重复零解码直判，关闭则全量帧级比对）",
            variable=self.fast_dedup_var,
        ).grid(row=8, column=0, columnspan=3, sticky="w", pady=(6, 0))

        # 多线程开关（可选优化：视频存本地 SSD 时开启；NAS 机械盘保持关闭）
        self.multithread_var = tk.BooleanVar(value=DEFAULT_CONFIG.MULTITHREAD_ENABLED)
        ttk.Checkbutton(
            form,
            text="多线程并行解码（可选：仅推荐视频存本地 SSD 时开启；NAS 机械盘请保持关闭）",
            variable=self.multithread_var,
        ).grid(row=9, column=0, columnspan=3, sticky="w", pady=(2, 0))

        param_row = 10
        ttk.Label(form, text="工作线程数（多线程时生效）:").grid(row=param_row, column=0, sticky="e", padx=(0, 6), pady=3)
        ttk.Spinbox(form, from_=1, to=32, increment=1, textvariable=self.workers_var, width=8).grid(
            row=param_row, column=1, sticky="w", pady=3
        )

        param_row = 11
        ttk.Label(form, text="MD5 读块(MB，可选调优):").grid(row=param_row, column=0, sticky="e", padx=(0, 6), pady=3)
        ttk.Spinbox(form, from_=1, to=16, increment=1, textvariable=self.md5_chunk_var, width=8).grid(
            row=param_row, column=1, sticky="w", pady=3
        )

        form.columnconfigure(1, weight=1)

    # ------------------------------------------------------------------
    # ② 运行控制按钮
    # ------------------------------------------------------------------
    def _build_actions(self) -> None:
        bar = ttk.Frame(self, padding=(10, 4))
        bar.pack(fill="x")

        self.execute_var = tk.BooleanVar(value=False)
        self.execute_check = ttk.Checkbutton(
            bar,
            text="执行删除（不勾选 = 仅试运行，只出报告不动文件）",
            variable=self.execute_var,
        )
        self.execute_check.pack(side="left")

        self.start_btn = ttk.Button(bar, text="▶ 开始运行", command=self._start_pipeline)
        self.start_btn.pack(side="left", padx=(16, 4))
        self.stop_btn = ttk.Button(bar, text="■ 停止", command=self._stop_pipeline, state="disabled")
        self.stop_btn.pack(side="left", padx=4)
        ttk.Button(bar, text="打开报告", command=self._open_report).pack(side="left", padx=4)
        ttk.Button(bar, text="打开回收目录", command=self._open_trash).pack(side="left", padx=4)

    # ------------------------------------------------------------------
    # ③ 运行日志
    # ------------------------------------------------------------------
    def _build_log_panel(self) -> None:
        frame = ttk.LabelFrame(self, text="② 运行日志", padding=6)
        frame.pack(fill="both", expand=True, padx=10, pady=4)

        self.log_text = tk.Text(frame, height=12, wrap="word", state="disabled",
                                font=("Consolas", 9))
        scroll = ttk.Scrollbar(frame, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scroll.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

    # ------------------------------------------------------------------
    # ④ 复核结果表
    # ------------------------------------------------------------------
    def _build_result_panel(self) -> None:
        frame = ttk.LabelFrame(self, text="③ 复核结果（来自 CSV 报告）", padding=6)
        frame.pack(fill="both", expand=True, padx=10, pady=(4, 10))

        top = ttk.Frame(frame)
        top.pack(fill="x", pady=(0, 4))
        ttk.Label(top, text="筛选:").pack(side="left")
        self.filter_var = tk.StringVar(value="全部")
        filter_box = ttk.Combobox(top, textvariable=self.filter_var, values=_ACTION_FILTERS,
                                  state="readonly", width=10)
        filter_box.pack(side="left", padx=6)
        filter_box.bind("<<ComboboxSelected>>", lambda _e: self._render_rows())
        self.summary_var = tk.StringVar(value="尚未加载报告")
        ttk.Label(top, textvariable=self.summary_var, foreground="#555555").pack(side="left", padx=16)

        table_frame = ttk.Frame(frame)
        table_frame.pack(fill="both", expand=True)

        self.result_tree = ttk.Treeview(table_frame, show="headings", selectmode="extended")
        y_scroll = ttk.Scrollbar(table_frame, command=self.result_tree.yview)
        x_scroll = ttk.Scrollbar(table_frame, orient="horizontal", command=self.result_tree.xview)
        self.result_tree.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)

        self.result_tree.grid(row=0, column=0, sticky="nsew")
        y_scroll.grid(row=0, column=1, sticky="ns")
        x_scroll.grid(row=1, column=0, sticky="ew")
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)

        # 行着色：待删除=红 / 疑似=橙 / 保留=绿 / 唯一=灰 / 无法解码=紫
        self.result_tree.tag_configure("delete", foreground="#c62828")
        self.result_tree.tag_configure("suspect", foreground="#e65100")
        self.result_tree.tag_configure("keep", foreground="#2e7d32")
        self.result_tree.tag_configure("unique", foreground="#757575")
        self.result_tree.tag_configure("corrupt", foreground="#6a1b9a")

    # ------------------------------------------------------------------
    # ⑤ 状态栏
    # ------------------------------------------------------------------
    def _build_statusbar(self) -> None:
        self.status_var = tk.StringVar(value="就绪（默认试运行：只出报告，不删文件）")
        status = ttk.Label(self, textvariable=self.status_var, relief="sunken", anchor="w", padding=(8, 3))
        status.pack(fill="x", side="bottom")

    # ==================================================================
    # 浏览按钮
    # ==================================================================
    def _browse_video_dir(self) -> None:
        path = filedialog.askdirectory(title="选择视频目录（NAS 挂载点）")
        if path:
            self.video_dir_var.set(path)

    def _browse_db_file(self) -> None:
        path = filedialog.asksaveasfilename(
            title="指纹缓存库路径", defaultextension=".db",
            filetypes=[("SQLite 数据库", "*.db"), ("全部文件", "*.*")],
        )
        if path:
            self.db_path_var.set(path)

    def _browse_report_file(self) -> None:
        path = filedialog.asksaveasfilename(
            title="CSV 报告输出路径", defaultextension=".csv",
            filetypes=[("CSV 文件", "*.csv"), ("全部文件", "*.*")],
        )
        if path:
            self.report_path_var.set(path)

    def _browse_trash_dir(self) -> None:
        path = filedialog.askdirectory(title="选择回收目录（留空 = 直接删除）")
        if path:
            self.trash_dir_var.set(path)

    # ==================================================================
    # 运行控制
    # ==================================================================
    def _build_config(self):
        """把表单值装配成 Config；数值非法时弹窗并返回 None。"""
        try:
            return dataclasses.replace(
                DEFAULT_CONFIG,
                VIDEO_DIR=self.video_dir_var.get().strip(),
                DB_PATH=self.db_path_var.get().strip(),
                REPORT_PATH=self.report_path_var.get().strip(),
                TRASH_DIR=self.trash_dir_var.get().strip(),
                FAST_DEDUP_ENABLED=bool(self.fast_dedup_var.get()),
                MULTITHREAD_ENABLED=bool(self.multithread_var.get()),
                WORKER_THREADS=max(1, int(self.workers_var.get())),
                MD5_CHUNK_SIZE=max(64 * 1024, int(float(self.md5_chunk_var.get()) * 1024 * 1024)),
                FIXED_INTERVAL_SEC=float(self.interval_var.get()),
                SCENE_THRESHOLD=float(self.scene_var.get()),
                PHASH_SCREENING_THRESHOLD=float(self.phash_var.get()),
                FINAL_SIMILARITY_THRESHOLD=float(self.final_var.get()),
            )
        except (tk.TclError, ValueError) as error:
            messagebox.showerror("参数错误", f"数值参数无效：{error}")
            return None

    def _start_pipeline(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            messagebox.showinfo("提示", "任务正在运行中，请先停止或等待完成。")
            return

        config = self._build_config()
        if config is None:
            return
        if not os.path.isdir(config.VIDEO_DIR):
            messagebox.showerror("参数错误", f"视频目录不存在：\n{config.VIDEO_DIR}")
            return

        execute = self.execute_var.get()
        if execute:
            if not config.TRASH_DIR:
                confirmed = messagebox.askyesno(
                    "危险操作确认",
                    "已勾选「执行删除」且未设置回收目录：\n"
                    "重复文件将被**直接删除，不可恢复**！\n\n确定要继续吗？",
                    icon="warning",
                )
            else:
                confirmed = messagebox.askyesno(
                    "执行确认",
                    f"将把重复文件移入回收目录：\n{config.TRASH_DIR}\n\n确定要继续吗？",
                )
            if not confirmed:
                return

        self._cancel_event.clear()
        self._set_running(True)
        self._clear_log()
        self._append_log(f"=== 开始运行（{'执行删除' if execute else '试运行 dry-run'}）===\n")

        # 同步挂接：文件日志 + GUI 队列日志（内容与命令行完全一致）
        setup_logging(config.LOG_FILE, config.LOG_LEVEL)
        app_logger = logging.getLogger("VideoDeduplication")
        self._queue_handler = QueueLogHandler(self._log_queue)
        self._queue_handler.setFormatter(
            logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
        )
        app_logger.addHandler(self._queue_handler)

        self._worker = threading.Thread(
            target=self._run_worker, args=(config, execute), daemon=True, name="dedup-pipeline",
        )
        self._worker.start()

    def _run_worker(self, config, execute: bool) -> None:
        """后台线程：执行流水线，结果以控制消息投递回主线程。"""
        try:
            run_pipeline(config, execute=execute, cancel_check=self._cancel_event.is_set)
            self._log_queue.put(("__finished__", ""))
        except PipelineCancelled:
            self._log_queue.put(("__cancelled__", ""))
        except Exception as error:  # noqa: BLE001 兜底展示任何运行期异常
            self._log_queue.put(("__error__", str(error)))

    def _stop_pipeline(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            self._cancel_event.set()
            self.status_var.set("正在停止…（等待当前视频处理完，缓存结果会保留）")
            self._append_log(">>> 收到停止请求，流水线将尽快中断（已入库缓存保留） <<<\n")

    def _set_running(self, running: bool) -> None:
        state_running = "disabled" if running else "normal"
        state_idle = "normal" if running else "disabled"
        self.start_btn.configure(state=state_running)
        self.stop_btn.configure(state=state_idle)
        self.execute_check.configure(state=state_running)

    # ==================================================================
    # 日志面板
    # ==================================================================
    def _clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def _append_log(self, text: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text)
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _poll_log_queue(self) -> None:
        """主线程定时拉取后台日志 / 控制消息（tkinter 只在主线程碰控件）。"""
        try:
            while True:
                item = self._log_queue.get_nowait()
                if isinstance(item, tuple):
                    self._handle_worker_event(item)
                else:
                    self._append_log(item + "\n")
        except queue.Empty:
            pass
        self.after(100, self._poll_log_queue)

    def _handle_worker_event(self, event: tuple) -> None:
        kind, detail = event
        if kind == "__finished__":
            self._set_running(False)
            self.status_var.set("运行完成。请在下方表格复核「待删除」行，确认后再执行删除。")
            self._append_log("=== 运行完成 ===\n")
            self._load_report()
        elif kind == "__cancelled__":
            self._set_running(False)
            self.status_var.set("已停止。缓存已保留，下次运行将断点续跑。")
            self._append_log("=== 已停止（缓存保留，下次续跑） ===\n")
        elif kind == "__error__":
            self._set_running(False)
            self.status_var.set(f"运行出错：{detail}")
            self._append_log(f"=== 运行出错：{detail} ===\n")
            messagebox.showerror("运行出错", detail)

    # ==================================================================
    # 结果表
    # ==================================================================
    def _load_report(self) -> None:
        """读取 CSV 复核报告并刷新结果表。"""
        report_path = self.report_path_var.get().strip()
        if not os.path.isfile(report_path):
            self.status_var.set(f"未找到报告文件：{report_path}")
            return
        try:
            with open(report_path, "r", encoding="utf-8-sig", newline="") as csv_file:
                reader = csv.reader(csv_file)
                self._report_headers = next(reader, [])
                self._report_rows = [row for row in reader if row]
        except OSError as error:
            messagebox.showerror("读取失败", f"无法读取报告：{error}")
            return

        self._setup_tree_columns()
        self._render_rows()

    def _setup_tree_columns(self) -> None:
        columns = self._report_headers
        self.result_tree.configure(columns=columns)
        for name in columns:
            if name in ("视频路径", "判定依据"):
                width = 320
            elif name in ("处置决定", "组ID", "保留总分", "综合相似度"):
                width = 90
            elif name in ("判定来源", "人工复核"):
                width = 110
            else:
                width = 80
            self.result_tree.heading(name, text=name, anchor="w")
            self.result_tree.column(name, width=width, anchor="w")

    def _render_rows(self) -> None:
        """按筛选条件重绘结果表，并更新统计。"""
        for item in self.result_tree.get_children():
            self.result_tree.delete(item)

        if not self._report_headers:
            return

        try:
            action_index = self._report_headers.index("处置决定")
        except ValueError:
            action_index = -1

        filter_name = self.filter_var.get()
        counts = {"待删除": 0, "疑似重复": 0, "保留": 0, "唯一": 0, "无法解码": 0}
        shown = 0
        for row in self._report_rows:
            action = row[action_index] if 0 <= action_index < len(row) else ""
            if action in counts:
                counts[action] += 1
            if filter_name != "全部" and action != filter_name:
                continue
            tag = _ACTION_TAGS.get(action, "")
            self.result_tree.insert("", "end", values=row, tags=(tag,))
            shown += 1

        self.summary_var.set(
            f"共 {len(self._report_rows)} 行 ｜ 待删除 {counts['待删除']}"
            f" ｜ 疑似 {counts['疑似重复']} ｜ 保留 {counts['保留']}"
            f" ｜ 唯一 {counts['唯一']} ｜ 无法解码 {counts['无法解码']} ｜ 当前显示 {shown} 行"
        )

    # ==================================================================
    # 辅助动作 / 退出
    # ==================================================================
    def _open_report(self) -> None:
        path = self.report_path_var.get().strip()
        if os.path.isfile(path):
            _open_with_system(path)
        else:
            messagebox.showinfo("提示", f"报告尚未生成：\n{path}")

    def _open_trash(self) -> None:
        path = self.trash_dir_var.get().strip()
        if path and os.path.isdir(path):
            _open_with_system(path)
        else:
            messagebox.showinfo("提示", "回收目录未配置或不存在。")

    def _on_close(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            if not messagebox.askyesno(
                "确认退出", "任务仍在运行，退出将中断处理（已入库缓存保留）。确定退出吗？",
                icon="warning",
            ):
                return
            self._cancel_event.set()
        self.destroy()


# =====================================================================
# 入口
# =====================================================================

def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        app = DedupGuiApp()
    except tk.TclError as error:
        print(
            "无法创建图形界面（当前环境没有显示器？）。\n"
            "请在本地桌面运行 python3 gui.py；服务器场景请改用命令行：python3 main.py --help\n"
            f"原始错误：{error}",
            file=sys.stderr,
        )
        return 1

    if "--demo" in argv:
        # 冒烟自测：加载已有示例报告，渲染后自动退出（供 CI / 开发自检）
        app.after(300, app._load_report)
        app.after(900, app.destroy)
    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
