# -*- coding: utf-8 -*-
"""
嘉年华箱子助手 (BoxBot) - V6
==================================================================
核心规则（只需框选一次按钮，其余全自动）：
  · 每 10 秒识别一次按钮文字，忽略所有数字，只认文字
  · 「等待掉落中」         -> 初始化完成（等待）
  · 「XX:XX后领取」倒计时  -> 即将开始（不点击）
  · 「限时领取」(可带数字) -> 准备点击 -> 鼠标左键单击一下
  · 点击后循环运行，永不停止

操作：
  1) 启动后指定 tesseract.exe 路径
  2) 5 秒后弹出截图，框选按钮区域（一次即可，既是监控区也是点击区）
  3) 自动识别并进入循环监控

热键：Ctrl+Alt+P 暂停/恢复 | Ctrl+Alt+Q 退出
依赖：pip install pillow pyautogui pytesseract keyboard pyinstaller
注：运行电脑需安装 Tesseract OCR + 中文语言包 chi_sim
公告：可在同目录 notice.txt 或配置 notice_url 远程加载公告文本
"""
import sys
import os
import re
import io
import time
import json
import random
import threading
import tkinter as tk
from tkinter import messagebox

try:
    from PIL import Image, ImageDraw, ImageGrab, ImageTk
except ImportError:
    print("[错误] 缺少依赖 pillow，请先运行：pip install pillow")
    sys.exit(1)
try:
    import pyautogui
    pyautogui.FAILSAFE = False
except ImportError:
    print("[错误] 缺少依赖 pyautogui，请先运行：pip install pyautogui")
    sys.exit(1)
try:
    import pytesseract
except ImportError:
    print("[错误] 缺少依赖 pytesseract，请先运行：pip install pytesseract")
    sys.exit(1)

# ---------------- 基本信息 ----------------
APP_NAME = "嘉年华箱子助手"
APP_ENGLISH = "BoxBot"
APP_VERSION = "6.0"
APP_TITLE = f"{APP_NAME} ({APP_ENGLISH}) v{APP_VERSION}"
CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), "boxbot_config.json")
NOTICE_FILE = os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), "notice.txt")
DEFAULT_TESSERACT = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

# ANSI 颜色（控制台彩色输出）
class C:
    RESET = "\033[0m"
    GRAY = "\033[90m"
    YELLOW = "\033[33m"
    GREEN = "\033[32m"
    CYAN = "\033[36m"
    RED = "\033[31m"
    BOLD = "\033[1m"
    BLUE = "\033[34m"

# 状态 -> (显示名, 颜色)
STATUS_STYLE = {
    "init":      ("初始化完成", C.GRAY),
    "countdown": ("即将开始",   C.YELLOW),
    "ready":     ("准备点击",   C.CYAN),
    "success":   ("领取成功",   C.GREEN),
    "warn":      ("注意",       C.RED),
}

# ---------------- 全局状态 ----------------
STATE = {
    "button_box": None,
    "trigger_keyword": "限时领取",
    "check_interval": 10.0,
    "tesseract_cmd": DEFAULT_TESSERACT,
    "notice_url": "http://res.7ml.cn/ad/ad-top.txt",       # 远程公告地址（留空则不远程加载）
}
RUNNING = True
PAUSED = False
last_status = None         # 上一次状态名，用于去重打印


# ---------------- 工具 ----------------
def now_ts():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def color_print(msg, color=C.RESET, bold=False):
    """带颜色的精简打印"""
    prefix = C.BOLD if bold else ""
    print(f"{prefix}{color}{msg}{C.RESET}")


def log_status(status, extra=""):
    """状态式日志：同名状态不重复打印，只在变化时输出一行彩色文字"""
    global last_status
    name, color = STATUS_STYLE.get(status, ("", C.RESET))
    if status == last_status and status not in ("success", "ready"):
        return  # 静默去重（领取成功/准备点击仍每次显示）
    last_status = status
    text = f"  [{name}]"
    if extra:
        text += f"  {extra}"
    color_print(text, color, bold=(status == "success"))


