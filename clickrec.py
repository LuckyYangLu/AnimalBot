"""记录鼠标点击坐标（归一化到 415x790 的游戏窗口坐标系）。

用法：python clickrec.py 20
运行期间正常操作游戏，所有左键点击会被记录下来并打印坐标。
"""
import ctypes
import sys
import time
from ctypes import wintypes

import game

u = ctypes.windll.user32


def norm_point(hwnd, sx, sy):
    """屏幕坐标 -> 归一化游戏坐标(415x790)。"""
    r = game.win_rect(hwnd)
    w = max(1, r.right - r.left)
    h = max(1, r.bottom - r.top)
    return int((sx - r.left) * 415 / w), int((sy - r.top) * 790 / h)


def main():
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 20.0
    hwnd, title = game.find_window()
    print(f"窗口 {title!r} hwnd={hwnd}，开始记录 {seconds:.0f} 秒内的鼠标点击…")
    down = False
    t0 = time.time()
    hits = []
    while time.time() - t0 < seconds:
        state = u.GetAsyncKeyState(0x01) & 0xFFFF  # VK_LBUTTON
        pressed = bool(state & 0x8000)
        if pressed and not down:
            down = True
            pt = wintypes.POINT()
            u.GetCursorPos(ctypes.byref(pt))
            nx, ny = norm_point(hwnd, pt.x, pt.y)
            hits.append((nx, ny))
            print(f"  +{time.time() - t0:5.1f}s  屏幕({pt.x},{pt.y}) -> 归一化({nx},{ny})")
        elif not pressed and down:
            down = False
        time.sleep(0.01)
    print("共记录", len(hits), "次点击")
    if hits:
        print("坐标列表:", hits)


if __name__ == "__main__":
    main()
