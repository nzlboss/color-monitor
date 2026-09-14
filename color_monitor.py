# -*- coding: utf-8 -*-
"""
ColorMonitor V5 - OCR 文字识别版 (Windows)
------------------------------------------------------------------
识别逻辑（极简，只需框一次按钮）：
  程序启动后先识别一次按钮当前文字，作为"初始状态"基准。
  之后每 CHECK_INTERVAL 秒（默认 10）识别一次按钮文字：
    - 文字发生变化（如 "等待掉落中" -> "XX:XX后领取"） -> 记录为【即将开始】
    - 文字稳定为触发关键词（默认 "限时领取"）          -> 鼠标左键单击一下
    - 点击后立刻进入下一轮循环（重新学习新基准，等下一次"限时领取"）
  全程循环运行，永不停止。

设计要点：
  * 只用 OCR，不看颜色 —— 光线/渐变/反光不影响
  * 倒计时（XX:XX / XXs / 数字）一律视为"即将开始"，不点击
  * 每次点击打印精确时间戳，方便核对"几点几分点的"

交互（解决模拟器置顶，全程在截图窗口上鼠标操作）：
  启动 -> 自动截全屏 -> 在截图窗口里【框选一次按钮区域】(拖框/单击均可)
       -> 向导确认：关键词/检测间隔/点击模式
       -> 开始循环监控

热键：Ctrl+Alt+P 暂停/恢复 | Ctrl+Alt+Q 退出
打包： pyinstaller --onefile --uac-admin color_monitor.py
依赖： pip install pillow pyautogui pytesseract keyboard pyinstaller
注：   运行电脑需安装 Tesseract OCR + 中文语言包 chi_sim
"""

import sys
import os
import re
import time
import json
import random
import threading
import tkinter as tk
from tkinter import messagebox

try:
    from PIL import Image, ImageGrab, ImageTk
except ImportError:
    print("[错误] 缺少依赖 pillow，请先运行： pip install pillow")
    sys.exit(1)

try:
    import pyautogui
    pyautogui.FAILSAFE = False
except ImportError:
    print("[错误] 缺少依赖 pyautogui，请先运行： pip install pyautogui")
    sys.exit(1)

try:
    import pytesseract
except ImportError:
    print("[错误] 缺少依赖 pytesseract，请先运行： pip install pytesseract")
    sys.exit(1)

# ---------------- 全局状态 ----------------
CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), "monitor_config.json")
STATE = {
    "button_box": None,        # (x1, y1, x2, y2) 按钮区域屏幕坐标
    "trigger_keyword": "限时领取",  # 出现这个文字 = 可点击
    "check_interval": 10.0,    # 识别间隔(秒)
    "tesseract_cmd": r"C:\Program Files\Tesseract-OCR\tesseract.exe",
}
RUNNING = True
PAUSED = False
# 运行时记忆：上一次识别到的"稳定文字"
last_stable_text = ""


# ---------------- 工具 ----------------
def log(msg):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}")


def normalize(text):
    """归一化：去空格/换行/常见标点，方便对比"""
    text = text or ""
    text = re.sub(r"\s+", "", text)
    for ch in "「」【】()（）[]<>：:·•":
        text = text.replace(ch, "")
    return text


def strip_digits(text):
    """判定前剥离所有数字(0-9)与冒号分隔符，只保留文字。
    这是 V5 的核心简化：倒计时数字全部忽略，只看文字。
        '05:23后领取' -> '后领取'
        '限时领取12s'  -> '限时领取s'  (s/秒等字母仍会被归一化清理)
        '限时领取'     -> '限时领取'
    注意：先剥数字再归一化，调用方应对结果再做 normalize()。"""
    text = text or ""
    # 去掉阿拉伯数字 0-9 和冒号（XX:XX 的分隔符）
    text = re.sub(r"[0-9:]+", "", text)
    # 清理残留的 s/秒 等时间单位字母（避免 "限时领取s" 影响）
    for ch in "sS秒":
        text = text.replace(ch, "")
    return text


