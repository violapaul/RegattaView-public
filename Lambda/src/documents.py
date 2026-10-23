"""Validated collaborative writes. Never issue raw PUT URLs for these documents."""
import copy
import hashlib
import hmac
import json
import math
import re
import time
import uuid
from botocore.exceptions import ClientError

import utils

BUCKET = 'regattaanalysis'
EDIT_KEY = 'www/Analyze/events_edits.json'
CURATED_KEY = 'www/Insta360/curated.json'
INDEX_KEY = 'www/Comments/index.json'
MAX_BYTES = 1024 * 1024
ALIAS = re.compile(r'^[A-Za-z0-9_-]{1,30}$')
IDENT = re.compile(r'^[A-Za-z0-9_.:@+ /-]{1,240}$')


class DocumentError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def obj(value, allowed, required=()):
    if not isinstance(value, dict) or set(value) - set(allowed) or not set(required) <= set(value):
        raise DocumentError('Invalid document fields')


def text(value, limit=10000, empty=True):
    if not isinstance(value, str) or len(value) > limit or (not empty and not value) or '\x00' in value:
        raise DocumentError('Invalid text')


def number(value, low=0, high=1e12):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
        raise DocumentError('Invalid number')


def latlng(value):
    if value is None: return
    if not isinstance(value, list) or len(value) != 2:
        raise DocumentError('Invalid position')
    number(value[0], low=-90, high=90); number(value[1], low=-180, high=180)


def ident(value):
    if not isinstance(value, str) or not IDENT.fullmatch(value) or value in ('__proto__', 'constructor', 'prototype'):
        raise DocumentError('Invalid identifier')


def validate(key, data):
    if key == EDIT_KEY:
        obj(data, ('version', 'updated_ts', 'edits'), ('version', 'edits'))
        if not isinstance(data['edits'], dict) or len(data['edits']) > 10000:
            raise DocumentError('Invalid edits')
        for name, edit in data['edits'].items():
            ident(name)
            obj(edit, ('status', 'notes', 'trim_start_s', 'trim_end_s', 'updated_ts',
                       'line_override', 'marks', 'label_schema_version'))
            if 'status' in edit and edit['status'] not in ('pending', 'confirmed', 'discarded'):
                raise DocumentError('Invalid review status')
            if 'notes' in edit: text(edit['notes'])
            if 'label_schema_version' in edit and (type(edit['label_schema_version']) is not int
                                                   or edit['label_schema_version'] not in (1, 2)):
                raise DocumentError('Invalid label schema version')
            if edit.get('line_override') is not None:
                obj(edit['line_override'], ('pin_ll', 'committee_ll'))
                for ll in edit['line_override'].values(): latlng(ll)
            if edit.get('marks') is not None:
                obj(edit['marks'], ('windward_ll', 'leeward_ll'))
                for ll in edit['marks'].values(): latlng(ll)
            for field in ('trim_start_s', 'trim_end_s', 'updated_ts'):
                if field in edit and edit[field] is not None: number(edit[field], low=-86400 if field.startswith('trim') else 0)
    elif key == CURATED_KEY:
        obj(data, ('version', 'events', 'playlists'), ('version', 'events', 'playlists'))
        for field in ('events', 'playlists'):
            if not isinstance(data[field], list) or len(data[field]) > 5000:
                raise DocumentError('Too many events or playlists')
        for event in data['events']:
            obj(event, ('id', 'video', 'time', 'type', 'comment', 'source', 'date'), ('id', 'video', 'time'))
            ident(event['id']); ident(event['video']); number(event['time'], high=86400)
            for field in ('type', 'source', 'date'):
                if field in event: text(event[field], 100)
            if 'comment' in event: text(event['comment'])
        for playlist in data['playlists']:
            obj(playlist, ('id', 'title', 'date', 'source', 'padding', 'events'), ('id', 'title', 'events'))
            ident(playlist['id']); text(playlist['title'], 300)
            for field in ('date', 'source'):
                if field in playlist: text(playlist[field], 100)
            if 'padding' in playlist: number(playlist['padding'], high=3600)
            if not isinstance(playlist['events'], list) or len(playlist['events']) > 10000:
                raise DocumentError('Invalid playlist events')
            for name in playlist['events']: ident(name)
    else:
        raise DocumentError('Document is not writable', 403)
    if type(data['version']) is not int or data['version'] != 1:
        raise DocumentError('Unsupported document version')
    if 'updated_ts' in data: number(data['updated_ts'])
    encode(data)


def encode(data):
    try: raw = json.dumps(data, allow_nan=False, separators=(',', ':')).encode()
    except (ValueError, TypeError): raise DocumentError('Invalid JSON document')
    if len(raw) > MAX_BYTES: raise DocumentError('Document is too large', 413)
    return raw


