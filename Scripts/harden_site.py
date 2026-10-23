#!/usr/bin/env python3
"""Review/apply RegattaView infrastructure safeguards; writes require --apply.

Saves the current configurations before changing them. Existing unrelated
CloudFront behaviors and bucket lifecycle rules are preserved.
"""
import argparse
import copy
import json
from pathlib import Path
import boto3

DIST = 'EPCTF7XCNPLK2'
FUNCTION = 'montlake_upload'
BUCKET = 'regattaanalysis'
LOG_BUCKET = 'regattaanalysis-logs'
LOG_GROUP = '/aws/lambda/montlake_upload'
POLICY_NAME = 'RegattaViewSecurityHeaders'
# Explicit script locations, never unsafe-inline/unsafe-eval for scripts.
CSP = "; ".join([
    "default-src 'self'",
    "script-src 'self' https://accounts.google.com/gsi/client https://accounts.google.com/gsi/ https://unpkg.com/leaflet@1.9.4/dist/leaflet.js https://cdn.jsdelivr.net/npm/leaflet-rotate@0.2.8/dist/leaflet-rotate-src.js https://cdnjs.cloudflare.com/ajax/libs/font-awesome/5.12.0-2/js/all.min.js",
    "style-src 'self' 'unsafe-inline' https://accounts.google.com/gsi/style https://cdnjs.cloudflare.com https://unpkg.com",
    "connect-src 'self' https://blsywilg2fba6564nixqxwntyy0iagoj.lambda-url.us-west-2.on.aws https://regattaanalysis.s3.us-west-2.amazonaws.com https://regattaanalysis.s3.amazonaws.com https://accounts.google.com/gsi/ https://accounts.google.com/o/fedcm/",
    "img-src 'self' data: blob: https://*.tile.openstreetmap.org https://unpkg.com https://regattaanalysis.s3.us-west-2.amazonaws.com",
    "media-src 'self' blob: https://regattaanalysis.s3.us-west-2.amazonaws.com https://regattaanalysis.s3.amazonaws.com",
    "font-src 'self' data:", "frame-src 'self' https://accounts.google.com/gsi/", "worker-src 'self' blob:",
    "object-src 'none'", "base-uri 'none'", "form-action 'self'", "frame-ancestors 'self'", "upgrade-insecure-requests",
])


def headers_config():
    return {'Name': POLICY_NAME, 'Comment': 'HTTPS and script isolation for the RegattaView application',
        'SecurityHeadersConfig': {
            'StrictTransportSecurity': {'AccessControlMaxAgeSec': 31536000, 'IncludeSubdomains': False, 'Preload': False, 'Override': True},
            'ContentTypeOptions': {'Override': True},
            'FrameOptions': {'FrameOption': 'SAMEORIGIN', 'Override': True},
            'ReferrerPolicy': {'ReferrerPolicy': 'strict-origin-when-cross-origin', 'Override': True},
            'ContentSecurityPolicy': {'ContentSecurityPolicy': CSP, 'Override': True},
        }}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--snapshot-dir', type=Path, required=True)
    args = ap.parse_args()
    args.snapshot_dir.mkdir(parents=True, exist_ok=True)
    def save(name, value):
        path = args.snapshot_dir / (name + '.json')
        if path.exists(): raise RuntimeError(f'Refusing to overwrite rollback snapshot: {path}')
        path.write_text(json.dumps(value, indent=2, default=str))
    cf = boto3.client('cloudfront'); s3 = boto3.client('s3', region_name='us-west-2')
    lam = boto3.client('lambda', region_name='us-west-2'); logs = boto3.client('logs', region_name='us-west-2')
    dist = cf.get_distribution_config(Id=DIST); save('distribution', dist)
    save('versioning', s3.get_bucket_versioning(Bucket=BUCKET))
    save('concurrency', lam.get_function_concurrency(FunctionName=FUNCTION))
    save('log-group', logs.describe_log_groups(logGroupNamePrefix=LOG_GROUP))
    try: lifecycle = s3.get_bucket_lifecycle_configuration(Bucket=LOG_BUCKET)
    except s3.exceptions.ClientError as e:
        if e.response['Error']['Code'] != 'NoSuchLifecycleConfiguration': raise
        lifecycle = {'Rules': []}
    save('log-lifecycle', lifecycle)
    listing = cf.list_response_headers_policies(Type='custom')['ResponseHeadersPolicyList']
    existing = next((item['ResponseHeadersPolicy'] for item in listing.get('Items', [])
                     if item['ResponseHeadersPolicy']['ResponseHeadersPolicyConfig']['Name'] == POLICY_NAME), None)
    if existing: save('headers-policy', cf.get_response_headers_policy(Id=existing['Id']))
    print(json.dumps({'distribution': DIST, 'default_protocol': 'redirect-to-https',
        'security_headers': headers_config(), 'main_bucket_versioning': 'Enabled',
        'lambda_reserved_concurrency': 10, 'log_retention_days': 365,
        'apply': args.apply}, indent=2))
    if not args.apply: return
    if existing:
        old = cf.get_response_headers_policy(Id=existing['Id'])
        cf.update_response_headers_policy(Id=existing['Id'], IfMatch=old['ETag'], ResponseHeadersPolicyConfig=headers_config())
        policy_id = existing['Id']
    else:
        policy_id = cf.create_response_headers_policy(ResponseHeadersPolicyConfig=headers_config())['ResponseHeadersPolicy']['Id']
    config = copy.deepcopy(dist['DistributionConfig'])
    config['DefaultCacheBehavior']['ViewerProtocolPolicy'] = 'redirect-to-https'
    config['DefaultCacheBehavior']['ResponseHeadersPolicyId'] = policy_id
    for behavior in config.get('CacheBehaviors', {}).get('Items', []):
        if behavior['PathPattern'] == '/www/sailview/*': behavior['ResponseHeadersPolicyId'] = policy_id
    cf.update_distribution(Id=DIST, IfMatch=dist['ETag'], DistributionConfig=config)
    s3.put_bucket_versioning(Bucket=BUCKET, VersioningConfiguration={'Status': 'Enabled'})
    lam.put_function_concurrency(FunctionName=FUNCTION, ReservedConcurrentExecutions=10)
    logs.put_retention_policy(logGroupName=LOG_GROUP, retentionInDays=365)
    rules = [rule for rule in lifecycle['Rules'] if rule['ID'] != 'RegattaViewOneYearLogs']
    rules.append({'ID': 'RegattaViewOneYearLogs', 'Status': 'Enabled', 'Filter': {'Prefix': ''},
                  'Expiration': {'Days': 365}})
    s3.put_bucket_lifecycle_configuration(Bucket=LOG_BUCKET, LifecycleConfiguration={'Rules': rules})
    print('Applied. Verify CloudFront deployment, headers, auth, and playback before finishing.')


if __name__ == '__main__': main()