def normalize(text):
    text = text or ""
    text = re.sub(r"\s+", "", text)
    for ch in "「」【】()（）[]<>：:·•":
        text = text.replace(ch, "")
    return text


def strip_digits(text):
    """判定前剥离所有数字(0-9)与冒号，只留文字 —— 核心简化"""
    text = text or ""
    text = re.sub(r"[0-9:]+", "", text)
    for ch in "sS秒":
        text = text.replace(ch, "")
    return text


def text_key(text):
    """剥数字 + 归一化，只留纯文字"""
    return normalize(strip_digits(text))


# ---------------- OCR ----------------
def ocr_text(bbox):
    try:
        img = ImageGrab.grab(bbox=bbox, all_screens=True).convert("RGB")
    except Exception:
        return ""
    try:
        return pytesseract.image_to_string(img, lang="chi_sim+eng")
    except Exception:
        return ""


def is_trigger(text, keyword):
    return bool(keyword) and (normalize(keyword) in normalize(text))


# 倒计时阶段的文字特征（剥数字后含这些 -> 即将开始）
_COUNTDOWN_MARKERS = ("后领取", "等待", "掉落", "倒计时")


def is_countdown(text):
    key = text_key(text)
    if not key:
        return False
    return any(m in key for m in _COUNTDOWN_MARKERS)


def should_click(text, keyword):
    if is_countdown(text):
        return False
    return is_trigger(text, keyword)


# ---------------- 图标（运行时生成，无需外部文件） ----------------
def build_icon_bytes():
    """用 PIL 生成一个简易彩色图标，返回 .ico 格式的 bytes"""
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # 圆角背景
    d.rounded_rectangle([2, 2, size - 3, size - 3], radius=14, fill=(30, 120, 80))
    # 箱子主体
    d.rectangle([16, 22, 48, 46], fill=(220, 170, 60))
    d.rectangle([16, 22, 48, 30], fill=(245, 200, 90))   # 箱盖
    d.line([32, 22, 32, 46], fill=(150, 100, 30), width=2)  # 锁扣
    # 对勾
    d.line([24, 34, 30, 40], fill=(255, 255, 255), width=3)
    d.line([30, 40, 42, 26], fill=(255, 255, 255), width=3)
    buf = io.BytesIO()
    img.save(buf, format="ICO", sizes=[(64, 64)])
    return buf.getvalue()


def apply_window_icon(root):
    """给 Tk 窗口设置图标（优先同目录 boxbot.ico，否则运行时生成）"""
    ico_path = os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), "boxbot.ico")
    try:
        if os.path.exists(ico_path):
            root.iconbitmap(ico_path)
            return
    except Exception:
        pass
    try:
        import base64, tempfile
        fd, tmp = tempfile.mkstemp(suffix=".ico")
        with os.fdopen(fd, "wb") as f:
            f.write(build_icon_bytes())
        root.iconbitmap(tmp)
        # 保留引用防止被清理
        root._icon_tmp = tmp
    except Exception:
        pass


# ---------------- 公告（本地 txt / 远程 url） ----------------
def load_notice():
    """返回公告文本。优先 notice_url 远程，其次本地 notice.txt，都没有返回空"""
    url = (STATE.get("notice_url") or "").strip()
    # 远程
    if url.startswith("http"):
        try:
            if sys.version_info >= (3, 9):
                from urllib.request import Request, urlopen
            else:
                from urllib2 import Request, urlopen
            req = Request(url, headers={"User-Agent": APP_ENGLISH})
            with urlopen(req, timeout=5) as r:
                return r.read().decode("utf-8", "ignore").strip()
        except Exception:
            pass
    # 本地
    if os.path.exists(NOTICE_FILE):
        try:
            with open(NOTICE_FILE, "r", encoding="utf-8") as f:
                return f.read().strip()
        except Exception:
            pass
    return ""


