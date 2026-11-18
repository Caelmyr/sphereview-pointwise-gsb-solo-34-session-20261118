import hashlib
import io
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import RLock
from uuid import uuid4
from zipfile import BadZipFile, ZIP_DEFLATED, ZipFile

from image_lab import validation as v
from . import imaging
from . import leveling
from . import state

MAX_ARCHIVE = 48 * 1024 * 1024


def atomic_bytes(path, data):
    with NamedTemporaryFile(dir=path.parent, prefix='.pending-', delete=False) as fd:
        temporary = Path(fd.name)
        try:
            fd.write(data)
            fd.flush()
            os.fsync(fd.fileno())
            fd.close()
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def record_bytes(data):
    return json.dumps(data, ensure_ascii=False, indent=2).encode('utf-8')


def archive_bytes(items):
    output = io.BytesIO()
    with ZipFile(output, 'w', ZIP_DEFLATED, compresslevel=3) as bundle:
        for name, content in items.items():
            bundle.writestr(name, content)
    return output.getvalue()


class Workspace:
    def __init__(self, root):
        self.root = Path(root)
        self.sources = self.root / 'sources'
        self.records = self.root / 'projects'
        self.sources.mkdir(parents=True, exist_ok=True)
        self.records.mkdir(parents=True, exist_ok=True)
        self.lock = RLock()

    def record_path(self, key):
        return self.records / (v.identifier(key) + '.json')

    def source_path(self, key):
        return self.sources / (v.sha(key) + '.png')

    def list(self):
        with self.lock:
            metadata = [json.loads(path.read_bytes()) for path in self.records.glob('*.json')]
        rows = sorted(metadata, key=lambda rec: rec['created_at'], reverse=True)
        return [{key: row[key] for key in ('id', 'name', 'source', 'revision', 'synthetic')} for row in rows]

    def get(self, key):
        path = self.record_path(key)
        try:
            return json.loads(path.read_bytes())
        except FileNotFoundError as exc:
            raise v.Invalid('全景项目不存在') from exc

    def create(self, image, name, synthetic=False, edits=state.OMIT):
        name = state.project_name(name)
        configuration = state.rehydrate(edits)
        canonical = imaging.png(image)
        digest = hashlib.sha256(canonical).hexdigest()
        source_path = self.source_path(digest)
        item = {
            'id': uuid4().hex,
            'name': name,
            'source': {'sha256': digest, 'width': image.shape[1], 'height': image.shape[0]},
            'revision': 0,
            'synthetic': bool(synthetic),
            'created_at': datetime.now(timezone.utc).isoformat(),
            **configuration,
        }
        with self.lock:
            if not source_path.exists():
                atomic_bytes(source_path, canonical)
            atomic_bytes(self.record_path(item['id']), record_bytes(item))
        return item

    def edit(self, key, body):
        revision = v.revision(body)
        with self.lock:
            record = self.get(key)
            if record['revision'] != revision:
                raise v.Conflict('项目已更新，请重新载入后再修改')
            updated = state.patch(record, body.get('action'), body)
            updated['revision'] += 1
            atomic_bytes(self.record_path(key), record_bytes(updated))
        return updated

    def level(self, key, body):
        """Try a vertical-edge level from the stored pose; save only when it converges."""
        revision = v.revision(body)
        edges = leveling.clean_edges(body.get('edges'))
        with self.lock:
            record = self.get(key)
            if record['revision'] != revision:
                raise v.Conflict('项目已更新，请重新载入后再修改')
            report = leveling.solve(record['view'], record['pose'], edges)
            if not report['applied']:
                return {'report': report, 'project': record}
            updated = state.apply_level(record, report)
            updated['revision'] += 1
            atomic_bytes(self.record_path(key), record_bytes(updated))
        return {'report': report, 'project': updated}

    def source_bytes(self, project):
        digest = project['source']['sha256']
        raw = self.source_path(digest).read_bytes()
        if hashlib.sha256(raw).hexdigest() != digest:
            raise v.Invalid('项目底图已损坏')
        return raw

    def source(self, project):
        return imaging.decode(self.source_bytes(project))

    def restore(self, data):
        if len(data) > MAX_ARCHIVE:
            raise v.Invalid('工程ZIP超过48MB')
        try:
            with ZipFile(io.BytesIO(data)) as bundle:
                record = read_member(bundle, 'project.json', 128 * 1024)
                raw = read_member(bundle, 'source.png', imaging.MAX_UPLOAD)
                document = v.object_value(json.loads(record), '工程档案')
                if document.get('format') != 'sphereview-1':
                    raise v.Invalid('无法识别工程版本')
                edits = v.object_value(document.get('project'), '项目')
                source = v.object_value(edits.get('source'), '来源')
                if hashlib.sha256(raw).hexdigest() != v.sha(source.get('sha256')):
                    raise v.Invalid('源图校验不符')
                synthetic = v.boolean(edits.get('synthetic'), '素材类型')
                image = imaging.decode(raw)
                return self.create(image, edits.get('name'), synthetic, edits)
        except (BadZipFile, EOFError, OSError, KeyError, RuntimeError,
                UnicodeError, ValueError) as exc:
            if isinstance(exc, v.Invalid):
                raise
            raise v.Invalid('无法读取SphereView工程ZIP') from exc


def read_member(bundle, name, maximum):
    if sum(item.filename == name for item in bundle.infolist()) != 1:
        raise v.Invalid('工程内文件缺失或重复')
    member = bundle.getinfo(name)
    if member.file_size > maximum:
        raise v.Invalid('工程中的单个文件过大')
    with bundle.open(member) as handle:
        content = handle.read(maximum + 1)
    if len(content) > maximum:
        raise v.Invalid('工程中的单个文件过大')
    return content
