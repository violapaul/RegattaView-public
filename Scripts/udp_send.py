#!/usr/bin/env /Users/viola/miniforge3/envs/anaconda/bin/python

import socket
import time

def broadcast_message(message, port=12345):
    # Create a UDP socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    # Set the socket to broadcast mode
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    # Define the broadcast address
    broadcast_address = ('<broadcast>', port)
    try:
        # Send the message
        for i in range(20):
            sock.sendto(message.encode(), broadcast_address)
            print(f"Message sent: {message}")
            # wait for 1 second
            time.sleep(1)
    finally:
        # Close the socket
        sock.close()

if __name__ == "__main__":
    message = "Hello, UDP receiver!"
    broadcast_message(message)