# ---------------- 截图选择器 ----------------
class ScreenshotSelector:
    """显示全屏截图，在其上拖框(区域)或单击(点)。<0.35s=单击，否则=拖框。"""

    def __init__(self, title, prompt, screenshot=None, countdown=5):
        self.prompt = prompt
        self.result = None
        self.confirmed = False

        self.root = tk.Tk()
        self.root.title(title)
        self.root.attributes("-topmost", True)
        apply_window_icon(self.root)

        self.full_img = screenshot if screenshot else ImageGrab.grab()
        sw, sh = self.full_img.size
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        scale = min(screen_w * 0.95 / sw, screen_h * 0.82 / sh, 1.0)
        self.scale = scale
        dw, dh = int(sw * scale), int(sh * scale)
        self.tk_img = ImageTk.PhotoImage(self.full_img.resize((dw, dh), Image.LANCZOS))

        self.canvas = tk.Canvas(self.root, width=dw, height=dh, cursor="cross")
        self.canvas.pack()
        self.bg_img_id = self.canvas.create_image(0, 0, anchor="nw", image=self.tk_img)

        self.sx = self.sy = None
        self.rect_id = None
        self.press_time = None

        self.canvas.bind("<ButtonPress-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)

        self._build_bottom()
        self.root.after(100, self._tick, countdown)
        self.root.protocol("WM_DELETE_WINDOW", self.on_cancel)
        self.root.mainloop()

    def _build_bottom(self):
        f = tk.Frame(self.root, bg="#1e3c4e")
        f.pack(side="bottom", fill="x")
        self.tip_var = tk.StringVar(value=self.prompt)
        tk.Label(f, textvariable=self.tip_var, fg="white", bg="#1e3c4e",
                 font=("Microsoft YaHei", 11), anchor="w", padx=12, pady=8).pack(side="left", fill="x", expand=True)
        tk.Button(f, text="确认", width=8, command=self.on_confirm,
                  bg="#2e8b57", fg="white", font=("Microsoft YaHei", 10)).pack(side="right", padx=8, pady=6)
        tk.Button(f, text="取消", width=8, command=self.on_cancel,
                  bg="#888", fg="white", font=("Microsoft YaHei", 10)).pack(side="right", padx=8, pady=6)

    def _tick(self, left):
        if left <= 0:
            self.tip_var.set("请在截图上框选【按钮区域】→ 点击「确认」")
            # 让截图窗口也置顶闪烁提示
            try:
                self.root.lift()
            except Exception:
                pass
            return
        self.tip_var.set(f"{self.prompt}  （{left} 秒后开始框选…）")
        self.root.after(1000, self._tick, left - 1)

    def _to_screen(self, ex, ey):
        return int(ex / self.scale), int(ey / self.scale)

    def on_press(self, e):
        self.sx, self.sy = e.x, e.y
        self.press_time = time.time()
        if self.rect_id:
            self.canvas.delete(self.rect_id)
            self.rect_id = None

    def on_drag(self, e):
        if self.sx is None:
            return
        if self.rect_id:
            self.canvas.delete(self.rect_id)
        self.rect_id = self.canvas.create_rectangle(self.sx, self.sy, e.x, e.y, outline="#00ff7f", width=3)

    def on_release(self, e):
        if self.sx is None:
            return
        dt = (time.time() - self.press_time) if self.press_time else 99
        x1, y1 = self._to_screen(self.sx, self.sy)
        x2, y2 = self._to_screen(e.x, e.y)
        if dt < 0.35:
            self.result = (x1, y1)
        else:
            x1, x2 = sorted((x1, x2)); y1, y2 = sorted((y1, y2))
            if abs(x2 - x1) < 5 or abs(y2 - y1) < 5:
                self.result = (x1, y1)
            else:
                self.result = (x1, y1, x2, y2)

    def on_confirm(self):
        if self.result is None:
            messagebox.showwarning("提示", "请先在截图上拖框或单击按钮")
            return
        self.confirmed = True
        self.root.destroy()

    def on_cancel(self):
        self.result = None
        self.confirmed = False
        self.root.destroy()


# ---------------- 配置 ----------------
def save_config():
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(STATE, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def load_config():
    global RUNNING
    if not os.path.exists(CONFIG_FILE):
        return False
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            saved = json.load(f)
        if "button_box" in saved and isinstance(saved["button_box"], list):
            saved["button_box"] = tuple(saved["button_box"])
        STATE.update(saved)
        return True
    except Exception:
        return False


# ---------------- 点击 ----------------
def do_click(box):
    if isinstance(box, tuple) and len(box) == 4:
        x1, y1, x2, y2 = box
        x1, x2 = sorted((x1, x2)); y1, y2 = sorted((y1, y2))
        cx = (x1 + x2) // 2 + random.randint(-4, 4)
        cy = (y1 + y2) // 2 + random.randint(-4, 4)
    else:
        cx, cy = box
        cx += random.randint(-4, 4)
        cy += random.randint(-4, 4)
    pyautogui.moveTo(cx, cy, duration=random.uniform(0.02, 0.08))
    pyautogui.click(cx, cy, button="left")
    return cx, cy


# ---------------- 监控循环 ----------------
def classify(text, keyword):
    """返回 (status, 是否点击)"""
    if should_click(text, keyword):
        return "ready", True
    if is_countdown(text):
        return "countdown", False
    return None, False


def monitor_loop():
    global RUNNING, PAUSED, last_status
    box = STATE["button_box"]
    keyword = STATE["trigger_keyword"]
    interval = STATE["check_interval"]

    last_status = None
    # 启动时识别一次，判定初始状态
    first = ocr_text(box)
    st, _ = classify(first, keyword)
    if st == "countdown":
        log_status("countdown")   # 即将开始
    else:
        log_status("init")        # 初始化完成

    while RUNNING:
        if PAUSED:
            time.sleep(0.3)
            continue

        text = ocr_text(box)
        key = text_key(text)

        if should_click(text, keyword):
            log_status("ready")
            try:
                cx, cy = do_click(box)
            except Exception as e:
                log_status("warn", str(e))
                _sleep(interval)
                continue
            log_status("success", now_ts())
            _sleep(interval)
            continue

        if key and is_countdown(text):
            log_status("countdown")
        elif key:
            # 非倒计时、非触发的其他文字 -> 视为初始化/等待
            log_status("init")

        _sleep(interval)

    color_print("  [已停止]", C.GRAY)


def _sleep(seconds):
    global RUNNING, PAUSED
    slept = 0.0
    step = 0.1
    while slept < seconds and RUNNING:
        if PAUSED:
            time.sleep(step)
            continue
        time.sleep(step)
        slept += step


# ---------------- 热键 ----------------
def hotkey_thread():
    try:
        import keyboard
        keyboard.add_hotkey("ctrl+alt+p", toggle_pause)
        keyboard.add_hotkey("ctrl+alt+q", quit_prog)
        keyboard.wait()
    except Exception:
        pass


def toggle_pause():
    global PAUSED
    PAUSED = not PAUSED
    color_print(f"  [{'已暂停' if PAUSED else '已恢复'}]", C.YELLOW)


def quit_prog():
    global RUNNING
    RUNNING = False


# ---------------- 向导（精简：只问路径 + 5秒后框选） ----------------
def guided_setup():
    global STATE
    color_print(f"\n  {APP_TITLE}", C.BOLD, bold=True)

    # 1) Tesseract 路径
    custom = input(f"  tesseract.exe 路径（留空用默认）\n    [{STATE['tesseract_cmd']}]\n  > ").strip()
    if custom:
        STATE["tesseract_cmd"] = custom
    try:
        pytesseract.pytesseract.tesseract_cmd = STATE["tesseract_cmd"]
        pytesseract.get_tesseract_version()
        color_print("  ✓ Tesseract 就绪", C.GREEN)
    except Exception as e:
        color_print(f"  ! Tesseract 未就绪：{e}", C.RED)
        color_print("    请先安装：https://github.com/tesseract-ocr/tesseract （含 chi_sim）", C.YELLOW)

    # 2) 公告地址（可选）
    url = input(f"  远程公告地址（留空跳过）\n    > ").strip()
    if url:
        STATE["notice_url"] = url

    # 3) 提示后延迟截图
    color_print("\n  「确认」后 5 秒弹出截图，请把模拟器切到目标界面（按钮可见）", C.CYAN)
    input("  准备好后按回车开始…")
    for i in range(5, 0, -1):
        color_print(f"  {i}…", C.YELLOW, bold=True)
        time.sleep(1)
    full = ImageGrab.grab()
    color_print("  正在弹出截图，请框选按钮区域…", C.CYAN)

    sel = ScreenshotSelector(APP_TITLE, "稍后请框选按钮区域，并点击「确认」", full, countdown=5)
    if not sel.confirmed or sel.result is None:
        color_print("  已取消。", C.GRAY)
        return False
    STATE["button_box"] = sel.result

    # 4) 测试 OCR（静默，只简短提示）
    test = ocr_text(STATE["button_box"])
    key = text_key(test)
    color_print(f"  识别文字（去数字）：「{key}」", C.CYAN)
    if should_click(test, STATE["trigger_keyword"]):
        color_print("  ✓ 当前已是「限时领取」，进入后将立即点击", C.GREEN)
    else:
        color_print("  ✓ 已记录，循环中将自动检测", C.GREEN)

    save_config()
    return True


def show_notice():
    notice = load_notice()
    if not notice:
        return
    color_print("—" * 40, C.GRAY)
    color_print("  公告", C.BOLD, bold=True)
    for line in notice.splitlines():
        color_print(f"    {line}", C.BLUE)
    color_print("—" * 40, C.GRAY)


def main():
    global RUNNING
    color_print("=" * 46, C.GRAY)
    color_print(f"  {APP_NAME}  {APP_ENGLISH}  v{APP_VERSION}", C.BOLD, bold=True)
    color_print("  框选一次按钮 · 自动识别限时领取 · 循环点击", C.GRAY)
    color_print("=" * 46, C.GRAY)

    if STATE.get("tesseract_cmd"):
        try:
            pytesseract.pytesseract.tesseract_cmd = STATE["tesseract_cmd"]
        except Exception:
            pass

    load_config()
    show_notice()

    # 若未配置按钮区域，强制走向导
    if not STATE.get("button_box"):
        choice = "1"
    else:
        print("\n  [1] 重新框选按钮   [2] 直接开始   [3] 退出")
        choice = input("  选择 [2]: ").strip() or "2"

    if choice == "1":
        if not guided_setup():
            color_print("  向导未完成，退出。", C.RED)
            return
    elif choice == "3":
        return
    elif choice != "2":
        color_print("  无效选项，直接开始。", C.YELLOW)

    if not STATE.get("button_box"):
        color_print("  未配置按钮区域，请重新运行并选择 [1]。", C.RED)
        return

    color_print("\n  开始监控（Ctrl+Alt+P 暂停 | Ctrl+Alt+Q 退出）", C.GREEN, bold=True)
    threading.Thread(target=hotkey_thread, daemon=True).start()
    try:
        monitor_loop()
    except KeyboardInterrupt:
        pass
    finally:
        RUNNING = False
        save_config()
        color_print("  程序结束。", C.GRAY)


if __name__ == "__main__":
    main()
