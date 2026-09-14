import sys
import os
import json
import time
import random
import re
import urllib.request
import threading
from datetime import datetime
from io import BytesIO

import tkinter as tk
from tkinter import ttk, messagebox, simpledialog
from PIL import Image, ImageGrab, ImageTk, ImageDraw
import pyautogui

# ============================================================
# 全局配置
# ============================================================
CONFIG_FILE = "boxbot_config.json"
NOTICE_URL = "http://res.7ml.cn/ad/ad-top.txt"  # ← 改成你的链接
APP_NAME = "嘉年华箱子助手"
APP_VERSION = "v6.0"
BOT_NAME = "BoxBot"

# 全局状态
running = False
paused = False
exit_flag = False

# ============================================================
# 辅助函数
# ============================================================

def strip_digits(text):
    """去掉所有数字和冒号"""
    return re.sub(r'[\d:]', '', text).strip()

def clean_ocr_text(text):
    """清理 OCR 结果：去空格换行符号"""
    text = text.replace(' ', '').replace('\n', '').replace('\r', '').strip()
    text = re.sub(r'[^\u4e00-\u9fff\w]', '', text)
    return text

def get_timestamp():
    """获取当前时间字符串"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def get_notice():
    """获取远程公告，失败返回默认"""
    try:
        req = urllib.request.Request(NOTICE_URL, headers={'User-Agent': 'Mozilla/5.0'})
        response = urllib.request.urlopen(req, timeout=5)
        content = response.read().decode('utf-8').strip()
        if content:
            return content
    except Exception:
        pass
    return "框选一次按钮 · 自动识别限时领取 · 循环点击"

def generate_icon():
    """生成一个简单的宝箱图标（256x256）"""
    img = Image.new('RGBA', (256, 256), (0, 255, 120, 180))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([20, 80, 236, 220], radius=30, fill=(139, 69, 19, 240))
    draw.rectangle([80, 130, 110, 170], fill=(218, 165, 32, 230))
    draw.rectangle([146, 90, 210, 150], fill=(34, 177, 76, 235))
    draw.polygon([(206, 86), (214, 74), (222, 82)], fill=(238, 154, 36, 245))
    return img

# ============================================================
# 截图选择器
# ============================================================

class ScreenshotSelector:
    def __init__(self, title="框选按钮区域"):
        self.root = tk.Tk()
        self.root.title(title)
        self.root.attributes("-topmost", True)
        
        icon_img = generate_icon()
        self.icon_photo = ImageTk.PhotoImage(icon_img.resize((32, 32), Image.LANCZOS))
        self.root.iconphoto(False, self.icon_photo)
        
        self.result = None
        self.selection_type = None
        
        self.full_screenshot = ImageGrab.grab()
        self.scale_factor = 1.0
        
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        img_w, img_h = self.full_screenshot.size
        
        scale = min(screen_w * 0.93 / img_w, screen_h * 0.88 / img_h, 1.0)
        self.scale_factor = scale
        display_w, display_h = int(img_w * scale), int(img_h * scale)
        
        resized_img = self.full_screenshot.resize((display_w, display_h), Image.LANCZOS)
        self.tk_img = ImageTk.PhotoImage(resized_img)
        
        self.canvas = tk.Canvas(self.root, width=display_w, height=display_h, cursor="cross")
        self.canvas.pack()
        self.canvas.create_image(0, 0, anchor="nw", image=self.tk_img)
        
        self.status_label = tk.Label(self.root, text="请框选按钮区域，然后点击「确认」", fg="blue", font=("微软雅黑", 12))
        self.status_label.pack(pady=2)
        
        self.canvas.bind("<ButtonPress-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_motion)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)
        
        self.start_x = None
        self.start_y = None
        self.rect_id = None
        self.press_time = None
        
        btn_frame = tk.Frame(self.root)
        btn_frame.pack(pady=3)
        tk.Button(btn_frame, text="确认", command=self.confirm, bg="#4CAF50", fg="white", font=("微软雅黑", 10)).pack(side="left", padx=10)
        tk.Button(btn_frame, text="取消", command=self.cancel, bg="#f44336", fg="white", font=("微软雅黑", 10)).pack(side="left", padx=10)
        
        self.confirmed = False
        self.root.protocol("WM_DELETE_WINDOW", self.cancel)
        self.root.mainloop()
    
    def on_press(self, event):
        self.start_x = event.x
        self.start_y = event.y
        self.press_time = time.time()
        if self.rect_id:
            self.canvas.delete(self.rect_id)
            self.rect_id = None
    
    def on_motion(self, event):
        if self.start_x is None:
            return
        if self.rect_id:
            self.canvas.delete(self.rect_id)
        self.rect_id = self.canvas.create_rectangle(
            self.start_x, self.start_y, event.x, event.y,
            outline="red", width=2
        )
    
    def on_release(self, event):
        if self.start_x is None:
            return
        elapsed = time.time() - self.press_time
        if elapsed < 0.35:
            sx = int(event.x / self.scale_factor)
            sy = int(event.y / self.scale_factor)
            self.result = (sx, sy)
            self.selection_type = "click"
        else:
            x1 = int(min(self.start_x, event.x) / self.scale_factor)
            y1 = int(min(self.start_y, event.y) / self.scale_factor)
            x2 = int(max(self.start_x, event.x) / self.scale_factor)
            y2 = int(max(self.start_y, event.y) / self.scale_factor)
            if abs(x2 - x1) > 5 and abs(y2 - y1) > 5:
                self.result = (x1, y1, x2, y2)
                self.selection_type = "drag"
            else:
                self.result = None
                self.selection_type = None
    
    def confirm(self):
        if self.result is None:
            messagebox.showwarning("提示", "请先框选按钮区域")
            return
        self.confirmed = True
        self.root.destroy()
    
    def cancel(self):
        self.result = None
        self.confirmed = False
        self.root.destroy()

# ============================================================
# 主程序
# ============================================================

def main():
    global running, paused, exit_flag
    
    notice = get_notice()
    
    print("=" * 52)
    print(f"  {APP_NAME}  {BOT_NAME}  {APP_VERSION}")
    print(f"  {notice}")
    print("=" * 52)
    print()
    
    # 加载已有配置
    config = {}
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                config = json.load(f)
        except Exception:
            pass
    
    # Tesseract 路径
    default_path = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
    tess_path = config.get("tesseract_path", default_path)
    
    user_input = input(f"  tesseract.exe 路径（留空用默认）\n    [{tess_path}]\n  > ").strip()
    if user_input:
        tess_path = user_input
    
    if not os.path.exists(tess_path):
        print(f"  [!] 未找到 tesseract.exe，请确认路径正确")
        input("  按回车退出...")
        return
    
    import pytesseract
    pytesseract.pytesseract.tesseract_cmd = tess_path
    
    # 5 秒倒计时后截图
    print()
    print("  准备好后按回车，5 秒后弹截图...")
    input("  > ")
    
    for i in range(5, 0, -1):
        print(f"  {i}…", end=" ", flush=True)
        time.sleep(1)
    print()
    
    # 截图框选
    print("  [截图窗口弹出，请框选按钮区域]")
    selector = ScreenshotSelector("框选按钮区域")
    if not selector.confirmed or selector.result is None:
        print("  [取消] 用户取消")
        input("  按回车退出...")
        return
    
    monitor_box = selector.result
    print(f"  监控区域已记录")
    
    # OCR 测试
    print("  正在测试 OCR 识别...")
    test_img = ImageGrab.grab(bbox=monitor_box)
    try:
        ocr_result = pytesseract.image_to_string(test_img, lang='chi_sim+eng')
        cleaned = clean_ocr_text(ocr_result)
        stripped = strip_digits(cleaned)
        print(f"  识别文字（去数字后）：'{stripped}'")
    except Exception as e:
        print(f"  OCR 测试失败：{e}")
        input("  按回车退出...")
        return
    
    # 关键词设置
    keyword = "限时领取"
    print(f"  触发关键词：'{keyword}'（去数字后匹配）")
    
    # 保存配置
    config["monitor_box"] = list(monitor_box) if isinstance(monitor_box, tuple) else list(monitor_box)
    config["tesseract_path"] = tess_path
    config["keyword"] = keyword
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
    
    print()
    print("  ✓ 配置已保存，开始循环监控")
    print("  Ctrl+Alt+P = 暂停/恢复 | Ctrl+Alt+Q = 退出")
    print()
    
    # 循环监控
    running = True
    prev_state = ""
    check_interval = 10
    
    try:
        while not exit_flag:
            if paused:
                time.sleep(0.5)
                continue
            
            # 截图 + OCR
            try:
                img = ImageGrab.grab(bbox=monitor_box)
                ocr_text = pytesseract.image_to_string(img, lang='chi_sim+eng')
                cleaned = clean_ocr_text(ocr_text)
                stripped = strip_digits(cleaned)
            except Exception:
                time.sleep(check_interval)
                continue
            
            # 状态判断
            if not stripped:
                # OCR 没读到文字，跳过
                time.sleep(check_interval)
                continue
            
            if keyword in stripped:
                if prev_state != "click":
                    prev_state = "click"
                    print(f"  >>> [准备点击]")
                
                # 随机延迟 3-10 秒
                delay = random.uniform(3, 10)
                time.sleep(delay)
                
                # 二次确认
                try:
                    img2 = ImageGrab.grab(bbox=monitor_box)
                    ocr2 = pytesseract.image_to_string(img2, lang='chi_sim+eng')
                    cleaned2 = clean_ocr_text(ocr2)
                    stripped2 = strip_digits(cleaned2)
                    
                    if keyword in stripped2:
                        # 点击
                        if isinstance(monitor_box, tuple) and len(monitor_box) == 4:
                            x1, y1, x2, y2 = monitor_box
                            cx = random.randint(x1, x2)
                            cy = random.randint(y1, y2)
                        else:
                            cx, cy = monitor_box
                        
                        pyautogui.click(cx, cy)
                        timestamp = get_timestamp()
                        print(f"  >>> [领取成功]  {timestamp}")
                        prev_state = "done"
                    else:
                        print(f"  >>> [状态变化，跳过]")
                except Exception:
                    pass
                
                # 重置状态，等待下一轮
                prev_state = ""
                
            elif "后领取" in stripped or "即将开始" in stripped:
                if prev_state != "waiting":
                    prev_state = "waiting"
                    print(f"  >>> [即将开始]")
            elif "等待掉落" in stripped or "初始化" in stripped:
                if prev_state != "init":
                    prev_state = "init"
                    print(f"  >>> [初始化完成]")
            else:
                if prev_state != "unknown":
                    prev_state = "unknown"
                    print(f"  >>> [等待中]  '{stripped}'")
            
            time.sleep(check_interval)
    
    except KeyboardInterrupt:
        print("\n  [退出] 用户中断")
    except Exception as e:
        print(f"\n  [错误] {e}")
    finally:
        print("  程序已停止")

if __name__ == "__main__":
    main()
