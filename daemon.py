"""挂机调度器：
  - 地面掉落物（鱼干/虫子/白色物等）：随机出现，发现就点掉
  - 手机宣传：每 15 分钟连点 40 下
  - 对话框/广告页：只识别、不点击（避免误点按钮）

默认 dry-run，--click 才真点。Ctrl+C 停止。
"""
import argparse
import configparser
import ctypes
import os
import queue
import random
import numpy as np
import sys
import threading
import time
from ctypes import wintypes

import bot
import game
import itemscan
import tray as tray_mod
import vision

HERE = os.path.dirname(os.path.abspath(__file__))
FLOOR = (0, 300, 415, 700)
PHONE_AD = (372, 731)
FACILITY_BTN = (60, 735)        # 非主界面时左下角的「设施」按钮
SCENE_RESTAURANT = (215, 400)   # 「设施」场景地图里的「餐厅」格子
LOG = os.path.join(HERE, "daemon.log")
PID_FILE = os.path.join(HERE, "daemon.pid")
CONFIG_FILE = os.path.join(HERE, "config.ini")
LAST_INPUT = 0.0
TIP_INTERVAL = 15 * 60   # 两次收小费之间的最小间隔（秒）
AD_WAIT_SECONDS = 60      # 认不出"已获得奖励"时，广告页至少等这么久再点关闭
PANIC_HIDDEN = [False]   # 被"连敲空格"隐藏过：此时不要自动还原窗口

# 控制台输出走独立线程 + 有界队列。
# 原因：Windows 控制台一旦进入"标记/选择"模式（在窗口里点一下或拖选），
# 任何写 stdout 的调用都会被阻塞，主循环会跟着卡死。走队列后，
# 控制台卡住只会丢日志，不会影响脚本运行。
_CONSOLE_Q = queue.Queue(maxsize=500)


def _console_writer():
    while True:
        line = _CONSOLE_Q.get()
        if line is None:
            return
        try:
            print(line, flush=True)
        except Exception:
            pass


def disable_quickedit():
    """关闭控制台的快速编辑模式，避免误点进"选择"状态把程序卡住。"""
    try:
        k32 = ctypes.windll.kernel32
        h = k32.GetStdHandle(-10)          # STD_INPUT_HANDLE
        mode = wintypes.DWORD()
        if k32.GetConsoleMode(h, ctypes.byref(mode)):
            ENABLE_QUICK_EDIT = 0x0040
            ENABLE_EXTENDED_FLAGS = 0x0080
            new = (mode.value | ENABLE_EXTENDED_FLAGS) & ~ENABLE_QUICK_EDIT
            k32.SetConsoleMode(h, new)
            return True
    except Exception:
        pass
    return False


def log(msg):
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d ')}{line}\n")
    except OSError:
        pass
    try:
        _CONSOLE_Q.put_nowait(line)
    except queue.Full:
        pass


_EXIT_HANDLER_REF = None


def install_exit_handler():
    """捕获关闭窗口/注销/关机事件，尽量写日志并释放电源请求。

    注意：即使进程被强制结束，Windows 也会自动回收电源请求，不会留下
    "系统永不睡眠"的副作用；这里主要目的是留下一条退出记录。
    """
    global _EXIT_HANDLER_REF
    handler_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)

    def _on_ctrl(ctrl_type):
        # 0=Ctrl+C 1=Ctrl+Break 交给 Python 默认处理（走 KeyboardInterrupt -> finally）
        # 2=关窗口 5=注销 6=关机 这几类系统会强杀进程，先记一笔
        if ctrl_type not in (2, 5, 6):
            return False
        try:
            game.keep_awake(False)
            # 关窗口时把小程序一起最小化，别让它留在屏幕上
            try:
                hwnd, _ = game.find_window()
                ctypes.windll.user32.ShowWindow(hwnd, 6)   # SW_MINIMIZE
            except Exception:
                pass
            log(f"收到系统关闭事件 (ctrl={ctrl_type})，小程序已最小化，退出")
        finally:
            os._exit(0)

    _EXIT_HANDLER_REF = handler_type(_on_ctrl)
    game.kernel32.SetConsoleCtrlHandler(_EXIT_HANDLER_REF, True)


def start_panic_watcher(hwnd, con_hwnd, presses=3, window=0.9, max_gap=0.25, cooldown=2.0):
    """短时间连敲空格 -> 立刻把小程序和控制台都最小化（相当于"一键隐藏"）。

    用 GetAsyncKeyState 轮询实现：不需要键盘钩子、不需要管理员权限，
    独立线程 30ms 一次，CPU 占用可以忽略。

    防误触设计（默认 3 次）：
      - 3 次全部落在 window 秒内（默认 0.9s）
      - 相邻两次间隔不超过 max_gap（默认 0.25s）——"哒哒哒"快速连敲才算，
        正常打字时空格间隔一般 300ms 以上，不会被这里抓住
      - 触发后 cooldown 秒内不再响应
    """
    user32 = ctypes.windll.user32
    VK_SPACE = 0x20

    def _loop():
        down = False
        hits = []
        last_trigger = 0.0
        while True:
            try:
                now = time.time()
                state = user32.GetAsyncKeyState(VK_SPACE) & 0xFFFF
                pressed = bool(state & 0x8000)
                if pressed and not down:
                    down = True
                    if hits and (now - hits[-1]) > max_gap:
                        hits = []
                    hits.append(now)
                    hits = [t for t in hits if now - t <= window]
                    if len(hits) >= presses and (now - last_trigger) > cooldown:
                        hits = []
                        last_trigger = now
                        try:
                            user32.ShowWindow(hwnd, 6)          # SW_MINIMIZE 小程序
                        except Exception:
                            pass
                        PANIC_HIDDEN[0] = True
                        # 只最小化游戏窗口，控制台不动（两者已解耦）
                        log("检测到连续敲空格 -> 小程序已最小化（脚本暂停，还原后继续）")
                elif (not pressed) and down:
                    down = False
            except Exception:
                pass
            time.sleep(0.03)

    th = threading.Thread(target=_loop, name="panic-watcher", daemon=True)
    th.start()
    return th


def find_map_btn(img):
    """模板定位顶部「地图」按钮，返回图标中心。

    窗口尺寸/比例变化会让图标位置平移、模板得分下降，
    所以阈值放低到 0.55，真正的确认交给"地图有没有打开"。
    """
    if "map_btn" not in bot.T:
        return None
    for name in ("map_btn2", "map_btn"):
        if name not in bot.T:
            continue
        s, box = vision.find(vision.to_gray(img), bot.T[name],
                             region=(280, 90, 415, 215), threshold=0.80)
        if box:
            return vision.center(box)
    return None

def ensure_main_page(hwnd, args, close_pts):
    """确保当前在餐厅主界面。

    每一轮依次尝试：红 X -> 面板外侧空白 -> 已知退出点 -> 地图->餐厅，
    不再"某一招失败就原地重试"，避免卡死在同一个动作上。
    """
    if not isinstance(close_pts, list):
        close_pts = [close_pts]
    exit_pt = close_pts[-1]

    for _round in range(3):
        img = game.capture(hwnd)
        if bot.bottom_bar_visible(img):
            return True
        rgb = np.asarray(img.convert("RGB"))

        # 0) 先处理广告页/广告确认框，否则后面的点击都落在广告上
        st, act, _ = bot.decide(img)
        if st == "give_up_ad" and act:
            log("    广告未看完 -> 点「继续」接着看（不放弃）")
            do_click(hwnd, act[1], not args.click, allow_bottom=True,
                     idle_seconds=args.idle_seconds, guard_ad=False)
            time.sleep(1.4)
            continue
        if st == "ad_done" and act:
            log("    广告已看完 -> 点关闭")
            do_click(hwnd, act[1], not args.click, allow_bottom=True,
                     idle_seconds=args.idle_seconds, guard_ad=False)
            time.sleep(1.4)
            continue
        if bot.ad_page(img):
            log("    当前是广告页，等待结束（不点击）")
            time.sleep(2.5)
            continue
        # 奖励/推荐礼包之类的中央弹框：点黄色确认按钮（确定/好的/领取）
        if st == "modal_other" and act:
            log("    检测到中央弹框 -> 点确认按钮")
            do_click(hwnd, act[1], not args.click, allow_bottom=True,
                     idle_seconds=args.idle_seconds)
            time.sleep(1.4)
            continue

        # 1) 红 X
        rx = bot.find_red_x(rgb)
        if rx:
            log(f"    点红 X {rx}")
            do_click(hwnd, rx, not args.click, allow_bottom=True,
                     idle_seconds=args.idle_seconds)
            time.sleep(1.2)

        # 2) 面板外侧空白（避开底部横幅）
        if bot._white_panel(rgb) > 0.25:
            log("    点面板左上角空白关闭")
            do_click(hwnd, (50, 110), not args.click, allow_bottom=True,
                     idle_seconds=args.idle_seconds)
            time.sleep(1.2)

        # 3) 已知退出点（水下视角、子页面）
        log(f"    尝试退出点 {exit_pt}")
        do_click(hwnd, exit_pt, not args.click, allow_bottom=True,
                 idle_seconds=args.idle_seconds)
        time.sleep(1.4)
        if bot.bottom_bar_visible(game.capture(hwnd)):
            return True

        # 4) 地图 -> 餐厅（必须先确认地图真的打开了）
        mp = find_map_btn(game.capture(hwnd)) or (326, 127)
        opened = False
        for _m in range(3):
            do_click(hwnd, mp, not args.click, allow_bottom=True,
                     idle_seconds=args.idle_seconds)
            time.sleep(2.2)
            chk = game.capture(hwnd)
            if "scene_map" in bot.T:
                s, box = vision.find(vision.to_gray(chk), bot.T["scene_map"],
                                     threshold=0.75)
                if box:
                    opened = True
                    break
            else:
                opened = True
                break
        if opened:
            do_click(hwnd, SCENE_RESTAURANT, not args.click, allow_bottom=True,
                     idle_seconds=args.idle_seconds)
            time.sleep(3.0)
        else:
            log("    地图没打开，跳过「餐厅」点击")

    return bot.bottom_bar_visible(game.capture(hwnd))

