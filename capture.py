"""抓取指定窗口的截图，用于校准与调试。
用法: python capture.py "动物餐厅" shots/out.png
"""
import ctypes
import sys
import time
from ctypes import wintypes

from PIL import Image

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass

EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long),
                ("biHeight", ctypes.c_long), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


def find_windows(substr):
    out = []

    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n == 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        if substr in buf.value:
            out.append((hwnd, buf.value))
        return True

    user32.EnumWindows(EnumProc(cb), 0)
    return out


def window_rect(hwnd):
    r = RECT()
    # 优先用 DWM 的实际边框，避免阴影/圆角裁切
    try:
        d = RECT()
        if dwmapi.DwmGetWindowAttribute(hwnd, 9, ctypes.byref(d), ctypes.sizeof(d)) == 0:
            return d
    except Exception:
        pass
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r


def print_window(hwnd):
    r = window_rect(hwnd)
    w, h = r.right - r.left, r.bottom - r.top
    if w <= 0 or h <= 0:
        raise RuntimeError("窗口尺寸异常")
    hdc = user32.GetWindowDC(hwnd)
    mem = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    gdi32.SelectObject(mem, bmp)
    # PW_RENDERFULLCONTENT = 2，对 Chromium 系窗口更可靠
    ok = user32.PrintWindow(hwnd, mem, 2)

    bi = BITMAPINFOHEADER()
    bi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bi.biWidth = w
    bi.biHeight = -h
    bi.biPlanes = 1
    bi.biBitCount = 32
    bi.biCompression = 0
    buf = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bi), 0)

    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mem)
    user32.ReleaseDC(hwnd, hdc)

    img = Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", 0, 1).convert("RGB")
    return img, ok, (r.left, r.top, r.right, r.bottom)


def grab_rect(rect):
    left, top, right, bottom = rect
    return Image.grab(bbox=(left, top, right, bottom)) if False else None


def main():
    title = sys.argv[1] if len(sys.argv) > 1 else "动物餐厅"
    out = sys.argv[2] if len(sys.argv) > 2 else "shots/out.png"

    wins = find_windows(title)
    if not wins:
        print(f"没找到标题包含「{title}」的可见窗口")
        return 1

    # 优先标题完全等于关键字，其次标题最短的（避免抓到浏览器标签页）
    exact = [w for w in wins if w[1].strip() == title]
    hwnd, text = exact[0] if exact else min(wins, key=lambda w: len(w[1]))
    print(f"窗口: hwnd={hwnd} 标题={text!r}")
    img, printed, rect = print_window(hwnd)
    print(f"PrintWindow ok={printed} rect={rect} size={img.size}")

    # 全黑通常意味着 PrintWindow 没渲染出来，退回屏幕抓取
    extrema = img.convert("L").getextrema()
    if extrema[1] - extrema[0] < 3 or extrema[1] < 10:
        print("PrintWindow 结果接近纯色，改用屏幕抓取（会临时置顶窗口）")
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)
        user32.SetForegroundWindow(hwnd)
        time.sleep(0.4)
        from PIL import ImageGrab
        img = ImageGrab.grab(bbox=(rect[0], rect[1], rect[2], rect[3]), all_screens=True)
        print(f"屏幕抓取 size={img.size}")

    import os
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    img.save(out)
    print(f"已保存 {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
