"""地面物品检测：小鱼干是低饱和灰蓝色块，用颜色分割 + 连通域找出来。"""
import numpy as np
from PIL import Image, ImageDraw


def fish_mask(rgb, max_spread=26, lo=95, hi=200):
    """返回布尔 mask：低饱和、偏冷、亮度适中。"""
    a = rgb.astype(np.int16)
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    mx = a.max(axis=2)
    mn = a.min(axis=2)
    spread = mx - mn
    mean = a.mean(axis=2)
    return (spread < max_spread) & (mean > lo) & (mean < hi) & (b >= r - 2) & (b >= g - 6)


def label(mask, min_area=60, max_area=2000, region=None):
    """在降采样网格上做 BFS 连通域，返回原图坐标的 bbox 列表。"""
    if region:
        rx0, ry0, rx1, ry1 = region
    else:
        rx0, ry0, rx1, ry1 = 0, 0, mask.shape[1], mask.shape[0]
    mask = mask.copy()
    mask[:ry0, :] = False
    mask[ry1:, :] = False
    mask[:, :rx0] = False
    mask[:, rx1:] = False

    h, w = mask.shape
    step = 3
    gh, gw = h // step, w // step
    small = np.zeros((gh, gw), dtype=bool)
    for y in range(gh):
        for x in range(gw):
            blk = mask[y * step:(y + 1) * step, x * step:(x + 1) * step]
            small[y, x] = blk.mean() > 0.5

    seen = np.zeros_like(small)
    boxes = []
    for sy in range(gh):
        for sx in range(gw):
            if not small[sy, sx] or seen[sy, sx]:
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
                        if 0 <= ny < gh and 0 <= nx < gw and small[ny, nx] and not seen[ny, nx]:
                            seen[ny, nx] = True
                            stack.append((ny, nx))
            area = len(cells) * step * step
            if area < min_area or area > max_area:
                continue
            ys = [c[0] for c in cells]
            xs = [c[1] for c in cells]
            y0, y1 = min(ys) * step, (max(ys) + 1) * step
            x0, x1 = min(xs) * step, (max(xs) + 1) * step
            bw, bh = x1 - x0, y1 - y0
            if bw < 12 or bh < 8 or bw > 70 or bh > 50:
                continue
            aspect = bw / float(bh)
            if aspect < 1.0 or aspect > 3.2:
                continue
            density = len(cells) * step * step / float(bw * bh)
            if density < 0.30:
                continue
            boxes.append((x0, y0, bw, bh, area, round(density, 2)))
    boxes.sort(key=lambda b: -b[4])
    return merge_boxes(boxes)


def merge_boxes(boxes, gap=16):
    """把挨得很近的碎块合并成一个候选。"""
    out = []
    for b in boxes:
        x, y, w, h = b[0], b[1], b[2], b[3]
        merged = False
        for i, o in enumerate(out):
            ox, oy, ow, oh = o[0], o[1], o[2], o[3]
            if abs(x - ox) < gap and abs(y - oy) < gap:
                nx, ny = min(x, ox), min(y, oy)
                nr, nb = max(x + w, ox + ow), max(y + h, oy + oh)
                out[i] = (nx, ny, nr - nx, nb - ny, o[4] + b[4], o[5])
                merged = True
                break
        if not merged:
            out.append(b)
    out.sort(key=lambda b: -b[4])
    return out


def annotate(src_path, out_path, mask_path=None, region=(0, 300, 415, 715)):
    rgb = np.asarray(Image.open(src_path).convert("RGB"))
    m = fish_mask(rgb)
    if mask_path:
        Image.fromarray((m * 255).astype(np.uint8)).save(mask_path)
    boxes = label(m, region=region)
    im = Image.open(src_path).convert("RGB")
    d = ImageDraw.Draw(im)
    for (x, y, w, h, area, dens) in boxes:
        d.rectangle([x, y, x + w, y + h], outline=(255, 0, 0), width=2)
    im.save(out_path)
    return boxes


if __name__ == "__main__":
    import sys
    src = sys.argv[1] if len(sys.argv) > 1 else "shots/ground.png"
    out = sys.argv[2] if len(sys.argv) > 2 else "shots/detect.png"
    boxes = annotate(src, out, mask_path="shots/mask.png")
    print(f"检测到 {len(boxes)} 个候选")
    for b in boxes[:20]:
        print(b)
