"""小费数字 OCR。

策略：
  1. 先确认位数——只有 6 位数（>=100000）才可能有意义（阈值 290000）
  2. 位数用"暗列连通段"数出来，且必须在 190..232 这条窄带里数
  3. 确认 6 位后，在 181..228 的固定区域等宽切 6 格，逐格比对字形模板

这样 5 位数（<100000）会被直接判为"未达阈值"，不会因为等宽切格错位而误读成 29 万。
"""
import os

import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
DIGIT_DIR = os.path.join(HERE, "templates", "digits")

NUM_BOX = (181, 274, 228, 293)   # 等宽切格用（6 位时正好贴合）
RUN_BOX = (190, 275, 232, 292)   # 数位数用（避开鱼干图标）
CELL_W = 8
CELL_H = 12


def _to_gray(img):
    if isinstance(img, str):
        img = Image.open(img)
    elif isinstance(img, np.ndarray):
        arr = img.astype(np.uint8) if img.dtype != np.uint8 else img
        img = Image.fromarray(arr)
    return img.convert("L")


def _binary(gray, box):
    a = np.asarray(gray.crop(box)).astype(np.float32)
    thr = (float(a.min()) + float(np.median(a))) / 2.0
    return a < thr


def digit_count(img, box=None):
    """数出数字的位数；切不出可信段数时返回 None。"""
    binary = _binary(_to_gray(img), box or RUN_BOX)
    cols = binary.sum(axis=0)
    runs, cur = [], None
    for i, v in enumerate(cols):
        if v > 0 and cur is None:
            cur = i
        elif v == 0 and cur is not None:
            runs.append((cur, i - 1))
            cur = None
    if cur is not None:
        runs.append((cur, len(cols) - 1))
    runs = [r for r in runs if (r[1] - r[0] + 1) >= 2]
    if not runs:
        return None
    return len(runs)


