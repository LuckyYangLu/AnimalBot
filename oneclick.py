"""单次动作：识别当前画面，执行一个安全动作，然后打印结果。

用于在正常权限（用户双击运行）下验证点击是否真的生效。
当前会处理：
  - skip_card  -> 点「不使用」
  - rest_popup -> 点「行吧」
  - bottom_sheet -> 点「跳过」
  - modal_other -> 点黄色确认按钮
"""
import os
import sys
import time

import bot
import game

# 这些动作是安全的，可以执行
SAFE = {"decline_skip", "rest_ok", "click_ok", "click_refuse", "click_left",
        "click_card", "dismiss_sheet", "close_ad", "tv_yes", "click_tip", "click_red_x"}

HERE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(HERE, "oneclick.log")


def log(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def main():
    log("=" * 50)
    log(f"python={sys.executable}")
    try:
        import subprocess
        out = subprocess.run(["whoami", "/groups"], capture_output=True,
                             encoding="utf-8", errors="ignore", timeout=5).stdout
        for ln in (out or "").splitlines():
            if "Mandatory" in ln:
                log("integrity: " + ln.strip())
    except Exception as e:
        log(f"integrity check failed: {e}")

    bot.load_templates()
    hwnd, title = game.find_window()
    log(f"窗口: {title!r} hwnd={hwnd}")

    img = game.capture(hwnd)
    state, action, score = bot.decide(img)
    log(f"状态: {state}  置信度: {score}  动作: {action}")
    if not action:
        log("当前画面没有可执行的动作。")
        return

    name, (cx, cy) = action
    if name not in SAFE:
        log("该动作不在白名单内，已放弃。")
        return

    log("等待鼠标静止 3 秒（请勿触碰鼠标）...")
    game.wait_for_idle(3.0,
                       on_pause=lambda: log("  [等待] 检测到鼠标移动"),
                       on_resume=lambda: log("  [继续] 鼠标已静止"))

    game.keep_awake(True)
    try:
        before = game.capture(hwnd)
        sx, sy, fg = game.click(hwnd, cx, cy, delay=1.2)
        log(f"已点击 window({cx},{cy}) -> screen({sx},{sy}) fg={fg}")
        time.sleep(1.5)
        after = game.capture(hwnd)
        st2, act2, sc2 = bot.decide(after)
        log(f"点击后状态: {st2} {act2} {sc2}")
        after.save(os.path.join(HERE, "shots", "oneclick_after.png"))
    finally:
        game.keep_awake(False)


if __name__ == "__main__":
    main()
