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
const MAX_EDGES = 4;
let picking = false;
let pickedEdges = [];
let edgeReport = null;

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
    if (mode !== 'view' && picking) {
        picking = false;
        $('level-pick').setAttribute('aria-pressed', 'false');
        $('level-pick').textContent = '点选竖边';
        $('primary-image').classList.remove('picking');
        $('pick-surface').hidden = true;
        $('hero').style.cursor = '';
    }
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
    drawEdges();
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
        pickedEdges = [];
        edgeReport = null;
        picking = false;
        $('probe-readout').textContent = '点击方向图转到指定位置并读取原图像素。';
        $('seam-result').replaceChildren();
    }
    current = project;
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
    $('map-level-state').textContent = project.leveling
        ? `已按竖边扶正 · 仰角 ${project.pose.pitch.toFixed(2)}° / 旋转角 ${project.pose.roll.toFixed(2)}°`
        : '';
    $('marker').style.left = (project.view.yaw + 180) / 3.6 + '%';
    $('marker').style.top = (90 - project.view.pitch) / 1.8 + '%';
    $('marker-position').textContent = project.view.yaw.toFixed(1) + '° / ' + project.view.pitch.toFixed(1) + '°';
    showLevelingRecord();
    populateBookmarks();
    showMode();
}

async function edit(action, values) {
    const data = await post(route(current.id, 'edit'), {revision: current.revision, action, ...values});
    setProject(data);
}

function setPicking(on) {
    picking = Boolean(on) && mode === 'view' && Boolean(current);
    $('level-pick').setAttribute('aria-pressed', String(picking));
    $('level-pick').textContent = picking ? '结束点选' : '点选竖边';
    $('primary-image').classList.toggle('picking', picking);
    $('pick-surface').hidden = !picking;
    $('hero').style.cursor = picking ? 'crosshair' : '';
    drawEdges();
}

function edgeCoordinates(event) {
    const geometry = overlayGeometry();
    if (!geometry) return null;
    const x = (event.clientX - geometry.stage.left - geometry.left) / geometry.width;
    const y = (event.clientY - geometry.stage.top - geometry.top) / geometry.height;
    if (x < 0 || x > 1 || y < 0 || y > 1) return null;
    return [x, y];
}

function addPoint(point) {
    const last = pickedEdges[pickedEdges.length - 1];
    if (last && last.length === 1) {
        if (Math.hypot(point[0] - last[0][0], point[1] - last[0][1]) < 0.004) return;
        last.push(point);
    } else {
        if (pickedEdges.length >= MAX_EDGES) {
            $('level-result').dataset.state = 'warn';
            $('level-result').textContent = `最多 ${MAX_EDGES} 条竖边，请先移除一条再点选。`;
            return;
        }
        pickedEdges.push([point]);
    }
    edgeReport = null;
    refreshEdges();
}

function refreshEdges() {
    drawEdges();
    populateEdgeList();
    const pending = pickedEdges.length && pickedEdges[pickedEdges.length - 1].length === 1;
    const complete = pickedEdges.filter(edge => edge.length === 2).length;
    if (pending) {
        $('level-result').dataset.state = '';
        $('level-result').textContent = `第 ${pickedEdges.length} 条边已点一端，请点另一端。`;
    } else if (edgeReport) {
        showLevelResult();
    } else {
        $('level-result').dataset.state = '';
        $('level-result').textContent = complete >= 2
            ? `已点 ${complete} 条完整竖边，可以扶正。`
            : '每条边依次点两端；至少两条、朝向不同才可靠。';
    }
}

function overlayGeometry() {
    const stage = $('primary-image').getBoundingClientRect();
    const hero = $('hero').getBoundingClientRect();
    const width = hero.width || stage.width;
    const height = hero.height || stage.height;
    if (!width || !height) return null;
    return {stage, width, height,
        left: hero.width ? hero.left - stage.left : 0,
        top: hero.height ? hero.top - stage.top : 0};
}

function drawEdges() {
    const layer = $('edge-layer');
    const show = mode === 'view' && !$('hero').hidden;
    layer.replaceChildren();
    if (!show || !current) return;
    const geometry = overlayGeometry();
    if (!geometry) return;
    const {width, height} = geometry;
    layer.setAttribute('viewBox', `0 0 ${width} ${height}`);
    layer.style.left = geometry.left + 'px';
    layer.style.top = geometry.top + 'px';
    layer.style.width = width + 'px';
    layer.style.height = height + 'px';
    const rejected = new Set((edgeReport?.ignored ?? []).map(item => item.index));
    pickedEdges.forEach((edge, index) => {
        const group = document.createElementNS('http://www.w3.org/2000/svg', 'g');
        const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
        line.setAttribute('x1', edge[0][0] * width);
        line.setAttribute('y1', edge[0][1] * height);
        const end = edge.length === 2 ? edge[1] : edge[0];
        line.setAttribute('x2', end[0] * width);
        line.setAttribute('y2', end[1] * height);
        if (edge.length !== 2) line.classList.add('pending');
        if (rejected.has(index + 1)) line.classList.add('rejected');
        group.append(line);
        for (const point of edge) {
            const circle = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
            circle.setAttribute('cx', point[0] * width);
            circle.setAttribute('cy', point[1] * height);
            circle.setAttribute('r', 4);
            circle.classList.add('edge-end');
            if (rejected.has(index + 1)) circle.classList.add('rejected');
            group.append(circle);
        }
        const label = document.createElementNS('http://www.w3.org/2000/svg', 'text');
        label.setAttribute('x', edge[0][0] * width + 7);
        label.setAttribute('y', edge[0][1] * height - 7);
        label.textContent = String(index + 1);
        group.append(label);
        layer.append(group);
    });
}

