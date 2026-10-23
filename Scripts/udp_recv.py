#!/usr/bin/env /Users/viola/miniforge3/envs/anaconda/bin/python

import socket
import argparse

def listen_for_messages(host, port):
    # Create a UDP socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    # Enable reuse address/port
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    # Additional line for allowing multiple sockets on the same port (if supported)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    # Bind the socket to the port
    sock.bind((host, port))

    print(f"Listening for messages on {host}:{port}...")
    
    count = 0
    while True:
        data, addr = sock.recvfrom(1024)  # buffer size is 1024 bytes
        first_byte = data[0]
        print(f"Received message: {count} {len(data)} =? {first_byte} from {addr}")
        count += 1

if __name__ == "__main__":
    # Setup command line argument parsing
    parser = argparse.ArgumentParser(description='UDP Receiver')
    parser.add_argument('--host', type=str, default='0.0.0.0',
                        help='Host IP address to bind to. Use 0.0.0.0 for all available interfaces.')
    parser.add_argument('--port', type=int, default=12345,
                        help='Port number to listen on.')
    
    args = parser.parse_args()
    
    listen_for_messages(args.host, args.port)
