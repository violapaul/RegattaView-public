#!/usr/bin/env /Users/viola/miniforge3/envs/anaconda/bin/python

import Libraries.boat_brain as bb

address = "192.168.4.39"
port = 100  # TCP port; UDP is 101
messages_to_read = -1

# connects to the boat brain computer (over tcp or udp) and then reads the data.
if __name__ == "__main__":
    # get the args
    import argparse
    parser = argparse.ArgumentParser(description='Send data to boat brain.')
    parser.add_argument('--address', type=str, default=address, help='address for connection')
    parser.add_argument('--port', type=str, default=port, help='port to connect to')
    # boolean to use udp
    parser.add_argument('--udp', action='store_true', help='use udp')    
    parser.add_argument('--count', type=int, default=messages_to_read, help='total messages to read, default is forever')
    args = parser.parse_args()
    print(args)

    read_count = 0
    while args.count < 0 or read_count < args.count:
        if args.udp:
            print("using udp")
            read_count += bb.read_boat_brain_data_udp(args.port, 10000)
        else:
            read_count += bb.read_boat_brain_data(args.address, args.port, args.count - read_count)
        break
        print("read count: ", read_count)

    

