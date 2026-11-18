"""Pick a small bookmark tour that keeps the panorama's horizon covered.

The direction map is a 360 degree circle, so every span in this module is an
arc.  A span crossing -180/180 degrees is one arc, not two unrelated ends.
"""

import math
from itertools import combinations

from .projection import ASPECTS

FULL_CIRCLE = 360.0
EPSILON = 1e-7
MAX_SEARCH_COMBINATIONS = 50_000


def normalize(degrees):
    """Return an angle in [-180, 180)."""
    value = (float(degrees) + 180.0) % 360.0 - 180.0
    return 0.0 if abs(value) < EPSILON else value


def clean(degrees):
    """Drop floating point dust before values are published."""
    value = round(float(degrees), 6)
    return 0.0 if abs(value) < 1e-5 else value


def horizon_span(view):
    """Longitude arc of the equator visible in one perspective view.

    The view is a rectangle in its own tangent plane.  Intersecting that
    rectangle with the panorama's equator gives a single arc just as long as
    the rectangle's intersection with a line.  Pitch and roll shorten or shift
    that arc; a view aimed away from the horizon can return no arc at all.
    """
    width, height = ASPECTS[view['aspect']]
    tangent_x = math.tan(math.radians(view['fov'] / 2))
    tangent_y = tangent_x * height / width
    pitch = math.radians(view['pitch'])
    roll = math.radians(view['roll'])
    cos_pitch = math.cos(pitch)
    sin_pitch = math.sin(pitch)
    cos_roll = math.cos(roll)
    sin_roll = math.sin(roll)

    # With t = tan(longitude - yaw), camera-plane coordinates divided by the
    # forward component are linear in t.  Clip that line to |x| <= tan(fov/2)
    # and |y| <= tan(vertical fov/2).
    low, high = -math.inf, math.inf
    low, high = _clip_line(low, high, cos_roll, -sin_pitch * sin_roll,
                           -tangent_x * cos_pitch, tangent_x * cos_pitch)
    low, high = _clip_line(low, high, -sin_roll, -sin_pitch * cos_roll,
                           -tangent_y * cos_pitch, tangent_y * cos_pitch)
    if low > high:
        return None
    start = math.degrees(math.atan(low))
    stop = math.degrees(math.atan(high))
    span = stop - start
    if span <= EPSILON:
        return None
    return {'start': normalize(view['yaw'] + start), 'width': min(FULL_CIRCLE, span)}


def _clip_line(low, high, coefficient, constant, minimum, maximum):
    """Intersect t in [low, high] with minimum <= coefficient*t+constant <= maximum."""
    if abs(coefficient) < 1e-12:
        if constant < minimum - EPSILON or constant > maximum + EPSILON:
            return 1.0, 0.0
        return low, high
    first = (minimum - constant) / coefficient
    second = (maximum - constant) / coefficient
    if first > second:
        first, second = second, first
    return max(low, first), min(high, second)


def _pieces(spans):
    """Split circular arcs into non-wrapping pieces in [0, 360)."""
    pieces = []
    for span in spans:
        if span is None or span['width'] <= EPSILON:
            continue
        width = min(FULL_CIRCLE, span['width'])
        if width >= FULL_CIRCLE - EPSILON:
            return [(0.0, FULL_CIRCLE)]
        start = (span['start'] + 180.0) % 360.0
        stop = start + width
        if stop <= FULL_CIRCLE:
            pieces.append((start, stop))
        else:
            pieces.append((start, FULL_CIRCLE))
            pieces.append((0.0, stop - FULL_CIRCLE))
    return pieces


def merged_spans(spans):
    """Union arcs, joining the two sides of the -180/180 seam."""
    pieces = sorted(_pieces(spans))
    if not pieces:
        return []
    merged = []
    for start, stop in pieces:
        if merged and start <= merged[-1][1] + EPSILON:
            merged[-1] = (merged[-1][0], max(merged[-1][1], stop))
        else:
            merged.append((start, stop))
    if len(merged) > 1 and merged[0][0] <= EPSILON and merged[-1][1] >= FULL_CIRCLE - EPSILON:
        first = merged.pop(0)
        last = merged.pop()
        merged.append((last[0] - FULL_CIRCLE, first[1]))
        merged.sort()
    return merged


def gap_spans(merged):
    """Uncovered arcs in the same unwrapped coordinates as merged_spans."""
    if not merged:
        return [(0.0, FULL_CIRCLE)]
    if len(merged) == 1 and merged[0][1] - merged[0][0] >= FULL_CIRCLE - EPSILON:
        return []
    gaps = []
    for before, after in zip(merged, merged[1:]):
        if after[0] - before[1] > EPSILON:
            gaps.append((before[1], after[0]))
    first_start = merged[0][0] + FULL_CIRCLE
    last_stop = merged[-1][1]
    if first_start - last_stop > EPSILON:
        gaps.append((last_stop, first_start))
    return gaps


def exported(segments):
    """Describe internal segments as arcs in [-180, 180) with a seam flag."""
    rows = []
    for start, stop in segments:
        width = min(FULL_CIRCLE, stop - start)
        start = clean(normalize(start - 180.0))
        crosses = width < FULL_CIRCLE - EPSILON and start + width > 180.0 + EPSILON
        rows.append({
            'start': clean(start),
            'end': clean(normalize(start + width)) if width < FULL_CIRCLE - EPSILON else 180.0,
            'width': clean(width),
            'crosses_seam': bool(crosses),
        })
    return rows


