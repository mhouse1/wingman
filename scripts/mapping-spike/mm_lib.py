"""Minimap reading for the position spike: heading from the N letter, and the
terrain's shift between two frames once both are turned north-up."""

import math
import cv2
import numpy as np

CX, CY = 880.0, 79.5  # minimap centre in the 960 by 600 recording
R_DISC, R_RING = 62, 79  # terrain disc (kept inside its edge), outer edge of the compass ring
UP = 2  # upscale before rotating
HALF = 84  # half-size of the crop round the centre


def crop(frame):
    x0, y0 = int(round(CX)) - HALF, int(round(CY)) - HALF
    c = frame[max(y0, 0) : y0 + 2 * HALF, x0 : x0 + 2 * HALF]
    if y0 < 0:
        c = cv2.copyMakeBorder(c, -y0, 0, 0, 0, cv2.BORDER_CONSTANT)
    if c.shape[1] < 2 * HALF:
        c = cv2.copyMakeBorder(c, 0, 0, 0, 2 * HALF - c.shape[1], cv2.BORDER_CONSTANT)
    return c


_yy, _xx = np.mgrid[0 : 2 * HALF, 0 : 2 * HALF]
_dx, _dy = _xx - (CX - (round(CX) - HALF)), _yy - (CY - (round(CY) - HALF))
_rr = np.hypot(_dx, _dy)
_bearing = np.degrees(np.arctan2(_dx, -_dy))  # clockwise from up


def north_angle(c):
    """Screen bearing of the orange N, degrees clockwise from up, or None.

    The ring is see-through, so an orange backdrop (canyon rock) floods it with
    redder, wider blobs. The letter is a small blob of one hue at one radius.
    """
    hsv = cv2.cvtColor(c, cv2.COLOR_BGR2HSV)
    m = cv2.inRange(hsv, (15, 160, 180), (25, 255, 255))
    m[(_rr < 71) | (_rr > 79)] = 0
    n, lab, st, cen = cv2.connectedComponentsWithStats(m)
    best = None
    for i in range(1, n):
        if not 4 <= st[i, 4] <= 22:
            continue
        ys, xs = np.nonzero(lab == i)
        if not 72.5 <= _rr[ys, xs].mean() <= 77.5:
            continue
        if best is not None:
            return None  # two candidates: do not guess
        best = math.degrees(math.atan2(_dx[ys, xs].mean(), -_dy[ys, xs].mean()))
    return best


def present(c):
    """Minimap on screen: the ring carries both its red arc and its blue arc."""
    hsv = cv2.cvtColor(c, cv2.COLOR_BGR2HSV)
    ring = (_rr > 67) & (_rr < R_RING)
    red = (
        cv2.inRange(hsv, (0, 70, 150), (8, 255, 255))
        | cv2.inRange(hsv, (170, 70, 150), (179, 255, 255))
    ) > 0
    blue = cv2.inRange(hsv, (105, 120, 150), (125, 255, 255)) > 0
    return int((red & ring).sum()) >= 25 and int((blue & ring).sum()) >= 25


def own_icon(c):
    """The white own-aircraft icon at the centre: the map is centred on us and we are alive."""
    hsv = cv2.cvtColor(c, cv2.COLOR_BGR2HSV)
    white = cv2.inRange(hsv, (0, 0, 225), (179, 45, 255)) > 0
    return int((white & (_rr < 8)).sum()) >= 12


def terrain_mask(c):
    """Terrain pixels: inside the disc, away from the own icon, the view cone and coloured icons."""
    hsv = cv2.cvtColor(c, cv2.COLOR_BGR2HSV)
    m = (_rr < R_DISC) & (_rr > 9)
    # The view cone follows the camera, so it is found by its green tint, with the icons.
    icons = cv2.inRange(hsv, (35, 55, 70), (135, 255, 255)) | cv2.inRange(
        hsv, (0, 0, 225), (179, 60, 255)
    )
    m &= cv2.dilate(icons, np.ones((5, 5), np.uint8)) == 0
    return m


def north_up(c, phi):
    """Grey picture and mask, upscaled and turned so north is up."""
    g = cv2.cvtColor(c, cv2.COLOR_BGR2GRAY).astype(np.float32)
    m = terrain_mask(c).astype(np.float32)
    size = 2 * HALF * UP
    g = cv2.resize(g, (size, size), interpolation=cv2.INTER_CUBIC)
    m = cv2.resize(m, (size, size), interpolation=cv2.INTER_NEAREST)
    ctr = ((_dx[0, 0] * -1) * UP, (_dy[0, 0] * -1) * UP)
    rot = cv2.getRotationMatrix2D(ctr, phi, 1.0)
    g = cv2.warpAffine(g, rot, (size, size), flags=cv2.INTER_LINEAR)
    m = cv2.warpAffine(m, rot, (size, size), flags=cv2.INTER_LINEAR) > 0.99
    return g, m


def _corr(a, b, shape):
    return np.fft.irfft2(np.conj(np.fft.rfft2(a, shape)) * np.fft.rfft2(b, shape), shape)


def shift(f, mf, g, mg, max_shift=40, min_overlap=0.35):
    """Where the terrain of f sits in g: (dx, dy) in recording pixels, and the match score."""
    h, w = f.shape
    shape = (2 * h, 2 * w)
    mf = mf.astype(np.float32)
    mg = mg.astype(np.float32)
    f = f * mf
    g = g * mg
    n = _corr(mf, mg, shape)
    sf, sg = _corr(f, mg, shape), _corr(mf, g, shape)
    sfg = _corr(f, g, shape)
    sff, sgg = _corr(f * f, mg, shape), _corr(mf, g * g, shape)
    ok = n > min_overlap * mf.sum()
    n = np.where(ok, n, 1.0)
    num = sfg - sf * sg / n
    den = np.sqrt(np.maximum(sff - sf * sf / n, 1e-6) * np.maximum(sgg - sg * sg / n, 1e-6))
    ncc = np.where(ok, num / den, -1.0)
    ncc = np.fft.fftshift(ncc)
    cy, cx = h, w
    win = ncc[cy - max_shift : cy + max_shift + 1, cx - max_shift : cx + max_shift + 1]
    iy, ix = np.unravel_index(np.argmax(win), win.shape)

    def sub(v, i):
        if 0 < i < len(v) - 1:
            d = v[i - 1] - 2 * v[i] + v[i + 1]
            return i + (0.5 * (v[i - 1] - v[i + 1]) / d if abs(d) > 1e-9 else 0.0)
        return float(i)

    sy, sx = sub(win[:, ix], iy), sub(win[iy, :], ix)
    return (sx - max_shift) / UP, (sy - max_shift) / UP, float(win[iy, ix])
