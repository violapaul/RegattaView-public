#!/usr/bin/env /Users/viola/anaconda3/envs/sail/bin/python

# %%
import os
import numpy as np
import time
import serial
import itertools as it
import datetime
import gzip
import struct
import socket


# connect to and listen on a udp port
def listen_udp(port, address="192.16.4.39"):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind((address, port))
        while True:
            data, addr = s.recvfrom(1024)
            print(f"received {len(data)} from {addr}")


# %%

def read_example():
    with serial.Serial(port, 115200, timeout=1) as ser:
        time_last_line_read = time.time()
        # in a loop read and display
        while True:
            line = ser.readline()
            if line != b'':
                print(line)
                time_last_line_read = time.time()
            else:
                if time.time() - time_last_line_read > 1:
                    print("no data for 1 second")
                    break
        

# %%
# These are the PGNs we actually care about.

pgns_to_keep = {
   126992: "System Time",
   127245: "Rudder",
   127250: "Vessel Heading",
   127251: "Rate of Turn",
   127257: "Attitude",
   127258: "Magnetic Variation",
   128259: "Speed",
   128267: "Water Depth",
   129025: "Position, Rapid Update",
   129026: "COG & SOG, Rapid Update",
   129029: "GNSS Position Data",
   130306: "Wind Data",
}

nmea2000_pgn_dict = {
    126992: "System Time",
    127245: "Rudder",
    127250: "Vessel Heading",
    127251: "Rate of Turn",
    127257: "Attitude",
    127258: "Magnetic Variation",
    127488: "Engine Parameters, Rapid Update",
    127489: "Engine Parameters, Dynamic",
    127505: "Fluid Level",
    127506: "DC Detailed Status",
    127508: "Battery Status",
    128259: "Speed",
    128267: "Water Depth",
    128275: "Distance Log",
    129025: "Position, Rapid Update",
    129026: "COG & SOG, Rapid Update",
    129029: "GNSS Position Data",
    129033: "Time & Date",
    129038: "AIS Class A Position Report",
    129039: "AIS Class B Position Report",
    129040: "AIS Class B Extended Position Report",
    129041: "AIS Aid to Navigation (AtoN) Report",
    129283: "Cross Track Error",
    129284: "Navigation Data",
    129285: "Navigation - Route/WP Information",
    129539: "GNSS DOPs",
    129540: "GNSS Sats in View",
    129545: "GNSS RAIM Output",
    129549: "DGNSS Corrections",
    129551: "GNSS Differential Correction Receiver Interface",
    129793: "AIS UTC and Date Report",
    129794: "AIS Class A Static and Voyage Related Data",
    129798: "AIS SAR Aircraft Position Report",
    129801: "AIS Addressed Safety Related Message",
    129802: "AIS Safety Related Broadcast Message",
    129808: "DSC Call Information",
    129809: "AIS Class B “CS” Static Data Report, Part A",
    129810: "AIS Class B “CS” Static Data Report, Part B",
    130306: "Wind Data",
    130310: "Environmental Parameters",
    130311: "Environmental Parameters",
    130312: "Temperature",
    130313: "Humidity",
    130314: "Actual Pressure",
    130316: "Temperature, Extended Range",
    130577: "Direction Data",
    65262: "Engine Temperature",
    61444: "Engine Speed"
}

# %%
# example of some actisense data from the logs (this has been read by actisense-serial and converted to text)
lines = [
    "2023-10-29T18:53:08.496Z,6,129539,127,255,8,00,d3,32,00,50,00,ff,7f",
    "2023-10-29T18:53:08.553Z,7,127258,127,255,8,00,f6,ff,ff,e7,09,ff,ff",
    "2023-10-29T18:53:08.553Z,3,127257,128,255,8,00,ff,7f,77,04,5d,08,ff",
    "2023-10-29T18:53:08.553Z,2,127250,4,255,8,ff,dc,0b,ff,7f,ff,7f,fd",
    "2023-10-29T18:53:08.553Z,2,65280,16,255,8,13,99,04,05,00,00,02,00",
    "2023-10-29T18:53:08.553Z,6,65420,3,255,8,41,9f,10,8d,57,0b,f1,ff",
    "2023-10-29T18:53:08.553Z,2,127245,15,255,8,ff,ff,ff,7f,28,05,ff,ff",
    "2023-10-29T18:53:08.553Z,2,127251,4,255,8,ff,14,aa,05,00,ff,ff,ff",
]