def clear_ad_if_any(hwnd, args, wait=None):
    """如果当前在广告页，先把广告处理掉。

    返回 True 表示"现在不在广告页了"，可以去点游戏界面。
    逻辑：认得出已获得奖励就立刻关；否则最多等 wait 秒让广告自然播完，
    再走通用关闭（点右上角）。
    """
    if wait is None:
        wait = AD_WAIT_SECONDS
    img = game.capture(hwnd)
    if not (bot.ad_page(img) or bot.ad_landing(img)):
        return True

    log("    当前在广告页，先处理广告")
    t0 = time.time()
    while time.time() - t0 < wait:
        st, act, _ = bot.decide(img)
        if st == "ad_done" and act:
            do_click(hwnd, act[1], not args.click, allow_bottom=True,
                     idle_seconds=args.idle_seconds, guard_ad=False)
            time.sleep(1.4)
        elif st == "give_up_ad" and act:
            # 「暂未获得奖励」-> 点继续，接着看
            do_click(hwnd, act[1], not args.click, allow_bottom=True,
                     idle_seconds=args.idle_seconds, guard_ad=False)
            time.sleep(1.4)
        time.sleep(2.0)
        img = game.capture(hwnd)
        if not (bot.ad_page(img) or bot.ad_landing(img)):
            log("    广告已结束")
            return True
    return close_ad_page(hwnd, args)

def goto_scene(hwnd, args, name, cell_tpl, fallback_pt):
    """通过「地图 -> 目标场景」切换场景。

    cell_tpl：地图上该场景格子的模板名（找不到就点 fallback_pt）。
    返回是否成功（以底部菜单栏出现为准）。
    """
    try:
        # 广告页上除了右上角关闭什么都不能点 —— 先把它清掉再谈切场景
        if not clear_ad_if_any(hwnd, args):
            log(f"    {name}：广告关不掉，放弃本次切换")
            return False

        # 水下视角（鱼塘里点进水面后）没有地图按钮，必须先点左下角红叉退出来
        for _ in range(2):
            img0 = game.capture(hwnd)
            if not nav_check(img0, "underwater"):
                break
            rx = bot.find_red_x(np.asarray(img0.convert("RGB")))
            if not rx:
                break
            log(f"    {name}：在水下视角，点红叉 {rx} 退出")
            do_click(hwnd, rx, not args.click, allow_bottom=True,
                     idle_seconds=args.idle_seconds, ignore_forbidden=True)
            time.sleep(1.5)

        if not open_scene_map(hwnd, args, name):
            log(f"    {name}：地图打不开")
            return False
        img = game.capture(hwnd)
        pt = fallback_pt
        if cell_tpl in bot.T:
            s, box = vision.find(vision.to_gray(img), bot.T[cell_tpl], threshold=0.80)
            if box:
                pt = vision.center(box)
        log(f"    {name}：点场景格子 {pt}")
        do_click(hwnd, pt, not args.click, allow_bottom=True,
                 idle_seconds=args.idle_seconds)
        time.sleep(3.0)
        # 万一地图还开着（本来就在该场景时点格子不生效），点空白关掉
        img2 = game.capture(hwnd)
        if "scene_map" in bot.T:
            s2, _ = vision.find(vision.to_gray(img2), bot.T["scene_map"], threshold=0.75)
            if s2 >= 0.75:
                do_click(hwnd, (30, 400), not args.click, allow_bottom=True,
                         idle_seconds=args.idle_seconds)
                time.sleep(1.2)
        return bot.bottom_bar_visible(game.capture(hwnd))
    except Exception as e:
        log(f"    {name}：出错 {e}")
        return False


def goto_restaurant(hwnd, args, name="切回餐厅"):
    """回到餐厅主界面。"""
    return goto_scene(hwnd, args, name, "cell_restaurant", (215, 400))



def nav_check(img, kind):
    """判断当前画面是否符合某一步的预期。"""
    if not kind:
        return True
    gray = vision.to_gray(img)
    if kind == "scene_map":
        if "scene_map" not in bot.T:
            return True
        s, box = vision.find(gray, bot.T["scene_map"], threshold=0.75)
        return box is not None
    if kind == "left_map":
        if "scene_map" not in bot.T:
            return True
        s, box = vision.find(gray, bot.T["scene_map"], threshold=0.75)
        return box is None
    if kind == "underwater":
        # 「下钩」（有鱼饵）和「免费」（鱼饵为 0）两种按钮都算水下视角
        for name in ("underwater_hook", "underwater_free"):
            if name not in bot.T:
                continue
            s, box = vision.find(gray, bot.T[name], threshold=0.80)
            if box:
                return True
        return ("underwater_hook" not in bot.T) and ("underwater_free" not in bot.T)
    if kind == "panel_any":
        return bot._white_panel(np.asarray(img.convert("RGB"))) > 0.25
    if kind in ("title_bait", "title_bottle"):
        return panel_is_open(img, kind)
    return True


def open_scene_map(hwnd, args, name):
    """打开场景地图：依次尝试多个候选点，直到地图真的打开。

    地图图标在不同场景里的可点位置差十几像素（实测大厅 326,127 / 露台 332,149），
    所以先用模板定位图标，再依次试"中心 / 偏移点 / 固定点"。
    """
    img = game.capture(hwnd)
    # 固定点最稳（地图图标基本固定在这个位置），模板定位只作补充
    pts = [(347, 146), (345, 145), (326, 127)]
    for _mb in ("map_btn2", "map_btn"):
        if _mb not in bot.T:
            continue
        s, box = vision.find(vision.to_gray(img), bot.T[_mb],
                             region=(280, 90, 415, 215), threshold=0.70)
        if box:
            cx, cy = vision.center(box)
            for _p in ((int(cx), int(cy)), (int(cx), int(cy) - 22)):
                if _p not in pts:
                    pts.append(_p)
            break
    for round_idx in range(2):
        if round_idx > 0:
            log("    地图仍未打开，等 2.5 秒再试一轮（可能还在切换动画）")
            time.sleep(2.5)
        for pt in pts:
            log(f"    {name} 打开地图：点击 {pt}")
            do_click(hwnd, pt, not args.click, allow_bottom=True,
                     idle_seconds=args.idle_seconds)
            time.sleep(2.2)
            if nav_check(game.capture(hwnd), "scene_map"):
                log("    地图已打开")
                return True
    return False