function populateEdgeList() {
    const rows = pickedEdges.map((edge, index) => {
        const item = document.createElement('li');
        const swatch = document.createElement('span');
        swatch.className = 'edge-swatch' +
            ((edgeReport?.ignored ?? []).some(entry => entry.index === index + 1) ? ' rejected' : '');
        const name = document.createElement('span');
        name.className = 'edge-name';
        name.textContent = `竖边 ${index + 1}` +
            (edge.length === 2 ? '' : '（等待另一端）') +
            (edgeReport?.ignored?.some(entry => entry.index === index + 1) ? ' · 不一致，已忽略' : '') +
            (edgeReport?.usable?.includes(index + 1) ? ' · 已采用' : '');
        const redo = document.createElement('button');
        redo.type = 'button';
        redo.textContent = '重画';
        redo.addEventListener('click', () => {
            pickedEdges.splice(index, 1);
            edgeReport = null;
            setPicking(true);
            refreshEdges();
        });
        const remove = document.createElement('button');
        remove.type = 'button';
        remove.textContent = '移除';
        remove.addEventListener('click', () => {
            pickedEdges.splice(index, 1);
            edgeReport = null;
            refreshEdges();
        });
        item.append(swatch, name, redo, remove);
        return item;
    });
    $('edge-list').replaceChildren(...rows);
    $('level-apply').disabled = pickedEdges.length < 2 ||
        pickedEdges.some(edge => edge.length !== 2);
}

function showLevelResult() {
    const box = $('level-result');
    if (!edgeReport) {
        box.textContent = '';
        box.dataset.state = '';
        return;
    }
    box.dataset.state = edgeReport.applied ? 'ok' : 'warn';
    box.textContent = edgeReport.message;
}

function showLevelingRecord() {
    const record = current?.leveling;
    const box = $('leveling-record');
    if (!record) {
        box.textContent = '';
        return;
    }
    const ignored = record.ignored.length ? `，忽略第 ${record.ignored.join('、')} 条` : '';
    box.textContent = `当前姿态由 ${record.evidence} 个独立方向的 ${record.usable.length} 条竖边扶正` +
        `（残差 ${record.residual_degrees.toFixed(2)}°，不确定度 ${record.uncertainty_degrees.toFixed(2)}°${ignored}）。` +
        '仍可手动微调。';
}

async function applyLevel() {
    const edges = pickedEdges.filter(edge => edge.length === 2);
    const data = await post(route(current.id, 'level'),
        {revision: current.revision, edges: edges.map(edge => ({a: edge[0], b: edge[1]}))});
    edgeReport = data.report;
    if (data.report.applied) {
        // The rendered view moved with the pose, so old pixel picks no longer
        // mark the same edges.
        pickedEdges = [];
        setProject(data.project);
        mode = 'leveled';
        showMode();
    }
    refreshEdges();
    showLevelResult();
}

function populateBookmarks() {
    const cards = current.bookmarks.map(item => {
        const card = document.createElement('article');
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
$('pose-form').addEventListener('submit', run(async event => {
    // A manual pose invalidates any earlier vertical-edge correction.
    pickedEdges = [];
    edgeReport = null;
    mode = 'leveled';
    await edit('pose', {pose: inputValues(event.target, ['yaw', 'pitch', 'roll'])});
    refreshEdges();
}));
$('level-pick').addEventListener('click', () => {
    if (mode !== 'view') {
        mode = 'view';
        showMode();
    }
    setPicking(!picking);
});
$('level-apply').addEventListener('click', run(applyLevel));
$('pick-surface').addEventListener('pointerdown', event => {
    if (!picking) return;
    event.preventDefault();
    const point = edgeCoordinates(event);
    if (point) addPoint(point);
});
document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && picking) setPicking(false);
});
window.addEventListener('resize', drawEdges);
$('hero').addEventListener('load', drawEdges);
$('planet-form').addEventListener('submit', run(event => edit('planet', {
    planet: inputValues(event.target, ['spin', 'radius'])
})));
$('exposure-form').addEventListener('submit', run(event => edit('exposure', inputValues(event.target, ['exposure']))));
$('bookmark-form').addEventListener('submit', run(async () => {
    await edit('bookmark', {name: $('bookmark-name').value});
    $('bookmark-name').value = '';
}));
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