def _cells(img, box=None):
    binary = _binary(_to_gray(img), box or NUM_BOX)
    h, w = binary.shape
    out = []
    for i in range(6):
        x0 = int(round(i * w / 6.0))
        x1 = int(round((i + 1) * w / 6.0))
        cell = binary[:, x0:x1]
        ys, xs = np.nonzero(cell)
        if len(ys) == 0:
            out.append(np.zeros((CELL_H, CELL_W), dtype=bool))
            continue
        crop = cell[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
        im = Image.fromarray((crop * 255).astype(np.uint8)).resize((CELL_W, CELL_H), Image.NEAREST)
        out.append(np.asarray(im) > 127)
    return out


def glyphs(img, require_six=True, box=None):
    n = digit_count(img, box)
    if require_six and n != 6:
        return None, f"digit-count:{n}"
    return _cells(img, box), n


def load_templates():
    tpl = {}
    if not os.path.isdir(DIGIT_DIR):
        return tpl
    for name in os.listdir(DIGIT_DIR):
        if not name.endswith(".png"):
            continue
        digit = name.split("_")[0]
        g = np.asarray(Image.open(os.path.join(DIGIT_DIR, name)).convert("L")) > 127
        tpl.setdefault(digit, []).append(g)
    return tpl


def classify(cell, templates):
    best, best_digit = 1e9, "?"
    for digit, samples in templates.items():
        for s in samples:
            if s.shape != cell.shape:
                continue
            d = int(np.count_nonzero(s != cell))
            if d < best:
                best, best_digit = d, digit
    return best_digit, best


def read_number(img, templates=None, max_dist=20, box=None):
    """返回 (int 或 None, 明细)。非 6 位数一律返回 (None, 'digit-count:N')。

    box 可以传入"数字所在矩形"，不传就用默认的固定区域。
    窗口尺寸变化时由调用方按标签位置推算，避免写死坐标错位。
    """
    if templates is None:
        templates = load_templates()
    cells, info = glyphs(img, box=box)
    if cells is None:
        return None, info
    digits, details = [], []
    for c in cells:
        d, dist = classify(c, templates)
        digits.append(d)
        details.append((d, dist))
    text = "".join(digits)
    if "?" in text:
        return None, text
    if max(d for _, d in details) > max_dist:
        return None, f"low-confidence:{text}"
    return int(text), details


def harvest(img, label, out_dir=DIGIT_DIR):
    """采集字形样本（要求 6 位数，标签必须 6 位）。"""
    if len(label) != 6:
        raise RuntimeError("只支持 6 位数样本")
    cells, info = glyphs(img)
    if cells is None:
        raise RuntimeError(f"位数不对: {info}")
    os.makedirs(out_dir, exist_ok=True)
    base = len(os.listdir(out_dir))
    for i, (ch, cell) in enumerate(zip(label, cells)):
        Image.fromarray((cell * 255).astype(np.uint8)).save(
            os.path.join(out_dir, f"{ch}_{base + i:03d}.png"))
    return len(cells)
def read_number_auto(img, roi, templates=None, max_dist=24):
    """按字符实际边界切分读取数字（比等宽切分稳得多）。

    roi：一个宽松的矩形，把数字整个包住即可（可含左侧图标）。
    做法：二值化 -> 统计暗列 -> 取最右边"宽度相近且连续"的一组字符 -> 逐个比字形。
    返回 (int 或 None, 明细)。
    """
    if templates is None:
        templates = load_templates()
    gray = _to_gray(img)
    a = np.asarray(gray.crop(roi)).astype(np.float32)
    if a.size == 0:
        return None, "empty"
    thr = (float(a.min()) + float(np.median(a))) / 2.0
    b = a < thr
    cols = b.sum(axis=0)
    runs, cur = [], None
    for i, v in enumerate(cols):
        if v > 0 and cur is None:
            cur = i
        elif v == 0 and cur is not None:
            runs.append((cur, i - 1))
            cur = None
    if cur is not None:
        runs.append((cur, len(cols) - 1))
    runs = [r for r in runs if (r[1] - r[0] + 1) >= 2]
    if not runs:
        return None, "no-runs"
    widths = [r[1] - r[0] + 1 for r in runs]
    med = float(np.median(widths))
    # 从右往左收集"宽度接近中位数"的字符（数字宽度基本一致）
    picked = []
    for r in reversed(runs):
        w = r[1] - r[0] + 1
        if med * 0.5 <= w <= med * 1.6:
            picked.append(r)
        elif picked:
            break
    picked.reverse()
    if len(picked) < 4:
        return None, f"runs:{len(picked)}"
    cells, details = [], []
    for (x0, x1) in picked:
        cell = b[:, x0:x1 + 1]
        ys, xs = np.nonzero(cell)
        if len(ys) == 0:
            return None, "empty-cell"
        crop = cell[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
        im = Image.fromarray((crop * 255).astype(np.uint8)).resize((CELL_W, CELL_H), Image.NEAREST)
        c = np.asarray(im) > 127
        d, dist = classify(c, templates)
        cells.append(d)
        details.append((d, dist))
    text = "".join(cells)
    if "?" in text:
        return None, text
    if max(d for _, d in details) > max_dist:
        return None, f"low-confidence:{text}"
    return int(text), details
def count_groups(img, label_box):
    """数小费数字有几"组"（每个数字一组）。

    不依赖字形模板，只做列投影分段，所以窗口尺寸/渲染变化后依然可用。
    实测：400000 -> 6 组；7 位数 -> 7 组。
    """
    gray = _to_gray(img)
    x, y, w, h = label_box
    box = (x + 40, y + 3, x + 96, y + h - 2)
    a = np.asarray(gray.crop(box)).astype(np.float32)
    if a.size == 0:
        return 0
    thr = (float(a.min()) + float(np.median(a))) / 2.0
    b = a < thr
    cols = b.sum(axis=0)
    runs, cur = [], None
    for i, v in enumerate(cols):
        if v > 0 and cur is None:
            cur = i
        elif v == 0 and cur is not None:
            runs.append((cur, i - 1))
            cur = None
    if cur is not None:
        runs.append((cur, len(cols) - 1))
    runs = [r for r in runs if (r[1] - r[0] + 1) >= 2]
    return len(runs)
