"""动物餐厅：广告翻倍 / 关闭广告 状态机。

默认 dry-run（只识别不点击）。加 --click 才真正点击。
必须在普通权限下运行（低权限沙箱无法注入鼠标）。

用法:
  python bot.py                 # 只识别，打印状态
  python bot.py --click         # 真实点击
  python bot.py --click --duration 600
按 Ctrl+C 停止。
"""
import argparse
import os
import sys
import time

import numpy as np

import game
import tipocr
import vision

HERE = os.path.dirname(os.path.abspath(__file__))
TPL_DIR = os.path.join(HERE, "templates")

# 小费达到这个数就点一下收取
TIP_THRESHOLD = 290000

T = {}

# 底部广告横幅 / 落地页按钮所在区域，任何情况下都不点
BOTTOM_GUARD_Y = 640


def load_templates():
    for name in ("rest_ok", "dlg_ok", "skip_decline", "skip_title", "tv_yes", "tv_head",
                 "tip_label", "queue_full", "btn_menu", "pest", "card_paw", "claim_ok", "tv_npc5", "tv_npc6",
                 "refuse_no", "refuse_shut", "refuse_bule", "refuse_bule2",
                 "ad_close", "ad_reward", "ad_play", "tip_ad2", "tip_take",
                 "ad_reward2", "ad_close2", "ad_close3", "ad_continue", "ad_continue2", "dialog_resume", "scene_map", "map_btn", "map_btn2", "ad_chip", "title_bait", "title_bottle", "underwater_hook", "underwater_free", "ad_reward3", "ad_reward4", "ad_reward5", "ad_reward6", "ad_reward7", "ad_chip2", "ad_chip3", "ad_close4", "ad_close5", "ad_close6", "cell_kitchen", "cell_restaurant", "cell_terrace", "cell_pond"):
        p = os.path.join(TPL_DIR, f"{name}.png")
        if os.path.exists(p):
            T[name] = vision.load_gray(p)
    missing = [n for n in ("ad_close", "ad_reward", "tip_ad2") if n not in T]
    if missing:
        raise SystemExit(f"缺少模板: {missing}，请确认 templates/ 目录完整")


def _looks_yellow(rgb, x, y, r=5):
    """确认按钮位置确实是黄色，防止模板误匹配到别处。"""
    h, w = rgb.shape[:2]
    patch = rgb[max(0, y - r):min(h, y + r), max(0, x - r):min(w, x + r)].reshape(-1, 3)
    if len(patch) == 0:
        return False
    m = patch.mean(axis=0)
    return bool(m[0] > 170 and m[1] > 115 and m[0] - m[2] > 70)


def _dim_level(gray):
    """左边缘平均亮度：正常画面约 170，弹框会把整屏压暗（100 左右），
    连续在线休息提醒压得更狠（10 左右）。"""
    return float(gray[330:700, 0:45].mean())


def _white_panel(rgb):
    """中央区域"白色面板"占比：大弹框 0.65~0.72，正常画面 0~0.1。
    比"边缘亮度"可靠——正常场景里边缘可能本来就很暗。"""
    sub = rgb[290:610, 100:320]
    if sub.size == 0:
        return 0.0
    w = ((sub[..., 0] > 225) & (sub[..., 1] > 218) & (sub[..., 2] > 200))
    return float(w.mean())


def _button_row(gray):
    """按钮行（y 665~702）左右两段的最暗值，用来判断"两个按钮并排"的 NPC 弹窗。"""
    row = gray[665:702, :]
    if row.size == 0:
        return (255, 255)
    return (int(row[:, 70:200].min()), int(row[:, 225:365].min()))


