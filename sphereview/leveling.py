"""Level a panorama from vertical edges picked in the current perspective view.

Two points of a real-world vertical edge, together with the camera centre,
define a vertical plane.  In the source panorama's coordinates that plane's
normal is perpendicular to the physical up direction.  Several such normals
therefore share one common perpendicular, which is the up direction.  The pose
is rebuilt from it with the same yaw, so only pitch and roll move.

Click error is handled by growing each candidate fit to every consistent edge,
by counting repeated picks of one edge as a single piece of evidence, and by
re-fitting under jittered endpoints to refuse a direction that is not pinned
down.  A failed attempt never changes the stored pose.
"""

import math
from itertools import combinations

import numpy as np

from image_lab import validation as v
from .projection import ASPECTS, rotation

MAX_EDGES = 4
MIN_EDGES = 2
MIN_EDGE_DEGREES = 1.0        # endpoints closer than this cannot define a plane
INLIER_DEGREES = 2.0          # a normal this far out of horizontal is an outlier
SPREAD_DEGREES = 8.0          # normals closer than this count as one direction
TIE_DEGREES = 0.05            # fits this close in RMS are equally supported
CLICK_NOISE = 0.003           # assumed endpoint click error, fraction of the view
NOISE_TRIALS = 48
MAX_UNCERTAINTY_DEGREES = 3.0
UNVERIFIED_CHANGE_DEGREES = 25.0
PITCH_LIMIT = 85.0


def _wrap(degrees):
    return (degrees + 180) % 360 - 180


def clean_edges(values):
    items = v.list_value(values, '竖边', MAX_EDGES)
    if len(items) < MIN_EDGES:
        raise v.Invalid('至少需要两条竖边')
    edges = []
    for number, item in enumerate(items, 1):
        data = v.object_value(item, f'第{number}条竖边')
        ends = []
        for key, label in (('a', '起点'), ('b', '终点')):
            ends.append(v.vector(data.get(key), f'第{number}条竖边{label}', 2, 0, 1))
        if math.dist(ends[0], ends[1]) < 1e-4:
            raise v.Invalid(f'第{number}条竖边两端重合')
        edges.append((ends[0], ends[1]))
    return edges


def _ray_from_uv(view, u, v):
    width, height = ASPECTS[view['aspect']]
    limit = math.tan(math.radians(view['fov']) / 2)
    ray = np.array([(2 * u - 1) * limit, (1 - 2 * v) * limit * height / width, 1.0])
    return ray / np.linalg.norm(ray)


def _ray(view, point):
    return _ray_from_uv(view, point[0], point[1])


def _edge_normal(transform, view, edge):
    first = transform @ _ray(view, edge[0])
    second = transform @ _ray(view, edge[1])
    cross = np.cross(first, second)
    length = float(np.linalg.norm(cross))
    angle = math.degrees(math.asin(min(1.0, length)))
    if angle < MIN_EDGE_DEGREES:
        return None, angle
    return cross / length, angle


def _up_from_normals(normals):
    matrix = sum(np.outer(normal, normal) for normal in normals)
    values, vectors = np.linalg.eigh(matrix)
    return vectors[:, 0], values


def _residual(normal, up):
    return math.degrees(math.asin(min(1.0, abs(float(normal @ up)))))


def _fit(normals, indices):
    up, values = _up_from_normals([normals[index] for index in indices])
    residuals = [_residual(normal, up) for normal in normals]
    inliers = [index for index, value in enumerate(residuals) if value <= INLIER_DEGREES]
    return up, residuals, inliers


def _clusters(normals, indices):
    """Group normals that point the same way; each group is one piece of evidence.

    Grouping uses the tighter spread tolerance rather than the inlier tolerance,
    so genuinely different directions are not collapsed into one just because
    both fit the current candidate up vector.
    """
    groups = []
    limit = math.cos(math.radians(SPREAD_DEGREES))
    for index in indices:
        for group in groups:
            if all(abs(float(normals[index] @ normals[member])) >= limit for member in group):
                group.append(index)
                break
        else:
            groups.append([index])
    return groups


def _representatives(groups, usable_lengths):
    """Longest pick per direction; re-marking one edge adds no evidence."""
    return [max(group, key=lambda index: (usable_lengths[index], -index)) for group in groups]


def _fit_quality(normals, inliers, residuals):
    groups = _clusters(normals, inliers)
    used = [residuals[index] for index in inliers]
    rms = math.sqrt(sum(value * value for value in used) / len(used)) if used else math.inf
    return len(groups), rms


