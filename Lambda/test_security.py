"""Regression tests for authorization, injection payloads, and concurrent writes."""
import base64
import hashlib
import io
import json
import os
import sys
import time
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))
import documents as docs
import lambda_function as lf
import utils
from botocore.exceptions import ClientError


def error(code):
    return ClientError({'Error': {'Code': code}}, 'SyntheticTest')


class S3:
    def __init__(self): self.objects = {}
    def get_object(self, Bucket, Key):
        if Key not in self.objects: raise error('NoSuchKey')
        raw, tag = self.objects[Key]
        return {'Body': io.BytesIO(raw), 'ETag': tag}
    def put_object(self, Bucket, Key, Body, **kwargs):
        old = self.objects.get(Key)
        if kwargs.get('IfNoneMatch') == '*' and old or 'IfMatch' in kwargs and (not old or old[1] != kwargs['IfMatch']):
            raise error('PreconditionFailed')
        tag = '"' + hashlib.md5(Body).hexdigest() + '"'
        self.objects[Key] = (Body, tag)
        return {'ETag': tag}


class Documents(unittest.TestCase):
    def setUp(self):
        self.s3 = S3()
        secret = patch.object(utils, 'SESSION_SECRET', b'test-secret'); secret.start(); self.addCleanup(secret.stop)
    def test_owner_id_is_keyed_not_bare_email_hash(self):
        self.assertNotEqual(docs.owner_id('alice@example.invalid'), hashlib.sha256(b'alice@example.invalid').hexdigest())
        self.assertEqual(docs.owner_id('Alice@Example.invalid'), docs.owner_id('alice@example.invalid'))
    def test_label_fields_validate(self):
        edit = {'status':'confirmed','label_schema_version':2,'line_override':{'pin_ll':[47.6,-122.3],'committee_ll':None},'marks':{'windward_ll':[47.7,-122.4]}}
        docs.validate(docs.EDIT_KEY, {'version':1,'edits':{'race-1':edit}})
        for bad in [{'marks':{'windward_ll':[91,0]}}, {'marks':{'top_ll':[0,0]}}, {'line_override':{'pin_ll':'<img>'}},
                    {'line_override':{'pin_ll':[1,2,3]}}, {'label_schema_version':True}, {'updated_by':'x'}]:
            with self.assertRaises(docs.DocumentError, msg=bad): docs.validate(docs.EDIT_KEY, {'version':1,'edits':{'race-1':bad}})
    def call(self, user='alice@example.invalid', **body): return docs.handle(self.s3, body, user)
    def test_ownership_survives_another_account_claim_and_append(self):
        self.call(method='REGISTER_ALIAS', alias='Alice')
        self.call(method='REGISTER_ALIAS', alias='Alice')
        for method, extra in [('REGISTER_ALIAS', {}), ('APPEND_COMMENT', {'comment': {'text':'spoof', 'target':{'kind':'review','start_id':'race-1'}}})]:
            with self.assertRaises(docs.DocumentError) as cm:
                self.call(user='bob@example.invalid', method=method, alias='Alice', **extra)
            self.assertEqual(cm.exception.status, 403)
        result = self.call(method='APPEND_COMMENT', alias='Alice', comment={'text':'<img onerror=alert(1)>','target':{'kind':'sailview','video':'VID_1','t':2}})
        self.assertEqual(result['comment']['text'], '<img onerror=alert(1)>')
        self.assertNotIn('alice@example.invalid', str(self.s3.objects))
    def test_distinct_aliases_and_comments_are_preserved(self):
        for name in ['Alice','Bob']:
            self.call(user=name, method='REGISTER_ALIAS', alias=name)
            for i in range(2): self.call(user=name, method='APPEND_COMMENT', alias=name, comment={'text':str(i),'target':{'kind':'review','start_id':'race-1'}})
        index, _ = docs.read(self.s3, docs.INDEX_KEY)
        self.assertEqual(len(index['users']), 2)
        self.assertEqual(len(docs.read(self.s3, docs.alias_key('Alice'))[0]['comments']), 2)
    def test_global_documents_require_matching_revision(self):
        original = {'version':1,'edits':{}}
        first = self.call(method='SAVE_JSON', filename=docs.EDIT_KEY, data=original, etag=None)
        changed = {'version':1,'edits':{'race-1':{'status':'confirmed','notes':'hello'}}}
        self.call(method='SAVE_JSON', filename=docs.EDIT_KEY, data=changed, etag=first['etag'])
        for stale in [None, first['etag']]:
            with self.assertRaises(docs.DocumentError) as cm:
                self.call(method='SAVE_JSON', filename=docs.EDIT_KEY, data=original, etag=stale)
            self.assertEqual(cm.exception.status, 409)
        self.assertEqual(docs.read(self.s3, docs.EDIT_KEY)[0], changed)
    def test_script_status_and_arbitrary_paths_are_rejected(self):
        for status in ['<img onerror=alert(1)>', 'confirmed" onclick="alert(1)', ['confirmed']]:
            with self.assertRaises(docs.DocumentError):
                self.call(method='SAVE_JSON', filename=docs.EDIT_KEY, data={'version':1,'edits':{'race':{'status':status}}}, etag=None)
        for key in ['auth/allowlist.json','www/sailview/js/login_pack.js',docs.INDEX_KEY,docs.alias_key('Alice')]:
            with self.assertRaises(docs.DocumentError):
                self.call(method='SAVE_JSON', filename=key, data={}, etag=None)
    def test_curated_schema_and_bounded_values(self):
        value={'version':1,'events':[{'id':'curated-1','video':'VID_1','time':0,'type':'start','comment':'A & B'}], 'playlists':[{'id':'curated-list','title':'<em>text</em>','events':['curated-1'],'padding':10}]}
        self.call(method='SAVE_JSON',filename=docs.CURATED_KEY,data=value,etag=None)
        for bad in [float('inf'), float('nan'), -1, True]:
            value['events'][0]['time']=bad
            with self.assertRaises(docs.DocumentError): docs.validate(docs.CURATED_KEY,value)
    def test_comment_cannot_forge_metadata(self):
        with self.assertRaises(docs.DocumentError):
            self.call(method='APPEND_COMMENT',alias='Alice',comment={'text':'x','created_ts':1,'target':{'kind':'review','start_id':'x'}})
    def test_absent_403_does_not_hide_real_permissions_error(self):
        s3=Mock();s3.get_object.side_effect=error('AccessDenied');s3.list_objects_v2.return_value={'Contents':[]}
        self.assertEqual(docs.read(s3,docs.EDIT_KEY),(None,None))
        s3.list_objects_v2.return_value={'Contents':[{'Key':docs.EDIT_KEY}]}
        with self.assertRaises(ClientError):docs.read(s3,docs.EDIT_KEY)
    def test_conflicting_comment_append_retries_without_loss(self):
        self.call(method='REGISTER_ALIAS',alias='Alice')
        original=self.s3.put_object; once=[True]
        def conflict(**kwargs):
            if once[0]: once[0]=False;raise error('PreconditionFailed')
            return original(**kwargs)
        with patch.object(self.s3,'put_object',side_effect=conflict):
            self.call(method='APPEND_COMMENT',alias='Alice',comment={'text':'retry','target':{'kind':'review','start_id':'x'}})
        self.assertEqual(len(docs.read(self.s3,docs.alias_key('Alice'))[0]['comments']),1)