def find_red_x(rgb):
    """找红色圆形关闭按钮（红色 X）。返回中心坐标或 None。"""
    a = rgb.astype(np.int16)
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    # 红 X 实测 (215,85,10)；橙色的「!」提示是 (198,115,55)，靠 g/b 通道区分
    mask = (r > 190) & (g < 100) & (b < 35) & (r - g > 110)
    if not mask.any():
        return None
    h, w = mask.shape
    seen = np.zeros_like(mask, dtype=bool)
    best = None
    ys, xs = np.nonzero(mask)
    for sy, sx in zip(ys, xs):
        if seen[sy, sx]:
            continue
        stack = [(sy, sx)]
        seen[sy, sx] = True
        cells = []
        while stack:
            cy, cx = stack.pop()
            cells.append((cy, cx))
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    ny, nx = cy + dy, cx + dx
                    if 0 <= ny < h and 0 <= nx < w and mask[ny, nx] and not seen[ny, nx]:
                        seen[ny, nx] = True
                        stack.append((ny, nx))
        if len(cells) < 160:
            continue
        cyy = [c[0] for c in cells]
        cxx = [c[1] for c in cells]
        bh = max(cyy) - min(cyy) + 1
        bw = max(cxx) - min(cxx) + 1
        if not (20 <= bw <= 60 and 20 <= bh <= 60):
            continue
        if len(cells) / float(bw * bh) < 0.45:
            continue
        # 红圈中间要有白色 X，否则只是红色装饰物
        sub = rgb[min(cyy):max(cyy) + 1, min(cxx):max(cxx) + 1]
        if sub.size == 0:
            continue
        white = ((sub[..., 0] > 210) & (sub[..., 1] > 200) & (sub[..., 2] > 190))
        if float(white.mean()) < 0.03:
            continue
        cx = (min(cxx) + max(cxx)) // 2
        cy = (min(cyy) + max(cyy)) // 2
        # 红 X 只出现在弹框/面板区域。底部那些橙色"!"角标本身就因
        # 颜色被排除了（g≈115 > 100），所以下限可以放宽到 y=760，
        # 否则「设施」面板这种位置偏低（y≈728）的关闭按钮会漏掉。
        # 红 X 出现在弹框/面板/水下视角的关闭按钮上（x≈40 也有）。
        # 底部左侧的橙色"!"角标颜色偏橙（g 较高），已被上面的颜色条件排除。
        if 80 <= cy <= 600:
            if not (15 <= cx <= 405):
                continue
        else:
            if not (15 <= cx <= 400 and cy <= 775):
                continue
        if best is None or len(cells) > best[2]:
            best = (cx, cy, len(cells))
    return (best[0], best[1]) if best else None


def _bottom_sheet(rgb):
    """底部是否被大块奶油色卡片占住（如"招客的多多"）。正常画面该区域是彩色场景。"""
    sub = rgb[615:785, 20:395]
    if sub.size == 0:
        return False
    m = ((np.abs(sub[..., 0].astype(int) - 247) < 14) &
         (np.abs(sub[..., 1].astype(int) - 241) < 14) &
         (np.abs(sub[..., 2].astype(int) - 226) < 16))
    return float(m.mean()) > 0.30


def template_scores(img):
    """返回 {模板名: 全图最高得分}，用于启动自检。

    目的：窗口/渲染变化时及时发现"某个模板变得勉强"，
    不用等它卡住某个功能才被发现。
    """
    gray = vision.to_gray(img)
    out = {}
    for name, tpl in T.items():
        try:
            s, _ = vision.find(gray, tpl, threshold=0.0)
            out[name] = s
        except Exception:
            pass
    return out

