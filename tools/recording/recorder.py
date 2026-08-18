import ctypes
import json
import os
import subprocess
import sys
import threading
import time
import tkinter as tk

from pynput import keyboard, mouse

# --- Windows DPI Awareness Fix ---
if sys.platform == "win32":
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # PROCESS_SYSTEM_DPI_AWARE
    except Exception:
        pass

# --- Windows Console Encoding Fix ---
if sys.platform == "win32" and sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# ---------- 统一视觉常量 ----------
COLORS = {
    "bg": "#1a1f2e",
    "bg_card": "#252b36",
    "bg_highlight": "#2c3544",
    "bg_input": "#1e2430",
    "text": "#f0f2f5",
    "text_secondary": "#8b92a0",
    "accent_green": "#22c55e",
    "accent_green_hover": "#4ade80",
    "accent_blue": "#3b82f6",
    "accent_blue_hover": "#60a5fa",
    "accent_red": "#dc2626",
    "accent_red_hover": "#ef4444",
    "accent_amber": "#f59e0b",
    "accent_amber_hover": "#fbbf24",
    "record_dot": "#dc2626",
    "record_dot_border": "#f87171",
    "border": "#3d4554",
    "separator": "#2d3340",
    "card": "#222838",
    "muted": "#6b7280",
}


def _style_button_enter(w, normal_bg, hover_bg):
    w.config(bg=hover_bg)


def _style_button_leave(w, normal_bg, hover_bg):
    w.config(bg=normal_bg)


def make_flat_button(
    parent, text, command, bg, hover_bg=None, fg="white", width=22, font_size=10, **pack_kw
):
    hover_bg = hover_bg or bg
    btn = tk.Button(
        parent,
        text=text,
        command=command,
        bg=bg,
        fg=fg,
        font=("Microsoft YaHei", font_size),
        relief=tk.FLAT,
        bd=0,
        highlightthickness=0,
        cursor="hand2",
        activebackground=bg,
        activeforeground=fg,
        padx=20,
        pady=10,
    )
    btn.bind("<Enter>", lambda e: _style_button_enter(btn, bg, hover_bg))
    btn.bind("<Leave>", lambda e: _style_button_leave(btn, bg, hover_bg))
    if width:
        btn.config(width=width)
    btn.pack(**(pack_kw or {"pady": 6}))
    return btn


