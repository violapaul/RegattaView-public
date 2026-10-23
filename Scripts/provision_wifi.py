#!/usr/bin/env python3
"""Generate local WiFi configuration. Run separately before building each device.

The generated ignored file is the only place the new AP password is written.
Read it locally to provision the iOS app; do not paste it into a chat or commit it.
"""
import argparse
import getpass
import json
import os
from pathlib import Path
import secrets


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--station-ssid',default='',help='Optional network for station mode')
    ap.add_argument('--out',type=Path,default=Path(__file__).resolve().parents[1]/'Arduino/include/wifi_credentials.local.hpp')
    args=ap.parse_args()
    station_password=getpass.getpass('Station network password: ') if args.station_ssid else ''
    config={'WIFI_SSID':args.station_ssid,'WIFI_PASSWORD':station_password,'WIFI_MYAP_PASSWORD':secrets.token_urlsafe(24)}
    args.out.parent.mkdir(parents=True,exist_ok=True)
    # Never silently rotate an already provisioned device configuration.
    descriptor=os.open(args.out,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
    with os.fdopen(descriptor,'w') as f:
        f.write('#pragma once\n')
        for name,value in config.items():f.write(f'#define {name} {json.dumps(value)}\n')
    print(f'Wrote private configuration to {args.out}. Build/flash this device, then save its AP password in BoatData.')

if __name__=='__main__':main()