def count_pgns(file):
    pgn_count = {}
    start_line = 500000
    start_time = None
    previous_time = None
    with gzip.open(file) as f:
        for i, line in enumerate(it.islice(f, start_line, start_line + 3000)):
            # decode the line from bytes to string
            line = line.decode('utf-8')
            # extract date, which is up to the first comma
            fields = line.split(',')
            date = fields[0]
            pgn = int(fields[2])
            if pgns_to_keep is not None and pgn not in pgns_to_keep:
                continue
            pgn_count[pgn] = pgn_count.get(pgn, 0) + 1
            # convert to datetime object
            dt = datetime.datetime.strptime(date, '%Y-%m-%dT%H:%M:%S.%fZ')
            if start_time is None:
                start_time = dt

    delta_time = dt - start_time
    print(f"delta time = {delta_time}")
    # messages per second
    print(f"messages per second = {i / delta_time.total_seconds()}")

    # sort by counts
    sorted_pgn_count = sorted(pgn_count.items(), key=lambda kv: kv[1], reverse=True)
    len(sorted_pgn_count)
    print(sorted_pgn_count)

    # lookup the name for each PGN
    for pgn, count in sorted_pgn_count:
        # print(pgn, count, nmea2000_pgn_dict.get(int(pgn), "unknown"))
        print(f"   {pgn}: {count} \"{nmea2000_pgn_dict.get(int(pgn), 'unknown')}\",")



# %%
# create a class that will read bytes from the serial buffer when available, and buffer
# them.  When a complete line is available, it will return that line, rather than None.

class SerialLineReader:
    def __init__(self, ser):
        self.ser = ser
        self.buffer = b''

    def readline(self):
        # read bytes from the serial buffer
        bytes_read = self.ser.read(100)
        # add them to the buffer
        self.buffer += bytes_read
        # if we have a complete line, return it
        if b'\n' in self.buffer:
            line, self.buffer = self.buffer.split(b'\n', 1)
            return line
        # otherwise return None
        return None



# %%

import math

def send_data(stream, port, start = 10000, count = 5000000, filter_rate = 0.0, hack_time=False, hack_wind=False):  
    start_send_time = datetime.datetime.now()
    start_file_time = None
    message_count = start
    with serial.Serial(port, 115200, timeout=0) as ser:
        reader = SerialLineReader(ser)
        for i, line in it.islice(enumerate(stream), start, start + count):
            # every 100 lines
            if i % 100 == 0:
                print(f"Status {i} {message_count} {datetime.datetime.now()}")
            # drop some messagse at random
            if np.random.rand() > (1.0 - filter_rate):
                continue
            # decode the line from bytes to string
            dline = line.decode('utf-8')
            # extract date, which is up to the first comma
            fields = dline.split(',')
            date = fields[0]
            pgn = fields[2]
            if int(pgn) in pgns_to_keep:
                if hack_time and pgn == "126992": # system time
                    line = actisense_system_time_message(date)
                if hack_wind and pgn == "130306": # wind data
                    # N2kWind_Magnetic = 1
                    tws = message_count / 100000.0
                    twd = math.fmod(tws * -5 * 2 * math.pi, 2 * math.pi)
                    twd = twd if twd >= 0 else twd + 2 * math.pi
                    line = actisense_wind_speed_message(dt, wind_speed=tws, wind_angle=twd, wind_reference=1)
                dt = datetime.datetime.strptime(date, '%Y-%m-%dT%H:%M:%S.%fZ')
                if start_file_time is None:
                    start_file_time = dt
                delta_file = dt - start_file_time
                ser.write(line)
                message_count += 1
                line = reader.readline()
                while line is not None:
                    print(line)
                    line = reader.readline()
                delta_send = datetime.datetime.now() - start_send_time
                if delta_send < delta_file:
                    time.sleep((delta_file - delta_send).total_seconds())


