import io
import math

import numpy as np
from PIL import Image

from . import projection

FRAMES = 50
FRAME_MS = 80
LONG_EDGE = 512
MARGIN = 0.78
MAX_SHIFT = 16.0
MAX_BOOST = 14.0
MAX_FOV = 140.0


def unit(vector):
    return vector / np.linalg.norm(vector)


def slerp(a, b, t):
    """Shortest great-circle interpolation between unit vectors, so crossing the
    ±180° longitude cut never takes the long way around."""
    dot = float(np.clip(np.dot(a, b), -1.0, 1.0))
    if abs(dot) > 0.9995:
        if dot > 0:
            return unit(a + (b - a) * t)
        side = np.cross(a, np.array([0.0, 1.0, 0.0]))
        if float(np.dot(side, side)) < 1e-8:
            side = np.cross(a, np.array([1.0, 0.0, 0.0]))
        mid = unit(side)
        return slerp(a, mid, t * 2) if t < 0.5 else slerp(mid, b, t * 2 - 1)
    angle = math.acos(dot)
    return (math.sin((1 - t) * angle) * a + math.sin(t * angle) * b) / math.sin(angle)


def smootherstep(t):
    return t * t * t * (t * (t * 6 - 15) + 10)


def rotate(vector, axis, degrees):
    angle = math.radians(degrees)
    return (vector * math.cos(angle) + np.cross(axis, vector) * math.sin(angle)
            + axis * float(np.dot(axis, vector)) * (1 - math.cos(angle)))


def shifted(camera, yaw_by, pitch_by):
    """Bias an orientation toward the point of interest, in camera-local degrees."""
    right, up, forward = camera[:, 0], camera[:, 1], camera[:, 2]
    right = unit(rotate(right, up, yaw_by))
    forward = unit(rotate(forward, up, yaw_by))
    forward = unit(rotate(forward, right, -pitch_by))
    up = unit(np.cross(forward, right))
    return np.column_stack((np.cross(up, forward), up, forward))


def base_track(view_a, view_b):
    start, stop = projection.rotation(view_a), projection.rotation(view_b)
    cameras, fovs = [], []
    for index in range(FRAMES):
        t = smootherstep(index / (FRAMES - 1))
        forward = slerp(start[:, 2], stop[:, 2], t)
        right = slerp(start[:, 0], stop[:, 0], t)
        right = right - forward * float(np.dot(right, forward))
        if float(np.dot(right, right)) < 1e-12:
            right = np.cross(np.array([0.0, 1.0, 0.0]), forward)
            if float(np.dot(right, right)) < 1e-12:
                right = np.cross(np.array([1.0, 0.0, 0.0]), forward)
        right = unit(right)
        cameras.append(np.column_stack((right, np.cross(forward, right), forward)))
        fovs.append(view_a['fov'] + (view_b['fov'] - view_a['fov']) * t)
    return cameras, fovs


def corrections(cameras, fovs, aspect, poi):
    """Per-frame camera bias and fov boost that keep the poi inside a margin."""
    ratio_w, ratio_h = projection.ASPECTS[aspect]
    shifts = np.zeros((len(cameras), 2))
    boosts = np.zeros(len(cameras))
    for index, (camera, fov) in enumerate(zip(cameras, fovs)):
        half_h = fov * MARGIN / 2
        half_v = math.degrees(math.atan(
            math.tan(math.radians(fov / 2)) * ratio_h / ratio_w)) * MARGIN
        local = camera.T @ poi
        if local[2] <= 1e-3:
            continue
        hx = math.degrees(math.atan2(float(local[0]), float(local[2])))
        hy = math.degrees(math.atan2(float(local[1]), float(local[2])))
        sx = math.copysign(min(MAX_SHIFT, max(0.0, abs(hx) - half_h)), hx)
        sy = math.copysign(min(MAX_SHIFT, max(0.0, abs(hy) - half_v)), hy)
        shifts[index] = (sx, sy)
        residual_h = max(0.0, abs(hx) - abs(sx) - half_h)
        residual_v = max(0.0, abs(hy) - abs(sy) - half_v)
        boost_v = 2 * math.degrees(math.atan(
            math.tan(math.radians(half_v + residual_v)) * ratio_w / ratio_h)) - fov
        boosts[index] = max(2 * residual_h, boost_v, 0.0)
    return shifts, boosts


def smooth(values, window=5):
    values = np.asarray(values, dtype=np.float64)
    single = values.ndim == 1
    grid = values[:, None] if single else values
    pad = window // 2
    padded = np.concatenate([np.repeat(grid[:1], pad, axis=0), grid,
                             np.repeat(grid[-1:], pad, axis=0)])
    kernel = np.ones(window) / window
    out = np.column_stack([np.convolve(padded[:, column], kernel, mode='valid')
                           for column in range(grid.shape[1])])
    return out[:, 0] if single else out


def ramp(count, edge=0.12):
    """Window that is exactly zero on the first and last frame and one in the
    middle, so endpoint frames stay exact while corrections apply fully
    in between."""
    t = np.linspace(0.0, 1.0, count)
    w = np.clip(np.minimum(t, 1.0 - t) / edge, 0.0, 1.0)
    return w * w * (3 - 2 * w)


def track_frames(source, pose, exposure, view_a, view_b, poi_yaw, poi_pitch, aspect):
    cameras, fovs = base_track(view_a, view_b)
    poi = projection.basis(poi_yaw, poi_pitch, 0)[:, 2]
    shifts, boosts = corrections(cameras, fovs, aspect, poi)
    window = ramp(FRAMES)
    shifts = np.clip(smooth(shifts) * window[:, None], -MAX_SHIFT, MAX_SHIFT)
    boosts = np.clip(smooth(boosts) * window, 0.0, MAX_BOOST)
    width, height = projection.view_size(aspect, LONG_EDGE)
    orient = projection.rotation(pose)
    output = []
    for camera, fov, (sx, sy), boost in zip(cameras, fovs, shifts, boosts):
        rays = projection.view_rays(width, height, min(MAX_FOV, fov + float(boost)))
        frame = projection.render(source, rays, orient @ shifted(camera, sx, sy), exposure)
        output.append(Image.fromarray(frame).convert('RGB'))
    return output


def encode(frames):
    """One shared palette keeps the animation free of per-frame palette flicker."""
    stamps = [frames[round(index * (len(frames) - 1) / 4)] for index in range(5)]
    mosaic = Image.new('RGB', (frames[0].width, frames[0].height * len(stamps)))
    for row, frame in enumerate(stamps):
        mosaic.paste(frame, (0, row * frame.height))
    palette = mosaic.quantize(colors=256, method=Image.Quantize.MEDIANCUT)
    pages = [frame.quantize(palette=palette, dither=Image.Dither.NONE) for frame in frames]
    stream = io.BytesIO()
    pages[0].save(stream, 'GIF', save_all=True, append_images=pages[1:],
                  duration=FRAME_MS, loop=0, disposal=1)
    return stream.getvalue()


def render_gif(source, project, start, stop, poi_yaw, poi_pitch):
    frames = track_frames(source, project['pose'], project['exposure'],
                          start['view'], stop['view'], poi_yaw, poi_pitch,
                          project['view']['aspect'])
    return encode(frames)
