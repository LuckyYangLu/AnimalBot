"""收集地面物品（小鱼干等）：检测 → 逐个点击 → 循环。

默认 dry-run，加 --click 才真点。
用法:
  python collect.py --clicks 20                 # 只识别，打印会点哪里
  python collect.py --clicks 20 --click         # 真点 20 下
  python collect.py --clicks 20 --click --phone # 同时点"手机宣传"
"""
import argparse
import os
import random
import sys
import time

import game
import itemscan

HERE = os.path.dirname(os.path.abspath(__file__))
FLOOR = (0, 300, 415, 700)      # 只在地面区域找
PHONE_AD = (370, 730)            # 底部"手机宣传"按钮


def popup_visible(img):
    """检测小费翻倍弹窗/广告页，出现时地面点击会误触，必须暂停。"""
    import vision
    g = vision.to_gray(img)
    for name, region, th in (("tip_ad2", None, 0.78),
                             ("ad_reward", (0, 40, 415, 140), 0.78)):
        p = os.path.join(HERE, "templates", f"{name}.png")
        if not os.path.exists(p):
            continue
        s, box = vision.find(g, vision.load_gray(p), region=region, threshold=th)
        if box:
            return name
    return None


def detections(img):
    rgb = img.convert("RGB")
    import numpy as np
    arr = np.asarray(rgb)
    return itemscan.detect(arr)


def jitter(x, y, r=4):
    return x + random.randint(-r, r), y + random.randint(-r, r)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--clicks", type=int, default=20, help="总共点击次数")
    ap.add_argument("--click", action="store_true", help="真正点击（默认 dry-run）")
    ap.add_argument("--phone", action="store_true", help="同时点击底部“手机宣传”")
    ap.add_argument("--rounds", type=int, default=10, help="最多循环轮数")
    ap.add_argument("--wait", type=float, default=0, help="地面没东西时最多等多少秒，0=立即结束")
    args = ap.parse_args()

    hwnd, title = game.find_window()
    print(f"窗口: {title!r}")
    print(f"模式: {'真实点击' if args.click else 'dry-run'}，目标 {args.clicks} 次")

    done = 0
    t_start = time.time()
    r = 0
    while r < args.rounds:
        r += 1
        if done >= args.clicks:
            break
        img = game.capture(hwnd)
        pop = popup_visible(img)
        if pop:
            print(f"检测到弹窗（{pop}），暂停地面点击，避免误点")
            break
        boxes = detections(img)
        print(f"第 {r} 轮：发现 {len(boxes)} 个目标 {[ (b[0], b[1]+b[3]//2, b[2]+b[4]//2) for b in boxes[:8] ]}")
        if not boxes:
            if args.wait and time.time() - t_start < args.wait:
                print(f"  第 {r} 轮地面空，{args.wait:.0f}s 内继续等…")
                time.sleep(1.5)
                r -= 1  # 空转不占轮数
                continue
            print("  地面干净了，停")
            break

        for (kind, x, y, w, h, area) in boxes:
            if done >= args.clicks:
                break
            cx, cy = x + w // 2, y + h // 2
            px, py = jitter(cx, cy)
            if args.click:
                game.wait_for_idle(3.0,
                                   on_pause=lambda: print("  [暂停] 鼠标被移动，等你操作完"),
                                   on_resume=lambda: print("  [继续] 鼠标静止 3s"))
                game.click(hwnd, px, py, delay=0.12)
            print(f"  {'点击' if args.click else '[dry-run] 要点'} {kind} ({px},{py})")
            done += 1
            time.sleep(0.12)

        if args.phone and done < args.clicks and args.click:
            game.wait_for_idle(3.0)
            game.click(hwnd, *PHONE_AD, delay=0.3)
            print(f"  点击 手机宣传 {PHONE_AD}")
            done += 1

    print(f"结束：{'已点击' if args.click else '计划点击'} {done} 次")


if __name__ == "__main__":
    sys.exit(main())