def _metrics(spans):
    merged = merged_spans(spans)
    gaps = gap_spans(merged)
    coverage = sum(stop - start for start, stop in merged)
    longest = max((stop - start for start, stop in gaps), default=0.0)
    uncovered = max(0.0, FULL_CIRCLE - coverage)
    return {
        'coverage': coverage,
        'uncovered': uncovered,
        'longest_gap': longest,
        'merged': merged,
        'gaps': gaps,
    }


def _key(metrics, count):
    # A degree of lost longitude and a degree of longest continuous gap are
    # weighed equally: splitting a gap is worth it exactly while it costs no
    # more horizon than the gap it removes.  Later terms prefer more total
    # coverage, then a shorter list.
    return (round(metrics['uncovered'] + metrics['longest_gap'], 6),
            round(metrics['uncovered'], 6),
            round(metrics['longest_gap'], 6),
            count)


def _search_exhaustive(spans, limit):
    indexes = list(range(len(spans)))
    best_key = _key(_metrics([]), 0)
    best = ()
    total = sum(math.comb(len(indexes), count)
                for count in range(min(limit, len(indexes)) + 1))
    if total > MAX_SEARCH_COMBINATIONS:
        return None
    for count in range(min(limit, len(indexes)) + 1):
        for chosen in combinations(indexes, count):
            metrics = _metrics([spans[index] for index in chosen])
            key = _key(metrics, count)
            if key < best_key:
                best_key, best = key, chosen
    return list(best)


class _Evaluator:
    """Memoized objective for bookmark subsets of one panorama."""

    def __init__(self, spans):
        self.spans = spans
        self.cache = {}

    def key(self, selected):
        chosen = tuple(sorted(selected))
        if chosen not in self.cache:
            metrics = _metrics([self.spans[index] for index in chosen])
            self.cache[chosen] = (_key(metrics, len(chosen)), metrics)
        return self.cache[chosen]


def _greedy(evaluator, seed, limit):
    selected = [] if seed is None else [seed]
    current = evaluator.key(selected)[0]
    while len(selected) < limit:
        best = None
        for index in range(len(evaluator.spans)):
            if index in selected:
                continue
            candidate = selected + [index]
            key = evaluator.key(candidate)[0]
            if key < current and (best is None or key < best[0]):
                best = (key, index)
        if best is None:
            break
        current, index = best
        selected.append(index)
    return selected


def _improve(evaluator, selected, limit):
    """Remove, add or swap one bookmark while the combined objective improves."""
    size = len(evaluator.spans)
    selected = list(selected)
    while True:
        current = evaluator.key(selected)[0]
        best_key, best = current, None
        for index in list(selected):
            candidate = [item for item in selected if item != index]
            key = evaluator.key(candidate)[0]
            if key < best_key:
                best_key, best = key, candidate
        if len(selected) < limit:
            for index in range(size):
                if index in selected:
                    continue
                candidate = selected + [index]
                key = evaluator.key(candidate)[0]
                if key < best_key:
                    best_key, best = key, candidate
        for removed in list(selected):
            for added in range(size):
                if added in selected:
                    continue
                candidate = [item for item in selected if item != removed] + [added]
                key = evaluator.key(candidate)[0]
                if key < best_key:
                    best_key, best = key, candidate
        if best is None:
            return selected
        selected = best


def _search_heuristic(spans, limit):
    evaluator = _Evaluator(spans)
    order = sorted(range(len(spans)),
                   key=lambda index: -(spans[index]['width'] if spans[index] else 0.0))
    seeds = [None, *order[:6]]
    candidates = []
    for seed in seeds:
        chosen = _improve(evaluator, _greedy(evaluator, seed, limit), limit)
        candidates.append((evaluator.key(chosen)[0], chosen))
    return min(candidates, key=lambda item: item[0])[1]


def plan(bookmarks, limit):
    """Choose at most limit bookmarks and describe the horizon they cover."""
    limit = min(limit, len(bookmarks))
    spans = []
    rows = []
    for bookmark in bookmarks:
        span = horizon_span(bookmark['view'])
        spans.append(span)
        rows.append({
            'id': bookmark['id'],
            'name': bookmark['name'],
            'yaw': bookmark['view']['yaw'],
            'pitch': bookmark['view']['pitch'],
            'fov': bookmark['view']['fov'],
            'aspect': bookmark['view']['aspect'],
            'start': span['start'] if span else None,
            'width': round(span['width'], 6) if span else 0.0,
        })

    selected_indexes = _search_exhaustive(spans, limit)
    if selected_indexes is None:
        selected_indexes = _search_heuristic(spans, limit)
    selected_indexes = sorted(selected_indexes)
    metrics = _metrics([spans[index] for index in selected_indexes])
    coverage = metrics['coverage']
    return {
        'limit': limit,
        'bookmark_count': len(bookmarks),
        'selected_ids': [rows[index]['id'] for index in selected_indexes],
        'selected': [rows[index] for index in selected_indexes],
        'covered': exported(metrics['merged']),
        'gaps': exported(metrics['gaps']),
        'coverage_degrees': round(coverage, 6),
        'coverage_fraction': round(coverage / FULL_CIRCLE, 6),
        'uncovered_degrees': round(metrics['uncovered'], 6),
        'gap_count': len(metrics['gaps']),
        'longest_gap_degrees': round(metrics['longest_gap'], 6),
        'note': '覆盖按方向图赤道一圈计算；未覆盖经度与最长连续空档合计最小，张数不超过上限。',
    }
