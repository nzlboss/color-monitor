# -*- coding: utf-8 -*-
"""
区域颜色变化监控 + 随机延迟 + 区域内随机位置多次点击 (Windows)
------------------------------------------------------------------
工作流程（每一步都有引导提醒）：
  1. 设置监控区域        （拖动鼠标框选，或手动输入坐标）
  2. 采样基准颜色指纹    （停留 3 秒后自动采样）
  3. 设置点击区域        （同样可框选）
  4. 设置点击次数        （整数）
  5. 设置检测间隔 / 相似度阈值 / 延迟区间
  6. 进入循环监控：
      检测到变化 -> 随机延迟 3~5s -> 在点击区域内随机位置点击 N 次 -> 继续循环
支持托盘热键：Ctrl+Alt+P 暂停/恢复，Ctrl+Alt+Q 退出。
打包： pyinstaller --onefile --uac-admin --add-binary "xxx;." color_monitor.py
"""

import sys
import os
import time
import random
import json
import ctypes
import threading
from datetime import datetime

# ---------------- 依赖导入（友好提示） ----------------
try:
    import numpy as np
except ImportError:
    print("[错误] 缺少依赖 numpy，请先运行： pip install numpy")
    sys.exit(1)

try:
    from PIL import ImageGrab, Image
except ImportError:
    print("[错误] 缺少依赖 pillow，请先运行： pip install pillow")
    sys.exit(1)

try:
    import pyautogui
    pyautogui.FAILSAFE = False  # 禁用角落防误触，改为热键控制
except ImportError:
    print("[错误] 缺少依赖 pyautogui，请先运行： pip install pyautogui")
    sys.exit(1)

# ---------------- Windows 高精度休眠 ----------------
if sys.platform == "win32":
    time_begin = ctypes.windll.kernel32.timeBeginPeriod
    time_end = ctypes.windll.kernel32.timeEndPeriod
    try:
        time_begin(1)
    except Exception:
        pass

# ---------------- 全局状态 ----------------
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), "monitor_config.json")
STATE = {
    "watch_region": None,   # (x1, y1, x2, y2)
    "click_region": None,   # (x1, y1, x2, y2)
    "click_count": 5,
    "check_interval": 0.5,   # 每次检测间隔(秒)
    "threshold": 8.0,        # 颜色指纹差异阈值(0-255，越小越灵敏)
    "delay_min": 3.0,
    "delay_max": 5.0,
    "click_interval": 0.05,  # 两次点击之间的间隔(秒)
}
RUNNING = True
PAUSED = False


# ---------------- 工具函数 ----------------
def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


def input_int(prompt, default, minval=None, maxval=None):
    while True:
        try:
            s = input(f"{prompt} [默认 {default}]: ").strip()
            if s == "":
                return default
            v = int(s)
            if (minval is not None and v < minval) or (maxval is not None and v > maxval):
                print(f"  请输入介于 {minval} 和 {maxval} 之间的整数。")
                continue
            return v
        except ValueError:
            print("  请输入有效的整数。")


def input_float(prompt, default, minval=None, maxval=None):
    while True:
        try:
            s = input(f"{prompt} [默认 {default}]: ").strip()
            if s == "":
                return default
            v = float(s)
            if (minval is not None and v < minval) or (maxval is not None and v > maxval):
                print(f"  请输入介于 {minval} 和 {maxval} 之间的数值。")
                continue
            return v
        except ValueError:
            print("  请输入有效的数值。")


# ---------------- 区域设置（支持拖动框选） ----------------
def grab_region(region):
    """抓取区域为 numpy 数组 (H, W, 3)"""
    x1, y1, x2, y2 = region
    bbox = (x1, y1, x2, y2)
    img = ImageGrab.grab(bbox=bbox, all_screens=True)
    return np.array(img)


def color_fingerprint(region):
    """计算区域颜色指纹：缩小到 16x16 后的平均色调 + 整体均值，作为稳定特征"""
    x1, y1, x2, y2 = region
    img = ImageGrab.grab(bbox=(x1, y1, x2, y2), all_screens=True).convert("RGB")
    small = img.resize((16, 16), Image.NEAREST)
    arr = np.array(small, dtype=np.float32)
    mean_color = arr.mean(axis=(0, 1))           # (3,) 整体均值
    # 用网格分块均值增加空间敏感度
    grid = arr.reshape(4, 4, 4, 4, 3).mean(axis=(1, 3)).reshape(-1, 3)
    return mean_color, grid


def fingerprint_diff(fp1, fp2):
    mean1, grid1 = fp1
    mean2, grid2 = fp2
    d_mean = np.abs(mean1 - mean2).mean()
    d_grid = np.abs(grid1 - grid2).mean()
    return float(d_mean + d_grid)