# %%




def unpack_float(struct_format, data, multiplier):
    value = struct.unpack(struct_format, data)[0]
    return float(value) * multiplier

# function that converts 4 bytes into an integer, and then a float using the multiplier
def bytes4_to_float(data, index, multiplier):
    return (unpack_float("i", data[index:index+4], multiplier), index+4)

def bytes2_to_float(data, index, multiplier):
    return (unpack_float("h", data[index:index+2], multiplier), index+2)

def ubytes4_to_float(data, index, multiplier):
    return (unpack_float("I", data[index:index+4], multiplier), index+4)

def ubytes2_to_float(data, index, multiplier):
    return (unpack_float("H", data[index:index+2], multiplier), index+2)

# convert 8 bytes to a double
def bytes8_to_float(data, index, multiplier):
    return (unpack_float("q", data[index:index+8], multiplier), index+8)

def data_checksum(data):
    # XOR all the bytes together
    checksum = 0
    for byte in data:
        checksum ^= byte
    return checksum


# assume that the data is stored in a binary file named "data.bin"
# open the file in read and binary mode
def deserialize_boat_state(data):
    # initialize an empty dict
    result = {}
    # first is the length of the data
    length = struct.unpack("B", data[:1])[0]
    msg_checksum = struct.unpack("B", data[1:2])[0]
    checksum = data_checksum(data[2:-1])
    if length != len(data):
        print(f"Length is wrong {length} != {len(data)}")
        return None
    if checksum != msg_checksum:
        print(f"Checksum is wrong {checksum} != {msg_checksum}")
        return None
    # initialize an index to keep track of the position in the data
    index = 2
    (result["lat"], index) = bytes4_to_float(data, index, 1e-7)
    (result["lon"], index) = bytes4_to_float(data, index, 1e-7)

    (result["sog"], index) = ubytes2_to_float(data, index, 1e-2)
    (result["cog"], index) = ubytes2_to_float(data, index, 1e-1)

    (result["hdg"], index) = ubytes2_to_float(data, index, 1e-1)
    (result["spd"], index) = ubytes2_to_float(data, index, 1e-2)

    (result["aws"], index) = ubytes2_to_float(data, index, 1e-2)
    (result["awa"], index) = bytes2_to_float(data, index, 1e-2)

    (result["saws"], index) = ubytes2_to_float(data, index, 1e-2)
    (result["sawa"], index) = bytes2_to_float(data, index, 1e-2)

    (result["depth"], index) = ubytes2_to_float(data, index, 1e-0)
    (result["rudder"], index) = bytes2_to_float(data, index, 1e-2)

    (result["twd"], index) = ubytes2_to_float(data, index, 1e-1)
    (result["twa"], index) = bytes2_to_float(data, index, 1e-2)
    (result["tws"], index) = ubytes2_to_float(data, index, 1e-2)

    (result["ptwd"], index) = ubytes2_to_float(data, index, 1e-1)
    (result["ptwa"], index) = bytes2_to_float(data, index, 1e-2)
    (result["ptws"], index) = ubytes2_to_float(data, index, 1e-2)

    (result["magVar"], index) = bytes2_to_float(data, index, 1e-2)
    
    (result["unixTime"], index) = bytes8_to_float(data, index, 1e-3)
    return result



