import cv2
import numpy as np
from scipy import ndimage as ndi
from .arrays import luminance, blur, gradients, normalize_weights, weighted_rgb
from .pyramids import multiband
from .focus_pin import apply_pin


def focus_measure(image, metric='laplacian', radius=2):
    gray = luminance(image)
    if metric == 'laplacian':
        response = np.abs(cv2.Laplacian(gray, cv2.CV_32F, ksize=3))
    elif metric == 'tenengrad':
        dx, dy = gradients(gray)
        response = dx * dx + dy * dy
    elif metric == 'variance':
        mean = blur(gray, radius)
        response = np.maximum(blur(gray * gray, radius) - mean * mean, 0)
    else:
        dx = np.abs(cv2.filter2D(gray, cv2.CV_32F, np.array([[-1, 2, -1]], np.float32)))
        dy = np.abs(cv2.filter2D(gray, cv2.CV_32F, np.array([[-1], [2], [-1]], np.float32)))
        response = dx + dy
    return blur(response, radius) + 1e-8


def focus_stack(prepared, settings):
    images = prepared['images']
    validities = prepared['validities']
    measures = np.stack([focus_measure(image, settings['metric'], settings['radius']) for image in images])
    measures *= np.stack(validities)
    indices = np.argmax(measures, axis=0).astype(np.uint8)
    if settings['consistency'] > 1:
        window = settings['consistency'] | 1
        indices = ndi.median_filter(indices, size=window, mode='nearest')
    max_measure = np.maximum(measures.max(axis=0), 1e-8)
    weights = (measures / max_measure[None]) ** settings['power']
    weights *= np.stack(validities) * np.asarray([f['weight'] for f in prepared['frames']])[:, None, None]
    weights = normalize_weights(weights)
    ordered = np.sort(measures, axis=0)
    confidence = ((ordered[-1] - ordered[-2]) / np.maximum(ordered[-1], 1e-8) if len(images) > 1 else np.ones_like(indices, dtype=np.float32)).astype(np.float32)
    pin = settings.get('pin', {'enabled': False})
    pin_report = {'enabled': False}
    pin_mask = np.zeros(indices.shape, dtype=np.float32)
    if pin.get('enabled'):
        indices, weights, pin_mask, _, pin_report = apply_pin(prepared, indices, weights, confidence, pin)
    if settings['method'] == 'hard':
        stack = np.stack(images)
        yy, xx = np.indices(indices.shape)
        image = stack[indices, yy, xx]
    elif settings['method'] == 'weighted':
        image = weighted_rgb(images, weights)
    else:
        image = multiband(images, weights, settings['levels'], validities)
    valid = np.max(validities, axis=0)
    image[valid < .001] = 0
    # 最终每个像素实际采用的帧编号（按最终权重的最大贡献者），如实反映锁定与回落
    source_index = np.argmax(weights, axis=0).astype(np.uint8)
    depth = indices.astype(np.float32) / max(len(images) - 1, 1)
    fields = {'image': np.clip(image, 0, 1), 'valid': valid, 'focus_index': depth, 'focus_source': source_index.astype(np.float32), 'focus_pin_mask': pin_mask, 'confidence': confidence.astype(np.float32), 'focus_measure': max_measure / max(float(np.percentile(max_measure, 99.5)), 1e-7), 'weights': weights}
    report = {'mode': 'focus', 'metric': settings['metric'], 'method': settings['method'], 'mean_confidence': float(confidence.mean()), 'frame_winning_fraction': [float(np.mean(source_index == i)) for i in range(len(images))], 'pin': pin_report, 'note': '焦点索引为帧编号，不是经过标定的物理深度；运动、遮挡和离焦光晕会影响结果。'}
    return fields, report