def select_region_interactive(name):
    """通过拖动鼠标框选区域；失败则退回手动输入"""
    print(f"\n>>> 接下来设置【{name}】")
    print("    方式1：在接下来的 10 秒内，按住鼠标左键拖动一个矩形框后松开")
    print("    方式2：直接回车跳过，改用手动输入坐标")
    try:
        from pynput.mouse import Button, Listener
    except ImportError:
        print("    [提示] 未安装 pynput，将使用手动输入方式。建议 pip install pynput")
        return _manual_region(name)

    state = {"start": None, "end": None, "done": False}

    def on_click(x, y, button, pressed):
        if button != Button.left:
            return
        if pressed:
            state["start"] = (x, y)
            state["end"] = None
        else:
            state["end"] = (x, y)
            state["done"] = True
            return False  # 停止监听

    listener = Listener(on_click=on_click)
    listener.start()
    for _ in range(100):  # 最长等待 10 秒
        if state["done"]:
            break
        time.sleep(0.1)
    listener.stop()
    if state["start"] and state["end"]:
        x1, y1 = map(int, state["start"])
        x2, y2 = map(int, state["end"])
        x1, x2 = sorted((x1, x2))
        y1, y2 = sorted((y1, y2))
        if x2 - x1 < 2 or y2 - y1 < 2:
            print("    框选区域过小，改用手动输入。")
            return _manual_region(name)
        print(f"    [已框选] {name}: ({x1}, {y1}) -> ({x2}, {y2})")
        return (x1, y1, x2, y2)
    return _manual_region(name)


def _manual_region(name):
    print(f"    请手动输入 {name} 的左上角和右下角坐标：")
    try:
        x1 = int(input("      x1 (左): "))
        y1 = int(input("      y1 (上): "))
        x2 = int(input("      x2 (右): "))
        y2 = int(input("      y2 (下): "))
    except ValueError:
        print("    输入无效，返回 None")
        return None
    x1, x2 = sorted((x1, x2))
    y1, y2 = sorted((y1, y2))
    return (x1, y1, x2, y2)


# ---------------- 配置保存/加载 ----------------
def save_config():
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(STATE, f, ensure_ascii=False, indent=2)
        log(f"配置已保存到 {CONFIG_PATH}")
    except Exception as e:
        log(f"保存配置失败：{e}")


def load_config():
    global STATE
    if not os.path.exists(CONFIG_PATH):
        return False
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            saved = json.load(f)
        # JSON 会把 tuple 变成 list，还原坐标为元组
        for key in ("watch_region", "click_region"):
            if key in saved and isinstance(saved[key], list):
                saved[key] = tuple(saved[key])
        STATE.update(saved)
        log(f"已加载已有配置：{CONFIG_PATH}")
        return True
    except Exception as e:
        log(f"加载配置失败，使用默认：{e}")
        return False


# ---------------- 监控循环 ----------------
def do_clicks(region, count):
    """在区域内随机位置连续点击"""
    x1, y1, x2, y2 = region
    for i in range(count):
        rx = random.randint(x1 + 1, max(x1 + 2, x2 - 1))
        ry = random.randint(y1 + 1, max(y1 + 2, y2 - 1))
        # 加一点随机抖动，更像人工
        pyautogui.moveTo(rx, ry, duration=random.uniform(0.02, 0.08))
        pyautogui.click(rx, ry)
        log(f"  点击 #{i + 1}/{count} -> ({rx}, {ry})")
        time.sleep(STATE["click_interval"] + random.uniform(0, 0.03))


def monitor_loop():
    global RUNNING, PAUSED
    log("=" * 50)
    log("开始监控循环。检测到变化后将：随机延迟 -> 随机位置点击 N 次 -> 继续循环")
    log("热键：Ctrl+Alt+P 暂停/恢复 | Ctrl+Alt+Q 退出")
    log("=" * 50)

    baseline = color_fingerprint(STATE["watch_region"])
    log(f"基准颜色指纹已采样（阈值={STATE['threshold']}）")

    while RUNNING:
        if PAUSED:
            time.sleep(0.3)
            continue

        try:
            current = color_fingerprint(STATE["watch_region"])
            diff = fingerprint_diff(baseline, current)
        except Exception as e:
            log(f"抓取画面失败：{e}")
            time.sleep(STATE["check_interval"])
            continue

        if diff > STATE["threshold"]:
            delay = random.uniform(STATE["delay_min"], STATE["delay_max"])
            log(f"*** 检测到颜色变化！差异={diff:.2f} > {STATE['threshold']}  |  延迟 {delay:.2f} 秒 ***")
            # 延迟期间仍响应暂停/退出
            slept = 0
            while slept < delay and RUNNING:
                time.sleep(0.1)
                slept += 0.1
            if not RUNNING:
                break
            if PAUSED:
                log("当前处于暂停状态，等待恢复...")
                while PAUSED and RUNNING:
                    time.sleep(0.3)
            try:
                do_clicks(STATE["click_region"], STATE["click_count"])
            except Exception as e:
                log(f"点击执行失败：{e}")
            log("本轮动作完成，重置基准指纹，继续监控...")
            try:
                baseline = color_fingerprint(STATE["watch_region"])
            except Exception:
                pass

        time.sleep(STATE["check_interval"])