def deserialize_windex_state(data):
    # initialize an empty dict
    result = {}
    # first is the length of the data
    length = struct.unpack("B", data[:1])[0]
    msg_checksum = struct.unpack("B", data[1:2])[0]
    checksum = data_checksum(data[2:-1])
    if length != len(data):
        print(f"Length is wrong {length} != {len(data)}")
        return None
    if checksum != msg_checksum:
        print(f"Checksum is wrong {checksum} != {msg_checksum}")
        return None
    # initialize an index to keep track of the position in the data
    index = 2
    (result["rawAwaAngle"], index) = bytes2_to_float(data, index, 1e-1)
    (result["rawAwaSpeed"], index) = bytes2_to_float(data, index, 1e-1)

    (result["rawAwsAngle"], index) = bytes2_to_float(data, index, 1e-1)
    (result["rawAwsSpeed"], index) = bytes2_to_float(data, index, 1e-1)

    (result["roll"], index) = bytes2_to_float(data, index, 1e-2)
    (result["pitch"], index) = bytes2_to_float(data, index, 1e-2)
    (result["yaw"], index) = bytes2_to_float(data, index, 1e-2)

    (result["lat"], index) = bytes4_to_float(data, index, 1e-7)
    (result["lon"], index) = bytes4_to_float(data, index, 1e-7)

    (result["sog"], index) = ubytes2_to_float(data, index, 1e-2)
    (result["cog"], index) = ubytes2_to_float(data, index, 1e-2)

    (result["hdg"], index) = bytes2_to_float(data, index, 1e-2)
    (result["spd"], index) = ubytes2_to_float(data, index, 1e-2)

    (result["aws"], index) = ubytes2_to_float(data, index, 1e-2)
    (result["awa"], index) = bytes2_to_float(data, index, 1e-2)

    (result["saws"], index) = ubytes2_to_float(data, index, 1e-2)
    (result["sawa"], index) = bytes2_to_float(data, index, 1e-2)

    (result["twd"], index) = bytes2_to_float(data, index, 1e-2)
    (result["twa"], index) = bytes2_to_float(data, index, 1e-2)
    (result["tws"], index) = ubytes2_to_float(data, index, 1e-2)

    (result["ptwd"], index) = bytes2_to_float(data, index, 1e-2)
    (result["ptwa"], index) = bytes2_to_float(data, index, 1e-2)
    (result["ptws"], index) = ubytes2_to_float(data, index, 1e-2)

    (result["magVar"], index) = bytes2_to_float(data, index, 1e-2)
    return result

def print_dict(d):
    # print each key and if value is a float print it with 2 decimal places
    for k, v in d.items():
        if isinstance(v, float):
            # fixed width including the sign
            if k in ["lat", "lon"]:
                print(f"|{k} {v: 10.6f}|", end=' ')
            elif k in ["unixTime"]:
                print(f"|{k} {v: 12.3f}|", end=' ')
            else:
                print(f"|{k} {v: 10.2f}|", end=' ')
        else:
            print(f"|{k} {v}|", end=' ')
    print()


# %%
# There is a data server on port 100 of 192.168.4.31
# It serves up data in a binary format, the terminator is 0xffff followed by a newline

import queue
# create a fifo queue
fifo = queue.Queue()

# class that is used to compute events per second using an exponential moving average
class EMA:
    def __init__(self, alpha, count=50):
        self.alpha = alpha
        self.value = 1
        self.last_time = datetime.datetime.now()
        self.history = queue.Queue()  # store the last 50 times
        # Just to make sure there is one
        self.history.put(0)
        self.count = count
        
    def event(self):
        now = datetime.datetime.now()
        delta = (now - self.last_time).total_seconds()
        self.last_time = now
        self.internal_update(delta)

    def internal_update(self, value):
        self.value = self.alpha * value + (1 - self.alpha) * self.value
        self.history.put(value)
        if self.history.qsize() > self.count:
            self.history.get()

    def update(self, value):
        self.internal_update(value)

    def get(self):
        # return the running average and the min and max of the last 50 times
        return self.value, min(self.history.queue), max(self.history.queue)
    
    def format(self, float_format="{:7.3f}"):
        # avg, min_time, max_time = msg_ema.get()
        # print(f"Message times ema = {avg:5.3f} min = {min_time:5.3f} max = {max_time:5.3f}")        
        self.get()
        # computed formatted versions
        formatted = [float_format.format(v) for v in [self.value, min(self.history.queue), max(self.history.queue)]]
        return f"avg = {formatted[0]}, min = {formatted[1]}, max = {formatted[2]}"