def decide(img):
    """返回 (状态, 动作或None, 置信度)。动作 = (名字, (x, y))。"""
    rgb = np.asarray(img.convert("RGB")) if not isinstance(img, np.ndarray) else img
    gray = vision.to_gray(rgb)

    # 连续在线休息提醒：能匹配到「行吧」就算（不再依赖整屏亮度）
    if "rest_ok" in T:
        s, box = vision.find(gray, T["rest_ok"], threshold=0.78)
        if box:
            return "rest_popup", ("rest_ok", vision.center(box)), round(s, 3)

    # 广告已播完的落地页：顶部显示「已获得奖励」（播放中显示「试玩N秒获得奖励」）。
    # 这种页面底部常常还有一个绿色「进入小游戏」大按钮，不能误判成"广告还在播"。
    # 顶部「广告」旁边由"倒计时"变成"已获得奖励"这类字样 = 奖励已到手。
    # 不同广告的背景色不同，所以准备了多种样式的模板，命中任意一个即可。
    for _rw in ("ad_reward7", "ad_reward6", "ad_reward5", "ad_reward4", "ad_reward3"):
        if _rw not in T:
            continue
        s, box = vision.find(gray, T[_rw], region=(40, 40, 280, 110), threshold=0.90)
        if box:
            close = ("close_ad", (370, 82))
            for _cn in ("ad_close6", "ad_close5", "ad_close4", "ad_close3"):
                if _cn not in T:
                    continue
                s2, b2 = vision.find(gray, T[_cn], region=(300, 40, 415, 140),
                                     threshold=0.80)
                if b2:
                    close = ("close_ad", vision.center(b2))
                    break
            return "ad_done", close, round(s, 3)
    # 优先用「继续」按钮本身的模板定位（比标题文字稳），标题只作兜底。
    for _rn in ("dialog_resume", "ad_continue2", "ad_continue"):
        if _rn not in T:
            continue
        s, box = vision.find(gray, T[_rn], threshold=0.75)
        if not box:
            continue
        if _rn == "dialog_resume":
            resume = vision.center(box)
        else:
            _, ty = vision.center(box)
            resume = (290, int(ty) + 83)
        return "give_up_ad", ("click_resume", resume), round(s, 3)
    # 注意：不再"看到右上角关闭按钮就当作广告播完"。
    # 播放中的广告也有同样的关闭按钮，那样会导致倒计时没走完就提前关闭，
    # 微信随即弹「暂未获得奖励」。是否播完只看顶部「已获得奖励」字样。

    if "ad_reward" in T:
        s, box = vision.find(gray, T["ad_reward"], threshold=0.78)
        if box:
            # 关闭按钮固定在右上角；模板匹配到就更精确，匹配不到就用兜底坐标
            close = ("close_ad", (370, 82))
            if "ad_close" in T:
                s2, b2 = vision.find(gray, T["ad_close"], region=(280, 30, 415, 150), threshold=0.72)
                if b2:
                    close = ("close_ad", vision.center(b2))
            return "ad_done", close, round(s, 3)

    # 另一种广告样式：顶部徽标从"试玩N秒获得奖励"变成"已获得奖励"
    # （实测 已获得奖励 0.98 vs 播放中 0.63，区分度很好）
    if "ad_reward2" in T:
        s, box = vision.find(gray, T["ad_reward2"], region=(0, 40, 415, 140), threshold=0.80)
        if box:
            close = ("close_ad", (372, 96))
            if "ad_close2" in T:
                s2, b2 = vision.find(gray, T["ad_close2"], region=(300, 40, 415, 140), threshold=0.80)
                if b2:
                    close = ("close_ad", vision.center(b2))
            return "ad_done", close, round(s, 3)

    if "ad_play" in T:
        s, box = vision.find(gray, T["ad_play"], region=(0, 650, 415, 790), threshold=0.75)
        if box:
            return "ad_playing", None, round(s, 3)

    # 广告页统一前置判定：还没到"已获得奖励"就一律等待，绝不碰画面
    # （这条能兜住样式各异的广告，防止把广告内容当成游戏去点）
    if ad_page(rgb):
        return "ad_playing", None, 0.0

    if "tip_ad2" in T:
        s, box = vision.find(gray, T["tip_ad2"], threshold=0.78)
        if box:
            return "tip_popup", ("click_ad2", vision.center(box)), round(s, 3)

    # 微信"是否使用PC端广告跳过卡"提示：必须点「不使用」，绝不能点「使用」
    if "skip_decline" in T:
        s, box = vision.find(gray, T["skip_decline"], threshold=0.78)
        if box:
            return "skip_card", ("decline_skip", vision.center(box)), round(s, 3)

    # 广告推销员弹窗：点「好呀」去看广告拿双倍鱼干
    if "tv_yes" in T:
        s, box = vision.find(gray, T["tv_yes"], threshold=0.78)
        if box:
            return "tv_offer", ("tv_yes", vision.center(box)), round(s, 3)

    # 「排队的客人有些多」提示：等它自己消失，不点任何按钮
    if "queue_full" in T:
        s, box = vision.find(gray, T["queue_full"], threshold=0.78)
        if box:
            return "queue_msg", None, round(s, 3)

    # NPC 弹窗（占卜师等）：优先点"拒绝"类按钮（我不信 / 闭嘴），绝不点同意/购买
    # 阈值 0.70：窗口尺寸变化会让模板得分下降（实测 0.72~1.00）。
    # 关键：只能点左半边——右边是「玩吧/同意」，绝不能点。
    for name in ("refuse_bule2", "refuse_bule", "refuse_no", "refuse_shut"):
        if name not in T:
            continue
        s, box = vision.find(gray, T[name], threshold=0.70)
        if box:
            cx, cy = vision.center(box)
            if cx > 210:
                continue
            return "refuse_dialog", ("click_refuse", (cx, cy)), round(s, 3)

    # 红色 X 关闭按钮：有就点它（优先于其它弹框处理）
    rx = find_red_x(rgb)
    if rx:
        return "red_x_close", ("click_red_x", rx), 0.0

    # 底部卡片：如果是"左右两个按钮"的 NPC 弹窗，统一点左边第一个选项
    if _bottom_sheet(rgb):
        # 带爪印的是剧情/跟随对话卡片：连点画面中部即可跳过
        if "card_paw" in T:
            s, pbox = vision.find(gray, T["card_paw"], region=(330, 710, 415, 790), threshold=0.75)
            if pbox:
                return "story_dialog", ("click_center", (207, 300)), round(s, 3)
        lo, ro = _button_row(gray)
        if lo < 120 and ro < 120:
            return "npc_dialog", ("click_left", (131, 682)), 0.0
        # 其它跟随型对话框：点卡片文字区推进/关闭
        return "bottom_sheet", ("click_card", (207, 665)), round(_white_panel(rgb), 2)

    # 中央大白色弹框（奖励/通知等）：找黄色确认按钮（好的 / 领取 / 确定）
    panel = _white_panel(rgb)
    if panel > 0.45:
        for name in ("claim_ok", "dlg_ok"):
            if name not in T:
                continue
            s, box = vision.find(gray, T[name], threshold=0.82)
            if box:
                py = box[1] + box[3] // 2
                probes = [box[0] + int(box[2] * f) for f in (0.12, 0.5, 0.88)]
                if any(_looks_yellow(rgb, px, py) for px in probes):
                    return "modal_other", ("click_ok", vision.center(box)), round(s, 3)
        return "modal_other", None, round(panel, 2)

    # 小费攒够：读数字，达到阈值就点一下收取。
    # 数字区域按「小费」标签的实际位置推算（标签右侧 36~76 像素），
    # 小费攒够：读数字，达到阈值就点一下收取。
    # 数字区域按「小费」标签的实际位置推算，窗口尺寸变化也能对准。
    # 不做逐字识别（跨渲染不可靠），改为数"数字组数"：
    # 6 组 = 至少 10 万，7 组 = 百万级。具体收不收由主循环按时间间隔决定。
    if "tip_label" in T:
        s, box = vision.find(gray, T["tip_label"], threshold=0.78)
        if box:
            groups = tipocr.count_groups(rgb, box)
            if groups >= 6:
                cx = box[0] + 46
                cy = box[1] + box[3] // 2
                return "tip_ready", ("click_tip", (cx, cy)), groups
    # 主界面上的广告推销员（电视头角色）：动画角色，用多帧模板取最高分
    best_s, best_box = 0.0, None
    for name in T:
        if not name.startswith("tv_npc"):
            continue
        s, box = vision.find(gray, T[name], threshold=0.0)
        if box and s > best_s:
            best_s, best_box = s, box
    if best_box is not None and best_s >= 0.65:
        bcx, bcy = vision.center(best_box)
        # 右上角 x>360 是"活动"按钮（长得像电视），不是 NPC，排除
        if 100 < bcy < 520 and 120 < bcx < 360:
            return "tv_char", ("click_tv", (bcx, bcy)), round(best_s, 3)

    return "unknown", None, 0.0