def run_nav_steps(hwnd, args, name, steps, checks, cells=None):
    """按顺序点每一步，每一步都校验画面是否符合预期，不符合就重试该步。

    cells 与 steps 对齐：某项给了模板名，就用模板在画面上定位目标，
    不再使用写死的坐标（地图格子在不同场景下位置会平移）。
    """
    total = len(steps)
    for idx, pt in enumerate(steps, 1):
        kind = checks[idx - 1] if idx - 1 < len(checks) else None
        cell = cells[idx - 1] if cells and idx - 1 < len(cells) else None
        ok = False
        for attempt in range(3):
            use_pt = pt
            # 重试时按小偏移微调，窗口尺寸变化会导致固定坐标差几像素
            if attempt == 1 and isinstance(pt, tuple) and len(pt) == 2:
                use_pt = (pt[0] + 8, pt[1] + 8)
            elif attempt == 2 and isinstance(pt, tuple) and len(pt) == 2:
                use_pt = (pt[0] - 8, pt[1] - 8)
            if cell and cell in bot.T:
                s0, b0 = vision.find(vision.to_gray(game.capture(hwnd)),
                                     bot.T[cell], threshold=0.85)
                if b0:
                    use_pt = vision.center(b0)
                    log(f"      模板定位 {cell} -> {use_pt}")
            log(f"    {name} 导航第 {idx}/{total} 步：点击 {use_pt}"
                + ("" if attempt == 0 else f"（重试 {attempt + 1}/3）"))
            do_click(hwnd, use_pt, not args.click, allow_bottom=True,
                     idle_seconds=args.idle_seconds)
            time.sleep(2.2)
            img = game.capture(hwnd)
            if nav_check(img, kind):
                ok = True
                break
            # 地图还开着且点格子没反应 —— 说明本来就在目标场景，点空白关掉地图即可
            s_map = 0.0
            if "scene_map" in bot.T:
                s_map, _ = vision.find(vision.to_gray(img), bot.T["scene_map"],
                                       threshold=0.0)
            if s_map > 0.75:
                log("      地图仍开着（可能已在该场景），点空白关闭地图")
                do_click(hwnd, (30, 400), not args.click, allow_bottom=True,
                         idle_seconds=args.idle_seconds)
                time.sleep(1.8)
                if nav_check(game.capture(hwnd), kind):
                    ok = True
                    break
            log(f"      第 {idx} 步没有达到预期画面（{kind}）")
            time.sleep(1.0)
        if not ok:
            return False
    return True

def open_task_page(hwnd, args, name, coords):
    """打开任务所在的面板。支持一步（entry）或多步导航（nav）。

    导航每一步都会切换场景（有加载动画），所以每步等 2.5 秒；
    每次尝试前都先确保回到餐厅主界面——地图图标在别的场景位置会偏移。
    """
    check = coords.get("check", "not_main")
    close_pts = coords["close"]
    if not isinstance(close_pts, list):
        close_pts = [close_pts]

    for attempt in range(2):
        if attempt > 0:
            log(f"  {name}：第 {attempt + 1} 次尝试")
            try:
                # 只点第一个点：它负责关掉面板，避免误点底部横幅
                do_click(hwnd, close_pts[0], not args.click, allow_bottom=True,
                         idle_seconds=args.idle_seconds)
                time.sleep(1.5)
            except Exception:
                pass

        if coords.get("nav"):
            if not ensure_main_page(hwnd, args, close_pts):
                log(f"  {name}：回不到餐厅主界面，跳过该任务")
                return False
            steps = list(coords["nav"])
            checks = list(coords.get("checks", []))
            log(f"  {name}：导航到目标页面（{len(steps)} 步）")
            # 第一步单独处理：逐个候选位置尝试打开地图
            if not open_scene_map(hwnd, args, name):
                log(f"  {name}：地图打不开，重来一轮")
                continue
            cells_all = coords.get("cells")
            cells_rest = list(cells_all)[1:] if cells_all else None
            if not run_nav_steps(hwnd, args, name, steps[1:], checks[1:],
                                 cells=cells_rest):
                log(f"  {name}：导航没能走到目标页面，重来一轮")
                continue
            time.sleep(1.5)
        else:
            log(f"  {name}：打开面板 {coords['entry']}")
            do_click(hwnd, coords["entry"], not args.click, allow_bottom=True,
                     idle_seconds=args.idle_seconds)

        # 面板有加载动画，最多等 10 秒确认它真的开了
        t0 = time.time()
        while time.time() - t0 < 10.0:
            time.sleep(0.8)
            if panel_is_open(game.capture(hwnd), check):
                return True
    return False


def run_one_task(hwnd, args, name, times, coords):
    """执行一个"看广告领取"任务，最多 times 次；点了没广告就认为次数用完。"""
    item, close = coords["item"], coords["close"]

    if not open_task_page(hwnd, args, name, coords):
        log(f"  {name}：面板没有打开，跳过该任务")
        return
    time.sleep(0.8)

    # retries：点了没弹广告时连续失败几次才放弃。
    # 鱼饵没有每日上限，偶尔点不出来是广告没加载好，重试即可；
    # 钻石每天固定次数，点不出来基本就是领完了，所以只试 1 次。
    retries = coords.get("retries", 3)
    done = 0
    fails = 0
    attempt = 0
    max_attempt = times * (retries + 1) + 3
    while done < times and attempt < max_attempt:
        attempt += 1
        log(f"  {name} 第 {done + 1}/{times} 次：点击 {item}")
        do_click(hwnd, item, not args.click, allow_bottom=True,
                 idle_seconds=args.idle_seconds)

        if not wait_for_ad(hwnd, 5.0):
            fails += 1
            if fails >= retries:
                if retries <= 1:
                    log(f"  {name}：点击后没有出现广告（已领完或已达上限），结束该任务")
                else:
                    log(f"  {name}：连续 {fails} 次都没弹出广告，结束该任务")
                break
            log(f"  {name}：广告没弹出来（第 {fails}/{retries} 次），2 秒后重试")
            time.sleep(2.0)
            continue

        fails = 0
        log(f"  {name}：广告已弹出，等待播完")
        result = finish_one_ad(hwnd, args)
        if not result:
            log(f"  {name}：本次广告没等到结束，不计入次数，继续下一次")
            time.sleep(1.5)
            continue
        if result != "reward":
            log(f"  {name}：本次没有拿到奖励，不计入次数，继续下一次")
            time.sleep(1.5)
            continue
        done += 1
        log(f"  {name}：第 {done} 次完成")
        time.sleep(1.2)

    log(f"  {name}：共完成 {done} 次，退出页面")
    # 如果还卡在广告里（加载中或播放中），先等它结束，别把退出点击打到广告上
    for _ in range(20):
        chk = game.capture(hwnd)
        dark = float(np.asarray(chk.convert("L")).mean()) < 45
        if not (bot.ad_page(chk) or dark or bot.decide(chk)[0] == "ad_playing"):
            break
        time.sleep(2.0)
    # close 可以是一个坐标，也可以是多个（按顺序点，逐步退出层层页面）
    pts = close if isinstance(close, list) else [close]
    for pt in pts:
        do_click(hwnd, pt, not args.click, allow_bottom=True,
                 idle_seconds=args.idle_seconds)
        time.sleep(1.4)
    # 再补一次就够（多退出会点到场景里的东西）
    if not bot.bottom_bar_visible(game.capture(hwnd)):
        do_click(hwnd, pts[-1], not args.click, allow_bottom=True,
                 idle_seconds=args.idle_seconds)
        time.sleep(1.4)


def run_extra_rewards(hwnd, args):
    """启动后、进入正常循环前执行的「额外奖励领取」。

    顺序：钻石 N 次 -> 鱼饵 N 次 -> 瓶子 N 次；某项为 0 则跳过。
    """
    cfg = load_extra_config()
    plan = [("钻石", cfg["diamond_times"]),
            ("鱼饵", cfg["bait_times"]),
            ("瓶子", cfg["bottle_times"])]
    if all(n <= 0 for _, n in plan):
        log("额外奖励领取：三项次数均为 0，跳过")
        return
    log("=== 额外奖励领取开始 ===")

    # 起始状态必须是餐厅主界面，否则后面的导航会点错位置
    if not ensure_main_page(hwnd, args, [(375, 300), (35, 130)]):
        log("  回不到餐厅主界面，额外奖励任领取跳过")
        return

    for name, times in plan:
        if times <= 0:
            log(f"  {name}：次数为 0，跳过")
            continue
        coords = EXTRA_ENTRY.get(name)
        if not coords:
            log(f"  {name}：坐标还没配置，先跳过")
            continue
        try:
            run_one_task(hwnd, args, name, times, coords)
        except Exception as e:
            log(f"  {name}：执行出错 {e}")
        # 任务之间留点缓冲，等游戏从上一个面板切回来
        time.sleep(2.5)
    log("=== 额外奖励领取结束，转入正常循环 ===")


FORBIDDEN_POINTS = [
    ((390, 140), 16),   # 右上角「展开更多按钮」的图标：点了会展开/收起其他按钮
    ((62, 738), 22),    # 左下角「菜单」按钮：点了会弹出菜单框
]


def is_forbidden(xy, radius_extra=0):
    try:
        x, y = int(xy[0]), int(xy[1])
    except Exception:
        return False
    for (fx, fy), r in FORBIDDEN_POINTS:
        if abs(x - fx) <= r + radius_extra and abs(y - fy) <= r + radius_extra:
            return True
    return False