def text_key(text):
    """用于判定的/打印的最终文字：剥数字 + 归一化，只留纯文字"""
    return normalize(strip_digits(text))


# ---------------- OCR ----------------
def ocr_text(bbox):
    """截取 bbox 区域，返回 OCR 原始字符串"""
    try:
        img = ImageGrab.grab(bbox=bbox, all_screens=True).convert("RGB")
    except Exception as e:
        log(f"截图失败：{e}")
        return ""
    try:
        text = pytesseract.image_to_string(img, lang="chi_sim+eng")
    except Exception as e:
        log(f"OCR 失败（Tesseract 未安装/未配中文包？）：{e}")
        return ""
    return text


def is_trigger(text, keyword):
    """归一化后包含触发关键词 -> 可点击"""
    return bool(keyword) and (normalize(keyword) in normalize(text))


# 倒计时阶段的文字特征（剥数字+归一化后若含这些，视为"即将开始"）
_COUNTDOWN_TEXT_MARKERS = ("后领取", "等待", "掉落", "倒计时", "倒计时中")


def is_countdown(text):
    """判断是否为倒计时阶段（只看文字，忽略数字）。
    剥掉所有数字后，若文字含倒计时特征词 -> 【即将开始】。
    阶段1 '等待掉落中' -> 含'等待/掉落' -> 倒计时阶段
    阶段2 'XX:XX后领取' -> 剥数字 -> '后领取' -> 倒计时阶段
    阶段3 '限时领取'    -> 剥数字 -> '限时领取' -> 不含特征词 -> 不是倒计时"""
    key = text_key(text)
    if not key:
        return False
    return any(marker in key for marker in _COUNTDOWN_TEXT_MARKERS)


def should_click(text, keyword):
    """判定优先级（V5 最终）：
      1) 文字剥数字后含倒计时特征 -> 绝不点击（即将开始）
      2) 否则，含触发关键词（如'限时领取'）-> 单击一下
    数字已全部忽略，只看文字。"""
    if is_countdown(text):
        return False
    return is_trigger(text, keyword)


