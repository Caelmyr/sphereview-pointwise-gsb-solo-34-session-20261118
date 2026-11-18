'use strict';

const $ = id => document.getElementById(id);
const modes = {
    view: ['透视取景', 'view.png'],
    leveled: ['整站校准经纬图', 'leveled.png'],
    cube: ['六张定向方形贴图', 'cube.zip'],
    planet: ['地面居中的小行星投影', 'planet.png'],
    source: ['未经调整的经纬原图', 'source.png']
};
const faces = {
    left: '左', front: '前', right: '右', back: '后', top: '顶', bottom: '底'
};
let current = null;
let mode = 'view';
let busy = false;
let navToken = '';
let tourPlan = null;

function route(key, product = '') {
    return '/sphere/projects/' + key + (product ? '/' + product : '');
}

async function json(url, options) {
    const response = await fetch(url, options);
    const body = await response.json();
    if (!response.ok) throw new Error(body.error || '请求失败');
    return body;
}

function post(url, content) {
    return json(url, {method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(content)});
}

function run(action) {
    return async event => {
        if (event) event.preventDefault();
        if (busy) return;
        busy = true;
        $('status').textContent = '';
        try {
            await action(event);
        } catch (error) {
            $('status').textContent = error.message;
        } finally {
            busy = false;
        }
    };
}

function inputValues(form, keys) {
    return Object.fromEntries(keys.map(key => [key, form.elements[key].value]));
}

function setValues(form, values) {
    for (const [key, value] of Object.entries(values)) {
        form.elements[key].value = value;
    }
}

function showMode() {
    if (!current) return;
    $('planet-settings').hidden = mode !== 'planet';
    for (const button of $('modes').querySelectorAll('button')) {
        button.setAttribute('aria-pressed', String(button.dataset.mode === mode));
    }
    const [description, filename] = modes[mode];
    $('mode-description').textContent = description;
    $('output').textContent = mode === 'cube' ? '下载六面 ZIP ↓' : '下载 PNG ↓';
    $('output').href = route(current.id, filename) + '?download=1';
    $('hero').hidden = mode === 'cube';
    $('cube-grid').hidden = mode !== 'cube';
    $('primary-image').classList.toggle('six-faces', mode === 'cube');
    if (mode === 'cube') {
        $('cube-grid').replaceChildren(...Object.entries(faces).map(([key, label]) => {
            const figure = document.createElement('figure');
            const image = document.createElement('img');
            image.src = route(current.id, 'cube/' + key + '.png') + '?r=' + current.revision;
            image.alt = label + '面';
            const caption = document.createElement('figcaption');
            caption.textContent = label;
            figure.append(image, caption);
            return figure;
        }));
    } else {
        $('hero').src = route(current.id, filename) + '?r=' + current.revision;
    }
}

async function projectList() {
    const projects = await json('/sphere/projects');
    $('projects').replaceChildren(...projects.map(project => {
        const row = document.createElement('li');
        const button = document.createElement('button');
        button.type = 'button';
        button.textContent = project.name;
        button.setAttribute('aria-current', current?.id === project.id ? 'true' : 'false');
        button.addEventListener('click', run(async () => {
            mode = 'view';
            setProject(await json(route(project.id)));
            await projectList();
        }));
        row.append(button);
        return row;
    }));
}

function setProject(project) {
    if (current?.id !== project.id) {
        $('probe-readout').textContent = '点击方向图转到指定位置并读取原图像素。';
        $('seam-result').replaceChildren();
    }
    current = project;
    if (tourPlan && tourPlan.signature !== bookmarkSignature()) {
        tourPlan = null;
        $('status').textContent = '书签已变化，请重新生成巡看清单';
    }
    $('workspace').hidden = false;
    $('empty').hidden = true;
    $('project-name').textContent = project.name;
    $('source-meta').textContent = [
        project.source.width + ' × ' + project.source.height + ' px',
        project.synthetic ? '合成演示' : '导入照片',
        '修订 ' + project.revision
    ].join('  ·  ');
    $('project-zip').href = route(project.id, 'project.zip');
    setValues($('view-form'), project.view);
    setValues($('pose-form'), project.pose);
    setValues($('planet-form'), project.planet);
    setValues($('exposure-form'), {exposure: project.exposure});
    $('new-name').value = project.name;
    const token = JSON.stringify([project.id, project.pose, project.exposure]);
    if (token !== navToken) {
        $('navigation-image').src = route(project.id, 'leveled.png') + '?r=' + project.revision;
        navToken = token;
        $('probe-readout').textContent = '点击方向图转到指定位置并读取原图像素。';
    }
    $('marker').style.left = (project.view.yaw + 180) / 3.6 + '%';
    $('marker').style.top = (90 - project.view.pitch) / 1.8 + '%';
    $('marker-position').textContent = project.view.yaw.toFixed(1) + '° / ' + project.view.pitch.toFixed(1) + '°';
    populateBookmarks();
    renderTour();
    showMode();
}

