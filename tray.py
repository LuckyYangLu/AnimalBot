"""极简 Win32 系统托盘图标（纯 ctypes，无第三方依赖）。

用法：
    t = Tray("动物餐厅挂机 — 运行中", log_path="daemon.log")
    while running:
        t.pump()          # 在消息循环里定期调用
        ...
    t.remove()
菜单：打开日志 / 退出。左键点击也打开日志。
"""
import ctypes
import os
import subprocess
import sys
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# 必须声明原型，否则 WPARAM/LPARAM 传大整数会 OverflowError
LPVOID = ctypes.c_void_p
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.DefWindowProcW.restype = ctypes.c_ssize_t
user32.RegisterClassExW.argtypes = [LPVOID]
user32.RegisterClassExW.restype = wintypes.ATOM
user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, LPVOID]
user32.CreateWindowExW.restype = wintypes.HWND
user32.LoadIconW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR]
user32.LoadIconW.restype = wintypes.HICON
user32.CreatePopupMenu.restype = wintypes.HMENU
user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
user32.TrackPopupMenu.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int,
                                  ctypes.c_int, wintypes.HWND, LPVOID]
user32.TrackPopupMenu.restype = ctypes.c_int
user32.DestroyMenu.argtypes = [wintypes.HMENU]
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.GetCursorPos.argtypes = [LPVOID]
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.PeekMessageW.argtypes = [LPVOID, wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT]
user32.TranslateMessage.argtypes = [LPVOID]
user32.DispatchMessageW.argtypes = [LPVOID]
user32.DispatchMessageW.restype = ctypes.c_ssize_t
user32.DestroyWindow.argtypes = [wintypes.HWND]
shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, LPVOID]
shell32.Shell_NotifyIconW.restype = wintypes.BOOL
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE

WM_APP = 0x8000
WM_TRAYICON = WM_APP + 1
WM_COMMAND = 0x0111
WM_DESTROY = 0x0002
WM_LBUTTONUP = 0x0202
WM_RBUTTONUP = 0x0205

NIM_ADD = 0
NIM_DELETE = 2
NIF_MESSAGE = 0x01
NIF_ICON = 0x02
NIF_TIP = 0x04

MF_STRING = 0x0000
TPM_LEFTBUTTON = 0x0000
TPM_RIGHTBUTTON = 0x0002
TPM_RETURNCMD = 0x0100

IDI_APPLICATION = 32512
HWND_MESSAGE = wintypes.HWND(-3)

ID_OPEN_LOG = 1001
ID_QUIT = 1002


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
    ]


WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
                             wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
        ("hIconSm", wintypes.HICON),
    ]


class Tray:
    def __init__(self, tooltip="Animal Restaurant Bot", log_path=None, on_quit=None):
        self.tooltip = tooltip[:127]
        self.log_path = log_path or ""
        self.on_quit = on_quit
        self.quit_requested = False
        self._class = f"TrayWnd_{os.getpid()}"
        self._proc = WNDPROC(self._wndproc)
        self.hwnd = None
        self._add_icon()

    # ---------------------------------------------------------------- 内部
    def _wndproc(self, hwnd, msg, wparam, lparam):
        if msg == WM_TRAYICON:
            ev = lparam & 0xFFFF
            if ev == WM_RBUTTONUP:
                self._menu()
            elif ev == WM_LBUTTONUP:
                self._open_log()
            return 0
        if msg == WM_COMMAND:
            cmd = wparam & 0xFFFF
            if cmd == ID_QUIT:
                self.quit_requested = True
                if self.on_quit:
                    self.on_quit()
            elif cmd == ID_OPEN_LOG:
                self._open_log()
            return 0
        if msg == WM_DESTROY:
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _menu(self):
        menu = user32.CreatePopupMenu()
        user32.AppendMenuW(menu, MF_STRING, ID_OPEN_LOG, "打开日志")
        user32.AppendMenuW(menu, MF_STRING, ID_QUIT, "退出")
        pt = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        user32.SetForegroundWindow(self.hwnd)
        cmd = user32.TrackPopupMenu(menu, TPM_LEFTBUTTON | TPM_RIGHTBUTTON | TPM_RETURNCMD,
                                    pt.x, pt.y, 0, self.hwnd, None)
        user32.PostMessageW(self.hwnd, WM_COMMAND, cmd, 0)
        user32.DestroyMenu(menu)

    def _open_log(self):
        if self.log_path and os.path.exists(self.log_path):
            try:
                subprocess.Popen(["notepad.exe", self.log_path])
            except OSError:
                pass

    def _add_icon(self):
        hinst = kernel32.GetModuleHandleW(None)
        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.lpfnWndProc = self._proc
        wc.hInstance = hinst
        wc.lpszClassName = self._class
        if not user32.RegisterClassExW(ctypes.byref(wc)):
            err = ctypes.get_last_error()
            if err != 1410:  # 已注册
                raise OSError(f"RegisterClassEx failed: {err}")
        self.hwnd = user32.CreateWindowExW(0, self._class, self._class, 0,
                                           0, 0, 0, 0, HWND_MESSAGE, None, hinst, None)
        if not self.hwnd:
            raise OSError(f"CreateWindowEx failed: {ctypes.get_last_error()}")

        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self.hwnd
        nid.uID = 1
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = WM_TRAYICON
        nid.hIcon = user32.LoadIconW(None, wintypes.LPCWSTR(IDI_APPLICATION))
        nid.szTip = self.tooltip
        self._nid = nid
        if not shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)):
            raise OSError("Shell_NotifyIcon(NIM_ADD) failed")

    # ---------------------------------------------------------------- 对外
    def notify(self, title, text):
        """弹一个气泡提示（Windows 10/11 可能只显示为通知）。"""
        try:
            nid = self._nid
            nid.uFlags = NIF_INFO
            nid.szInfo = text[:255]
            nid.szInfoTitle = title[:63]
            nid.dwInfoFlags = 0x1  # NIIF_INFO
            shell32.Shell_NotifyIconW(1, ctypes.byref(nid))  # NIM_MODIFY
            self._nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        except Exception:
            pass

    def pump(self, limit=20):
        """处理托盘消息，防止菜单无响应。"""
        msg = wintypes.MSG()
        for _ in range(limit):
            if not user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):  # PM_REMOVE
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def remove(self):
        try:
            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid))
        except Exception:
            pass
        if self.hwnd:
            try:
                user32.DestroyWindow(self.hwnd)
            except Exception:
                pass