# ---------------- 截图选择器（置顶，解决模拟器问题） ----------------
class ScreenshotSelector:
    """显示全屏截图，在其上拖框(区域)或单击(点)。
    按下到松开 < 0.35s -> 单击；否则 -> 拖框。"""

    def __init__(self, title, prompt, screenshot=None):
        self.title = title
        self.prompt = prompt
        self.result = None
        self.confirmed = False

        self.root = tk.Tk()
        self.root.title(title)
        self.root.attributes("-topmost", True)

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
        self.canvas.create_image(0, 0, anchor="nw", image=self.tk_img)

        self.sx = self.sy = None
        self.rect_id = None
        self.press_time = None

        self.canvas.bind("<ButtonPress-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)

        self._build_buttons()
        self.root.protocol("WM_DELETE_WINDOW", self.on_cancel)
        self.root.mainloop()

    def _build_buttons(self):
        f = tk.Frame(self.root)
        f.pack(pady=6)
        tk.Label(f, text=self.prompt, fg="blue", wraplength=600, justify="left").pack(side="left", padx=10)
        tk.Button(f, text="确定", width=8, command=self.on_confirm).pack(side="right", padx=5)
        tk.Button(f, text="取消", width=8, command=self.on_cancel).pack(side="right", padx=5)

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
        self.rect_id = self.canvas.create_rectangle(self.sx, self.sy, e.x, e.y, outline="red", width=2)

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


# ---------------- 配置保存/加载 ----------------
def save_config():
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(STATE, f, ensure_ascii=False, indent=2)
        log(f"配置已保存到 {CONFIG_FILE}")
    except Exception as e:
        log(f"保存失败：{e}")


def load_config():
    if not os.path.exists(CONFIG_FILE):
        return False
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            saved = json.load(f)
        if "button_box" in saved and isinstance(saved["button_box"], list):
            saved["button_box"] = tuple(saved["button_box"])
        STATE.update(saved)
        log(f"已加载配置：{CONFIG_FILE}")
        return True
    except Exception as e:
        log(f"加载失败：{e}")
        return False


# ---------------- 点击 ----------------
def do_click(box):
    """在按钮区域内单击一次（左键）"""
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
    log(f"已单击 -> ({cx}, {cy})")


# ---------------- 监控循环 ----------------
def monitor_loop():
    global RUNNING, PAUSED, last_stable_text
    box = STATE["button_box"]
    keyword = STATE["trigger_keyword"]
    interval = STATE["check_interval"]

    log("=" * 60)
    log("开始【持续循环】OCR 监控。规则：")
    log(f"  每 {interval:.0f} 秒识别一次按钮文字")
    log(f"  触发关键词 = {keyword!r}（出现即单击一下）")
    log(f"  倒计时(XX:XX / XXs / 数字) -> 记录为【即将开始】")
    log("  点击后自动进入下一轮循环")
    log("热键：Ctrl+Alt+P 暂停/恢复 | Ctrl+Alt+Q 退出")
    log("=" * 60)

    # 启动时先学习一次"当前文字"作为基准（只认文字，忽略数字）
    first = ocr_text(box)
    last_stable_text = text_key(first)            # 关键：用 text_key 去数字
    log(f"初始状态文字：{last_stable_text!r}")

    while RUNNING:
        if PAUSED:
            time.sleep(0.3)
            continue

        text = ocr_text(box)
        key = text_key(text)                      # 关键：判定全程用 text_key

        # 1) 触发关键词 -> 单击一下（should_click 内部已排除倒计时）
        if should_click(text, keyword):
            click_ts = time.strftime("%Y-%m-%d %H:%M:%S")
            log(f"*** 识别到【{keyword}】！准备单击... ***")
            try:
                do_click(box)
                log(f"✅ 点击成功！时间：{click_ts}")
            except Exception as e:
                log(f"点击失败：{e}")
            last_stable_text = key
            # 点击后等待一个间隔再进入下一轮（按钮状态会刷新）
            _sleep(interval)
            continue

        # 2) 倒计时阶段 -> 一律【即将开始】
        if key and is_countdown(text):
            if key != last_stable_text:
                log(f"阶段变化：{last_stable_text!r} -> {key!r} 【即将开始】")
                last_stable_text = key
        # 3) 文字发生变化（非倒计时）-> 记录阶段
        elif key and key != last_stable_text:
            log(f"阶段变化：{last_stable_text!r} -> {key!r}")
            last_stable_text = key
        # 文字没变（或识别为空）-> 静默等待

        _sleep(interval)

    log("监控循环已退出")


def _sleep(seconds):
    """可中断的睡眠（响应暂停/退出）"""
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
    except Exception as e:
        log(f"热键监听不可用（可忽略）：{e}")


def toggle_pause():
    global PAUSED
    PAUSED = not PAUSED
    log(">>> " + ("已暂停" if PAUSED else "已恢复") + " <<<")


def quit_prog():
    global RUNNING
    RUNNING = False
    log(">>> 收到退出信号 <<<")


# ---------------- 向导 ----------------
def guided_setup():
    global STATE, last_stable_text
    print("\n" + "=" * 56)
    print("  引导配置向导（全程鼠标，只需框选一次按钮）")
    print("=" * 56)

    # 准备：Tesseract 路径
    custom = input(f"\n指定 tesseract.exe 路径？留空用默认\n  [{STATE['tesseract_cmd']}]\n  > ").strip()
    if custom:
        STATE["tesseract_cmd"] = custom
    try:
        pytesseract.pytesseract.tesseract_cmd = STATE["tesseract_cmd"]
        pytesseract.get_tesseract_version()
        log("Tesseract 检测 OK")
    except Exception as e:
        print(f"  [警告] Tesseract 未就绪：{e}")
        print("  请先安装：https://github.com/tesseract-ocr/tesseract （含 chi_sim 中文包）")

    # 截图
    log("3 秒后截取全屏，请把模拟器调到目标界面（按钮可见）...")
    time.sleep(3)
    full = ImageGrab.grab()
    log("已截图。请在弹出的置顶窗口上框选【按钮区域】。")

    # 只需框选一次（既是监控区也是点击区）
    print("\n【步骤 1/1】框选【按钮区域】（拖框框住整个按钮；单击=按钮中心）")
    print("  说明：框一次即可，程序会在此区域识别文字并点击。")
    sel = ScreenshotSelector("框选按钮区域", "拖框框住按钮（限时领取/倒计时文字区）；单击=按钮中心", full)
    if not sel.confirmed or sel.result is None:
        print("已取消。")
        return False
    STATE["button_box"] = sel.result
    log(f"按钮区域 = {sel.result}")

    # 关键词
    cur = STATE["trigger_keyword"] or "限时领取"
    kw = input(f"\n出现哪个文字就点击？(留空默认 '{cur}')\n  > ").strip()
    if kw:
        STATE["trigger_keyword"] = kw
    log(f"触发关键词 = {STATE['trigger_keyword']!r}")

    # 识别间隔
    ci = input(f"\n每隔多少秒识别一次？(留空默认 {int(STATE['check_interval'])})\n  > ").strip()
    try:
        STATE["check_interval"] = float(ci) if ci else STATE["check_interval"]
    except ValueError:
        pass
    if STATE["check_interval"] < 1:
        STATE["check_interval"] = 10.0
    log(f"识别间隔 = {STATE['check_interval']:.0f} 秒")

    # 测试识别当前按钮
    print("\n正在用当前按钮区域测试 OCR（数字将被自动忽略，只看文字）...")
    test = ocr_text(STATE["button_box"])
    log(f"  当前按钮 OCR 结果（去数字后）：{text_key(test)!r}")
    if should_click(test, STATE["trigger_keyword"]):
        log("  ✅ 已识别到触发关键词，将点击！")
    elif is_countdown(test):
        log("  ✅ 识别为倒计时阶段（即将开始），等变成可点击文字即触发")
    else:
        log("  ⚠️ 暂未识别到明确阶段——请确认框选覆盖了文字，可重新配置。向导仍可继续。")

    save_config()
    return True


def main():
    global RUNNING
    print("\n" + "*" * 56)
    print("   ColorMonitor V5 - OCR 文字识别版")
    print("   规则：每10秒识别按钮 -> 限时领取 -> 单击一下 -> 循环")
    print("*" * 56)

    if STATE.get("tesseract_cmd"):
        try:
            pytesseract.pytesseract.tesseract_cmd = STATE["tesseract_cmd"]
        except Exception:
            pass

    load_config()

    while RUNNING:
        print("\n请选择：")
        print("  [1] 引导配置（首次使用 / 重新框选按钮）")
        print("  [2] 使用当前配置直接开始循环监控")
        print("  [3] 查看当前配置")
        print("  [4] 退出")
        choice = input("输入选项 [1]: ").strip() or "1"

        if choice == "1":
            if guided_setup():
                break
        elif choice == "2":
            if not STATE["button_box"]:
                print("尚未配置按钮区域，请先执行 [1]。")
                continue
            break
        elif choice == "3":
            print(json.dumps(STATE, ensure_ascii=False, indent=2))
        elif choice == "4":
            return
        else:
            print("无效选项。")

    threading.Thread(target=hotkey_thread, daemon=True).start()
    try:
        monitor_loop()
    except KeyboardInterrupt:
        log("收到 Ctrl+C，退出。")
    finally:
        RUNNING = False
        save_config()
        log("程序结束。")


if __name__ == "__main__":
    main()