def queue_full(img):
    """是否出现「排队的客人有些多」提示（手机宣传连点过多时会弹）。"""
    if "queue_full" not in T:
        return False
    s, box = vision.find(vision.to_gray(img), T["queue_full"], threshold=0.78)
    return box is not None


def bottom_bar_visible(img):
    """是否在餐厅主界面（左下角「菜单」按钮可见）。

    阈值 0.85：实测主界面 0.979，「设施」页左下角那个金色按钮只有 0.631。
    阈值定低会把设施页误判成主界面，进而把右下按钮当成「手机宣传」点下去。
    """
    if "btn_menu" not in T:
        return False
    s, box = vision.find(vision.to_gray(img), T["btn_menu"],
                         region=(0, 690, 120, 790), threshold=0.85)
    return box is not None


def bottom_banner(img):
    """底部是否是广告横幅（点一下会跳转到广告主的页面）。

    实测：有横幅时底部 620~790 的白色占比 0.66；正常场景 0.04~0.12。
    阈值 0.40 —— 这类横幅绝对不能点。
    """
    rgb = np.asarray(img.convert("RGB")) if not isinstance(img, np.ndarray) else img
    sub = rgb[620:790, 0:415]
    if sub.size == 0:
        return False
    w = ((sub[..., 0] > 225) & (sub[..., 1] > 220) & (sub[..., 2] > 215))
    return float(w.mean()) > 0.40


