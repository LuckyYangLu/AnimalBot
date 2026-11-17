"""地面掉落物检测：以地板色为基准，抓"小尺寸异常色块"。

比按颜色分类更通用：鱼干(灰蓝)、虫子(紫)、幽灵(白)、扭蛋(绿)都能命中，
而桌子/客人/装饰因为尺寸过大被过滤掉。
"""
import numpy as np
from PIL import Image, ImageDraw

# 地面区域（避开顶部货架/角色和底部按钮栏）
FLOOR_REGION = (0, 355, 415, 700)


def _majority3(mask):
    """3x3 多数滤波，去掉孤立噪点。"""
    m = mask.astype(np.uint8)
    acc = np.zeros_like(m, dtype=np.int16)
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            acc += np.roll(np.roll(m, dy, axis=0), dx, axis=1)
    return acc >= 5


def detect(rgb, region=FLOOR_REGION, dist_thr=26, min_side=8, max_side=52):
    x0, y0, x1, y1 = region
    sub = rgb[y0:y1, x0:x1].astype(np.int16)

    # 地板基准色：取区域颜色中位数（地板占比最大）
    floor = np.median(sub.reshape(-1, 3), axis=0)
    dist = np.sqrt(((sub - floor) ** 2).sum(axis=2))
    mask = _majority3(dist > dist_thr)

    boxes = _components(mask, min_area=45)
    out = []
    for (bx, by, bw, bh, area) in boxes:
        if bw < min_side or bh < min_side or bw > max_side or bh > max_side:
            continue
        fill = area / float(bw * bh)
        if fill < 0.28:
            continue
        out.append((bx + x0, by + y0, bw, bh, area, round(fill, 2)))
    out.sort(key=lambda b: -b[4])
    return out


def _components(mask, min_area=45):
    """8 邻域连通域，纯 BFS（图不大，够用）。"""
    h, w = mask.shape
    seen = np.zeros((h, w), dtype=bool)
    boxes = []
    ys, xs = np.nonzero(mask)
    for sy, sx in zip(ys, xs):
        if seen[sy, sx]:
            continue
        stack = [(sy, sx)]
        seen[sy, sx] = True
        cells = 0
        miny = maxy = sy
        minx = maxx = sx
        while stack:
            cy, cx = stack.pop()
            cells += 1
            if cy < miny: miny = cy
            if cy > maxy: maxy = cy
            if cx < minx: minx = cx
            if cx > maxx: maxx = cx
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    ny, nx = cy + dy, cx + dx
                    if 0 <= ny < h and 0 <= nx < w and mask[ny, nx] and not seen[ny, nx]:
                        seen[ny, nx] = True
                        stack.append((ny, nx))
        if cells < min_area:
            continue
        boxes.append((minx, miny, maxx - minx + 1, maxy - miny + 1, cells))
    return boxes


def annotate(src, out, **kw):
    rgb = np.asarray(Image.open(src).convert("RGB"))
    boxes = detect(rgb, **kw)
    im = Image.open(src).convert("RGB")
    d = ImageDraw.Draw(im)
    for (x, y, w, h, area, fill) in boxes:
        d.rectangle([x, y, x + w, y + h], outline=(255, 0, 0), width=2)
    im.save(out)
    return boxes


if __name__ == "__main__":
    import sys
    src = sys.argv[1] if len(sys.argv) > 1 else "shots/fresh.png"
    out = sys.argv[2] if len(sys.argv) > 2 else "shots/obj_detect.png"
    boxes = annotate(src, out)
    print(f"{len(boxes)} candidates")
    for b in boxes[:15]:
        print("  ", b)