def do_click(hwnd, xy, dry, allow_bottom=True, idle_seconds=3.0, guard_ad=True,
             ignore_forbidden=False):
    """点击。allow_bottom 默认 True——弹框按钮常在最底部（y≈682）；
    只有地面掉落物那条路径会显式传入 bottom_bar_visible 来控制禁区。

    guard_ad：默认 True —— 当前画面是广告页就拒绝点击。
    广告播放中随便点一下都可能跳到别的广告/落地页，这是硬性保护。
    已经确认在主界面、或明确要点"关闭广告"的调用可以传 False。
    """
    x, y = xy
    # 游戏窗口最小化 = 脚本暂停：原地等用户还原窗口，
    # 绝不自己把窗口弹出来（否则"连敲空格隐藏"会失效）。
    # 控制台窗口的最小化/还原与这里无关，完全不影响脚本。
    try:
        if ctypes.windll.user32.IsIconic(hwnd):
            log("  [暂停] 小程序已最小化，等你还原窗口…")
            while ctypes.windll.user32.IsIconic(hwnd):
                time.sleep(0.5)
            PANIC_HIDDEN[0] = False
            log("  [继续] 小程序已还原，脚本继续")
    except Exception:
        pass

    if (not ignore_forbidden) and is_forbidden((x, y)):
        log(f"  ! 跳过 ({x},{y})：这是禁止点击的图标/按钮")
        return False
    if y > 640 and not allow_bottom:
        log(f"  ! 跳过 ({x},{y})：底部禁区")
        return False
    if guard_ad:
        try:
            cur = game.capture(hwnd)
            on_ad = bot.ad_page(cur) or bot.ad_landing(cur)
            if on_ad:
                # 广告页上只允许点右上角「关闭」，别的地方一律不点
                if not (x >= 330 and y <= 110):
                    log(f"  ! 跳过 ({x},{y})：当前是广告页，只能点右上角「关闭」")
                    return False
        except Exception:
            pass
    # 底部若是广告横幅，点一下会跳到广告主页面 —— 任何情况下都不点
    if y > 600:
        try:
            if bot.bottom_banner(game.capture(hwnd)):
                log(f"  ! 跳过 ({x},{y})：底部是广告横幅，点了会跳转")
                return False
        except Exception:
            pass

    try:
        if not ctypes.windll.user32.IsIconic(hwnd):
            PANIC_HIDDEN[0] = False   # 窗口已恢复正常，解除隐藏锁
    except Exception:
        pass

    if dry:
        log(f"  > [dry] 点击 ({x},{y})")
        return True
    # 用户正在操作鼠标时让路，等光标静止 idle_seconds 再继续
    game.wait_for_idle(
        idle_seconds,
        on_pause=lambda: log("  [暂停] 检测到鼠标被移动，等你操作完"),
        on_resume=lambda: log("  [继续] 鼠标静止 %.0fs，脚本恢复" % idle_seconds),
    )
    sx, sy, fg = game.click(hwnd, x, y, delay=0.15)
    log(f"  > 点击 ({x},{y}) -> ({sx},{sy}) fg={fg}")
    global LAST_INPUT
    LAST_INPUT = time.time()
    return True


# 广告页「关闭」按钮的位置：实测 7 种不同的广告样式，按钮始终在
# x 355~400 / y 60~95 这个范围内，中心约 (378, 80)。
CLOSE_CANDIDATES = [(378, 80), (370, 82), (383, 84), (372, 76)]


def close_ad_page(hwnd, args, tries=3):
    """通用关闭广告页：依次点几个候选位置，每次点完复查是否离开广告页。

    不依赖任何模板 —— 广告样式千变万化，但"关闭"按钮的位置是固定的。
    返回 True 表示已经不在广告页上了。
    """
    for r in range(tries):
        for pt in CLOSE_CANDIDATES:
            if ctypes.windll.user32.IsIconic(hwnd):
                return False          # 窗口被最小化，等用户还原
            do_click(hwnd, pt, not args.click, allow_bottom=True,
                     idle_seconds=args.idle_seconds, guard_ad=False,
                     ignore_forbidden=True)
            time.sleep(1.3)
            chk = game.capture(hwnd)
            if not (bot.ad_page(chk) or bot.ad_landing(chk)):
                log(f"    广告已关闭（点 {pt}）")
                return True
        if r < tries - 1:
            log(f"    第 {r + 1} 轮关闭没成功，换位置再试")
            time.sleep(1.5)
    return False

def load_extra_config():
    """读取 config.ini 的 [extra_reward] 三项次数；缺失/非法一律按 0 处理。"""
    cfg = {"diamond_times": 0, "bait_times": 0, "bottle_times": 0}
    if not os.path.exists(CONFIG_FILE):
        log(f"未找到配置文件 {CONFIG_FILE}，额外奖励任务跳过")
        return cfg
    cp = configparser.ConfigParser()
    try:
        cp.read(CONFIG_FILE, encoding="utf-8-sig")   # 兼容带 BOM 的文件
    except Exception as e:
        log(f"读取 config.ini 失败: {e}")
        return cfg
    if cp.has_section("extra_reward"):
        for k in cfg:
            try:
                cfg[k] = max(0, int(cp.get("extra_reward", k, fallback="0")))
            except (ValueError, TypeError):
                cfg[k] = 0
    return cfg


# 三个任务的坐标（归一化帧 415x790）。
#   entry: 打开面板的按钮
#   nav  : 需要多步导航时，依次点击的点（替代 entry）
#   cells: 与 nav 对齐，某项给了模板名就用模板定位（地图格子会随窗口/场景平移）
#   item : 面板里"看广告领取"那一格的点击位置
#   close: 结束后的退出按钮（可多个，按顺序点）
#   check: 判定"面板已打开"的方式
#   retries: 点了没弹广告时连续失败几次才放弃
EXTRA_ENTRY = {
    # 钻石：点顶部礼包图标打开面板，点「💙x10 看广告额外获得」，退出点左侧红叉
    "钻石": {"entry": (160, 91), "item": (120, 548), "close": (40, 135),
             "check": "not_main", "retries": 1},
    # 鱼饵：地图 -> 鱼塘 -> 水面(进入水下视角) -> 鱼饵「+」
    "鱼饵": {"nav": [(321, 131), (113, 355), (200, 375), (233, 126)],
             "checks": ["scene_map", "left_map", "underwater", "title_bait"],
             "cells": [None, "cell_pond", None, None],
             "item": (280, 450), "close": [(375, 300), (26, 739)], "retries": 3,
             "check": "title_bait"},
    # 瓶子：地图 -> 露台 -> 点瓶子数量旁的「+」直接打开「购买瓶子」面板
    "瓶子": {"nav": [(326, 127), (293, 314), (233, 126)],
             "checks": ["scene_map", "left_map", "title_bottle"],
             "cells": [None, "cell_terrace", None],
             "item": (270, 410), "close": [(29, 677), (28, 750)], "retries": 3,
             "check": "title_bottle"},
}


def panel_is_open(img, mode):
    """判断目标面板是否已打开。

    mode 可以是面板标题模板名（title_bait / title_bottle），
    用标题文字精确确认，避免被别的大面板（比如设施面板）蒙混过关。
    """
    if mode in ("title_bait", "title_bottle"):
        if mode in bot.T:
            s, box = vision.find(vision.to_gray(img), bot.T[mode], threshold=0.85)
            return box is not None
        mode = "white_panel"
    if mode == "white_panel":
        return bot._white_panel(np.asarray(img.convert("RGB"))) > 0.25
    return not bot.bottom_bar_visible(img)


def wait_for_ad(hwnd, seconds=12.0):
    """点击后等待广告页出现。返回 True 表示确实弹广告了。

    广告经常加载很慢（黑屏好几秒），所以：
      - 等待时间给到 12 秒
      - 画面全黑 / 极暗 视为"还在加载"，继续等，不能判定为"没广告"
    """
    t0 = time.time()
    while time.time() - t0 < seconds:
        time.sleep(0.8)
        img = game.capture(hwnd)
        st = bot.decide(img)[0]
        # "ad_done" = 广告已播完的落地页，同样说明广告确实弹过了
        if bot.ad_page(img) or st in ("ad_playing", "ad_done"):
            return True
        if float(np.asarray(img.convert("L")).mean()) < 45:
            continue          # 黑屏加载中
        if bot.bottom_bar_visible(img):
            return False      # 已经在主界面了，确实没弹广告
    return False


