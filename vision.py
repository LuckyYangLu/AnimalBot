"""模板匹配（FFT 归一化互相关）。

全图搜索 O(N log N)，内存只与图像尺寸相关，不会随模板大小爆炸。
"""
import numpy as np
from PIL import Image


def load_gray(path):
    return np.asarray(Image.open(path).convert("L"), dtype=np.uint8)


def to_gray(img):
    """PIL Image 或 ndarray（灰度/彩色）统一转成灰度 uint8。"""
    if isinstance(img, np.ndarray):
        arr = img.astype(np.uint8) if img.dtype != np.uint8 else img
        if arr.ndim == 3:
            return np.asarray(Image.fromarray(arr).convert("L"), dtype=np.uint8)
        return arr
    return np.asarray(img.convert("L"), dtype=np.uint8)


def _ncc_map(hay, tpl):
    """返回 NCC 分数图，形状 (H-h+1, W-w+1)，值域约 [-1, 1]。"""
    hay = hay.astype(np.float64)
    tpl = tpl.astype(np.float64)
    H, W = hay.shape
    h, w = tpl.shape
    if H < h or W < w:
        return None

    Hp, Wp = H + h - 1, W + w - 1
    A = np.zeros((Hp, Wp), dtype=np.float64)
    A[:H, :W] = hay
    T = np.zeros((Hp, Wp), dtype=np.float64)
    T[:h, :w] = tpl

    corr = np.fft.irfft2(np.fft.rfft2(A) * np.conj(np.fft.rfft2(T)), s=(Hp, Wp))

    # 局部和 / 平方和，用积分图
    S = np.zeros((H + 1, W + 1), dtype=np.float64)
    S[1:, 1:] = np.cumsum(np.cumsum(hay, axis=0), axis=1)
    Sq = np.zeros((H + 1, W + 1), dtype=np.float64)
    Sq[1:, 1:] = np.cumsum(np.cumsum(hay * hay, axis=0), axis=1)

    box_sum = S[h:, w:] - S[:-h, w:] - S[h:, :-w] + S[:-h, :-w]
    box_sq = Sq[h:, w:] - Sq[:-h, w:] - Sq[h:, :-w] + Sq[:-h, :-w]

    n = float(h * w)
    mean_i = box_sum / n
    var_i = np.maximum(box_sq - n * mean_i * mean_i, 0.0)

    mean_t = tpl.mean()
    var_t = max(float((tpl * tpl).sum()) - n * mean_t * mean_t, 1e-9)

    num = corr[: H - h + 1, : W - w + 1] - n * mean_i * mean_t
    den = np.sqrt(var_i) * np.sqrt(var_t)
    return np.where(den > 1e-6, num / (den + 1e-9), 0.0)


def find(hay, tpl, region=None, threshold=0.80):
    """返回 (score, (x, y, w, h))；找不到返回 (score, None)。"""
    x0, y0 = 0, 0
    sub = hay
    if region:
        x0, y0, x1, y1 = region
        sub = hay[y0:y1, x0:x1]

    sm = _ncc_map(sub, tpl)
    if sm is None:
        return 0.0, None

    idx = int(np.argmax(sm))
    i, j = np.unravel_index(idx, sm.shape)
    best = float(sm[i, j])
    if best < threshold:
        return best, None
    h, w = tpl.shape
    return best, (int(j) + x0, int(i) + y0, w, h)


def find_all(hay, tpl, region=None, threshold=0.80, limit=5):
    """返回若干不重叠候选 [(score, (x,y,w,h)), ...]。"""
    x0, y0 = 0, 0
    sub = hay
    if region:
        x0, y0, x1, y1 = region
        sub = hay[y0:y1, x0:x1]

    sm = _ncc_map(sub, tpl)
    if sm is None:
        return []

    h, w = tpl.shape
    flat = sm.ravel()
    order = np.argsort(flat)[::-1]
    out, taken = [], []
    for idx in order[:5000]:
        if flat[idx] < threshold:
            break
        i, j = np.unravel_index(idx, sm.shape)
        if any(abs(i - a) < h * 0.6 and abs(j - b) < w * 0.6 for a, b in taken):
            continue
        taken.append((i, j))
        out.append((float(flat[idx]), (int(j) + x0, int(i) + y0, w, h)))
        if len(out) >= limit:
            break
    return out


def center(box):
    x, y, w, h = box
    return x + w // 2, y + h // 2
