"""动物餐厅 PC 微信小程序：截图 / 点击 / 探针工具。

用法:
  python game.py info
  python game.py capture shots/x.png
  python game.py click 265 519 [--delay 0.3]

坐标为窗口内相对坐标（左上角为原点），窗口移动不影响。
"""
import ctypes
import os
import sys
import time
from ctypes import wintypes

from PIL import Image

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass

WINDOW_TITLE = "动物餐厅"
# 所有坐标、模板、识别区域都以这个基准尺寸为准；
# 窗口被拖动缩放后，截图会归一化到这个尺寸，点击再按实际尺寸换算回去。
REF_SIZE = (415, 790)
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


def find_window(substr=WINDOW_TITLE):
    hits = []

    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n == 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        if substr in buf.value:
            hits.append((hwnd, buf.value))
        return True

    user32.EnumWindows(EnumProc(cb), 0)
    if not hits:
        raise SystemExit(f"没找到标题包含「{substr}」的可见窗口，先打开小程序")
    # 必须标题完全一致。浏览器标签页里也可能带"动物餐厅"字样
    # （比如"动物餐厅代玩请求 — DeepSeek Harness"），模糊匹配会点到别的窗口上，
    # 导致"点了打开广告/窗口跳转"这类莫名其妙的问题。
    exact = [h for h in hits if h[1].strip() == substr]
    if not exact:
        others = "、".join(h[1] for h in hits[:3])
        raise SystemExit(
            f"没找到标题正好是「{substr}」的窗口（相近的有：{others}）。"
            f"请先打开小程序窗口，再重新启动脚本。")
    return exact[0]


def win_rect(hwnd):
    d = RECT()
    if dwmapi.DwmGetWindowAttribute(hwnd, 9, ctypes.byref(d), ctypes.sizeof(d)) == 0:
        return d
    r = RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r


def ensure_foreground(hwnd):
    """把窗口切到前台。Windows 有前台锁定，需要多级回退。"""
    if user32.GetForegroundWindow() == hwnd:
        return True

    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE

    user32.SetForegroundWindow(hwnd)
    user32.BringWindowToTop(hwnd)
    time.sleep(0.15)
    if user32.GetForegroundWindow() == hwnd:
        return True

    # 回退 1：模拟一次 ALT，解除前台锁定
    VK_MENU, KEYEVENTF_KEYUP = 0x12, 0x0002
    user32.keybd_event(VK_MENU, 0, 0, 0)
    user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.15)
    if user32.GetForegroundWindow() == hwnd:
        return True

    # 回退 2：AttachThreadInput 借用前台线程的输入队列
    fg = user32.GetForegroundWindow()
    if fg:
        tid_fg = user32.GetWindowThreadProcessId(fg, None)
        tid_me = ctypes.windll.kernel32.GetCurrentThreadId()
        if tid_fg and tid_fg != tid_me:
            user32.AttachThreadInput(tid_me, tid_fg, True)
            try:
                user32.SetForegroundWindow(hwnd)
                user32.BringWindowToTop(hwnd)
            finally:
                user32.AttachThreadInput(tid_me, tid_fg, False)
            time.sleep(0.15)
            if user32.GetForegroundWindow() == hwnd:
                return True

    return user32.GetForegroundWindow() == hwnd


def capture(hwnd):
    r = win_rect(hwnd)
    w, h = r.right - r.left, r.bottom - r.top
    if w <= 0 or h <= 0:
        raise RuntimeError("窗口尺寸异常")

    hdc = user32.GetWindowDC(hwnd)
    mem = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    gdi32.SelectObject(mem, bmp)
    ok = user32.PrintWindow(hwnd, mem, 2)

    bi = BITMAPINFOHEADER()
    bi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bi.biWidth, bi.biHeight = w, -h
    bi.biPlanes, bi.biBitCount, bi.biCompression = 1, 32, 0
    buf = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bi), 0)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mem)
    user32.ReleaseDC(hwnd, hdc)

    img = Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", 0, 1).convert("RGB")
    # PrintWindow 对 Chromium 常返回黑图，退回屏幕抓取
    lo, hi = img.convert("L").getextrema()
    if not ok or hi < 12:
        # 窗口最小化时不要强行切前台（那会把它"弹出来"），直接返回黑图，
        # 由上层判断"窗口被隐藏了"并暂停。
        try:
            if user32.IsIconic(hwnd):
                return Image.new("RGB", REF_SIZE, (0, 0, 0))
        except Exception:
            pass
        ensure_foreground(hwnd)
        time.sleep(0.25)
        from PIL import ImageGrab
        img = ImageGrab.grab(bbox=(r.left, r.top, r.right, r.bottom), all_screens=True)
    # 统一到基准尺寸，但必须"等比缩放 + 居中留边"。
    # 直接拉伸会随窗口比例变化把元素位置挤偏，导致所有坐标失效。
    if img.size != REF_SIZE:
        img = _fit_ref(img)
    return img