async function edit(action, values) {
    const data = await post(route(current.id, 'edit'), {revision: current.revision, action, ...values});
    setProject(data);
}

function bookmarkSignature() {
    return JSON.stringify([current.id, current.bookmarks.map(item => [item.id, item.view])]);
}

function formatArc(row) {
    if (row.width >= 360) return '整圈 360°';
    return row.crosses_seam
        ? `${row.start.toFixed(1)}° → ${row.end.toFixed(1)}°（跨 ±180°）`
        : `${row.start.toFixed(1)}° 至 ${row.end.toFixed(1)}°`;
}

function arcPieces(row) {
    // Screen pieces in percent of the direction map, splitting arcs that wrap
    // across the -180/180 edge into a left and a right piece.
    const percent = degrees => degrees / 3.6;
    const start = row.start;
    const stop = start + row.width;
    if (stop <= 180) return [[(start + 180) / 3.6, percent(row.width)]];
    if (start >= 180) return [[(start - 180) / 3.6, percent(row.width)]];
    return [
        [(start + 180) / 3.6, percent(180 - start)],
        [0, percent(stop - 180)]
    ];
}

function renderTour() {
    if (!tourPlan) {
        $('tour-result').hidden = true;
        $('tour-result').replaceChildren();
        $('tour-overlay').replaceChildren();
        $('tour-legend').hidden = true;
        return;
    }
    const plan = tourPlan;
    const overlay = document.createElement('div');
    overlay.className = 'tour-overlay';
    for (const [kind, rows] of plan.bookmark_count ? [['covered', plan.covered], ['gap', plan.gaps]] : []) {
        for (const row of rows) {
            for (const [left, width] of arcPieces(row)) {
                const band = document.createElement('span');
                band.className = 'tour-band ' + kind;
                band.style.left = left + '%';
                band.style.width = width + '%';
                overlay.append(band);
            }
        }
    }
    $('tour-overlay').replaceChildren(overlay);
    $('tour-legend').hidden = false;

    const covered = document.createElement('p');
    covered.className = 'tour-summary';
    if (!plan.bookmark_count) {
        covered.textContent = '还没有取景书签，先在下方保存几张再生成巡看清单。';
        $('tour-result').replaceChildren(covered);
        $('tour-result').hidden = false;
        return;
    }
    covered.textContent = `已覆盖 ${plan.coverage_degrees.toFixed(1)}°（${(plan.coverage_fraction * 100).toFixed(1)}%），` +
        `未覆盖 ${plan.uncovered_degrees.toFixed(1)}°，${plan.gap_count} 段空档，最长 ${plan.longest_gap_degrees.toFixed(1)}°。`;
    const list = document.createElement('ol');
    list.className = 'tour-list';
    for (const item of plan.selected) {
        const row = document.createElement('li');
        const recall = document.createElement('button');
        recall.type = 'button';
        recall.textContent = item.name;
        recall.title = '切换到这张取景';
        recall.addEventListener('click', run(async () => {
            mode = 'view';
            await edit('recall', {bookmark: item.id});
        }));
        const span = document.createElement('span');
        span.textContent = item.width > 0
            ? `${item.width.toFixed(1)}° 经度`
            : '赤道不可见';
        row.append(recall, span);
        list.append(row);
    }
    const ranges = document.createElement('p');
    ranges.className = 'tour-ranges';
    ranges.textContent = plan.covered.length
        ? '覆盖经度：' + plan.covered.map(formatArc).join('；')
        : '覆盖经度：无';
    const gaps = document.createElement('p');
    gaps.className = 'tour-ranges';
    gaps.textContent = plan.gaps.length
        ? '未覆盖：' + plan.gaps.map(formatArc).join('；')
        : '未覆盖：无，整圈都有取景';
    $('tour-result').replaceChildren(covered, list, ranges, gaps);
    $('tour-result').hidden = false;
}

function runTour() {
    return run(async () => {
        const limit = Number($('tour-limit').value);
        if (!Number.isInteger(limit) || limit < 1 || limit > 12) {
            throw new Error('巡看张数上限需为 1 至 12 的整数');
        }
        tourPlan = {...await json(route(current.id, 'tour') + '?limit=' + limit),
            signature: bookmarkSignature()};
        populateBookmarks();
        renderTour();
    })();
}