def _dialog_center_over(parent, w, h, offset_x=0, offset_y=0):
    x = parent.winfo_x() + max(0, (parent.winfo_width() - w) // 2) + offset_x
    y = parent.winfo_y() + max(0, (parent.winfo_height() - h) // 2) + offset_y
    return f"{w}x{h}+{x}+{y}"


def _default_recording_dir():
    """录屏默认保存到用户目录，避免堆积在项目文件夹内。"""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA", os.path.expanduser("~"))
        return os.path.join(base, "JianyingEditor", "recordings")
    return os.path.join(os.path.expanduser("~"), "JianyingEditor", "recordings")


def _find_carnac_executable():
    """Windows: 定位 Carnac 可执行文件；未找到返回 None。"""
    if sys.platform != "win32":
        return None
    env = (os.environ.get("CARNAC_EXE") or "").strip()
    if env and os.path.isfile(env):
        return env
    # 便携包：运行 tools/recording/install_carnac.py 后解压至 carnac_bundle/
    bundled = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "carnac_bundle",
        "lib",
        "net45",
        "Carnac.exe",
    )
    if os.path.isfile(bundled):
        return bundled
    try:
        r = subprocess.run(
            ["where", "Carnac.exe"],
            capture_output=True,
            text=True,
            timeout=8,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if r.returncode == 0 and r.stdout.strip():
            p = r.stdout.strip().splitlines()[0].strip()
            if os.path.isfile(p):
                return p
    except Exception:
        pass
    local = os.environ.get("LOCALAPPDATA", "")
    candidates = [
        os.path.join(local, "Carnac", "Carnac.exe"),
        os.path.join(local, "Programs", "Carnac", "Carnac.exe"),
    ]
    for p in candidates:
        if os.path.isfile(p):
            return p
    # Squirrel：%LocalAppData%\Carnac\app-x.y.z\Carnac.exe
    root = os.path.join(local, "Carnac")
    if os.path.isdir(root):
        try:
            for name in sorted(os.listdir(root), reverse=True):
                if not name.startswith("app-"):
                    continue
                exe = os.path.join(root, name, "Carnac.exe")
                if os.path.isfile(exe):
                    return exe
        except Exception:
            pass
    return None


def _is_carnac_running_win32():
    if sys.platform != "win32":
        return False
    try:
        r = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq Carnac.exe"],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return "Carnac.exe" in (r.stdout or "")
    except Exception:
        return False


_install_carnac_mod = None


def _ensure_bundled_carnac():
    """Check for an already bundled Carnac executable without downloading."""
    global _install_carnac_mod
    if _install_carnac_mod is None:
        import importlib.util

        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "install_carnac.py")
        spec = importlib.util.spec_from_file_location("jy_install_carnac", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _install_carnac_mod = mod
    return _install_carnac_mod.ensure_bundled_carnac()


def _get_windows_displays():
    """返回 [(显示名称, (x, y, w, h)), ...]，首项为「全部」；非 Windows 或失败时仅返回 [('全部', None)]。"""
    if sys.platform != "win32":
        return [("全部", None)]
    try:

        user32 = ctypes.windll.user32
        monitors = []

        def _callback(hmon, hdc, rect_ptr, data):
            r = rect_ptr.contents
            x, y = r.left, r.top
            w, h = r.right - r.left, r.bottom - r.top
            monitors.append((x, y, w, h))
            return 1

        MONITORENUMPROC = ctypes.WINFUNCTYPE(
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.wintypes.RECT),
            ctypes.c_void_p,
        )
        cb = MONITORENUMPROC(_callback)
        user32.EnumDisplayMonitors(None, None, cb, 0)
        if not monitors:
            return [("全部", None)]

        # 全部 = 虚拟桌面包围盒
        xs = [m[0] for m in monitors]
        ys = [m[1] for m in monitors]
        x2 = [m[0] + m[2] for m in monitors]
        y2 = [m[1] + m[3] for m in monitors]
        all_x, all_y = min(xs), min(ys)
        all_w = max(x2) - all_x
        all_h = max(y2) - all_y
        result = [("全部（所有显示器）", (all_x, all_y, all_w, all_h))]
        for i, (x, y, w, h) in enumerate(monitors):
            result.append((f"屏幕 {i + 1} ({w}×{h})", (x, y, w, h)))
        return result
    except Exception:
        return [("全部", None)]


class ProGuiRecorder:
    def __init__(self, output_dir=None, audio_device=None):
        # 默认保存到用户目录（不落在项目内）；仅当显式传入 output_dir 时才用项目路径
        default_dir = _default_recording_dir()
        self.output_dir = os.path.abspath(output_dir or default_dir)
        os.makedirs(self.output_dir, exist_ok=True)

        self.config_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "recorder_config.json"
        )
        self.audio_device = audio_device
        self.is_recording = False
        self.start_time = 0
        self.events = []
        self.process = None
        self._capture_region = None  # (x, y, w, h) 或 None=全部，录制时写入
        self._carnac_process = None  # 由本助手 subprocess 启动的 Carnac；结束录制或关闭窗口时结束
        self._carnac_lock = threading.Lock()
        self._events_lock = threading.Lock()  # pynput 回调与主线程对 events 的互斥

        # 可选显示器列表（Windows 多屏）
        self.displays = _get_windows_displays()

        # UI Setup
        self.root = tk.Tk()
        self.enable_zoom_record = tk.BooleanVar(value=True)
        self.enable_carnac = tk.BooleanVar(value=True)
        self.capture_display_var = tk.StringVar(value=self.displays[0][0])
        self.root.title("剪映录屏助手")
        self.root.attributes("-topmost", True)
        self.root.configure(bg=COLORS["bg"], highlightthickness=0)
        self.root.minsize(440, 620)

        # 加载记忆位置
        self.load_config()

        # --- 主界面 ---
        self.main_frame = tk.Frame(self.root, bg=COLORS["bg"])
        self.main_frame.pack(fill=tk.BOTH, expand=True)

        # 顶部标题条
        header = tk.Frame(self.main_frame, bg=COLORS["bg_card"], height=52)
        header.pack(fill=tk.X)
        header.pack_propagate(False)
        tk.Label(
            header,
            text="剪映录屏助手",
            fg=COLORS["text"],
            bg=COLORS["bg_card"],
            font=("Microsoft YaHei", 13, "bold"),
        ).pack(side=tk.LEFT, padx=24, pady=14)
        accent_line = tk.Frame(self.main_frame, bg=COLORS["accent_blue"], height=2)
        accent_line.pack(fill=tk.X)

        content = tk.Frame(self.main_frame, bg=COLORS["bg"], padx=20, pady=16)
        content.pack(fill=tk.BOTH, expand=True)

        # 紧凑状态条
        status_row = tk.Frame(content, bg=COLORS["bg_highlight"], padx=14, pady=10)
        status_row.pack(fill=tk.X, pady=(0, 12))
        tk.Label(
            status_row,
            text="●",
            fg=COLORS["accent_green"],
            bg=COLORS["bg_highlight"],
            font=("Microsoft YaHei", 11),
        ).pack(side=tk.LEFT, padx=(0, 8))
        self.status_label = tk.Label(
            status_row,
            text="准备就绪",
            fg=COLORS["text"],
            bg=COLORS["bg_highlight"],
            font=("Microsoft YaHei", 12, "bold"),
        )
        self.status_label.pack(side=tk.LEFT)

        # 录制设置卡片
        settings_card = tk.Frame(content, bg=COLORS["card"], padx=14, pady=12)
        settings_card.pack(fill=tk.X, pady=(0, 12))
        tk.Label(
            settings_card,
            text="录制设置",
            fg=COLORS["muted"],
            bg=COLORS["card"],
            font=("Microsoft YaHei", 9),
        ).pack(anchor=tk.W, pady=(0, 10))

        row_display = tk.Frame(settings_card, bg=COLORS["card"])
        row_display.pack(fill=tk.X, pady=(0, 8))
        tk.Label(
            row_display,
            text="屏幕",
            fg=COLORS["text_secondary"],
            bg=COLORS["card"],
            font=("Microsoft YaHei", 10),
            width=5,
            anchor=tk.W,
        ).pack(side=tk.LEFT, padx=(0, 8))
        display_names = [d[0] for d in self.displays]
        self.display_menu = tk.OptionMenu(row_display, self.capture_display_var, *display_names)
        self.display_menu.config(
            bg=COLORS["bg_input"],
            fg=COLORS["text"],
            font=("Microsoft YaHei", 10),
            relief=tk.FLAT,
            bd=0,
            highlightthickness=1,
            highlightbackground=COLORS["border"],
            cursor="hand2",
            activebackground=COLORS["bg_highlight"],
            activeforeground=COLORS["text"],
        )
        self.display_menu["menu"].config(
            bg=COLORS["bg_highlight"], fg=COLORS["text"], font=("Microsoft YaHei", 10)
        )
        self.display_menu.pack(side=tk.LEFT, fill=tk.X, expand=True)

        audio_status = "已开启" if audio_device else "已禁用"
        tk.Label(
            settings_card,
            text=f"系统音频　{audio_status}",
            fg=COLORS["text_secondary"],
            bg=COLORS["card"],
            font=("Microsoft YaHei", 10),
            anchor=tk.W,
        ).pack(anchor=tk.W, pady=(0, 8))

        tk.Label(
            settings_card,
            text="保存路径（点击打开文件夹）",
            fg=COLORS["muted"],
            bg=COLORS["card"],
            font=("Microsoft YaHei", 9),
            anchor=tk.W,
        ).pack(anchor=tk.W, pady=(0, 4))
        self.path_box = tk.Frame(
            settings_card, bg=COLORS["bg_input"], padx=10, pady=8, cursor="hand2"
        )
        self.path_box.pack(fill=tk.X)
        self.info_label = tk.Label(
            self.path_box,
            text=self.output_dir,
            fg=COLORS["accent_blue"],
            bg=COLORS["bg_input"],
            font=("Consolas", 9),
            justify=tk.LEFT,
            anchor=tk.W,
            wraplength=380,
            cursor="hand2",
        )
        self.info_label.pack(anchor=tk.W, fill=tk.X)
        self.info_label.bind("<Button-1>", self._open_recordings_folder)
        self.path_box.bind("<Button-1>", self._open_recordings_folder)
        self.path_box.bind("<Enter>", self._path_link_enter)
        self.path_box.bind("<Leave>", self._path_link_leave)

        # 主操作
        btn_frame = tk.Frame(content, bg=COLORS["bg"])
        btn_frame.pack(fill=tk.X, pady=(0, 12))
        self.start_btn = tk.Button(
            btn_frame,
            text="  🎬  开始录制  ",
            command=self.start_countdown,
            bg=COLORS["accent_green"],
            fg="white",
            font=("Microsoft YaHei", 12, "bold"),
            relief=tk.FLAT,
            bd=0,
            cursor="hand2",
            activebackground=COLORS["accent_green_hover"],
            activeforeground="white",
            padx=24,
            pady=14,
        )
        self.start_btn.pack(fill=tk.X)
        self.start_btn.bind(
            "<Enter>",
            lambda e: _style_button_enter(
                self.start_btn, COLORS["accent_green"], COLORS["accent_green_hover"]
            ),
        )
        self.start_btn.bind(
            "<Leave>",
            lambda e: _style_button_leave(
                self.start_btn, COLORS["accent_green"], COLORS["accent_green_hover"]
            ),
        )

        # 选项卡片（复选框左对齐、统一宽度避免错位）
        opts_card = tk.Frame(content, bg=COLORS["card"], padx=14, pady=12)
        opts_card.pack(fill=tk.BOTH, expand=True)
        tk.Label(
            opts_card,
            text="选项",
            fg=COLORS["muted"],
            bg=COLORS["card"],
            font=("Microsoft YaHei", 9),
        ).pack(anchor=tk.W, pady=(0, 8))

        chk_style = {
            "bg": COLORS["card"],
            "fg": COLORS["text"],
            "selectcolor": COLORS["bg_input"],
            "activebackground": COLORS["card"],
            "activeforeground": COLORS["text"],
            "font": ("Microsoft YaHei", 10),
            "cursor": "hand2",
            "anchor": tk.W,
            "wraplength": 380,
            "justify": tk.LEFT,
            "highlightthickness": 0,
        }
        self.cb_carnac = tk.Checkbutton(
            opts_card,
            text="录制时启动 Carnac（屏幕按键显示）",
            variable=self.enable_carnac,
            command=self._on_carnac_toggle,
            **chk_style,
        )
        self.zoom_cb = tk.Checkbutton(
            opts_card,
            text="智能缩放记录（鼠标 / 键盘）",
            variable=self.enable_zoom_record,
            **chk_style,
        )
        self.carnac_hint_label = tk.Label(
            opts_card,
            text="",
            fg=COLORS["accent_amber"],
            bg=COLORS["card"],
            font=("Microsoft YaHei", 9),
            justify=tk.LEFT,
            wraplength=380,
        )
        if sys.platform == "win32":
            self.cb_carnac.pack(anchor=tk.W, fill=tk.X, pady=(0, 6))
            self.carnac_hint_label.pack(anchor=tk.W, fill=tk.X, pady=(0, 8))
        self.zoom_cb.pack(anchor=tk.W, fill=tk.X, pady=(0, 0))

        # --- 录制中简洁界面：可拖动移开，短按停止。用 arrow 避免悬停时光标切换导致“颤抖” ---
        self.mini_frame = tk.Frame(
            self.root, bg=COLORS["record_dot_border"], cursor="arrow", padx=2, pady=2
        )
        inner_mini = tk.Frame(self.mini_frame, bg=COLORS["record_dot"], cursor="arrow")
        inner_mini.pack(fill=tk.BOTH, expand=True)
        self.record_indicator = tk.Label(
            inner_mini, text="●", fg="white", bg=COLORS["record_dot"], font=("Arial", 22)
        )
        self.record_indicator.pack(expand=True)
        self._mini_drag_start = None  # (x, y) 按下时窗口位置，用于区分点击/拖拽

        def _on_mini_press(e):
            self._mini_drag_start = (self.root.winfo_x(), self.root.winfo_y())

        def _on_mini_release(e):
            if self._mini_drag_start is None:
                return
            dx = self.root.winfo_x() - self._mini_drag_start[0]
            dy = self.root.winfo_y() - self._mini_drag_start[1]
            if dx * dx + dy * dy < 64:  # 移动不足 8 像素视为点击 → 停止录制
                self.stop_recording()
            self._mini_drag_start = None

        for w in (self.mini_frame, inner_mini, self.record_indicator):
            w.bind("<Button-1>", _on_mini_press)
            w.bind("<ButtonRelease-1>", _on_mini_release)
            w.bind("<B1-Motion>", self.drag_window)

        # 初始隐藏 mini
        self.mini_frame.pack_forget()

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.m_listener = None
        self.k_listener = None
        self.screen_width = self.root.winfo_screenwidth()
        self.screen_height = self.root.winfo_screenheight()
        if sys.platform == "win32":
            self.root.after(200, self._update_carnac_hint)
            self.root.after(500, self._prefetch_carnac_background)

    def _prefetch_carnac_background(self):
        """Refresh optional-tool guidance without downloading an executable."""
        self._update_carnac_hint()

    def _on_carnac_toggle(self):
        self._update_carnac_hint()

    def _open_recordings_folder(self, event=None):
        try:
            p = os.path.abspath(self.output_dir)
            os.makedirs(p, exist_ok=True)
            if sys.platform == "win32":
                os.startfile(p)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", p], close_fds=True)
            else:
                subprocess.Popen(["xdg-open", p], close_fds=True)
        except Exception as e:
            print(f"打开保存目录失败: {e}")

    def _path_link_enter(self, _event=None):
        self.info_label.config(fg=COLORS["accent_blue_hover"], bg=COLORS["bg_highlight"])
        self.path_box.config(bg=COLORS["bg_highlight"])

    def _path_link_leave(self, _event=None):
        self.info_label.config(fg=COLORS["accent_blue"], bg=COLORS["bg_input"])
        self.path_box.config(bg=COLORS["bg_input"])

    def drag_window(self, event):
        dx, dy = 28, 28  # 约一半圆点尺寸，使拖拽时中心跟随
        x = self.root.winfo_x() + event.x - dx
        y = self.root.winfo_y() + event.y - dy
        self.root.geometry(f"+{x}+{y}")

    def _geometry_with_min_size(self, pos, min_w, min_h):
        """恢复窗口位置时保证宽高不低于最小值（避免增高 UI 后底部控件被裁切）。"""
        try:
            parts = pos.split("+")
            wh = parts[0].split("x")
            if len(wh) < 2:
                return pos
            w, h = int(wh[0]), int(wh[1])
            w = max(w, min_w)
            h = max(h, min_h)
            geo = f"{w}x{h}"
            if len(parts) >= 3:
                geo += f"+{parts[1]}+{parts[2]}"
            return geo
        except Exception:
            return pos

    def _geometry_on_screen(self, geo):
        """检查 geometry 是否在当前屏幕内，避免窗口跑到屏外。"""
        try:
            # 格式: "WxH+X+Y" 或 "WxH"
            parts = geo.split("+")
            w, h = 300, 240
            if parts:
                wh = parts[0].split("x")
                if len(wh) >= 2:
                    w, h = int(wh[0]), int(wh[1])
            x, y = 50, 50
            if len(parts) >= 3:
                x, y = int(parts[1]), int(parts[2])
            sw = self.root.winfo_screenwidth()
            sh = self.root.winfo_screenheight()
            margin = 20
            if x + w < margin or y + h < margin or x > sw - margin or y > sh - margin:
                return False
            return True
        except Exception:
            return False

    def load_config(self):
        default_geo = "460x640+50+50"
        min_w, min_h = 440, 620
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, "r") as f:
                    config = json.load(f)
                    if "enable_carnac" in config:
                        self.enable_carnac.set(bool(config["enable_carnac"]))
                    pos = config.get("window_pos", "")
                    if pos:
                        pos = self._geometry_with_min_size(pos, min_w, min_h)
                        if self._geometry_on_screen(pos):
                            parts = pos.split("+")
                            if len(parts) >= 1:
                                wh = parts[0].split("x")
                                if len(wh) >= 2:
                                    w, h = int(wh[0]), int(wh[1])
                                    if w >= min_w and h >= min_h:
                                        self.root.geometry(pos)
                                        return
            except Exception:
                pass
        self.root.geometry(default_geo)

    def on_close(self):
        try:
            if self.is_recording:
                self.stop_recording(show_post_dialog=False)
            else:
                self._terminate_carnac_if_we_started()
        except Exception as e:
            print(f"录屏助手退出清理异常: {e}")
        try:
            geo = self.root.geometry()
            with open(self.config_path, "w") as f:
                json.dump(
                    {"window_pos": geo, "enable_carnac": self.enable_carnac.get()},
                    f,
                    ensure_ascii=False,
                )
        except Exception:
            pass
        self.root.destroy()

    def generate_filename(self):
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        self.output_path = os.path.join(self.output_dir, f"recording_{timestamp}.mp4")
        self.events_path = self.output_path.replace(".mp4", "_events.json")

    def _norm_xy(self, x, y):
        """将桌面坐标转为录制区域内的归一化坐标 (0–1)。若不在区域内返回 None。"""
        if self._capture_region is None:
            return round(x / self.screen_width, 4), round(y / self.screen_height, 4)
        ox, oy, w, h = self._capture_region
        nx = (x - ox) / w if w else 0
        ny = (y - oy) / h if h else 0
        if 0 <= nx <= 1 and 0 <= ny <= 1:
            return round(nx, 4), round(ny, 4)
        return None

    def on_click(self, x, y, button, pressed):
        if self.is_recording and pressed and self.enable_zoom_record.get():
            norm = self._norm_xy(x, y)
            if norm is None:
                return
            rel_time = time.time() - self.start_time
            with self._events_lock:
                self.events.append(
                    {"type": "click", "time": round(rel_time, 3), "x": norm[0], "y": norm[1]}
                )

    def on_press(self, key):
        if self.is_recording and self.enable_zoom_record.get():
            rel_time = time.time() - self.start_time
            with self._events_lock:
                self.events.append({"type": "keypress", "time": round(rel_time, 3)})

    def _update_carnac_hint(self):
        """本机无 Carnac 时在窗口内提示（非弹窗，不遮挡操作）。"""
        if sys.platform != "win32" or not hasattr(self, "carnac_hint_label"):
            return
        if not self.enable_carnac.get():
            self.carnac_hint_label.config(text="")
            return
        if _find_carnac_executable():
            self.carnac_hint_label.config(text="")
            return
        self.carnac_hint_label.config(
            text=(
                "未找到 Carnac.exe，本次录制无屏幕按键叠加。\n"
                "请先安装 Carnac 或设置 CARNAC_EXE；当前发布包不会自动下载未验证的可执行文件。"
            )
        )

    def _terminate_carnac_if_we_started(self):
        """仅结束由本助手启动的 Carnac（录制前已自行打开的实例不杀）。"""
        if sys.platform != "win32":
            return
        with self._carnac_lock:
            proc = self._carnac_process
            if proc is None:
                return
            self._carnac_process = None
        try:
            if proc.poll() is not None:
                return
            pid = proc.pid
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                timeout=20,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception:
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    def _ensure_carnac_for_recording(self):
        if sys.platform != "win32" or not self.enable_carnac.get():
            return
        exe = _find_carnac_executable()
        if not exe:
            return
        if _is_carnac_running_win32():
            return
        try:
            with self._carnac_lock:
                if self._carnac_process is not None and self._carnac_process.poll() is None:
                    return
                self._carnac_process = subprocess.Popen(
                    [exe],
                    cwd=os.path.dirname(exe) or None,
                    close_fds=True,
                )
            time.sleep(0.35)
            with self._carnac_lock:
                p = self._carnac_process
            if p is not None and p.poll() is not None:
                print("Carnac 启动后立即退出，按键叠加可能不可用")
                with self._carnac_lock:
                    self._carnac_process = None
        except Exception as e:
            print(f"Carnac 启动失败: {e}")
            with self._carnac_lock:
                self._carnac_process = None

    def start_countdown(self):
        self.start_btn.config(state=tk.DISABLED)
        if sys.platform == "win32" and self.enable_carnac.get():
            if not _find_carnac_executable():
                self.status_label.config(
                    text="未找到 Carnac，将不显示按键叠加", fg=COLORS["accent_amber"]
                )
                self.root.update_idletasks()
        self._update_carnac_hint()
        for i in range(3, 0, -1):
            self.status_label.config(text=f"即将开始 ({i})…", fg=COLORS["accent_amber"])
            self.root.update()
            time.sleep(1)
        try:
            self.start_actual_recording()
        except Exception as e:
            print(f"开始录制失败: {e}")
            self.status_label.config(text="开始录制失败，请重试", fg=COLORS["accent_red"])
            self.start_btn.config(state=tk.NORMAL)

    def start_actual_recording(self):
        self.generate_filename()
        self.is_recording = True
        self.start_time = time.time()
        self.events = []
        self._ensure_carnac_for_recording()
        # 当前选中的录制区域（用于 FFmpeg 与事件归一化）
        sel = self.capture_display_var.get()
        self._capture_region = None
        for name, rect in self.displays:
            if name == sel and rect is not None:
                self._capture_region = rect
                break

        # 切换到迷你录制圆点：先取消最小尺寸限制，否则窗口无法缩到 56x56
        self.root.minsize(1, 1)
        self.root.overrideredirect(True)
        parts = self.root.geometry().split("+")
        x, y = (parts[1], parts[2]) if len(parts) >= 3 else ("100", "100")
        self.root.geometry(f"56x56+{x}+{y}")
        self.root.update_idletasks()
        self.main_frame.pack_forget()
        self.mini_frame.pack(fill=tk.BOTH, expand=True)

        self.m_listener = mouse.Listener(on_click=self.on_click, on_move=self.on_move)
        self.k_listener = keyboard.Listener(on_press=self.on_press)
        self.m_listener.start()
        self.k_listener.start()

        threading.Thread(target=self.run_ffmpeg, daemon=True).start()

    def on_move(self, x, y):
        # 即使只记录坐标，数据量也可能很大。增加限制:
        # 1. 仅在录制期间
        # 2. 距离上次记录时间 > 0.1s (10FPS采样)
        # 3. 距离上次坐标变化 > 阈值 (例如 5 像素)
        if not self.is_recording or not self.enable_zoom_record.get():
            return

        now = time.time()
        if not hasattr(self, "_last_move_time"):
            self._last_move_time = 0
            self._last_move_pos = (x, y)

        if (now - self._last_move_time) > 0.1:
            last_x, last_y = self._last_move_pos
            if (x - last_x) ** 2 + (y - last_y) ** 2 > 25:
                norm = self._norm_xy(x, y)
                if norm is None:
                    return
                rel_time = now - self.start_time
                with self._events_lock:
                    self.events.append(
                        {"type": "move", "time": round(rel_time, 3), "x": norm[0], "y": norm[1]}
                    )
                self._last_move_time = now
                self._last_move_pos = (x, y)

    def run_ffmpeg(self):
        cmd = ["ffmpeg", "-y", "-f", "gdigrab", "-framerate", "30"]
        if self._capture_region is not None:
            ox, oy, w, h = self._capture_region
            cmd.extend(["-offset_x", str(ox), "-offset_y", str(oy), "-video_size", f"{w}x{h}"])
        # 缓解 gdigrab 缓冲积压导致的卡顿或丢帧
        cmd.extend(["-thread_queue_size", "512", "-i", "desktop"])
        if self.audio_device:
            cmd.extend(["-f", "dshow", "-i", f"audio={self.audio_device}"])
            cmd.extend(["-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-crf", "20"])
        else:
            cmd.extend(["-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20"])
        cmd.append(self.output_path)

        # 必须设置 PYTHONIOENCODING，否则子进程在 Windows Pipe 中打印 Emoji 会报 GBK 编码错误
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"

        self.log_file = os.path.join(self.output_dir, "ffmpeg_log.txt")
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
        with open(self.log_file, "w", encoding="utf-8") as f:
            self.process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=f,
                stderr=subprocess.STDOUT,
                env=env,
                creationflags=creationflags,
            )
            self.process.wait()

    def _kill_ffmpeg_tree(self, pid):
        if sys.platform != "win32" or not pid:
            return
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                timeout=15,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception:
            pass

    def stop_recording(self, show_post_dialog=True):
        if not self.is_recording:
            return
        self.is_recording = False

        # 恢复界面
        self.root.overrideredirect(False)
        self.mini_frame.pack_forget()
        self.main_frame.pack(fill=tk.BOTH, expand=True)
        self.root.minsize(440, 620)
        self.load_config()
        self.status_label.config(text="已保存", fg=COLORS["accent_green"])

        # 重新启用开始按钮
        self.start_btn.config(state=tk.NORMAL)

        if self.m_listener:
            self.m_listener.stop()
        if self.k_listener:
            self.k_listener.stop()

        if self.process:
            proc = self.process
            try:
                if proc.poll() is None:
                    time.sleep(0.3)
                    if proc.stdin is not None:
                        try:
                            proc.stdin.write(b"q")
                            proc.stdin.flush()
                        except (OSError, BrokenPipeError, ValueError):
                            pass
                        try:
                            proc.wait(timeout=8)
                        except subprocess.TimeoutExpired:
                            self._kill_ffmpeg_tree(proc.pid)
                    else:
                        self._kill_ffmpeg_tree(proc.pid)
                else:
                    return_code = proc.poll()
                    print(f"⚠️ FFmpeg stopped early with code {return_code}")
                    if os.path.exists(self.log_file):
                        try:
                            with open(self.log_file, "r", encoding="utf-8", errors="ignore") as f:
                                err = f.read()
                                print(f"[-] FFmpeg Last Logs:\n{err[-500:]}")
                        except Exception:
                            pass
            except Exception as e:
                print(f"⚠️ FFmpeg 停止异常: {e}")
                try:
                    self._kill_ffmpeg_tree(proc.pid)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass

        try:
            with self._events_lock:
                ev = list(self.events)
            with open(self.events_path, "w", encoding="utf-8") as f:
                json.dump(ev, f, indent=4)
        except Exception:
            pass

        self._terminate_carnac_if_we_started()

        if os.path.exists(self.output_path) and os.path.getsize(self.output_path) > 100:
            print(f"✅ 录制成功: {self.output_path}")
            if show_post_dialog:
                self.show_post_action_dialog()
        else:
            if show_post_dialog:
                self._show_error_dialog(
                    "录制失败", "FFmpeg 未能生成有效的视频文件。请检查音频设备设置。"
                )
                self.status_label.config(text="录制失败", fg=COLORS["accent_red"])
            else:
                self.status_label.config(text="已关闭（未生成有效视频）", fg=COLORS["accent_amber"])

    def _ask_project_name(self, parent, default_name):
        """主题一致的「输入项目名称」弹窗"""
        win = tk.Toplevel(parent)
        win.title("创建草稿")
        win.geometry("380x180")
        win.configure(bg=COLORS["bg"])
        win.attributes("-topmost", True)
        win.geometry(_dialog_center_over(parent, 380, 180))
        win.transient(parent)
        result = [None]

        body = tk.Frame(win, bg=COLORS["bg"], padx=24, pady=20)
        body.pack(fill=tk.BOTH, expand=True)
        tk.Label(
            body,
            text="请输入剪映项目名称：",
            fg=COLORS["text"],
            bg=COLORS["bg"],
            font=("Microsoft YaHei", 10),
        ).pack(anchor=tk.W, pady=(0, 8))
        entry = tk.Entry(
            body,
            font=("Microsoft YaHei", 11),
            bg=COLORS["bg_input"],
            fg=COLORS["text"],
            insertbackground=COLORS["text"],
            relief=tk.FLAT,
            bd=0,
        )
        entry.pack(fill=tk.X, ipady=8, ipadx=10, pady=(0, 20))
        entry.insert(0, default_name)
        entry.select_range(0, tk.END)
        entry.focus_set()

        def on_ok():
            result[0] = entry.get().strip()
            win.destroy()

        def on_cancel():
            win.destroy()

        btn_frame = tk.Frame(body, bg=COLORS["bg"])
        btn_frame.pack(fill=tk.X)
        make_flat_button(
            btn_frame,
            "确定",
            on_ok,
            COLORS["accent_green"],
            COLORS["accent_green_hover"],
            width=10,
            pady=6,
            side=tk.RIGHT,
            padx=(8, 0),
        )
        make_flat_button(
            btn_frame,
            "取消",
            on_cancel,
            COLORS["bg_highlight"],
            COLORS["border"],
            fg=COLORS["text"],
            width=10,
            pady=6,
            side=tk.RIGHT,
        )
        win.bind("<Return>", lambda e: on_ok())
        win.bind("<Escape>", lambda e: on_cancel())
        win.grab_set()
        win.wait_window()
        return result[0]

    def show_post_action_dialog(self):
        """显示录制后操作选单，高度保证底部「关闭」不被遮挡"""
        dialog = tk.Toplevel(self.root)
        dialog.title("录制完成")
        dialog.minsize(440, 420)
        dialog.configure(bg=COLORS["bg"])
        dialog.attributes("-topmost", True)
        dialog.geometry(_dialog_center_over(self.root, 440, 420))

        top = tk.Frame(dialog, bg=COLORS["bg"])
        top.pack(fill=tk.X)
        tk.Label(
            top,
            text="录制完成",
            fg=COLORS["text"],
            bg=COLORS["bg"],
            font=("Microsoft YaHei", 14, "bold"),
        ).pack(pady=(24, 6))
        tk.Label(
            top,
            text="视频已保存，下一步做什么？",
            fg=COLORS["text_secondary"],
            bg=COLORS["bg"],
            font=("Microsoft YaHei", 10),
        ).pack(pady=(0, 16))
        tk.Frame(dialog, bg=COLORS["accent_blue"], height=2).pack(fill=tk.X)

        content = tk.Frame(dialog, bg=COLORS["bg"], padx=28, pady=28)
        content.pack(fill=tk.BOTH, expand=True)

        def do_create_draft():
            import datetime

            default_name = f"演示_{datetime.datetime.now().strftime('%H%M%S')}"
            name = self._ask_project_name(dialog, default_name)
            if not name:
                return
            dialog.destroy()
            self.create_smart_draft(name)

        def open_folder():
            os.startfile(self.output_dir)
            dialog.destroy()

        make_flat_button(
            content,
            "✨  自动生成智能草稿",
            do_create_draft,
            COLORS["accent_blue"],
            COLORS["accent_blue_hover"],
            width=24,
            pady=12,
        )
        make_flat_button(
            content,
            "📂  打开文件位置",
            open_folder,
            COLORS["bg_highlight"],
            COLORS["border"],
            fg=COLORS["text"],
            width=24,
            pady=12,
        )
        make_flat_button(
            content,
            "关闭",
            dialog.destroy,
            COLORS["accent_red"],
            COLORS["accent_red_hover"],
            width=24,
            pady=12,
        )
        tk.Frame(content, bg=COLORS["bg"], height=20).pack(fill=tk.X)

    def _show_success_dialog(self, project_name, draft_path):
        """成功提示弹窗（含复制路径），无白边"""
        win = tk.Toplevel(self.root)
        win.title("成功")
        win.minsize(420, 380)
        win.configure(bg=COLORS["bg"], highlightthickness=0, highlightbackground=COLORS["bg"])
        win.attributes("-topmost", True)
        try:
            win.option_add("*HighlightThickness", 0)
        except Exception:
            pass
        win.geometry(_dialog_center_over(self.root, 480, 380))

        content = tk.Frame(win, bg=COLORS["bg"], padx=28, pady=24, highlightthickness=0)
        content.pack(fill=tk.BOTH, expand=True)
        tk.Label(
            content,
            text="草稿已创建",
            fg=COLORS["text"],
            bg=COLORS["bg"],
            font=("Microsoft YaHei", 14, "bold"),
        ).pack(pady=(0, 6))
        tk.Frame(content, bg=COLORS["accent_green"], height=2).pack(fill=tk.X, pady=(0, 12))
        tk.Label(
            content,
            text=f"「{project_name}」已保存到本地，请打开剪映在首页查看。",
            fg=COLORS["text_secondary"],
            bg=COLORS["bg"],
            font=("Microsoft YaHei", 10),
            wraplength=400,
            justify=tk.LEFT,
        ).pack(pady=(0, 14))
        if draft_path:
            path_frame = tk.Frame(
                content, bg=COLORS["bg_highlight"], padx=14, pady=12, highlightthickness=0
            )
            path_frame.pack(fill=tk.X, pady=(0, 20))
            tk.Label(
                path_frame,
                text=draft_path,
                fg=COLORS["text_secondary"],
                bg=COLORS["bg_highlight"],
                font=("Consolas", 9),
                wraplength=400,
                justify=tk.LEFT,
            ).pack(anchor=tk.W)

            def copy_path():
                try:
                    win.clipboard_clear()
                    win.clipboard_append(draft_path)
                except Exception:
                    pass

            btn_row = tk.Frame(content, bg=COLORS["bg"])
            btn_row.pack(fill=tk.X, pady=(0, 16))
            make_flat_button(
                btn_row,
                "复制路径",
                copy_path,
                COLORS["bg_highlight"],
                COLORS["border"],
                fg=COLORS["text"],
                width=12,
                pady=10,
                side=tk.LEFT,
                padx=(0, 10),
            )
            make_flat_button(
                btn_row,
                "确定",
                win.destroy,
                COLORS["accent_green"],
                COLORS["accent_green_hover"],
                width=12,
                pady=10,
                side=tk.LEFT,
            )
        else:
            make_flat_button(
                content,
                "确定",
                win.destroy,
                COLORS["accent_green"],
                COLORS["accent_green_hover"],
                width=12,
                pady=10,
            )

    def _show_error_dialog(self, title, message):
        """主题一致的错误提示弹窗"""
        win = tk.Toplevel(self.root)
        win.title(title)
        win.geometry("420x200")
        win.configure(bg=COLORS["bg"])
        win.attributes("-topmost", True)
        win.geometry(_dialog_center_over(self.root, 420, 200))
        body = tk.Frame(win, bg=COLORS["bg"], padx=24, pady=24)
        body.pack(fill=tk.BOTH, expand=True)
        tk.Frame(body, bg=COLORS["accent_red"], height=2).pack(fill=tk.X, pady=(0, 12))
        tk.Label(
            body,
            text=message,
            fg=COLORS["text_secondary"],
            bg=COLORS["bg"],
            font=("Microsoft YaHei", 10),
            wraplength=360,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(0, 20))
        make_flat_button(
            body,
            "确定",
            win.destroy,
            COLORS["accent_red"],
            COLORS["accent_red_hover"],
            width=12,
            pady=8,
        )

    def create_smart_draft(self, project_name):
        """调用 wrapper 创建草稿"""
        try:
            script_dir = os.path.dirname(os.path.abspath(__file__))
            # 假设结构: tools/recording/xxx.py -> scripts/jy_wrapper.py
            # recording -> tools -> Auto-Cut -> scripts
            wrapper_path = os.path.abspath(
                os.path.join(script_dir, "..", "..", "scripts", "jy_wrapper.py")
            )

            if not os.path.exists(wrapper_path):
                self._show_error_dialog("错误", f"找不到 jy_wrapper.py：\n{wrapper_path}")
                return

            cmd = [
                sys.executable,
                wrapper_path,
                "apply-zoom",
                "--name",
                project_name,
                "--video",
                self.output_path,
                "--json",
                self.events_path,
                "--scale",
                "150",  # 默认缩放
            ]

            # 显示运行中
            self.status_label.config(text="正在生成草稿…", fg=COLORS["accent_blue"])
            self.root.update()

            env = os.environ.copy()
            env["PYTHONIOENCODING"] = "utf-8"
            result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", env=env)

            if result.returncode == 0:
                self.status_label.config(text="草稿创建成功！", fg=COLORS["accent_green"])
                draft_path = ""
                for line in (result.stdout or "").splitlines():
                    if line.strip().startswith("DRAFT_PATH="):
                        draft_path = line.strip().split("DRAFT_PATH=", 1)[-1].strip()
                        break
                self._show_success_dialog(project_name, draft_path)
            else:
                self.status_label.config(text="创建失败", fg=COLORS["accent_red"])
                self._show_error_dialog("创建失败", result.stderr or "未知错误")
        except Exception as e:
            self._show_error_dialog("异常", str(e))

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    # 默认不采集系统声音，避免本机没有“立体声混音”时 FFmpeg 报错。
    # 如需录系统声：用 ffmpeg -list_devices true -f dshow -i dummy 查看音频设备，
    # 将下方改为您的立体声混音设备 ID，例如 audio_device="您的设备名"
    AUDIO_ID = None  # 仅录画面；设为具体设备 ID 可录系统声音

    # 可以接受路径作为保存目录
    out_dir = sys.argv[1] if len(sys.argv) > 1 else None
    recorder = ProGuiRecorder(out_dir, audio_device=AUDIO_ID)
    recorder.run()