class Authentication(unittest.TestCase):
    def test_allowlist_outage_is_503_not_removal(self):
        with patch.object(utils,'verify_session_token',return_value='a@example.invalid'),patch.object(utils,'get_allowlist',return_value=None),patch.object(lf.boto3,'client'):
            result=lf.lambda_handler({'body':json.dumps({'session_token':'t','method':'TEST'}),'requestContext':{'http':{'method':'POST'}}},None)
        self.assertEqual((result['statusCode'],json.loads(result['body'])['reason']),(503,'allowlist unavailable'))
    def test_interactive_users_cannot_sign_upload_puts(self):
        self.assertIsNotNone(lf._signing_policy_reason('PUT','montlakeupload','x','a@example.invalid'))
        self.assertIsNone(lf._signing_policy_reason('PUT','montlakeupload','x',lf.LEGACY_USERNAME))
    def test_stale_allowlist_fails_closed(self):
        cached={'data':[{'email':'revoked@example.invalid'}],'fetched_at':time.time()-86400}
        s3=Mock();s3.get_object.side_effect=error('AccessDenied')
        with patch.object(utils,'_allowlist_cache',cached): self.assertIsNone(utils.get_allowlist(s3))
    def test_unknown_kid_refresh_is_bounded_and_recovers(self):
        token=utils._b64url_encode(json.dumps({'alg':'RS256','kid':'unknown'}))+'.e30.AA'
        cache={'keys':{'old':{}},'fetched_at':1000}
        with patch.object(utils,'_jwks_cache',cache),patch.object(utils,'_jwks_last_attempt',0),patch.object(utils,'GOOGLE_CLIENT_ID','test'),patch.object(utils.time,'time',return_value=1000) as clock,patch.object(utils,'_fetch_google_jwks_direct',return_value={'keys':[{'kid':'old'}]}) as fetch:
            for _ in range(3): self.assertIsNone(utils.verify_google_id_token(token,Mock()))
            self.assertEqual(fetch.call_count,1)
            clock.return_value=1061
            self.assertIsNone(utils.verify_google_id_token(token,Mock()))
            self.assertEqual(fetch.call_count,2)
    def test_bad_bodies_return_400_without_creating_client(self):
        for raw in ['not-json','[]','123','null','{"id_token":123}','{"session_token":{}}', '[' * 2000 + '0' + ']' * 2000]:
            with patch.object(lf.boto3,'client') as client:
                result=lf.lambda_handler({'body':raw,'requestContext':{'http':{'method':'POST'}}},None)
                self.assertEqual(result['statusCode'],400,raw);client.assert_not_called()
    def test_malformed_tokens_fail_without_exceptions(self):
        for token in [None,[],{},123,'not-a-token','é.é.é',utils._b64url_encode('[]')+'.e30.AA']:
            self.assertIsNone(utils.verify_session_token(token))
            self.assertIsNone(utils.verify_google_id_token(token,Mock()))
    def test_no_raw_shared_put_even_if_old_feature_flag_disabled(self):
        with patch.object(lf,'SIGNING_POLICY_ENFORCE',False),patch.object(lf,'_authenticate',return_value=('alice',3600,None,None)),patch.object(lf.boto3,'client'):
            result=lf.lambda_handler({'body':json.dumps({'method':'PUT','bucket':'regattaanalysis','filename':docs.EDIT_KEY,'exact':True}),'requestContext':{'http':{'method':'POST'}}},None)
            self.assertEqual(result['statusCode'],403)

if __name__=='__main__':unittest.main()