def is_restaurant(img):
    """当前是否在餐厅主界面。

    判据：顶部那行「小费 xxx」计数器只在餐厅出现，别的场景（鱼塘/露台/花园/
    前院/厨房）都没有它。比"底部有没有菜单按钮"可靠得多。
    """
    if "tip_label" not in T:
        return True
    s, box = vision.find(vision.to_gray(img), T["tip_label"], threshold=0.75)
    return box is not None

def ad_landing(img):
    """是否能识别出广告页。

    两条判据（任一成立即可）：
      1) 顶部左侧有「广告」小标签（多种背景样式）
      2) 右上角有「关闭」按钮（多种样式）—— 广告样式千变万化，
         但关闭按钮的位置和形状基本固定，这条作为通用兜底。
    """
    gray = vision.to_gray(img)
    for name in ("ad_chip3", "ad_chip2", "ad_chip"):
        if name not in T:
            continue
        s, box = vision.find(gray, T[name], region=(0, 40, 220, 110), threshold=0.70)
        if box:
            return True
    for name in ("ad_close6", "ad_close5", "ad_close4", "ad_close3"):
        if name not in T:
            continue
        s, box = vision.find(gray, T[name], region=(320, 40, 415, 130), threshold=0.78)
        if box:
            return True
    return False


def ad_page(img):
    """是否是微信广告页：底部有一条亮绿色"进入小游戏"按钮。
    广告 0.17~0.21，正常画面 ~0.00。"""
    rgb = np.asarray(img.convert("RGB")) if not isinstance(img, np.ndarray) else img
    sub = rgb[560:785, 20:400]
    if sub.size == 0:
        return False
    a = sub.astype(np.int16)
    g = (a[..., 1] > 110) & (a[..., 1] - a[..., 0] > 40) & (a[..., 1] - a[..., 2] > 20)
    return float(g.mean()) > 0.05


def safe_click(hwnd, name, xy, dry_run):
    x, y = xy
    if y > BOTTOM_GUARD_Y:
        print(f"    ! 拒绝点击 {name} ({x},{y})：落在底部禁区，可能误触广告")
        return False
    if dry_run:
        print(f"    > [dry-run] 本应点击 {name} @ ({x},{y})")
        return True
    game.wait_for_idle(3.0,
                       on_pause=lambda: print("    [暂停] 鼠标被移动，等你操作完"),
                       on_resume=lambda: print("    [继续] 鼠标静止 3s"))
    sx, sy, fg = game.click(hwnd, x, y)
    print(f"    > 点击 {name} @ window({x},{y}) -> screen({sx},{sy}) fg={fg}")
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--click", action="store_true", help="真正执行点击（默认只识别）")
    ap.add_argument("--interval", type=float, default=0.7, help="每轮间隔秒")
    ap.add_argument("--duration", type=float, default=0, help="运行多少秒后退出，0=一直跑")
    ap.add_argument("--once", action="store_true", help="只识别一次就退出")
    ap.add_argument("--max-actions", type=int, default=6, help="最多执行多少个点击动作，防止失控")
    args = ap.parse_args()

    load_templates()
    hwnd, title = game.find_window()
    print(f"窗口: {title!r} hwnd={hwnd}")
    print(f"模式: {'真实点击' if args.click else 'dry-run（只识别）'}")

    last_action = 0.0
    start = time.time()
    count = 0
    actions_done = 0
    log_path = os.path.join(HERE, "bot.log")

    def log(msg):
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass

    log(f"start click={args.click} max_actions={args.max_actions}")

    try:
        while True:
            img = game.capture(hwnd)
            state, action, score = decide(img)
            count += 1
            stamp = time.strftime("%H:%M:%S")
            print(f"[{stamp}] #{count:04d} {state:12s} score={score}")
            log(f"#{count:04d} {state} score={score}")

            if action and time.time() - last_action > 1.2:
                if actions_done >= args.max_actions:
                    print(f"已达到动作上限 {args.max_actions}，停止")
                    break
                done = safe_click(hwnd, action[0], action[1], dry_run=not args.click)
                if done:
                    last_action = time.time()
                    actions_done += 1
                    log(f"action {action[0]} at {action[1]}")
                    time.sleep(1.0)

            if args.once:
                break
            if args.duration and time.time() - start > args.duration:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\n已停止（Ctrl+C）")

    log(f"end recognized={count} actions={actions_done}")
    mode = "（dry-run，未真正点击）" if not args.click else ""
    print(f"共识别 {count} 次，动作 {actions_done} 次{mode}")


if __name__ == "__main__":
    sys.exit(main())