# ---------------- 热键监听 ----------------
def hotkey_listener():
    global RUNNING, PAUSED
    try:
        from pynput.keyboard import GlobalHotKeys
        bindings = {
            "<ctrl>+<alt>+p": toggle_pause,
            "<ctrl>+<alt>+q": quit_program,
        }
        with GlobalHotKeys(bindings) as h:
            h.join()
    except Exception as e:
        log(f"热键监听初始化失败（可忽略，不影响主功能）：{e}")


def toggle_pause():
    global PAUSED
    PAUSED = not PAUSED
    log(">>> 已" + ("暂停" if PAUSED else "恢复") + "监控 <<<")


def quit_program():
    global RUNNING
    RUNNING = False
    log(">>> 收到退出信号，正在退出... <<<")


# ---------------- 引导配置 ----------------
def guided_setup():
    print("\n" + "=" * 50)
    print("  区域颜色监控 - 引导配置向导")
    print("=" * 50)

    # 1. 监控区域
    print("\n【步骤 1/6】设置监控区域（检测颜色变化的地方）")
    if STATE["watch_region"]:
        print(f"  当前监控区域：{STATE['watch_region']}")
        if input("  重新设置？(y/N): ").strip().lower() == "y":
            STATE["watch_region"] = select_region_interactive("监控区域")
    else:
        STATE["watch_region"] = select_region_interactive("监控区域")

    # 2. 采样基准
    print("\n【步骤 2/6】采样基准颜色")
    print("  保持监控区域为你想要的'原始状态'，脚本将在 3 秒后自动采样")
    if STATE["watch_region"]:
        for i in range(3, 0, -1):
            print(f"  ... {i}")
            time.sleep(1)
        try:
            color_fingerprint(STATE["watch_region"])
            print("  [OK] 基准采样成功")
        except Exception as e:
            print(f"  [失败] {e}")

    # 3. 点击区域
    print("\n【步骤 3/6】设置点击区域（变化后在哪里点击）")
    if STATE["click_region"]:
        print(f"  当前点击区域：{STATE['click_region']}")
        if input("  重新设置？(y/N): ").strip().lower() == "y":
            STATE["click_region"] = select_region_interactive("点击区域")
    else:
        STATE["click_region"] = select_region_interactive("点击区域")

    # 4. 点击次数
    print("\n【步骤 4/6】设置点击次数")
    STATE["click_count"] = input_int("  每次触发后点击多少次", STATE["click_count"], 1, 1000)

    # 5. 延迟区间
    print("\n【步骤 5/6】设置触发后的随机延迟区间(秒)")
    STATE["delay_min"] = input_float("  最短延迟(秒)", STATE["delay_min"], 0.0, 60.0)
    STATE["delay_max"] = input_float("  最长延迟(秒)", STATE["delay_max"], STATE["delay_min"], 60.0)

    # 6. 高级参数
    print("\n【步骤 6/6】高级参数")
    STATE["check_interval"] = input_float("  检测间隔(秒, 越小越灵敏越占CPU)", STATE["check_interval"], 0.05, 10.0)
    STATE["threshold"] = input_float("  颜色差异阈值(0-255, 越小越灵敏)", STATE["threshold"], 0.5, 100.0)

    save_config()

    # 校验
    if not STATE["watch_region"] or not STATE["click_region"]:
        print("\n[警告] 监控区域和点击区域都必须设置完整才能运行！")
        return False
    return True


# ---------------- 主程序 ----------------
def main():
    global RUNNING
    print("\n" + "*" * 50)
    print("   区域颜色变化监控工具  v1.0")
    print("   Windows 专用 | 检测到变化 -> 随机延迟 -> 随机点击")
    print("*" * 50)

    load_config()

    while True:
        print("\n请选择操作：")
        print("  [1] 重新引导配置（向导模式）")
        print("  [2] 使用当前配置直接开始监控")
        print("  [3] 查看当前配置")
        print("  [4] 退出")
        choice = input("输入选项 [1]: ").strip() or "1"

        if choice == "1":
            if not guided_setup():
                continue
            break
        elif choice == "2":
            if not STATE["watch_region"] or not STATE["click_region"]:
                print("配置不完整，请先执行引导配置(1)。")
                continue
            break
        elif choice == "3":
            print(json.dumps(STATE, ensure_ascii=False, indent=2))
        elif choice == "4":
            return
        else:
            print("无效选项。")

    # 启动热键监听线程
    t = threading.Thread(target=hotkey_listener, daemon=True)
    t.start()

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