import pickle

# create a file name from current date and time
def create_dt_file_name():
    dt = datetime.datetime.now()
    return f"{dt.year}_{dt.month:02}_{dt.day:02}_{dt.hour:02}_{dt.minute:02}_{dt.second:02}"

class MessageCollector:
    def __init__(self, file_name):
        self.file_name = file_name
        self.rows = []
        self.last_write_time = datetime.datetime.now()
        
    def add_message(self, result):
        # convert the message to a dict
        self.rows.append(result)
        # if it has been more than 10 seconds, write the data to a file
        if datetime.datetime.now() - self.last_write_time > datetime.timedelta(seconds=30):
            self.write_data()
            self.last_write_time = datetime.datetime.now()
        
    def write_data(self):
        # picklet the data and write
        with gzip.open(self.file_name, 'wb') as f:
            pickle.dump(self.rows, f)
        
    def close(self):
        # write the data to a file
        self.write_data()
        # empty the dataframe


def read_boat_brain_data(address, port, messages_to_read = 1000):
    results = queue.Queue()
    buffer = b""
    message_count = 0
    # exponential moving avg, to measure rates of messages and latency
    msg_ema = EMA(0.05)        # how often are messages received
    latency_ema = EMA(0.05)    # time latency between sender and receiver
    block_ema = EMA(0.05)      # size of data blocks received
    msg_collector = MessageCollector("/Users/viola/tmp/" + create_dt_file_name() + ".pd.gz")
    ex_ema = EMA(0.05)         # time between exceptions

    start_time = time.time()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.connect((address, port))
        print("Connected")
        # s.settimeout(0.01)
        while messages_to_read < 0 or message_count < messages_to_read:
            # Read data until the separator is encountered
            while b"\xff\xff\n" not in buffer:
                buffer += s.recv(1)
            block_ema.update(len(buffer))
            message = buffer[:-3]
            delta_time = time.time() - start_time
            start_time = time.time()
            buffer = b""
            # Split the buffer into messages, note the last message is incomplete
            if True:
                msg_ema.event()
                result = deserialize_boat_state(message)
                msg_collector.add_message(result)
                if result is None:
                    continue
                current_time = time.time()
                latency = current_time - result["unixTime"]
                latency_ema.update(latency)
                message_count += 1
                if message_count % 30 == 0:
                    print(f"  {message_count} ", end='')
                    print_dict(result)
                    print("Message times   ", msg_ema.format())
                    print("Time latency    ", latency_ema.format())
                    print("Data block size ", block_ema.format())
                    print("Exception rate ", ex_ema.format())
            # The last message is incomplete, so save it for next time
        
    return message_count


def read_boat_brain_data_udp(port, messages_to_read = 1000):
    results = queue.Queue()
    buffer = b""
    message_count = 0
    # exponential moving avg, to measure rates of messages and latency
    msg_ema = EMA(0.05)        # how often are messages received
    latency_ema = EMA(0.05)    # time latency between sender and receiver
    block_ema = EMA(0.05)      # size of data blocks received
    msg_collector = MessageCollector("/Users/viola/tmp/" + create_dt_file_name() + ".pd.gz")
    ex_ema = EMA(0.05)         # time between exceptions

    start_time = time.time()
    # Enable reuse address/port
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # Additional line for allowing multiple sockets on the same port (if supported)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        # Bind the socket to the port
        sock.bind(("0.0.0.0", int(port)))

        while message_count < messages_to_read:
            data, addr = sock.recvfrom(1024)  # buffer size is 1024 bytes
            # check that the message is terminated correctly
            if data[-3:] != b"\xff\xff\n":
                print("Message not terminated correctly")
                continue
            message = data[:-3] 
            msg_ema.event()
            result = deserialize_boat_state(message)
            msg_collector.add_message(result)
            if result is None:
                continue
            current_time = time.time()
            latency = current_time - result["unixTime"]
            latency_ema.update(latency)
            message_count += 1
            if message_count % 100 == 0:
                print(f"  {message_count} ", end='')
                print_dict(result)
                print("Message times   ", msg_ema.format())
                # print("Time latency    ", latency_ema.format())
            # The last message is incomplete, so save it for next time
    return message_count