def read(s3, key):
    try:
        response = s3.get_object(Bucket=BUCKET, Key=key)
        raw = response['Body'].read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES: raise DocumentError('Document is too large', 413)
        return json.loads(raw), response['ETag']
    except ClientError as e:
        code = e.response['Error']['Code']
        if code in ('NoSuchKey', '404'): return None, None
        # GetObject can report 403 for an absent key when ListBucket is scoped.
        if code in ('AccessDenied', '403'):
            rows = s3.list_objects_v2(Bucket=BUCKET, Prefix=key, MaxKeys=1).get('Contents', [])
            if not rows or rows[0]['Key'] != key: return None, None
        raise


def write(s3, key, data, etag):
    condition = {'IfMatch': etag} if etag else {'IfNoneMatch': '*'}
    try:
        response = s3.put_object(Bucket=BUCKET, Key=key, Body=encode(data),
                                 ContentType='application/json', CacheControl='private, no-store', **condition)
        return response['ETag']
    except ClientError as e:
        if e.response['Error']['Code'] in ('PreconditionFailed', 'ConditionalRequestConflict', '412', '409'):
            raise DocumentError('Changed by another editor. Reload before saving.', 409)
        raise


def mutate(s3, key, transform):
    for _ in range(5):
        value, etag = read(s3, key)
        updated = transform(value)
        try:
            write(s3, key, updated, etag)
            return updated
        except DocumentError as e:
            if e.status != 409: raise
    raise DocumentError('Another save is in progress. Please retry.', 409)


def alias_key(alias):
    if not isinstance(alias, str) or not ALIAS.fullmatch(alias):
        raise DocumentError('Use 1–30 letters, numbers, underscores or hyphens for your name')
    return f'www/Comments/{alias}/comments.json'


def owner_id(identity):
    # Stable opaque owner ID; email addresses are not published in team documents.
    # Keyed with the session secret: a bare hash would let any reader confirm a
    # guessed email. Rotating the secret orphans existing alias ownership.
    secret = utils._session_secret()
    if not secret: raise DocumentError('Comment ownership is not configured', 500)
    return hmac.new(secret, b'owner:' + identity.lower().encode(), hashlib.sha256).hexdigest()


def claim(s3, alias, identity):
    key = alias_key(alias)
    def transform(value):
        if value is not None:
            if value.get('owner_id') != owner_id(identity):
                raise DocumentError('That comment name belongs to another account', 403)
            return value
        return {'version': 1, 'alias': alias, 'owner_id': owner_id(identity), 'comments': []}
    mutate(s3, key, transform)
    now = int(time.time())
    def index(value):
        result = value or {'version': 1, 'users': []}
        users = result['users']
        current = next((u for u in users if u['alias'] == alias), None)
        if current is None:
            current = {'alias': alias, 'first_seen_ts': now}
            users.append(current)
        current['last_seen_ts'] = now
        result['updated_ts'] = now
        return result
    mutate(s3, INDEX_KEY, index)


def comment_payload(body):
    obj(body, ('text', 'target'), ('text', 'target'))
    text(body['text'], empty=False)
    target = body['target']
    if not isinstance(target, dict): raise DocumentError('Invalid comment target')
    if target.get('kind') == 'sailview':
        obj(target, ('kind', 'video', 't'), ('kind', 'video', 't'))
        ident(target['video']); number(target['t'], high=86400)
    elif target.get('kind') == 'review':
        obj(target, ('kind', 'start_id'), ('kind', 'start_id'))
        ident(target['start_id'])
    else: raise DocumentError('Invalid comment target')


def handle(s3, body, identity):
    method = body['method']
    if method in ('READ_JSON', 'SAVE_JSON'):
        key = body.get('filename')
        if not isinstance(key, str) or key not in (EDIT_KEY, CURATED_KEY, INDEX_KEY) and not re.fullmatch(r'www/Comments/[A-Za-z0-9_-]{1,30}/comments\.json', key):
            raise DocumentError('Document is not allowed', 403)
        if method == 'READ_JSON':
            data, etag = read(s3, key)
            return {'data': data, 'etag': etag}
        data = body.get('data')
        validate(key, data)
        if 'etag' not in body or body['etag'] is not None and not re.fullmatch(r'"[a-fA-F0-9-]{1,80}"', str(body['etag'])):
            raise DocumentError('Read the current document before saving', 409)
        return {'etag': write(s3, key, data, body['etag'])}
    alias = body.get('alias')
    if method == 'REGISTER_ALIAS':
        claim(s3, alias, identity)
        return {'ok': True}
    if method == 'APPEND_COMMENT':
        incoming = body.get('comment')
        comment_payload(incoming)
        key = alias_key(alias)
        comment = {
            'id': 'c-' + uuid.uuid4().hex, 'created_ts': int(time.time())}
        comment.update(copy.deepcopy(incoming))
        def transform(value):
            if not value or value.get('owner_id') != owner_id(identity):
                raise DocumentError('This comment name is not registered to your account', 403)
            if len(value['comments']) >= 5000: raise DocumentError('Comment limit reached', 413)
            value['comments'].append(comment)
            value['updated_ts'] = comment['created_ts']
            return value
        mutate(s3, key, transform)
        return {'comment': comment}
    raise DocumentError('Unsupported document operation')
