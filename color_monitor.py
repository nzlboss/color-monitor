import sys
import os
import json
import time
import random
import math
import threading
from io import BytesIO

import tkinter as tk
from tkinter import ttk, messagebox
from PIL import Image, ImageGrab, ImageTk
import pyautogui

CONFIG_FILE = "monitor_config.json"

# ---------- 全局状态 ----------
running = False
paused = False
exit_flag = False

# ---------- 辅助函数 ----------
def color_distance(c1, c2):
    return math.sqrt(sum((a - b) ** 2 for a, b in zip(c1, c2)))

def average_color(img, box):
    """计算区域平均RGB"""
    region = img.crop(box)
    pixels = list(region.getdata())
    if not pixels:
        return (0, 0, 0)
    r = sum(p[0] for p in pixels) // len(pixels)
    g = sum(p[1] for p in pixels) // len(pixels)
    b = sum(p[2] for p in pixels) // len(pixels)
    return (r, g, b)

def multi_sample_color(img, box, grid=(4, 4)):
    """多点采样平均"""
    x1, y1, x2, y2 = box
    w, h = x2 - x1, y2 - y1
    sw, sh = w // grid[0], h // grid[1]
    samples = []
    for gx in range(grid[0]):
        for gy in range(grid[1]):
            sx = x1 + gx * sw + sw // 2
            sy = y1 + gy * sh + sh // 2
            try:
                samples.append(img.getpixel((sx, sy)))
            except Exception:
                pass
    if not samples:
        return (0, 0, 0)
    r = sum(p[0] for p in samples) // len(samples)
    g = sum(p[1] for p in samples) // len(samples)
    b = sum(p[2] for p in samples) // len(samples)
    return (r, g, b)