def _fit_ref(img):
    """等比缩放到 REF_SIZE 内，四周用黑边补齐，元素相对位置不随窗口比例变化。"""
    w, h = img.size
    scale = min(REF_SIZE[0] / float(w), REF_SIZE[1] / float(h))
    nw, nh = max(1, int(round(w * scale))), max(1, int(round(h * scale)))
    out = Image.new("RGB", REF_SIZE, (0, 0, 0))
    out.paste(img.resize((nw, nh), Image.LANCZOS), ((REF_SIZE[0] - nw) // 2,
                                                    (REF_SIZE[1] - nh) // 2))
    return out


def ref_geometry(hwnd):
    """返回 (left, top, scale, pad_x, pad_y)，用于把基准坐标换算回屏幕坐标。"""
    r = win_rect(hwnd)
    w, h = max(1, r.right - r.left), max(1, r.bottom - r.top)
    scale = min(REF_SIZE[0] / float(w), REF_SIZE[1] / float(h))
    nw, nh = int(round(w * scale)), int(round(h * scale))
    return r.left, r.top, scale, (REF_SIZE[0] - nw) // 2, (REF_SIZE[1] - nh) // 2


def _to_abs(x, y):
    sw, sh = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
    return int(x * 65535 / max(sw - 1, 1)), int(y * 65535 / max(sh - 1, 1))


# ---------------------------------------------------------------- 用户接管检测
# 脚本每点一次就把光标放到已知位置；只要光标偏离这个位置，就说明用户在操作，
# 立刻暂停，等光标静止 idle_seconds 后再继续。
_last_cursor = None
CURSOR_TOLERANCE = 3


def cursor_pos():
    pt = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return (int(pt.x), int(pt.y))


def note_cursor(x, y):
    global _last_cursor
    _last_cursor = (int(x), int(y))


def poll_cursor(tol=CURSOR_TOLERANCE):
    """返回 True 表示光标相对上次记录移动过（通常是用户动鼠标）。"""
    global _last_cursor
    cur = cursor_pos()
    if _last_cursor is None:
        _last_cursor = cur
        return False
    if abs(cur[0] - _last_cursor[0]) > tol or abs(cur[1] - _last_cursor[1]) > tol:
        _last_cursor = cur
        return True
    return False


def wait_for_idle(idle_seconds=3.0, on_pause=None, on_resume=None, poll=0.15):
    """若用户正在操作鼠标，阻塞到光标静止 idle_seconds 为止。"""
    if not poll_cursor():
        return False
    if on_pause:
        on_pause()
    last_move = time.time()
    while True:
        time.sleep(poll)
        if poll_cursor():
            last_move = time.time()
        elif time.time() - last_move >= idle_seconds:
            if on_resume:
                on_resume()
            return True


# ---------------------------------------------------------------- 阻止息屏
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002


def keep_awake(enable=True):
    """阻止显示器休眠/系统睡眠。注意：它挡不住手动锁屏或组策略强制锁屏。"""
    flags = ES_CONTINUOUS | ((ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED) if enable else 0)
    return bool(kernel32.SetThreadExecutionState(flags))


def nudge_cursor():
    """极小幅移动再移回，用来刷新系统空闲计时，不算真正的点击。"""
    user32.mouse_event(0x0001, 1, 0, 0, 0)
    time.sleep(0.03)
    user32.mouse_event(0x0001, -1, 0, 0, 0)


def is_foreground(hwnd):
    return user32.GetForegroundWindow() == hwnd


def click(hwnd, x, y, delay=0.4):
    fg_ok = ensure_foreground(hwnd)
    r = win_rect(hwnd)
    w, h = r.right - r.left, r.bottom - r.top
    # x, y 是基准尺寸下的坐标，按窗口实际大小换算
    left, top, sc, pad_x, pad_y = ref_geometry(hwnd)
    sx = left + int(round((int(x) - pad_x) / sc))
    sy = top + int(round((int(y) - pad_y) / sc))
    ax, ay = _to_abs(sx, sy)
    # 用绝对移动而不是 SetCursorPos：Chromium 需要真实的 pointermove 事件
    user32.mouse_event(0x0001 | 0x8000, ax, ay, 0, 0)
    time.sleep(0.12)
    # 小幅抖动，确保 hover / pointermove 状态更新
    user32.mouse_event(0x0001, 2, 0, 0, 0)
    time.sleep(0.05)
    user32.mouse_event(0x0001, -2, 0, 0, 0)
    time.sleep(0.12)
    user32.mouse_event(0x0002, 0, 0, 0, 0)  # LEFTDOWN
    time.sleep(0.08)
    user32.mouse_event(0x0004, 0, 0, 0, 0)  # LEFTUP
    note_cursor(sx, sy)                      # 记录脚本自己放的位置
    time.sleep(delay)
    return sx, sy, fg_ok


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 1
    cmd = args[0]
    hwnd, title = find_window()
    r = win_rect(hwnd)
    size = (r.right - r.left, r.bottom - r.top)

    if cmd == "info":
        print(f"hwnd={hwnd} title={title!r}")
        print(f"rect=({r.left},{r.top},{r.right},{r.bottom}) size={size[0]}x{size[1]}")
        print(f"foreground={user32.GetForegroundWindow() == hwnd}")
        print(f"screen={user32.GetSystemMetrics(0)}x{user32.GetSystemMetrics(1)}")
        try:
            print(f"window_dpi={user32.GetDpiForWindow(hwnd)}")
        except Exception:
            pass
        try:
            import ctypes as _c
            aware = _c.c_int()
            _c.windll.shcore.GetProcessDpiAwareness(0, _c.byref(aware))
            print(f"process_dpi_awareness={aware.value} (0=unaware 1=system 2=permonitor)")
        except Exception:
            pass
    elif cmd == "capture":
        out = args[1] if len(args) > 1 else "shots/shot.png"
        os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
        img = capture(hwnd)
        img.save(out)
        print(f"saved {out} size={img.size}")
    elif cmd == "click":
        x, y = int(args[1]), int(args[2])
        sx, sy, fg_ok = click(hwnd, x, y)
        print(f"clicked window({x},{y}) -> screen({sx},{sy}) foreground_ok={fg_ok}")
    else:
        print(f"未知命令 {cmd}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