# %%
def create_actisense_line(datetime, proiority, pgn, src, dst, data):
    byte_count = len(data)
    head = f"{datetime},{proiority},{pgn},{src},{dst},{byte_count},"
    # for each byte in the data, convert it to hex and add a comma
    data_str = ",".join([f"{b:02x}" for b in data])
    return head + data_str + "\n"

# //*****************************************************************************
# // System time
# void SetN2kPGN126992(tN2kMsg &N2kMsg, unsigned char SID, uint16_t SystemDate,
#                      double SystemTime, tN2kTimeSource TimeSource) {
#     N2kMsg.SetPGN(126992L);
#     N2kMsg.Priority=3;
#     N2kMsg.AddByte(SID);
#     N2kMsg.AddByte((TimeSource & 0x0f) | 0xf0);
#     N2kMsg.Add2ByteUInt(SystemDate);
#     N2kMsg.Add4ByteUDouble(SystemTime,0.0001);
# }


def actisense_system_time_message(datetime="2023-10-29T16:14:01.625Z"):
    # the system time is pgn 126992
    pgn = 126992
    priority = 3
    # one byte for the source address
    src = 5
    # one byte for the destination address
    dst = 255
    data = b""
    # one byte for the SID not used    
    data += bytes([0])   
    # one byte for the time source
    data += bytes([0xf0])
    # days since unix epoch
    days = int(time.time() / 86400)
    # days are encoded as 2 bytes unsigned int
    data += struct.pack("H", days)
    # seconds since midnight ()
    seconds = int((time.time() % 86400) * 10000)
    # seconds are encoded as 4 bytes unsigned int
    data += struct.pack("I", seconds)
    line = create_actisense_line(datetime, priority, pgn, src, dst, data)
    return line.encode('utf-8')

# //*****************************************************************************
# // Wind Speed
# void SetN2kPGN130306(tN2kMsg &N2kMsg, unsigned char SID, double WindSpeed, double WindAngle, tN2kWindReference WindReference) {
#     N2kMsg.SetPGN(130306L);
#     N2kMsg.Priority=2;
#     N2kMsg.AddByte(SID);
#     N2kMsg.Add2ByteUDouble(WindSpeed,0.01);
#     N2kMsg.Add2ByteUDouble(WindAngle,0.0001);
#     N2kMsg.AddByte((unsigned char)WindReference);
#     N2kMsg.AddByte(0xff); // Reserved
#     N2kMsg.AddByte(0xff); // Reserved
# }

# Wind Speed message (PGN 130306)
def actisense_wind_speed_message(datetime="2023-10-29T16:14:01.625Z", wind_speed=5.2, wind_angle=180.0, wind_reference=0):
    pgn = 130306
    priority = 2
    src = 5
    dst = 255
    
    data = b""
    data += bytes([0]) # SID (not used)
    
    # Wind Speed (encoded as 2 bytes unsigned double, resolution 0.01)
    wind_speed_encoded = int(wind_speed * 100)
    data += struct.pack("H", wind_speed_encoded)
    
    # Wind Angle (encoded as 2 bytes unsigned double, resolution 0.0001)
    wind_angle_encoded = int(wind_angle * 10000)
    data += struct.pack("H", wind_angle_encoded)
    
    # Wind Reference (encoded as 1 byte)
    data += bytes([wind_reference])
    
    # Reserved bytes
    data += bytes([0xff, 0xff])
    
    line = create_actisense_line(datetime, priority, pgn, src, dst, data)
    return line.encode('utf-8')

# %%