def finish_one_ad(hwnd, args, timeout=90.0):
    """等当前这条广告播完并关闭。

    返回 "reward" 表示正常看完领到奖励；
    返回 "closed" 表示广告没了但没确认领到奖励（比如点了「放弃」）；
    返回 None 表示超时。
    """
    t0 = time.time()
    tried_close = False
    while time.time() - t0 < timeout:
        img = game.capture(hwnd)
        state, action, _ = bot.decide(img)

        # 「暂未获得奖励 是否继续观看视频」：点左边「放弃」，这次不算领取成功
        if state == "give_up_ad" and action:
            log("    弹出「暂未获得奖励」-> 点「继续」继续观看")
            do_click(hwnd, action[1], not args.click, allow_bottom=True,
                     idle_seconds=args.idle_seconds, guard_ad=False)
            time.sleep(1.5)
            continue

        if state == "ad_done" and action:
            do_click(hwnd, action[1], not args.click, allow_bottom=True,
                     idle_seconds=args.idle_seconds, guard_ad=False)
            tried_close = True
            time.sleep(1.6)
            # 复查：如果微信又弹出「是否继续观看」，说明这次没拿到奖励
            chk = game.capture(hwnd)
            st2, act2, _ = bot.decide(chk)
            if st2 == "give_up_ad" and act2:
                log("    关闭后弹出「暂未获得奖励」-> 点「继续」继续观看")
                do_click(hwnd, act2[1], not args.click, allow_bottom=True,
                         idle_seconds=args.idle_seconds, guard_ad=False)
                time.sleep(1.5)
                continue
            return "reward"

        if bot.ad_page(img) or state == "ad_playing":
            time.sleep(2.0)
            continue

        # 没有绿色按钮、但顶部还有「广告」标签：广告可能还没播完。
        # 至少等满 AD_WAIT_SECONDS（默认 60 秒）再点关闭，提前关会弹「暂未获得奖励」。
        if bot.ad_landing(img):
            if time.time() - t0 < AD_WAIT_SECONDS:
                time.sleep(2.0)
                continue
            log(f"    广告已播放 {int(time.time() - t0)} 秒，点右上角关闭")
            do_click(hwnd, (378, 76), not args.click, allow_bottom=True,
                     idle_seconds=args.idle_seconds, guard_ad=False)
            tried_close = True
            time.sleep(1.6)
            continue

        # 只有确实回到游戏画面（底部菜单栏出现 / 已知面板标题匹配）才算广告结束；
        # 否则可能只是广告还在加载（黑屏、白屏、过渡动画），要继续等。
        if time.time() - t0 > 4:
            back_to_game = (bot.bottom_bar_visible(img)
                            or panel_is_open(img, "title_bait")
                            or panel_is_open(img, "title_bottle"))
            if back_to_game:
                return "closed"
            time.sleep(1.5)
            continue

        if not tried_close and time.time() - t0 > 40:
            log("    广告超过 40 秒仍未识别到奖励，尝试点关闭")
            do_click(hwnd, (370, 82), not args.click, allow_bottom=True,
                     idle_seconds=args.idle_seconds, guard_ad=False)
            tried_close = True
            time.sleep(1.6)
            continue
        time.sleep(1.0)
    # 超时了也别直接放弃：如果还在广告页上，先点一次右上角关闭
    try:
        img = game.capture(hwnd)
        if bot.ad_page(img) or bot.ad_landing(img):
            log("    广告超时仍未识别结束，点右上角关闭")
            do_click(hwnd, (378, 76), not args.click, allow_bottom=True,
                     idle_seconds=args.idle_seconds, guard_ad=False)
            time.sleep(1.6)
            return "closed"
    except Exception:
        pass
    return None


