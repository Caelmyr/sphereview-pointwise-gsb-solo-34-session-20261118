import io

from flask import Blueprint, jsonify, render_template, request, send_file

from image_lab import validation as v
from . import imaging, projection, state, tour
from .storage import MAX_ARCHIVE, Workspace, archive_bytes, record_bytes


def receive_upload(limit):
    file = request.files.get('file')
    if file is None or not file.filename:
        raise v.Invalid('请选择文件')
    return file, file.stream.read(limit + 1)


def respond(data, name, mimetype, download=False):
    response = send_file(io.BytesIO(data), mimetype=mimetype, as_attachment=download,
                         download_name=name, max_age=0)
    response.headers['Cache-Control'] = 'no-store'
    return response


def png_response(pixels, name):
    return respond(imaging.png(pixels), name, 'image/png', request.args.get('download') == '1')


def register(app, root):
    workspace = Workspace(root)
    api = Blueprint('sphere', __name__, url_prefix='/sphere')

    @app.get('/')
    def home():
        return render_template('sphereview.html')

    @api.get('/health')
    def health():
        return jsonify(product='SphereView', status='ok')

    @api.get('/projects')
    def projects():
        return jsonify(workspace.list())

    @api.post('/projects')
    def create():
        file, raw = receive_upload(imaging.MAX_UPLOAD)
        suggested = file.filename.replace('\\', '/').split('/')[-1].rsplit('.', 1)[0]
        image = imaging.decode(raw)
        return jsonify(workspace.create(image, suggested[:80] or '全景照片')), 201

    @api.post('/demo')
    def demo():
        return jsonify(workspace.create(imaging.demo(), '蓝湾观景台 · 合成演示', True)), 201

    @api.post('/restore')
    def restore():
        _, raw = receive_upload(MAX_ARCHIVE)
        return jsonify(workspace.restore(raw)), 201

    @api.get('/projects/<key>')
    def get(key):
        return jsonify(workspace.get(key))

    @api.post('/projects/<key>/edit')
    def edit(key):
        return jsonify(workspace.edit(key, v.object_value(request.get_json(silent=True))))

    @api.get('/projects/<key>/source.png')
    def source(key):
        return respond(workspace.source_bytes(workspace.get(key)), 'source.png', 'image/png',
                       request.args.get('download') == '1')

    @api.get('/projects/<key>/view.png')
    def view(key):
        project = workspace.get(key)
        return png_response(projection.view_image(workspace.source(project), project['view'],
                                                 project['pose'], project['exposure']), 'view.png')

    @api.get('/projects/<key>/leveled.png')
    def leveled(key):
        project = workspace.get(key)
        source = workspace.source(project)
        width = source.shape[1] if request.args.get('download') == '1' else 960
        return png_response(projection.leveled_image(source, project['pose'], project['exposure'], width),
                            'leveled.png')

    @api.get('/projects/<key>/planet.png')
    def planet(key):
        project = workspace.get(key)
        return png_response(projection.planet_image(workspace.source(project), project['pose'],
                            project['exposure'], project['planet'], 640), 'planet.png')

    @api.get('/projects/<key>/cube/<face>.png')
    def cube(key, face):
        v.choice(face, '立方体面', projection.FACES)
        project = workspace.get(key)
        return png_response(projection.cube_face(workspace.source(project), project['pose'],
                            project['exposure'], face, 384), face + '.png')

    @api.get('/projects/<key>/cube.zip')
    def cube_export(key):
        project = workspace.get(key)
        source = workspace.source(project)
        faces = {
            face + '.png': imaging.png(projection.cube_face(source, project['pose'],
                                                           project['exposure'], face, 384))
            for face in projection.FACES
        }
        faces['faces.json'] = record_bytes({
            'source': project['source'], 'pose': project['pose'], 'exposure': project['exposure'],
            'edge': 384, 'faces_yaw_pitch_degrees': projection.FACES,
            'axes': 'right=+X, up=+Y, front=+Z, image Y down',
        })
        return respond(archive_bytes(faces), 'cube.zip', 'application/zip', True)

    @api.get('/projects/<key>/bookmark/<bookmark>.png')
    def bookmark_preview(key, bookmark):
        project = workspace.get(key)
        snapshot = state.find_bookmark(project, bookmark)
        return png_response(projection.view_image(workspace.source(project), snapshot['view'],
                            project['pose'], project['exposure'], 256), 'bookmark.png')

    @api.get('/projects/<key>/tour.gif')
    def tour_gif(key):
        project = workspace.get(key)
        start = state.find_bookmark(project, request.args.get('from'))
        stop = state.find_bookmark(project, request.args.get('to'))
        if start['id'] == stop['id']:
            raise v.Invalid('起点和终点不能是同一张书签')
        yaw = v.number(request.args.get('yaw'), '入口水平角', -180, 180)
        pitch = v.number(request.args.get('pitch'), '入口仰角', -90, 90)
        data = tour.render_gif(workspace.source(project), project, start, stop, yaw, pitch)
        return respond(data, 'tour.gif', 'image/gif', request.args.get('download') == '1')

    @api.get('/projects/<key>/probe')
    def probe(key):
        project = workspace.get(key)
        yaw = v.number(request.args.get('yaw'), '探针水平角', -180, 180)
        pitch = v.number(request.args.get('pitch'), '探针仰角', -90, 90)
        return jsonify(projection.probe(workspace.source(project), project['pose'], yaw, pitch))

    @api.get('/projects/<key>/seam')
    def seam(key):
        project = workspace.get(key)
        return jsonify(imaging.seam_report(workspace.source(project)))

    @api.get('/projects/<key>/project.zip')
    def project_export(key):
        project = workspace.get(key)
        source = workspace.source_bytes(project)
        view = projection.view_image(imaging.decode(source), project['view'], project['pose'],
                                     project['exposure'])
        contents = {
            'source.png': source, 'view.png': imaging.png(view),
            'project.json': record_bytes({'format': 'sphereview-1', 'project': project}),
        }
        return respond(archive_bytes(contents), 'sphereview-project.zip', 'application/zip', True)

    app.register_blueprint(api)
