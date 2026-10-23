
from datetime import datetime, timezone, timedelta
import os
import unittest

def timezone():
    "Return the local timezone."
    return datetime.now().astimezone().tzinfo

def time_to_filename(dt=None):
    "Convert a datetime object to a filename string. Includes timezone offset."
    if dt is None:
        dt = datetime.now()
    else:
        dt = dt

    # Get the timezone offset in minutes
    offset_minutes = int(dt.astimezone().utcoffset().total_seconds() / 60)

    # Build offset string
    offset = '{0:+01d}'.format(offset_minutes // 60)

    # Convert to a string in the format 'YYYYMMDDHHMMSS+HHMM'
    time_string = dt.strftime('%Y%m%d_%H%M%S') + offset

    return time_string

def filename_to_time(filename):
    "Convert a filename string to a datetime object in the local timezone."

    # Extract the time and offset from the filename
    time_string = filename[:-2]
    offset_string = filename[-2:]
    offset_minutes = int(offset_string) * 60

    # Convert string back to datetime object
    dt = datetime.strptime(time_string, '%Y%m%d_%H%M%S')

    # Apply the offset
    dt = dt.replace(tzinfo=timezone(timedelta(minutes=offset_minutes)))
    # and map to the local timezone
    tz = dt.now().astimezone().tzinfo
    return dt.astimezone(tz)


class TestTimeUtils(unittest.TestCase):

    def setUp(self):
        pass

    def test_base(self):
        time_string = time_to_filename()
        time_object = filename_to_time(time_string)
        self.assertEqual(time_string, time_to_filename(time_object))

    def test_tz_offset(self):
        tt1 = '20230617_121816-7'
        tt2 = '20230617_121816-8'  # same local time, but different timezone 
        tt3 = '20230617_111816-8'  # different local time, but same time
        ff2 = filename_to_time(tt2)
        ff3 = filename_to_time(tt3)
        self.assertNotEqual(tt1, time_to_filename(ff2))
        self.assertEqual(tt1, time_to_filename(ff3))

if False:
    if __name__ == '__main__':
        unittest.main()
    else: 
        tdl = TestTimeUtils()
        tdl.setUp()
        tdl.test_base()
        tdl.test_tz_offset()