def _consensus(normals):
    """Best-agreeing up direction, or ``None`` when two readings both fit.

    Every subset is fitted and then grown to all consistent edges, so a single
    mis-picked edge gets dropped.  Independent directions are scored first so
    that re-marking one edge cannot count as extra evidence; when two equally
    supported readings point to materially different verticals, the picks
    themselves are ambiguous and no pose change is justified.
    """
    fits = []
    for size in range(len(normals), 0, -1):
        for indices in combinations(range(len(normals)), size):
            up, residuals, inliers = _fit(normals, list(indices))
            if len(_clusters(normals, inliers)) < 2:
                continue
            groups, rms = _fit_quality(normals, inliers, residuals)
            fits.append((groups, rms, up, inliers))
    if not fits:
        return None, False
    best_groups = max(groups for groups, _, _, _ in fits)
    top = [fit for fit in fits if fit[0] == best_groups]
    best_rms = min(rms for _, rms, _, _ in top)
    # Numerically identical fits are ties, not a winner.
    leaders = [(up, inliers) for _, rms, up, inliers in top if rms <= best_rms + TIE_DEGREES]
    up, inliers = leaders[0]
    ambiguous = any(
        math.degrees(math.acos(min(1.0, abs(float(other @ up))))) > INLIER_DEGREES
        for other, _ in leaders[1:])
    return (up, inliers), ambiguous


def _common_up(normals):
    """Consensus up direction plus per-edge residuals for reporting."""
    found, ambiguous = _consensus(normals)
    if found is None:
        return None, [math.inf] * len(normals), [], ambiguous
    up, inliers = found
    up, residuals, inliers = _fit(normals, inliers)
    return up, residuals, inliers, ambiguous


def _edge_uncertainty(transform, view, edges, rng):
    """Spread of the fitted up direction under plausible endpoint click error.

    Each endpoint is jittered by a fraction of the view, every edge is
    re-normalised, and the common perpendicular is fitted again.  The spread of
    those samples is how well the picked edges really pin the vertical down:
    two near-parallel short edges leave the common direction loose, while
    several long edges around the horizon lock it tightly.
    """
    samples = []
    for _ in range(NOISE_TRIALS):
        jittered = []
        for first, second in edges:
            moved = [rng.normal(0, CLICK_NOISE, 2) for _ in range(2)]
            normal, _ = _edge_normal(
                transform, view,
                ((first[0] + moved[0][0], first[1] + moved[0][1]),
                 (second[0] + moved[1][0], second[1] + moved[1][1])))
            if normal is not None:
                jittered.append(normal)
        if len(jittered) >= 2:
            samples.append(_up_from_normals(jittered)[0])
    if len(samples) < 2:
        return 90.0
    reference = samples[0]
    worst = 0.0
    for sample in samples:
        worst = max(worst, math.degrees(math.acos(min(1.0, abs(float(sample @ reference))))))
    return worst


def _pose_for_up(yaw, current_pitch, up):
    """Pitch and roll that make ``up`` the panorama's vertical, keeping yaw."""
    sy, cy = math.sin(math.radians(yaw)), math.cos(math.radians(yaw))
    horizontal = sy * up[0] + cy * up[2]
    vertical = up[1]
    magnitude = math.hypot(horizontal, vertical)
    if magnitude < 1e-12:
        pitches = [current_pitch]
    else:
        offset = math.degrees(math.atan2(horizontal, vertical))
        pitches = [_wrap(-offset), _wrap(180 - offset)]
    pitch = min(pitches, key=lambda value: abs(_wrap(value - current_pitch)))
    if abs(pitch) > PITCH_LIMIT + 1e-9:
        return None
    sp, cp = math.sin(math.radians(pitch)), math.cos(math.radians(pitch))
    up_axis = np.array([-sy * sp, cp, -cy * sp])
    right_axis = np.array([cy, 0, -sy])
    a = float(up @ up_axis)
    b = float(up @ right_axis)
    if math.hypot(a, b) < 1e-9:
        return None
    roll = _wrap(math.degrees(math.atan2(-b, a)))
    return {'yaw': yaw, 'pitch': pitch, 'roll': roll}


def _change(before, after, key):
    return round(_wrap(after[key] - before[key]), 3)


def _summary(pose):
    return {key: round(pose[key], 3) for key in ('yaw', 'pitch', 'roll')}