def install_error_logging():
    """把未捕获异常写进 daemon.log —— 否则报错只出现在控制台窗口里，看不到。"""
    import traceback

    def _hook(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            return
        try:
            log("!!! 未捕获异常:\n" + "".join(
                traceback.format_exception(exc_type, exc, tb)))
        except Exception:
            pass

    sys.excepthook = _hook

def detect_items(img):
    """鱼干 + 虫子 + 白色物 + 地板异色块，统一返回 (kind, x, y, w, h, area)。"""
    arr = np.asarray(img.convert("RGB"))
    return itemscan.detect(arr)


def _pest_verified(gray, x, y, w, h):
    """用真实虫子模板二次校验，过滤掉"紫色眼镜"这类误判。"""
    tpl = bot.T.get("pest")
    if tpl is None:
        return True
    pad = 6
    x0 = max(0, x - pad)
    y0 = max(0, y - pad)
    x1 = min(gray.shape[1], x + w + pad)
    y1 = min(gray.shape[0], y + h + pad)
    if x1 <= x0 or y1 <= y0:
        return False
    s, _ = vision.find(gray[y0:y1, x0:x1], tpl, threshold=0.0)
    return s >= 0.70

def main():
    global LAST_INPUT
    ap = argparse.ArgumentParser()
    ap.add_argument("--click", action="store_true", help="真正点击")
    ap.add_argument("--phone-interval", type=float, default=900, help="手机宣传间隔秒（默认 900=15分钟）")
    ap.add_argument("--phone-clicks", type=int, default=40, help="每次连点几下（默认 40）")
    ap.add_argument("--phone-now", action="store_true", help="启动后立即执行一次宣传")
    ap.add_argument("--handle-popup", action="store_true",
                    help="启用后才会点击弹窗/广告里的按钮（默认关闭，只识别不点）")
    ap.add_argument("--click-ok", action=argparse.BooleanOptionalAction, default=True,
                    help="自动点奖励/通知弹框的黄色确认按钮（默认开；--no-click-ok 关闭）")
    ap.add_argument("--wait-min", type=float, default=60, help="两轮收取之间的最短等待秒（默认 60=1分钟）")
    ap.add_argument("--wait-max", type=float, default=300, help="最长等待秒（默认 300=5分钟）")
    ap.add_argument("--collect-seconds", type=float, default=180, help="每次连续收取持续秒（默认 180=3分钟）")
    ap.add_argument("--scan-interval", type=float, default=3.0, help="收取阶段每次扫描的间隔秒（默认 3）")
    ap.add_argument("--panic-presses", type=int, default=3,
                    help="连敲空格多少次触发一键最小化（默认 3）")
    ap.add_argument("--panic-window", type=float, default=0.9,
                    help="上述连敲必须落在这个秒数内（默认 0.9）")
    ap.add_argument("--tick", type=float, default=1.5, help="等待阶段轮询间隔秒")
    ap.add_argument("--idle-seconds", type=float, default=3.0,
                    help="鼠标静止多少秒后恢复脚本（默认 3）")
    ap.add_argument("--keepalive", type=float, default=240,
                    help="多久没有点击就动一下光标防锁屏（秒，默认 240）")
    ap.add_argument("--no-keep-awake", action="store_true",
                    help="不使用 SetThreadExecutionState 阻止息屏")
    ap.add_argument("--minutes", type=float, default=0, help="运行多少分钟后退出，0=一直跑")
    ap.add_argument("--tray", action="store_true",
                    help="使用系统托盘（默认关闭，保留控制台窗口）")
    ap.add_argument("--no-extra", action="store_true",
                    help="跳过启动时的「额外奖励领取」（钻石/鱼饵/瓶子）")
    args = ap.parse_args()

    # 控制台非阻塞输出 + 关闭快速编辑（否则在窗口里点一下就会把程序卡住）
    threading.Thread(target=_console_writer, name="console-writer", daemon=True).start()
    disable_quickedit()

    install_error_logging()
    install_exit_handler()
    bot.load_templates()
    # 找窗口：游戏没开/标题不对时先等一会儿再找，不要直接抛异常退出
    hwnd = title = None
    for attempt in range(6):
        try:
            hwnd, title = game.find_window()
            break
        except SystemExit as e:
            log(f"找窗口失败（第 {attempt + 1}/6 次）: {e}")
            time.sleep(5)
    if hwnd is None:
        log("多次都没找到游戏窗口，退出。请先打开动物餐厅小程序再启动脚本。")
        return 1
    if not args.no_keep_awake:
        game.keep_awake(True)
        log("已请求阻止息屏/睡眠（锁屏仍需系统策略配合）")
    log(f"启动 window={title!r} click={args.click} phone={args.phone_interval}s/{args.phone_clicks}clicks")

    # 启动自检：把"勉强能认出来"的模板记进日志，便于提前发现识别退化
    try:
        sc = bot.template_scores(game.capture(hwnd))
        weak = {k: round(v, 2) for k, v in sc.items() if 0.60 <= v < 0.92}
        if weak:
            log(f"模板自检（0.60~0.92 偏低的）: {weak}")
        else:
            log("模板自检：全部模板匹配良好")
    except Exception as e:
        log(f"模板自检失败: {e}")
    log(f"收取节奏：随机等 {args.wait_min:.0f}-{args.wait_max:.0f}s -> 连续收 {args.collect_seconds:.0f}s")

    t0 = time.time()
    next_phone = t0 if args.phone_now else t0 + args.phone_interval
    last_popup_action = 0.0
    last_popup_log = 0.0
    modal_since = None
    stuck_since = None
    last_scene_check = 0.0   # 上次确认"是不是餐厅"的时间
    last_tip_collect = 0.0   # 上次收小费的时间
    last_tip_log = 0.0
    empty_streak = 0      # 连续"没东西可捡"的次数
    in_kitchen = False    # 当前是不是在厨房场景
    ad_since = None
    last_tv_click = 0.0
    last_offer_click = 0.0
    con_hwnd = ctypes.windll.kernel32.GetConsoleWindow()
    log(f"控制台窗口句柄 con_hwnd={con_hwnd}")
    paused = False
    # 窗口状态"上一轮"快照：只对变化做出反应
    npc_streak = [0]
    prev_con_min = bool(ctypes.windll.user32.IsIconic(con_hwnd)) if con_hwnd else False
    prev_game_min = bool(ctypes.windll.user32.IsIconic(hwnd))
    # 启动时若小程序处于最小化状态，先还原它。
    # 否则双向同步会立刻把控制台也最小化，看起来像"闪退"。
    if ctypes.windll.user32.IsIconic(hwnd):
        ctypes.windll.user32.ShowWindow(hwnd, 9)   # SW_RESTORE
        log("启动时检测到小程序处于最小化状态，已自动还原")
        time.sleep(0.6)
    stats = {"items": 0, "phone": 0, "popup": 0, "rounds": 0, "rest": 0,
             "sheet": 0, "skipcard": 0, "tv": 0, "adclose": 0, "tip": 0, "tipad": 0,
             "refuse": 0, "story": 0, "redx": 0}

    # 收取节奏状态机
    phase = "wait"                                   # wait | collect
    phase_until = t0 + random.uniform(args.wait_min, args.wait_max)
    next_rest_check = t0

    # 系统托盘（可选，默认不用）
    tr = None
    try:
        with open(PID_FILE, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except OSError:
        pass
    if args.tray:
        try:
            tr = tray_mod.Tray("动物餐厅挂机 — 运行中（双击 stop.bat 可停止）", LOG)
            log("已创建系统托盘图标")
            tr.notify("脚本已启动", "托盘图标可能被收进溢出区（点任务栏的 ∧ 展开）。"
                                   "随时双击 stop.bat 可强制停止。")
            con = ctypes.windll.kernel32.GetConsoleWindow()
            if con:
                ctypes.windll.user32.ShowWindow(con, 0)   # SW_HIDE
                log("控制台已隐藏到托盘")
        except Exception as e:
            log(f"托盘创建失败，保持控制台显示: {e}")
            tr = None
    else:
        log("控制台模式（未启用托盘）；停止请按 Ctrl+C 或双击 stop.bat")

    # 连敲空格 = 一键隐藏（小程序 + 控制台同时最小化）
    try:
        start_panic_watcher(hwnd, con_hwnd,
                            presses=args.panic_presses,
                            window=args.panic_window)
        log(f"快捷键就绪：{args.panic_presses} 次快速连敲空格 -> 一键隐藏（同时最小化）")
    except Exception as e:
        log(f"快捷键监听启动失败: {e}")

    # 进入软件后的第一件事：额外奖励领取（钻石 -> 鱼饵 -> 瓶子）
    if not args.no_extra:
        try:
            run_extra_rewards(hwnd, args)
        except Exception as e:
            log(f"额外奖励领取出错，继续正常流程: {e}")

    try:
        while True:
            if args.minutes and time.time() - t0 > args.minutes * 60:
                log("达到运行时长，退出")
                break

            now = time.time()
            LAST_INPUT = LAST_INPUT or now

            # ---- 窗口策略（控制台与游戏完全解耦）
            #   游戏最小化 -> 脚本暂停；游戏恢复 -> 脚本继续
            #   控制台最小化/还原 -> 完全不影响脚本执行
            #   关闭控制台 -> 脚本结束并把游戏最小化（在退出处理里）
            #   连敲空格 -> 只最小化游戏（由 PANIC_HIDDEN 提前处理）
            game_min = bool(ctypes.windll.user32.IsIconic(hwnd))
            if game_min:
                if not paused:
                    paused = True
                    log("小程序已最小化 -> 脚本暂停（还原窗口后自动继续）")
            else:
                if paused:
                    paused = False
                    log("小程序已还原 -> 脚本继续")
            if paused:
                time.sleep(1.0)
                continue

            # 托盘消息泵：右键菜单、退出
            if tr is not None:
                tr.pump()
                if tr.quit_requested:
                    log("从托盘菜单退出")
                    break

            # ---- 空闲保活：长时间没有点击时轻微动一下光标，避免被判定为空闲而锁屏
            if now - LAST_INPUT > args.keepalive:
                game.nudge_cursor()
                LAST_INPUT = now
                log(f"超过 {args.keepalive:.0f}s 没有点击，发送一次微小移动防止锁屏")

            # 手机宣传：必须确认在主界面（底部操作栏可见 + 不是广告页）才点，
            # 否则会把广告画面当游戏，连点 40 下点到广告内容、打开新窗口
            if now >= next_phone:
                # 手机宣传只在餐厅页面点：先强制走一次「地图 -> 餐厅」。
                # 鱼塘/露台/花园底部也有「菜单」按钮，光看底栏判断不出场景。
                goto_restaurant(hwnd, args, "宣传前")
                pre = game.capture(hwnd)
                pre_state, _, _ = bot.decide(pre)
                on_main = bot.bottom_bar_visible(pre) and not bot.ad_page(pre)
                # tv_char 只是"电视头 NPC 站在店里"，不影响点宣传按钮，放行
                blocking = pre_state not in ("unknown", "tv_char")
                if blocking or not on_main:
                    reason = pre_state if blocking else "广告页/非主界面"
                    log(f"当前画面是 {reason}，手机宣传推迟 60 秒")
                    next_phone = now + 60
                    continue
                log(f"=== 手机宣传：连点 {args.phone_clicks} 下 ===")
                done = 0
                cut_reason = ""
                for i in range(args.phone_clicks):
                    if con_hwnd and ctypes.windll.user32.IsIconic(con_hwnd):
                        cut_reason = "控制台被最小化"
                        break
                    # 每 3 下复核一次：画面变了（弹出广告/弹框）立刻停手
                    if (i + 1) % 3 == 0:
                        chk = game.capture(hwnd)
                        if bot.ad_page(chk) or not bot.bottom_bar_visible(chk):
                            cut_reason = "画面已变（广告页/非主界面）"
                            break
                    do_click(hwnd, PHONE_AD, not args.click, allow_bottom=True,
                             idle_seconds=args.idle_seconds, guard_ad=False)
                    stats["phone"] += 1
                    done = i + 1
                    time.sleep(0.28)
                    # 每 3 下检查一次：客人排队过多就先不点了
                    if (i + 1) % 3 == 0:
                        if bot.queue_full(game.capture(hwnd)):
                            cut_reason = "排队客人过多"
                            break
                if cut_reason:
                    # 被中断的一轮不算完成，1 分钟后补做，不要白等一整个周期
                    log(f"本轮宣传中断（{cut_reason}），已点 {done}/{args.phone_clicks} 下，60 秒后补做")
                    next_phone = time.time() + 60
                else:
                    log(f"宣传连点完成 {args.phone_clicks} 下")
                    next_phone = time.time() + args.phone_interval
                log(f"下次宣传：{time.strftime('%H:%M:%S', time.localtime(next_phone))}")
                time.sleep(1.0)
                continue

            # ---- 阶段切换 ----
            if now >= phase_until:
                if phase == "wait":
                    phase = "collect"
                    stats["rounds"] += 1
                    log(f"=== 第 {stats['rounds']} 轮收取开始，持续 {args.collect_seconds:.0f}s ===")
                    # 先切回餐厅，切完再开始计时（切场景本身要十几秒）
                    goto_restaurant(hwnd, args, "本轮开始")
                    round_start = time.time()
                    phase_until = round_start + args.collect_seconds
                    scene_switched = False
                    in_kitchen = False
                else:
                    phase = "wait"
                    gap = random.uniform(args.wait_min, args.wait_max)
                    phase_until = now + gap
                    log(f"=== 收取结束，等待 {gap:.0f}s 后下一轮 ===")
                    time.sleep(1.0)
                    continue

            # ---- 等待阶段：不点地面，只顺手处理休息弹框 ----
            if phase == "wait":
                game.poll_cursor()
                if time.time() >= next_rest_check and game.is_foreground(hwnd):
                    img = game.capture(hwnd)
                    state, action, _ = bot.decide(img)
                    if state == "rest_popup" and action:
                        log("等待期间检测到休息提醒 -> 点击「行吧」")
                        do_click(hwnd, action[1], not args.click, idle_seconds=args.idle_seconds)
                        stats["rest"] += 1
                    next_rest_check = time.time() + 20
                time.sleep(args.tick)
                continue

            # ---- 收取阶段 ----
            img = game.capture(hwnd)
            state, action, score = bot.decide(img)

            # 广告提前关闭后微信的确认框：点「放弃」，绝不点「继续」
            if state == "give_up_ad" and action:
                log("广告未看完 -> 点「继续」接着看（不放弃）")
                do_click(hwnd, action[1], not args.click, idle_seconds=args.idle_seconds)
                time.sleep(1.3)
                continue

            if state != "npc_dialog":
                npc_streak[0] = 0

            # 连续在线休息提醒：必须点掉，否则整个游戏被挡住
            if state == "rest_popup" and action:
                log("检测到连续在线休息提醒 -> 点击「行吧」")
                do_click(hwnd, action[1], not args.click, idle_seconds=args.idle_seconds)
                stats["rest"] += 1
                time.sleep(1.5)
                continue

            # 微信广告跳过卡提示：点「不使用」（点「使用」会消耗卡片）
            if state == "skip_card" and action:
                log("检测到「是否使用广告跳过卡」-> 点击「不使用」")
                do_click(hwnd, action[1], not args.click, idle_seconds=args.idle_seconds)
                stats["skipcard"] += 1
                time.sleep(1.2)
                continue

            # 小费：数字达到 6 位就收，但两次收取之间至少间隔 15 分钟
            if state == "tip_ready" and action:
                if now - last_tip_collect < TIP_INTERVAL:
                    if now - last_tip_log > 300:
                        last_tip_log = now
                        log(f"小费已够（{score} 位数字），但距上次收取不足 {TIP_INTERVAL//60} 分钟，稍后再收")
                    time.sleep(1.0)
                    continue
                log(f"小费已达 {score} 位数字，点击收取")
                do_click(hwnd, action[1], not args.click, idle_seconds=args.idle_seconds)
                stats["tip"] += 1
                last_tip_collect = time.time()
                time.sleep(1.5)
                continue

            # 广告推销员弹窗：点「好呀」开始看广告（双倍鱼干）
            if state == "tv_offer" and action:
                log("检测到广告推销员 -> 点击「好呀」")
                do_click(hwnd, action[1], not args.click, idle_seconds=args.idle_seconds)
                stats["tv"] += 1
                time.sleep(1.5)
                continue

            # 主界面上出现电视头角色（带感叹号）：点它进入流程
            if state == "tv_char" and action:
                if now - last_tv_click < 6:
                    time.sleep(1.0)
                    continue
                log("发现广告推销员角色 -> 点击它")
                do_click(hwnd, action[1], not args.click, idle_seconds=args.idle_seconds)
                last_tv_click = time.time()
                stats["tv"] += 1
                time.sleep(2.0)
                continue

            # 「排队的客人有些多」：不点任何东西，等它自己消失
            if state == "queue_msg":
                if now - last_popup_log > 8:
                    log("「排队的客人有些多」提示，等它消失")
                    last_popup_log = now
                time.sleep(2.0)
                continue

            # 红色 X 关闭按钮：优先点它
            if state == "red_x_close" and action:
                log("检测到红色 X -> 点击关闭")
                do_click(hwnd, action[1], not args.click, idle_seconds=args.idle_seconds)
                stats["redx"] += 1
                time.sleep(1.2)
                continue

            # 剧情/跟随对话卡片：连续点画面中部即可跳过
            if state == "story_dialog" and action:
                log("检测到剧情对话框 -> 连续点击画面中部跳过")
                for _ in range(3):
                    do_click(hwnd, action[1], not args.click, idle_seconds=args.idle_seconds)
                    time.sleep(0.45)
                stats["story"] += 1
                continue

            # NPC 弹窗（通用）：左右两个按钮时，统一点左边第一个选项
            if state == "npc_dialog" and action:
                npc_streak[0] += 1
                if npc_streak[0] > 4:
                    log(f"连续 {npc_streak[0]} 次都在「NPC 弹窗」状态，疑似误判卡死，改为回主界面")
                    npc_streak[0] = 0
                    ensure_main_page(hwnd, args, [(375, 300), (26, 739)])
                    time.sleep(1.0)
                    continue
                log(f"检测到 NPC 弹窗 -> 点击左边第一个选项（第 {npc_streak[0]} 次）")
                do_click(hwnd, action[1], not args.click, idle_seconds=args.idle_seconds)
                stats["refuse"] += 1
                time.sleep(1.2)
                continue

            # NPC 弹窗（占卜师等）：只点拒绝类按钮，绝不点同意/购买
            if state == "refuse_dialog" and action:
                log("检测到 NPC 弹窗 -> 点击「拒绝」类按钮")
                do_click(hwnd, action[1], not args.click, idle_seconds=args.idle_seconds)
                stats["refuse"] += 1
                time.sleep(1.2)
                continue

            # 底部卡片（招客/感谢/跟随对话）：先点卡片本身，没关掉再点爪印
            if state == "bottom_sheet" and action:
                log("检测到底部卡片 -> 点击卡片关闭")
                do_click(hwnd, action[1], not args.click,
                         allow_bottom=True, idle_seconds=args.idle_seconds)
                time.sleep(1.2)
                st2, _, _ = bot.decide(game.capture(hwnd))
                if st2 == "bottom_sheet":
                    log("卡片仍在 -> 点爪印兜底")
                    do_click(hwnd, (375, 745), not args.click,
                             allow_bottom=True, idle_seconds=args.idle_seconds)
                    time.sleep(1.2)
                stats["sheet"] += 1
                continue

            # 广告看完（已获得奖励）：点「关闭」领取，并确认真的关掉了
            if state == "ad_done" and action:
                log("广告已看完 -> 点击「关闭」")
                for attempt in range(3):
                    do_click(hwnd, action[1], not args.click, idle_seconds=args.idle_seconds, guard_ad=False)
                    stats["adclose"] += 1
                    time.sleep(1.6)
                    chk = game.capture(hwnd)
                    # 只要不在广告页了就不再点，避免第二下点到落地页的"进入小游戏"
                    if not (bot.ad_page(chk) or bot.ad_landing(chk)):
                        break
                    if attempt < 2:
                        log("广告还没关掉，再点一次「关闭」")
                ad_since = None
                time.sleep(0.8)
                continue

            # 小费翻倍弹窗：点「鱼干 × 2」看广告拿双倍。
            # 这是核心功能，不受 --handle-popup 影响，必须点。
            if state == "tip_popup" and action:
                log(f"小费翻倍弹窗 -> 点「鱼干 × 2」{action[1]}")
                do_click(hwnd, action[1], not args.click, idle_seconds=args.idle_seconds)
                stats["tipad"] += 1
                last_popup_action = time.time()
                time.sleep(1.2)
                continue

            # 其他对话框 / 播放中的广告：只识别不点
            if state in ("tip_popup", "ad_playing", "modal_other"):
                auto_ok = (state == "modal_other" and args.click_ok
                           and action is not None and action[0] == "click_ok")
                if (args.handle_popup or auto_ok) and action and now - last_popup_action > 1.5:
                    log(f"弹窗处理：{state} -> {action[0]} @ {action[1]}")
                    do_click(hwnd, action[1], not args.click, idle_seconds=args.idle_seconds)
                    last_popup_action = time.time()
                    stats["popup"] += 1
                    time.sleep(1.0)
                    continue
                # 广告落地页上的内容卡片（白底 + "查看"按钮）：只能点右上角「关闭」。
                # 这类卡片会被当成未知弹框，但点空白会被广告页保护拦住，所以直接点关闭。
                if state == "modal_other" and action is None and bot.ad_landing(img):
                    if modal_since is None:
                        modal_since = now
                    elif now - modal_since > AD_WAIT_SECONDS and now - last_popup_action > 5:
                        log("广告页上的内容卡片 -> 点右上角「关闭」")
                        do_click(hwnd, (378, 76), not args.click, allow_bottom=True,
                                 idle_seconds=args.idle_seconds, guard_ad=False)
                        last_popup_action = time.time()
                        modal_since = None
                        time.sleep(1.5)
                        continue

                # 未知弹框没有可识别的确认按钮时，点"弹框外的左上空白"把它关掉。
                # 不要点下方：这类弹框下面常伴生广告横幅，点了会跳转。
                if state == "modal_other" and action is None:
                    if modal_since is None:
                        modal_since = now
                    elif now - modal_since > 6 and now - last_popup_action > 5:
                        log("未知弹框停留 6 秒，点击左上角空白处关闭")
                        do_click(hwnd, (50, 110), not args.click,
                                 allow_bottom=True, idle_seconds=args.idle_seconds)
                        last_popup_action = time.time()
                        modal_since = None
                        time.sleep(1.2)
                        continue
                # 广告播放：等够 40 秒还没结束就点右上角关闭，避免一直干等
                if state == "ad_playing":
                    if ad_since is None:
                        ad_since = now
                    elif now - ad_since > AD_WAIT_SECONDS:
                        log(f"    广告已播放 {int(now - ad_since)} 秒，点右上角关闭")
                        do_click(hwnd, (378, 76), not args.click, allow_bottom=True,
                                 idle_seconds=args.idle_seconds, guard_ad=False)
                        ad_since = None
                        time.sleep(1.6)
                        continue

                if now - last_popup_log > 8:
                    if state == "modal_other":
                        log("检测到未知弹框（奖励/通知等），按设置不点击 —— 需要你手动关")
                    elif state == "ad_playing":
                        log("广告播放中，等待结束…")
                    else:
                        log(f"检测到对话框（{state}），按设置不点击")
                    last_popup_log = now
                # 卡太久（多半是广告页的关闭按钮太淡没识别到）：兜底点右上角
                if state == "modal_other":
                    if modal_since is None:
                        modal_since = now
                    elif now - modal_since > 30 and now - last_popup_action > 20:
                        log("弹框停留超过 30 秒，尝试点击右上角关闭")
                        do_click(hwnd, (370, 82), not args.click, idle_seconds=args.idle_seconds)
                        last_popup_action = time.time()
                        modal_since = None
                        time.sleep(1.5)
                        continue
                time.sleep(1.5)
                continue
            else:
                modal_since = None
            # 每 15 秒确认一次"还在不在餐厅"（判据：顶部小费计数器只在餐厅出现）。
            # 不在餐厅才切过去 —— 比"每 90 秒无条件切一次"省事得多。
            if phase == "collect" and now - last_scene_check > 15:
                last_scene_check = now
                # 厨房是我们自己要去的场景，不能当成"跑偏"再切回去
                if ((not in_kitchen) and not bot.is_restaurant(img)
                        and not nav_check(img, "scene_map")
                        and not bot.ad_page(img) and not bot.ad_landing(img)
                        and bot.bottom_bar_visible(img)):
                    log("检测到不在餐厅场景 -> 通过地图返回餐厅")
                    goto_restaurant(hwnd, args, "切回餐厅")
                    in_kitchen = False
                    continue

            # 场景地图打开时绝不能捡"掉落物"：地图上的场景图标会被误判成掉落物，
            # 点上去就会进入别的场景（自助/花园/厨房等）。先点空白把地图关掉。
            if phase == "collect" and nav_check(img, "scene_map"):
                log("地图还开着 -> 点空白关闭地图（避免误点场景格子）")
                do_click(hwnd, (30, 400), not args.click, allow_bottom=True,
                         idle_seconds=args.idle_seconds)
                time.sleep(1.2)
                continue

            # 安全闸：只有确认在主界面（底部操作栏可见）才点掉落物，
            # 否则可能是广告页/全屏页，乱点会导致跳转
            if not bot.bottom_bar_visible(img):
                if bot.ad_page(img) or bot.ad_landing(img):
                    ad_since = ad_since or now
                    if now - ad_since > AD_WAIT_SECONDS:
                        log(f"广告已播放 {int(now - ad_since)} 秒，点击右上角「关闭」")
                        closed = False
                        for attempt in range(3):
                            do_click(hwnd, (370, 82), not args.click,
                                     idle_seconds=args.idle_seconds)
                            time.sleep(1.6)
                            chk = game.capture(hwnd)
                            if not (bot.ad_page(chk) or bot.ad_landing(chk)):
                                log("广告已关闭，回到餐厅主界面")
                                closed = True
                                break
                            if attempt < 2:
                                log("广告还没关掉，再点一次「关闭」")
                        if not closed:
                            log("多次点击仍未关闭广告，继续等待下一轮")
                        ad_since = None
                        continue
                    if now - last_popup_log > 8:
                        log(f"广告播放中（已 {int(now - ad_since)} 秒），等待结束，不点击广告画面")
                        last_popup_log = now
                    time.sleep(2.0)
                    continue

                # 广告加载阶段常常是纯黑屏，识别不出广告特征。
                # 这种全屏极暗画面一律当作广告等待，绝不能去点「设施/餐厅」。
                if float(np.asarray(img.convert("L")).mean()) < 45:
                    if now - last_popup_log > 8:
                        log("画面全黑（广告加载中），等待，不点击")
                        last_popup_log = now
                    time.sleep(2.0)
                    continue

                ad_since = None
                if stuck_since is None:
                    stuck_since = now
                elif now - stuck_since > 15:
                    # 通用兜底：不管卡在广告页、落地页、还是认不出的弹框，
                    # 右上角「关闭」的位置是固定的，直接点它。
                    log("在非游戏页面停留超过 15 秒 -> 通用关闭（点右上角）")
                    if close_ad_page(hwnd, args):
                        log("已关闭广告，回到游戏画面")
                    else:
                        log("通用关闭没成功，改走回主界面流程")
                        ensure_main_page(hwnd, args, [(375, 300), (26, 739)])
                    stuck_since = now
                    time.sleep(1.0)
                    continue
                if now - last_popup_log > 8:
                    log("当前不是主界面，等待（15 秒后会自动点右上角关闭）")
                    last_popup_log = now
                time.sleep(2.0)
                continue
            stuck_since = None
            ad_since = None

            # 场景轮换：一轮收取的前半段在餐厅，后半段切到厨房，
            # 这样两个场景的掉落物都能收到（"没东西可捡"这个判据不可靠，
            # 因为背景装饰经常被误判成掉落物，永远不"空"）。
            if (phase == "collect" and not scene_switched
                    and bot.bottom_bar_visible(img) and not bot.ad_landing(img)
                    and now - round_start > args.collect_seconds * 0.5):
                scene_switched = True
                log("本轮前半段在餐厅，现在切到厨房收")
                if goto_scene(hwnd, args, "去厨房", "cell_kitchen", (295, 480)):
                    in_kitchen = True
                    last_scene_check = time.time()
                time.sleep(0.6)
                continue

            boxes = detect_items(img)
            if boxes:
                empty_streak = 0
                bottom_ok = bot.bottom_bar_visible(img)
                kinds = {}
                for b in boxes:
                    kinds[b[0]] = kinds.get(b[0], 0) + 1
                log(f"发现 {len(boxes)} 个掉落物 {kinds}"
                    + ("" if bottom_ok else "（底部被广告遮挡，只点上半部）"))
                for (kind, x, y, w, h, area) in boxes[:12]:
                    if ctypes.windll.user32.IsIconic(hwnd):
                        break   # 游戏窗口被最小化，立刻停手
                    if kind == "pest" and not _pest_verified(vision.to_gray(img), x, y, w, h):
                        continue
                    cx = x + w // 2 + random.randint(-3, 3)
                    cy = y + h // 2 + random.randint(-3, 3)
                    do_click(hwnd, (cx, cy), not args.click,
                             allow_bottom=bottom_ok, idle_seconds=args.idle_seconds,
                             guard_ad=False)
                    stats["items"] += 1
                    time.sleep(0.18)

            else:
                empty_streak += 1   # 连续没东西可捡

            time.sleep(args.scan_interval)
    except KeyboardInterrupt:
        log("Ctrl+C 停止")
    finally:
        if tr is not None:
            try:
                tr.remove()
            except Exception:
                pass
        # 退出时把小程序最小化，避免留在屏幕上
        try:
            ctypes.windll.user32.ShowWindow(hwnd, 6)   # SW_MINIMIZE
        except Exception:
            pass
        try:
            os.remove(PID_FILE)
        except OSError:
            pass
        if not args.no_keep_awake:
            game.keep_awake(False)

    log(f"结束 stats={stats}")


if __name__ == "__main__":
    # 主流程异常时不直接退出：写进日志，等 8 秒后重新进入主流程。
    import traceback
    while True:
        try:
            rc = main()
            try:
                sys.stdout.flush()
            except Exception:
                pass
            # 用 os._exit 直接退出：避免后台线程（快捷键监听）在解释器收尾时
            # 争用 stdout 锁，报 "_enter_buffered_busy" 这种无意义错误
            os._exit(rc if isinstance(rc, int) else 0)
        except SystemExit:
            raise
        except KeyboardInterrupt:
            raise
        except Exception:
            try:
                log("!!! 主流程异常，8 秒后自动重启:\n" + traceback.format_exc())
            except Exception:
                pass
            time.sleep(8)
