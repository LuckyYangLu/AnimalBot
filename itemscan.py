"""地面掉落物识别：多路检测取并集。

地面上的东西没有统一外观，所以用几条互补的规则并起来：
  1) 鱼干：灰蓝低饱和（命中率最高）
  2) 虫子/紫色物：紫身（b>r>g）
  3) 白色物：幽灵/白团（高亮度、低暖色）
  4) 地板色差：地板是纯奶油色，任何偏离它的小块都可能是掉落物
每条各自做连通域，再按重心去重。
"""
import numpy as np
from PIL import Image, ImageDraw

from objects import _components, _majority3

# 地面活动区：避开顶部货架/角色、底部按钮栏、左右翻页箭头
SCAN_REGION = (48, 350, 370, 700)
FLOOR_RGB = np.array([245, 222, 186], dtype=np.int16)

KIND_COLORS = {
    "fish": (255, 0, 0),
    "pest": (0, 190, 255),
    "white": (0, 170, 0),
    "odd": (255, 0, 255),
}


def _dilate(mask, r=1):
    out = mask.copy()
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            out |= np.roll(np.roll(mask, dy, axis=0), dx, axis=1)
    return out


def masks(rgb):
    a = rgb.astype(np.int16)
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    mx = a.max(axis=2)
    mn = a.min(axis=2)
    mean = a.mean(axis=2)

    fish = (mx - mn < 26) & (mean > 95) & (mean < 200) & (b >= r - 2) & (b >= g - 6)
    pest = (b - r > 22) & (r - g > 18) & (b > 140)
    white = (mn > 224) & (r - b < 34) & (mn < 252)
    dist = np.sqrt(((a.astype(np.int32) - FLOOR_RGB.astype(np.int32)) ** 2).sum(axis=2))
    odd = dist > 30
    return {"fish": fish, "pest": _dilate(pest, 2), "white": white, "odd": odd}


def _outline_boxes(rgb, region):
    """深色描边的小物件（贴地掉落物通常都有深色轮廓，而地板是纯色）。"""
    from objects import _components
    x0, y0, x1, y1 = region
    gray = np.asarray(Image.fromarray(rgb.astype(np.uint8)).convert("L")).astype(np.int16)
    mask = _majority3(gray < 125)
    sub = np.zeros_like(mask)
    sub[y0:y1, x0:x1] = mask[y0:y1, x0:x1]
    out = []
    for (bx, by, bw, bh, area) in _components(sub, min_area=50):
        # 只认"和鱼干差不多大"的小物件；顾客/家具都明显更大
        # 尺寸以归一化后的 415x790 坐标系为准，换机器也等效
        if not (12 <= bw <= 30 and 12 <= bh <= 30):
            continue
        if area / float(bw * bh) < 0.18:
            continue
        out.append(("outline", bx, by, bw, bh, area))
    return out


def detect(rgb, region=SCAN_REGION, min_area=45, max_side=52):
    x0, y0, x1, y1 = region
    out = []
    for kind, m in masks(rgb).items():
        sub = np.zeros_like(m)
        sub[y0:y1, x0:x1] = m[y0:y1, x0:x1]
        sub = _majority3(sub)
        for (bx, by, bw, bh, area) in _components(sub, min_area=min_area):
            if bw < 9 or bh < 9 or bw > max_side or bh > max_side:
                continue
            if area / float(bw * bh) < 0.30:
                continue
            out.append((kind, bx, by, bw, bh, area))

    out.extend(_outline_boxes(rgb, region))

    out.sort(key=lambda f: -f[5])
    kept = []
    for f in out:
        cx, cy = f[1] + f[3] // 2, f[2] + f[4] // 2
        # 右上角「活动」按钮区域（和电视 NPC 很像，但不是掉落物，不能点）
        if 355 <= cx <= 415 and 230 <= cy <= 315:
            continue
        if any(abs(cx - (k[1] + k[3] // 2)) < 13 and abs(cy - (k[2] + k[4] // 2)) < 13 for k in kept):
            continue
        kept.append(f)
    return kept


def annotate(src, out, **kw):
    rgb = np.asarray(Image.open(src).convert("RGB"))
    found = detect(rgb, **kw)
    im = Image.open(src).convert("RGB")
    d = ImageDraw.Draw(im)
    for (kind, x, y, w, h, area) in found:
        d.rectangle([x, y, x + w, y + h], outline=KIND_COLORS[kind], width=2)
        d.text((x, max(0, y - 11)), kind, fill=KIND_COLORS[kind])
    im.save(out)
    return found


if __name__ == "__main__":
    import sys
    src = sys.argv[1] if len(sys.argv) > 1 else "shots/fresh.png"
    out = sys.argv[2] if len(sys.argv) > 2 else "shots/scan2.png"
    found = annotate(src, out)
    print(f"{len(found)} items")
    for it in found:
        print("  ", it[0], [int(v) for v in it[1:]])