def solve(view, pose, edges):
    view_rotation = rotation(view)
    pose_rotation = rotation(pose).astype(np.float64)
    transform = pose_rotation @ view_rotation.astype(np.float64)
    normals = []
    usable_edges = []
    numbers = []
    lengths = []
    usable_lengths = []
    invalid = []
    for number, edge in enumerate(edges, 1):
        normal, angle = _edge_normal(transform, view, edge)
        lengths.append(angle)
        if normal is None:
            invalid.append({'index': number, 'length_degrees': round(angle, 3)})
        else:
            normals.append(normal)
            usable_edges.append(edge)
            usable_lengths.append(angle)
            numbers.append(number)

    report = {
        'applied': False, 'status': 'unstable', 'message': '',
        'edges': len(edges), 'usable': [], 'ignored': [], 'duplicates': [], 'evidence': 0,
        'residual_degrees': None, 'uncertainty_degrees': None,
        'lengths_degrees': [round(value, 3) for value in lengths],
        'before': _summary(pose), 'after': None, 'change': None,
    }
    if len(normals) < MIN_EDGES:
        report['ignored'] = invalid
        report['message'] = ('能用于判断的竖边不足两条，姿态未改动。'
                             '请换几个位置，在画面里点选更长、更竖直的边。')
        return report

    up, residuals, inliers, ambiguous = _common_up(normals)
    groups = _clusters(normals, inliers)
    representatives = [usable_edges[index] for index in _representatives(groups, usable_lengths)]
    used = [residuals[index] for index in inliers]
    rms = math.sqrt(sum(value * value for value in used) / len(used)) if used else None
    report.update({
        'usable': sorted(numbers[index] for index in inliers),
        'evidence': len(groups),
        'residual_degrees': round(rms, 3) if rms is not None else None,
    })
    report['duplicates'] = [sorted(numbers[index] for index in group)
                            for group in groups if len(group) > 1]
    for index, value in enumerate(residuals):
        if value > INLIER_DEGREES:
            report['ignored'].append({'index': numbers[index],
                                      'residual_degrees': round(value, 3)})
    report['ignored'].extend(invalid)
    report['ignored'].sort(key=lambda item: item['index'])

    def reject(message):
        report['message'] = message
        return report

    if len(inliers) < MIN_EDGES or len(groups) < 2:
        return reject('这些竖边的方向太接近，无法互相印证共同的竖直方向；'
                      '同一方向的边只算一条证据，姿态未改动。'
                      '请换几个位置，在画面不同方向再点几条竖直边。')
    if ambiguous:
        return reject('这些竖边能拼出两个同样合理的竖直方向，无法判断哪个正确；'
                      '姿态未改动。请换几个位置重新点选。')

    # How well the surviving edges actually pin down the common vertical.
    uncertainty = _edge_uncertainty(transform, view, representatives,
                                    np.random.default_rng(0))
    report['uncertainty_degrees'] = round(uncertainty, 3)
    if uncertainty > MAX_UNCERTAINTY_DEGREES:
        return reject('竖边的共同方向判断不稳，姿态未改动。'
                      '请换几个位置、点选更长且朝向更多样的竖边再试。')

    # Two edges always agree exactly, so a wrongly picked line among them cannot
    # be exposed; require a third independent direction before believing a big move.
    current_up = pose_rotation[:, 1]
    candidate = up if float(up @ current_up) >= 0 else -up
    deflection = math.degrees(math.acos(max(-1.0, min(1.0, float(candidate @ current_up)))))
    if len(groups) == 2 and len(inliers) == 2 and deflection > UNVERIFIED_CHANGE_DEGREES:
        return reject(f'两条竖边会要求约 {deflection:.0f}° 的大幅扶正，但没有第三条边能证实；'
                      '姿态未改动。请换几个位置再点一条朝向不同的竖边。')

    after = _pose_for_up(pose['yaw'], pose['pitch'], candidate)
    if after is None:
        return reject('校正后需要超出姿态范围的仰角，姿态未改动。'
                      '请先手动调整仰角，或换几个位置重新点选。')

    report.update({
        'applied': True, 'status': 'ok', 'after': _summary(after),
        'change': {'pitch': _change(pose, after, 'pitch'), 'roll': _change(pose, after, 'roll')},
    })
    used_text = '、'.join(str(index) for index in report['usable'])
    message = (f"已按第 {used_text} 条竖边扶正："
               f"仰角 {report['before']['pitch']:.1f}°→{report['after']['pitch']:.1f}°，"
               f"旋转角 {report['before']['roll']:.1f}°→{report['after']['roll']:.1f}°。")
    if report['ignored']:
        message += '第 ' + '、'.join(str(item['index']) for item in report['ignored']) + ' 条与其余边不一致，已忽略。'
    if report['duplicates']:
        message += '方向相同的边按一条证据计。'
    report['message'] = message
    return report
