import numpy as np
from scipy import ndimage as ndi
from .validation import Invalid


def region_pixel_bounds(bounds, shape):
    h, w = shape[:2]
    x0, y0, x1, y1 = bounds
    left = min(w - 1, int(x0 * w))
    top = min(h - 1, int(y0 * h))
    right = max(left + 1, min(w, int(np.ceil(x1 * w))))
    bottom = max(top + 1, min(h, int(np.ceil(y1 * h))))
    return left, top, right, bottom


def resolve_frame(prepared, frame_id):
    for index, frame in enumerate(prepared['frames']):
        if frame['id'] == frame_id:
            return index
    raise Invalid('参考锁定照片未启用或不存在，请重新选择启用中的照片')


def compute_masks(prepared, confidence, pin, region):
    """返回种子区、扩散区与最终锁定遮罩。

    种子＝检测区域内指定照片确实有有效像素的位置；扩散只穿过低把握像素，
    高把握的清晰轮廓作为屏障，扩散半径限制蔓延距离。指定照片无像素的位置
    不进入锁定，交还给原算法选择其他照片。
    """
    frame_index = resolve_frame(prepared, pin['frame_id'])
    shape = confidence.shape[:2]
    left, top, right, bottom = region_pixel_bounds(region['bounds'], shape)
    region_raster = np.zeros(shape, dtype=bool)
    region_raster[top:bottom, left:right] = True
    valid_pin = prepared['validities'][frame_index] > .5
    seed = region_raster & valid_pin
    threshold = pin['low_confidence']
    if not seed.any():
        # 区域内指定照片全部不可用：不硬取，整体交还原算法选择其他照片
        empty = np.zeros(shape, dtype=bool)
        return {
            'frame_index': frame_index,
            'seed': empty,
            'spread': empty,
            'mask': empty,
            'fallback': region_raster & ~valid_pin,
            'region_raster': region_raster,
            'anchor': False,
        }
    passable = confidence < threshold
    structure = ndi.generate_binary_structure(2, 1)
    labels, count = ndi.label(passable, structure=structure)
    if count:
        touching = np.unique(labels[seed])
        touching = touching[touching > 0]
        connected = np.isin(labels, touching) if touching.size else np.zeros(shape, dtype=bool)
    else:
        connected = np.zeros(shape, dtype=bool)
    spread = int(pin['spread'])
    if spread > 0 and connected.any():
        distance = ndi.distance_transform_cdt(~seed, metric='chessboard')
        spread_area = connected & (distance <= spread)
    else:
        spread_area = np.zeros(shape, dtype=bool)
    pin_mask = seed | spread_area
    # 指定照片被遮罩排除或本身无像素的位置不硬取
    pin_mask &= valid_pin
    fallback = region_raster & ~valid_pin
    return {
        'frame_index': frame_index,
        'seed': seed,
        'spread': spread_area & pin_mask,
        'mask': pin_mask,
        'fallback': fallback,
        'region_raster': region_raster,
        'anchor': True,
    }


def apply_pin(prepared, indices, weights, confidence, pin):
    region = prepared.get('pin_region')
    if region is None:
        raise Invalid('参考锁定缺少检测区域，请重新选择检测区域')
    if region.get('id') != pin['region_id']:
        raise Invalid('参考锁定的检测区域与工程区域不一致，请重新选择')
    result = compute_masks(prepared, confidence, pin, region)
    target = result['frame_index']
    mask = result['mask']
    pinned_indices = indices.copy()
    pinned_indices[mask] = target
    pinned_weights = weights.copy()
    pinned_weights[:, mask] = 0
    pinned_weights[target, mask] = 1
    frame = prepared['frames'][target]
    total = mask.size
    report = {
        'enabled': True,
        'active': bool(result['anchor']),
        'degraded': not result['anchor'],
        'region_id': pin['region_id'],
        'region_name': region.get('name', ''),
        'frame_id': pin['frame_id'],
        'frame_index': target,
        'frame_name': frame['name'],
        'low_confidence': pin['low_confidence'],
        'spread': pin['spread'],
        'seed_fraction': float(result['seed'].sum() / total),
        'spread_fraction': float(result['spread'].sum() / total),
        'pinned_fraction': float(mask.sum() / total),
        'fallback_fraction': float(result['fallback'].sum() / max(result['region_raster'].sum(), 1)),
        'note': '锁定遮罩内强制使用指定照片；指定照片无像素处回落原算法；高把握清晰轮廓阻止连通扩散。' if result['anchor'] else '指定照片在检测区域内全部不可用（排除遮罩或无覆盖），本次合成整体回落原算法，未硬取任何像素。',
    }
    return pinned_indices, pinned_weights, mask.astype(np.float32), result, report


def preview_overlay(prepared, confidence, pin, region):
    result = compute_masks(prepared, confidence, pin, region)
    base = prepared['images'][result['frame_index']].copy()
    overlay = base * .55
    # 种子区：琥珀色实涂
    amber = np.array([.95, .62, .18], dtype=np.float32)
    # 连通低把握扩散区：半透明琥珀
    overlay[result['spread']] = base[result['spread']] * .45 + amber * .55
    overlay[result['seed']] = amber
    # 区域内指定照片不可用、需要回落的位置：红色斜线提示
    fallback = result['fallback']
    yy, xx = np.indices(fallback.shape)
    hatch = ((xx + yy) % 12 < 4) & fallback
    overlay[hatch] = [.85, .16, .12]
    # 检测区域边框
    left, top, right, bottom = region_pixel_bounds(region['bounds'], base.shape)
    overlay[top:top + 2, left:right] = [.2, .95, .75]
    overlay[bottom - 2:bottom, left:right] = [.2, .95, .75]
    overlay[top:bottom, left:left + 2] = [.2, .95, .75]
    overlay[top:bottom, right - 2:right] = [.2, .95, .75]
    return np.clip(overlay, 0, 1).astype(np.float32), result
