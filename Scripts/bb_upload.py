#!/usr/bin/env /Users/viola/miniforge3/envs/sailing/bin/python

import Libraries.boat_brain as bb

port1 = "/dev/cu.usbserial-0001"
port3 = "/dev/cu.usbserial-3"
# file = "/Users/viola/Python/sailing/Data/CompressedLogs/actisense_2023_10_28_09_51_52_976.log.gz"
file = "/Users/viola/Python/sailing/Data/CompressedLogs/actisense_2023_08_14_20_54_29_20035.log.gz"

# reads log file and sends to the actisense relay computer over the serial port.  This data is then
# sent to the boat brain computer over N2k, and then we can read this data back using the bb_data.py script.
if __name__ == "__main__":
    # get the args
    import argparse
    parser = argparse.ArgumentParser(description='Send data to boat brain.')
    parser.add_argument('--file', type=str, default=file, help='file to read')
    parser.add_argument('--port', type=str, default=port3, help='port to send to')
    parser.add_argument('--start', type=int, default=10000, help='start line')
    parser.add_argument('--count', type=int, default=10000000, help='number of lines to send')
    parser.add_argument('--filter', type=float, default=0.0, help='fraction of lines to filter out')
    # boolean arg hack time or not?
    parser.add_argument('--hack_time', action='store_true', help='hack the time or not')
    parser.add_argument('--hack_wind', action='store_true', help='hack the wind')
    parser.add_argument('--repeat', action='store_true', help='repeated send the data')    
    
    args = parser.parse_args()
    print(args)

    import gzip
    # does file end with gz?
    if args.file.endswith('.gz'):
        with gzip.open(args.file, 'rb') as f:
            bb.send_data(f, args.port, args.start, args.count, args.filter, hack_time=args.hack_time, hack_wind=args.hack_wind)
    else:
        with open(args.file, 'rb') as f:
            bb.send_data(f, args.port, args.start, args.count, args.filter, hack_time=args.hack_time)


# %%