function populateBookmarks() {
    const order = new Map((tourPlan?.selected_ids || []).map((id, index) => [id, index + 1]));
    const cards = current.bookmarks.map(item => {
        const card = document.createElement('article');
        const rank = order.get(item.id);
        if (rank) {
            card.classList.add('tour-pick');
            const badge = document.createElement('span');
            badge.className = 'tour-badge';
            badge.textContent = String(rank);
            card.append(badge);
        }
        const image = document.createElement('img');
        image.alt = item.name + '的取景';
        image.loading = 'lazy';
        image.src = route(current.id, 'bookmark/' + item.id + '.png') + '?r=' + current.revision;
        const info = document.createElement('div');
        const title = document.createElement('h4');
        title.textContent = item.name;
        const recall = document.createElement('button');
        recall.textContent = '切换取景';
        recall.addEventListener('click', run(async () => {
            mode = 'view';
            await edit('recall', {bookmark: item.id});
        }));
        const remove = document.createElement('button');
        remove.textContent = '移除';
        remove.addEventListener('click', run(() => edit('delete-bookmark', {bookmark: item.id})));
        info.append(title, recall, remove);
        card.append(image, info);
        return card;
    });
    $('bookmark-list').replaceChildren(...cards);
}

function bindUpload(open, input, path) {
    $(open).addEventListener('click', () => $(input).click());
    $(input).addEventListener('change', run(async event => {
        const file = event.target.files[0];
        event.target.value = '';
        if (!file) return;
        const body = new FormData();
        body.append('file', file);
        mode = 'view';
        setProject(await json(path, {method: 'POST', body}));
        await projectList();
    }));
}

bindUpload('upload-open', 'upload', '/sphere/projects');
bindUpload('restore-open', 'restore', '/sphere/restore');
$('demo').addEventListener('click', run(async () => {
    mode = 'view';
    setProject(await post('/sphere/demo', {}));
    await projectList();
}));

for (const button of $('modes').querySelectorAll('button')) {
    button.addEventListener('click', () => {
        mode = button.dataset.mode;
        showMode();
    });
}

$('view-form').addEventListener('submit', run(event => {
    mode = 'view';
    return edit('view', {view: inputValues(event.target, ['yaw', 'pitch', 'roll', 'fov', 'aspect'])});
}));
$('pose-form').addEventListener('submit', run(event => {
    mode = 'leveled';
    return edit('pose', {pose: inputValues(event.target, ['yaw', 'pitch', 'roll'])});
}));
$('planet-form').addEventListener('submit', run(event => edit('planet', {
    planet: inputValues(event.target, ['spin', 'radius'])
})));
$('exposure-form').addEventListener('submit', run(event => edit('exposure', inputValues(event.target, ['exposure']))));
$('bookmark-form').addEventListener('submit', run(async () => {
    await edit('bookmark', {name: $('bookmark-name').value});
    $('bookmark-name').value = '';
}));
$('tour-run').addEventListener('click', () => runTour());
$('rename-form').addEventListener('submit', run(async () => {
    await edit('name', {name: $('new-name').value});
    await projectList();
}));

$('direction-map').addEventListener('click', run(async event => {
    const box = $('direction-map').getBoundingClientRect();
    const yaw = +(((event.clientX - box.left) / box.width) * 360 - 180).toFixed(2);
    const pitch = Math.min(89.5, Math.max(-89.5, +(90 - ((event.clientY - box.top) / box.height) * 180).toFixed(2)));
    mode = 'view';
    await edit('view', {view: {...current.view, yaw, pitch}});
    const search = new URLSearchParams({yaw, pitch});
    const probe = await json(route(current.id, 'probe') + '?' + search);
    $('probe-readout').textContent = '原图像素 (' + probe.column + ', ' + probe.row +
        ')  ·  RGBA ' + probe.rgba.join(', ');
}));

$('seam').addEventListener('click', run(async () => {
    const report = await json(route(current.id, 'seam'));
    const headline = document.createElement('p');
    const differences = report.mean_rgb_difference == null ? '无有效对照' : report.mean_rgb_difference;
    headline.textContent = `有效行 ${report.valid_rows} / ${report.rows}，平均差 ${differences}`;
    const table = document.createElement('table');
    table.setAttribute('aria-label', '按行段的接缝差');
    for (const band of report.bands) {
        const row = table.insertRow();
        row.insertCell().textContent = `${band.row_start}–${band.row_stop - 1} 行`;
        row.insertCell().textContent = band.mean_rgb_difference ?? '—';
    }
    $('seam-result').replaceChildren(headline, table);
}));

run(projectList)();