# ---------- 截图选择器 ----------
class ScreenshotSelector:
    def __init__(self, title="选择区域"):
        self.root = tk.Tk()
        self.root.title(title)
        self.root.attributes("-topmost", True)
        self.result = None
        self.selection_type = None  # 'click' or 'drag'
        
        # 截全屏
        self.full_screenshot = ImageGrab.grab()
        self.scale_factor = 1.0
        
        # 适应屏幕
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        img_w, img_h = self.full_screenshot.size
        
        # 缩放以适应屏幕
        scale = min(screen_w * 0.95 / img_w, screen_h * 0.85 / img_h, 1.0)
        self.scale_factor = scale
        display_w, display_h = int(img_w * scale), int(img_h * scale)
        
        resized_img = self.full_screenshot.resize((display_w, display_h), Image.LANCZOS)
        self.tk_img = ImageTk.PhotoImage(resized_img)
        
        # Canvas
        self.canvas = tk.Canvas(self.root, width=display_w, height=display_h, cursor="cross")
        self.canvas.pack()
        self.canvas.create_image(0, 0, anchor="nw", image=self.tk_img)
        
        # 绑定事件
        self.canvas.bind("<ButtonPress-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_motion)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)
        
        self.start_x = None
        self.start_y = None
        self.rect_id = None
        
        # 底部按钮
        btn_frame = tk.Frame(self.root)
        btn_frame.pack(pady=5)
        tk.Label(btn_frame, text="拖框选区域 / 单击选点").pack(side="left", padx=10)
        tk.Button(btn_frame, text="确定", command=self.confirm).pack(side="right", padx=5)
        tk.Button(btn_frame, text="取消", command=self.cancel).pack(side="right", padx=5)
        
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
        elapsed = time.time() - self.press_time
        if elapsed < 0.35:
            # 单击
            sx = int(event.x / self.scale_factor)
            sy = int(event.y / self.scale_factor)
            self.result = (sx, sy)
            self.selection_type = "click"
        else:
            # 拖框
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
            messagebox.showwarning("提示", "请先拖框或单击选择区域")
            return
        self.confirmed = True
        self.root.destroy()
    
    def cancel(self):
        self.result = None
        self.confirmed = False
        self.root.destroy()

# ---------- 颜色采样器（取目标色） ----------
class ColorSampler:
    def __init__(self, screenshot, prompt="请让按钮变成【可点击色】，然后单击它"):
        self.root = tk.Tk()
        self.root.title("取色")
        self.root.attributes("-topmost", True)
        self.result = None
        
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        img_w, img_h = screenshot.size
        
        scale = min(screen_w * 0.75 / img_w, screen_h * 0.65 / img_h, 1.0)
        self.scale_factor = scale
        display_w, display_h = int(img_w * scale), int(img_h * scale)
        
        resized_img = screenshot.resize((display_w, display_h), Image.LANCZOS)
        self.tk_img = ImageTk.PhotoImage(resized_img)
        
        self.canvas = tk.Canvas(self.root, width=display_w, height=display_h, cursor="cross")
        self.canvas.pack()
        self.canvas.create_image(0, 0, anchor="nw", image=self.tk_img)
        
        self.canvas.bind("<Button-1>", self.on_click)
        
        btn_frame = tk.Frame(self.root)
        btn_frame.pack(pady=5)
        tk.Label(btn_frame, text=prompt, fg="blue").pack(side="left", padx=10)
        tk.Button(btn_frame, text="确定", command=self.confirm).pack(side="right", padx=5)
        tk.Button(btn_frame, text="取消", command=self.cancel).pack(side="right", padx=5)
        
        self.confirmed = False
        self.root.protocol("WM_DELETE_WINDOW", self.cancel)
        self.root.mainloop()
    
    def on_click(self, event):
        x = int(event.x / self.scale_factor)
        y = int(event.y / self.scale_factor)
        self.result = (x, y)
        self.canvas.delete("marker")
        r = 5
        self.canvas.create_oval(event.x-r, event.y-r, event.x+r, event.y+r,
                                outline="red", width=2, tags="marker")
    
    def confirm(self):
        if self.result is None:
            messagebox.showwarning("提示", "请先在图片上单击取色")
            return
        self.confirmed = True
        self.root.destroy()
    
    def cancel(self):
        self.result = None
        self.confirmed = False
        self.root.destroy()

# ---------- 主程序 ----------
def main():
    global running, paused, exit_flag
    
    print("=" * 50)
    print("ColorMonitor V3 - 目标色检测版")
    print("=" * 50)
    
    # 加载已有配置
    config = {}
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                config = json.load(f)
            print(f"[配置] 已加载 {CONFIG_FILE}")
        except Exception:
            pass
    
    # 截图
    print("\n[步骤 1/4] 正在截图...")
    screenshot = ImageGrab.grab()
    
    # 1. 选择监控区域
    print("[步骤 1/4] 请框选【监控区域】（按钮颜色变化的区域）")
    selector = ScreenshotSelector("选择监控区域")
    if not selector.confirmed or selector.result is None:
        print("[退出] 用户取消")
        return
    monitor_box = selector.result
    print(f"  监控区域: {monitor_box}")
    
    # 2. 取目标色
    print("\n[步骤 2/4] 请让按钮变成【可点击色】，然后在截图上单击取色")
    sampler = ColorSampler(screenshot, "请让按钮变成【可点击色】，然后单击它")
    if not sampler.confirmed or sampler.result is None:
        print("[退出] 用户取消")
        return
    click_x, click_y = sampler.result
    target_color = screenshot.getpixel((click_x, click_y))
    print(f"  目标色: RGB{target_color}  @ ({click_x}, {click_y})")
    
    # 3. 选择点击区域
    print("\n[步骤 3/4] 请框选/单击【点击区域】")
    selector2 = ScreenshotSelector("选择点击区域")
    if not selector2.confirmed or selector2.result is None:
        print("[退出] 用户取消")
        return
    click_area = selector2.result
    print(f"  点击区域: {click_area}")
    
    # 4. 参数设置
    print("\n[步骤 4/4] 参数设置（直接回车使用默认值）")
    try:
        delay_min = float(input("  最小延迟(秒) [3]: ") or "3")
        delay_max = float(input("  最大延迟(秒) [10]: ") or "10")
        click_count = int(input("  点击次数 [1]: ") or "1")
        tolerance = int(input("  颜色容差 [18]: ") or "18")
        interval = float(input("  检测间隔(秒) [0.3]: ") or "0.3")
    except Exception:
        print("[错误] 参数无效，使用默认值")
        delay_min, delay_max = 3, 10
        click_count = 1
        tolerance = 18
        interval = 0.3
    
    # 保存配置
    config = {
        "monitor_box": list(monitor_box) if isinstance(monitor_box, tuple) else list(monitor_box),
        "target_color": list(target_color),
        "click_area": list(click_area) if isinstance(click_area, tuple) else list(click_area),
        "delay_min": delay_min,
        "delay_max": delay_max,
        "click_count": click_count,
        "tolerance": tolerance,
        "interval": interval
    }
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
    print(f"\n[配置] 已保存至 {CONFIG_FILE}")
    
    # 开始监控
    print("\n" + "=" * 60)
    print("开始监控！")
    print("  Ctrl+Alt+P = 暂停/恢复")
    print("  Ctrl+Alt+Q = 退出")
    print("=" * 60)
    
    running = True
    last_check_color = None
    
    try:
        while not exit_flag:
            if paused:
                time.sleep(0.5)
                continue
            
            # 截取监控区域
            region_img = ImageGrab.grab(bbox=monitor_box)
            current_color = multi_sample_color(region_img, (0, 0, region_img.width, region_img.height))
            
            dist = color_distance(current_color, target_color)
            
            if dist < tolerance:
                print(f"[触发] 颜色匹配! 距离={dist:.1f} / 容差={tolerance}")
                
                # 随机延迟
                delay = random.uniform(delay_min, delay_max)
                print(f"  等待 {delay:.1f} 秒...")
                time.sleep(delay)
                
                # 二次确认
                region_img2 = ImageGrab.grab(bbox=monitor_box)
                current_color2 = multi_sample_color(region_img2, (0, 0, region_img2.width, region_img2.height))
                dist2 = color_distance(current_color2, target_color)
                
                if dist2 < tolerance:
                    print(f"  二次确认通过 (距离={dist2:.1f})，准备点击")
                    
                    # 执行点击
                    if isinstance(click_area, tuple) and len(click_area) == 4:
                        # 拖框区域 → 随机点击
                        x1, y1, x2, y2 = click_area
                        for i in range(click_count):
                            cx = random.randint(x1, x2)
                            cy = random.randint(y1, y2)
                            pyautogui.click(cx, cy)
                            time.sleep(random.uniform(0.05, 0.15))
                            print(f"  点击 #{i+1}: ({cx}, {cy})")
                    else:
                        # 单击点 → 点附近
                        cx, cy = click_area
                        for i in range(click_count):
                            ox = random.randint(-5, 5)
                            oy = random.randint(-5, 5)
                            pyautogui.click(cx + ox, cy + oy)
                            time.sleep(random.uniform(0.05, 0.15))
                            print(f"  点击 #{i+1}: ({cx+ox}, {cy+oy})")
                else:
                    print(f"  二次确认未通过 (距离={dist2:.1f})，跳过此次")
            
            time.sleep(interval)
    
    except KeyboardInterrupt:
        print("\n[退出] 用户中断")
    except Exception as e:
        print(f"\n[错误] {e}")
    finally:
        print("程序已停止")

if __name__ == "__main__":
    main()
