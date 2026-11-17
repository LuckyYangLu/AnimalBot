"""演示录制器：同时记录鼠标点击坐标和游戏画面。

用法：python record_demo.py 120
输出：shots/demo_NNN.png + 控制台打印每次点击的归一化坐标
"""
import ctypes
import sys
import time
from ctypes import wintypes

import game

u = ctypes.windll.user32


def norm(hwnd, sx, sy):
    r = game.win_rect(hwnd)
    w = max(1, r.right - r.left)
    h = max(1, r.bottom - r.top)
    return int((sx - r.left) * 415 / w), int((sy - r.top) * 790 / h)


def main():
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 120.0
    hwnd, title = game.find_window()
    print(f"窗口={title!r} hwnd={hwnd} 录制 {seconds:.0f} 秒", flush=True)
    down = False
    t0 = time.time()
    next_shot = 0.0
    n = 0
    clicks = []
    while time.time() - t0 < seconds:
        now = time.time()
        state = u.GetAsyncKeyState(0x01) & 0xFFFF
        pressed = bool(state & 0x8000)
        if pressed and not down:
            down = True
            pt = wintypes.POINT()
            u.GetCursorPos(ctypes.byref(pt))
            nx, ny = norm(hwnd, pt.x, pt.y)
            clicks.append((round(now - t0, 1), nx, ny))
            print(f"  点击 +{now - t0:5.1f}s -> ({nx},{ny})", flush=True)
        elif not pressed and down:
            down = False

        if now >= next_shot:
            next_shot = now + 1.0
            n += 1
            try:
                game.capture(hwnd).save(f"shots/demo_{n:03d}.png")
            except Exception as e:
                print("截图失败", e, flush=True)

        time.sleep(0.02)

    print("=== 录制结束 ===", flush=True)
    for c in clicks:
        print(f"  +{c[0]:5.1f}s  ({c[1]},{c[2]})", flush=True)
    print("共", len(clicks), "次点击，", n, "帧", flush=True)


if __name__ == "__main__":
    main()
