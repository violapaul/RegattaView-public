# %% [markdown]
# # Analyzing the Boat Brain logs
# 
# These are close but not quite like the RPi logs.
# 
# - Glue the multiple log files together into a single logical log file.
#     - Files are created in 20 min blocks.
# - Create a stream of JSON records and read them into memory
#     - Could be a stream, but not for now.
# - Compute the record times for the NMEA records.
#     - The raw messages are not time stamped.
#     - The System Time message only arrives 1x a second.
#     - BoatBrain sends an additional message 10x a second.
#     - Combine this all into a single time.
# - Create a pandas dataframe.
#     - Clean and process this data to the standard format
# - Save the data.
# 

# %%

import os
import math
import sys
import time
from datetime import datetime, timezone
import calendar
import pytz
import copy
import concurrent
import subprocess

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt


from Libraries.global_variables import G
from Libraries.log_helper import LOG

import Libraries.utils as utils
from Libraries import card_report

import Libraries.process as p
import Libraries.metadata as metadata
import Libraries.race_logs as race_logs

import Libraries.canboat as cb


# The log files have names based on the UTC time 2024-04-16T00_57_50Z.log, and they are 20 mins apart.
# Find the matching files and combine them into a single file.

LOG_LENGTH_MINS = 20
BB_CARD_DIR = "/Volumes/BOATBRAIN3/"
BBLOG_DIR = "/Users/viola/Data/BBLog/"



def bb_file_to_datetime(file):
    """
    Converts a file name in the format 'YYYY-MM-DDTHH_MM_SSZ.log' to a datetime object in the US/Pacific timezone.

    Args:
        file (str): The file name to convert.

    Returns:
        datetime: The converted datetime object in the US/Pacific timezone.
    """
    dt = datetime.strptime(file, "%Y-%m-%dT%H_%M_%SZ.log")
    utc_dt = pytz.timezone('UTC').localize(dt)
    LOG.logger.debug(f"Original time: {dt}, UTC time: {utc_dt}")
    return utc_dt.astimezone(pytz.timezone('US/Pacific'))

def pandas_basename(dt):
    return dt.strftime("%Y-%m-%dT%H_%M")

def find_log_file_sequences(dir):
    files = os.listdir(dir)
    files.sort()
    previous_time = 0
    current_sequence = []
    sequences = [current_sequence]
    for file in files:
        path = os.path.join(dir, file)
        if file.endswith("Z.log"):
            dt = bb_file_to_datetime(file)
            unixtime = dt.timestamp()
            if previous_time == 0:
                current_sequence.append(path)
            elif abs(unixtime - (previous_time + LOG_LENGTH_MINS*60)) > 30:
                current_sequence = [path]
                sequences.append(current_sequence)
            else:
                current_sequence.append(path)
            previous_time = unixtime
    return sequences

def clean_bad_logs(sequences, dry_run=True, min_length=3):
    # Remove the sequences that have only one file
    bad_sequences = [s for s in sequences if len(s) < min_length]
    for s in bad_sequences:
        for file in s:
            LOG.logger.warn(f"Moving {file}")
            if not dry_run:
                # move the file to the Backup subdirectory
                # split off the file name
                name = os.path.basename(file)
                backup = os.path.join(os.path.dirname(file), "Backup")
                os.makedirs(backup, exist_ok=True)
                os.rename(file, os.path.join(backup, name))
                LOG.logger.info(f"Moved to {backup}")

PGN_WHITELIST = set(G.PGN_NAME.keys())  # set of PGNs to process
PGN_FILTER = cb.pgn_filter(PGN_WHITELIST)

def read_sequence_json(file_path):
    LOG.logger.debug(f"Reading {file_path}")    
    jjj = cb.json_records( cb.file_lines(file_path), PGN_FILTER)
    return jjj


# %%

# Minimum JSON-to-raw-log size ratio. A well-formed `actisense-serial |
# analyzer -json` expands the binary NMEA stream ~5-6x into JSON; anything
# below this ratio is a truncated file (the bug that corrupted the 04-19
# parquet: a killed pipeline left a ~0.05x JSON in place).
JSON_MIN_RATIO_VS_LOG = 1.0


def json_file_from_bb_file(filepath, directory="/tmp", force=False):
    """
    Convert a BoatBrain log file to a JSON file via actisense-serial|analyzer.

    Cached JSONs are skipped only if they look complete: the file is written
    atomically (via .part → os.replace) so any cached file was the output of a
    clean subprocess exit, and its size is sanity-checked against the raw log
    (a truncated cache from a pre-fix killed run is caught here).
    """
    filename = os.path.basename(filepath)
    basename = p.log_basename(filename)
    LOG.logger.info(f"Processing {basename}")
    json_path = os.path.join(directory, basename + ".json")
    stderr_path = os.path.join(directory, basename + ".stderr")
    if not force and os.path.exists(json_path):
        try:
            json_size = os.path.getsize(json_path)
            log_size = os.path.getsize(filepath)
            if json_size >= JSON_MIN_RATIO_VS_LOG * log_size:
                LOG.logger.info(f"File exists, skipping {json_path}")
                return json_path
            LOG.logger.warning(
                f"{json_path} is {json_size} bytes vs {log_size} raw log bytes "
                f"(ratio {json_size/max(1,log_size):.2f}x < {JSON_MIN_RATIO_VS_LOG}x); regenerating"
            )
        except OSError as e:
            LOG.logger.warning(f"could not stat {json_path}: {e}; regenerating")
    tmp_path = json_path + ".part"
    command = f"actisense-serial -r {filepath} | analyzer -json"
    LOG.logger.debug(f"Running command: {command}, stderr {stderr_path}, json {tmp_path}")
    with open(tmp_path, 'w') as json_file:
        with open(stderr_path, 'w') as stderr_file:
            result = subprocess.run(command, shell=True, stderr=stderr_file, stdout=json_file)
    if result.returncode != 0:
        LOG.logger.error(f"actisense-serial | analyzer failed for {filepath} (rc={result.returncode}); leaving {tmp_path} for inspection")
        raise RuntimeError(f"actisense-serial | analyzer failed for {filepath}")
    os.replace(tmp_path, json_path)
    size = os.path.getsize(json_path)
    LOG.logger.info(f"Created {json_path}, with size {size}")
    return json_path

def read_log_files(log_files, max_workers=8):
    LOG.logger.info(f"Reading log {len(log_files)} files")
    pgn_whitelist = set(G.PGN_NAME.keys())  # set of PGNs to process
    pgn_keep = cb.pgn_filter(pgn_whitelist)
    def helper(file):
        LOG.logger.debug(f"Converting log: {file}")
        jfile = json_file_from_bb_file(file)
        LOG.logger.debug(f"Reading {jfile}")
        # read all the lines from the file
        jj = [j for j in read_sequence_json(jfile) if pgn_keep(j)]
        LOG.logger.debug(f"Found {len(jj)} records")
        return jj

    json_records = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
        json_records = list(ex.map(helper, log_files))

    res = []
    for jj in json_records:
        res += jj
    LOG.logger.info(f"Total records: {len(res)}")
    return res
                    

# There are a few sources of time messages, two from the GPS devices the other from the boat brain
# device.  GPS time only comes in 1x a second. The boat brain time comes at 10x a second, and it is
# initialized from the GPS, but it can drift (because we don't have a real-time clock).  

# Just to simplify things, let's just capture the time messages the ZG100 gps and the BoatBrain device

GPS_DEVICE=127
BB_DEVICE=99
SYS_TIME_PGN=126992

def extract_time_messages(messages):
    # and src is 127 (GPS) or 99 (BoatBrain)
    time_messages = []
    for m in messages:
        if (m['pgn'] == SYS_TIME_PGN and m['src'] in [GPS_DEVICE, BB_DEVICE]
            and 'fields' in m and 'Date' in m['fields'] and 'Time' in m['fields']):
            tm = m
            # convert date and time into a unix timestamp
            m_date = tm['fields']['Date']  # 2024.04.18
            m_time = tm['fields']['Time']  # 00:17:48 or 00:17:48.1234
            try:
                if '.' in m_time:  # has milliseconds
                    tstamp = datetime.strptime(m_date + " " + m_time, "%Y.%m.%d %H:%M:%S.%f")
                else:
                    tstamp = datetime.strptime(m_date + " " + m_time, "%Y.%m.%d %H:%M:%S")
                # careful to ensure this is a UTC time
                utc_dt = pytz.timezone('UTC').localize(tstamp)
                # tm['timestamp'] = utc_dt
                tm['unixtime'] = utc_dt.timestamp()
                time_messages.append(tm)
            except:
                LOG.logger.warn(f"Error: {m_date} {m_time}")
    return time_messages
        

def find_time_correction(time_messages):
    """
    Compute the offset and drift between the GPS time and the BoatBrain time.
    """
    gps_times = []
    bb_times = []
    # Find triples of time messages. The middle one is the true GPS time.
    for i in range(len(time_messages) - 2):
        tm1, tm2, tm3 = time_messages[i], time_messages[i+1], time_messages[i+2]
        if tm1['src'] == BB_DEVICE and tm2['src'] == GPS_DEVICE and tm3['src'] == BB_DEVICE:
            gps_times.append(tm2['unixtime'])
            bb_times.append(tm1['unixtime'])
            gps_times.append(tm2['unixtime'])
            bb_times.append(tm3['unixtime'])

    if len(bb_times) == 0:
        LOG.logger.warn("No BB time messages found")
        return None

    # this is the target time
    start_time = gps_times[0]
    gps_times = np.array(gps_times) - start_time
    # this is the time from the boat brain, add a col which is always 1
    bb_times = np.array(bb_times) - start_time
    bb_times = np.column_stack((bb_times, np.ones(bb_times.shape[0])))

    # solve for params using least squares
    params = np.linalg.lstsq(bb_times, gps_times, rcond=None)[0]

    # now we can use these params to correct the time
    predicted_gps_times = np.dot(bb_times, params)

    LOG.logger.info(f"Params: {params}")
    LOG.logger.info(f"Mean error: {np.mean(predicted_gps_times - gps_times)}")
    # max error
    LOG.logger.info(f"Max error: {np.max(np.abs(predicted_gps_times - gps_times))}")
    return dict(start_time = start_time, alpha=params[0], beta=params[1])

def correct_time_messages(nmea_messages, start_time, alpha, beta):
    """
    Correct the boat brain time messages using the parameters, shifting them and perhaps scaling
    them.
    """
    LOG.logger.info(f"Correcting time: alpha={alpha}, beta={beta}")
    for m in nmea_messages:
            if m['pgn'] == SYS_TIME_PGN and m['src'] == BB_DEVICE:
                delta = m['unixtime'] - start_time
                new_time = (alpha * delta + beta) + start_time
                m['unixtime'] = new_time
                # tstamp = datetime.fromtimestamp(new_time)
                # m['timestamp'] = pytz.timezone('UTC').localize(tstamp)
        

# %%
# This is kind of subtle.  In general messages do not have a time associated with them.  So we find
# pairs of time messages and then assign the time to the messages in between.  For lack of a better
# plan, we just linearly interpolate.  Its approximate, but not too bad.

def bracketed_messages(nmea_messages, use_gps_only=False):
    """Find matched pairs of system time messages.  These determine the times of the messages in between."""
    start = -1
    end = -1
    for i, m in enumerate(nmea_messages):
        # time message 
        if m['pgn'] == SYS_TIME_PGN and 'unixtime' in m:
            if use_gps_only and m['src'] != GPS_DEVICE:
                continue
            # if empty collect append
            if start == -1:
                start = i
            else:
                end = i
                # if not empty, yield the sequence
                yield (start, end)
                start = i
                end = -1

def assign_time_to_messages(nmea_messages, use_gps_only=False):
    """
    Assign time to the messages in between the time messages.  By assumption, the time just
    increases linearly between.  It can't be too far off, since the messages are only 0.1 seconds
    apart.
    """
    LOG.logger.info(f"Assigning time to messages")
    begin = len(nmea_messages)
    finish = 0
    for (start, end) in bracketed_messages(nmea_messages, use_gps_only):
        begin = min(begin, start)
        finish = max(finish, end)
        # find the time messages
        start_time = nmea_messages[start]['unixtime']
        end_time = nmea_messages[end]['unixtime']
        diff = end_time - start_time
        delta = diff / (end - start)
        # print(f"Start: {start} End: {end}")
        # print(f"Diff: {diff} Delta: {delta}")
        for i in range(start+1, end):
            nmea_messages[i]['unixtime'] = start_time + delta * (i - start)
            # timetimestamp assumes the local timezone unless you are specific!!
            # nmea_messages[i]['timestamp'] = datetime.fromtimestamp(nmea_messages[i]['unixtime'], timezone.utc)
    return nmea_messages[begin:finish]


# %%
# message are streamed asynchonously, so we need to assign them to rows in a dataframe.  Every 0.1
# seconds we will collect up the latest values and generate a new row.

def messages_to_dataframe(messages, pandas_time_step=0.1):
    LOG.logger.info(f"Converting {len(messages)} messages to dataframe")
    rows = []
    data_dict = {}
    row_seconds = 0
    base_time = messages[0]['unixtime']
    for num, m in enumerate(messages):
        record = cb.transform_record(m)
        log_seconds = record['unixtime'] - base_time
        data_dict['log_seconds'] = log_seconds
        data_dict['unixtime'] = record['unixtime']
        # data_dict['timestamp'] = record['timestamp']
        if 'fields' in record:
            data_dict.update(record['fields'])
        if log_seconds > row_seconds:
            # When log time progresses past the row second, then issue a new row.
            data_dict['row_seconds'] = row_seconds
            rows.append(copy.copy(data_dict))
            row_seconds += pandas_time_step
    LOG.logger.info(f"Generated {len(rows)} rows")
    return pd.DataFrame(rows)



# %%
def extract_pandas_file_name(log_files):
    """
    Extracts the pandas file name from a list of log files.

    Args:
        log_files (list): A list of log file paths.

    Returns:
        str: The pandas file name extracted from the first log file in the list.
    """
    first_path = log_files[0]
    first_file = os.path.basename(first_path)
    first_time = bb_file_to_datetime(first_file)
    return pandas_basename(first_time)

def find_usb_directory():
    # usb drives are mounted in /Volumes/ they match BOATBRAIN*
    vols = os.listdir("/Volumes")
    usb_drives = [v for v in vols if v.startswith("BOATBRAIN")]
    if len(usb_drives) == 0:
        return None
    return "/Volumes/" + usb_drives[0] + "/"

def _verify_and_wipe_boatbrain_card(bb_card_dir):
    """Verify BB card against BBLOG_DIR, then wipe + unmount.

    Mirror of insta360._verify_and_wipe_insta360_card: on verify
    failure, log ERROR and (when interactive) prompt y/N. Non-
    interactive runs stay strict — refuse and leave the card mounted.

    Returns True iff the card was wiped.
    """
    ok, problems = utils.verify_card_copy_complete(bb_card_dir, BBLOG_DIR)
    if not ok:
        LOG.logger.error(
            f"Refusing to wipe BoatBrain card: {len(problems)} file(s) "
            f"unverified. Card left mounted. First few: {problems[:5]}"
        )
        if not sys.stdin.isatty():
            card_report.record("BoatBrain", mounted=True, wiped=False)
            card_report.set_note("BoatBrain", f"mounted, {len(problems)} unverified file(s) — REFUSED to wipe; card left mounted")
            card_report.add_problem("BoatBrain", f"{len(problems)} unverified file(s); card left mounted, not wiped/ejected")
            return False
        print(f"\n  Card has {len(problems)} unverified file(s):", flush=True)
        for p_ in problems[:10]:
            print(f"    {p_}")
        if len(problems) > 10:
            print(f"    ... and {len(problems) - 10} more")
        try:
            ans = input("Wipe BoatBrain card anyway? [y/N]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return False
        if ans != "y":
            card_report.record("BoatBrain", mounted=True, wiped=False)
            card_report.set_note("BoatBrain", f"mounted, {len(problems)} unverified file(s) — REFUSED to wipe; card left mounted")
            card_report.add_problem("BoatBrain", f"{len(problems)} unverified file(s); card left mounted, not wiped/ejected")
            return False
        LOG.logger.info(
            "Operator approved wipe despite unverified file(s)"
        )
    else:
        LOG.logger.info("Card verified against local, cleaning BB card")

    utils.fsync_tree(BBLOG_DIR)
    cmd = f"rm -rf {bb_card_dir}*"
    LOG.logger.info(f"Running command: {cmd}")
    subprocess.run(cmd, shell=True, check=True)
    # Eject by mounted volume path (not a bare name). bb_card_dir is the
    # volume root with a trailing slash, e.g. "/Volumes/BOATBRAIN3/".
    volume_path = bb_card_dir.rstrip("/")
    LOG.logger.info(f"Ejecting card volume: {volume_path}")
    ejected = utils.eject_card(volume_path)
    card_report.record("BoatBrain", mounted=True, wiped=True, ejected=ejected)
    if ejected:
        card_report.set_note("BoatBrain", "mounted, copied + verified, wiped, ejected OK")
    else:
        card_report.set_note("BoatBrain", "mounted, copied + verified, wiped, EJECT FAILED — remove manually")
        card_report.add_problem("BoatBrain", "eject failed; unmount/remove the card manually")
    return True


def copy_bblogs_from_card():
    # rsync -av /Volumes/BOATBRAIN*/ /Users/viola/Data/BBLog/
    bb_card_dir = find_usb_directory()
    if bb_card_dir is None:
        # Soft-fail: a re-run after a successful copy is a legitimate state.
        # Surfaced loudly in the run summary via the WARNING level.
        LOG.logger.warning(
            "NO_INPUT: BoatBrain card not mounted at /Volumes/BOATBRAIN* — "
            "skipping copy; downstream uses existing BBLOG_DIR contents"
        )
        card_report.record("BoatBrain", mounted=False)
        card_report.set_note("BoatBrain", "not mounted (no card)")
        return
    # Exclude macOS per-volume metadata so Spotlight's unreadable index dir
    # (.Spotlight-V100) doesn't trip rsync with exit 23.
    excludes = "--exclude '.Spotlight-V100' --exclude '.fseventsd' --exclude '.Trashes' --exclude '.DS_Store'"
    cmd = f"rsync -av {excludes} {bb_card_dir} {BBLOG_DIR}"
    LOG.logger.info(f"Running command: {cmd}")
    # run twice for good measure (catches late-flushing files on the card)
    subprocess.run(cmd, shell=True, check=True)
    subprocess.run(cmd, shell=True, check=True)
    # Belt-and-braces gate: re-list the card and assert every file has a
    # same-size local sibling under BBLOG_DIR before wiping. rsync's exit
    # code alone is not enough — catches files that appeared post-rsync,
    # short reads, and bookkeeping surprises. On verify failure the helper
    # logs ERROR and, when interactive, prompts to wipe anyway.
    card_report.record("BoatBrain", mounted=True)
    _verify_and_wipe_boatbrain_card(bb_card_dir)

COLS_TO_KEEP = set(['log_seconds', 'unixtime', 'date', 'time', 'row_seconds', 'rudder_position',
'sog', 'latitude', 'longitude', 'zg100_heading', 'zg100_variation', 'zg100_reference',
'wind_speed', 'wind_angle', 'zg100_turn_rate', 'turn_rate', 'altitude', 'variation', 'zg100_pitch',
'zg100_roll', 'heading', 'zeus_latitude', 'zeus_longitude', 'zeus_cog', 'zeus_sog', 'zeus_source',
'true_wind_speed', 'true_wind_angle', 'true_north_wind_speed', 'true_north_wind_angle', 'zeus_variation',
'speed_water_referenced', 'depth', 'offset', 'cog', 'deviation'])

def compute_pandas_log(log_files, force=False):
    """
    Compute the pandas log file from the given list of log files.

    - Combines multiple bb log files into a single logical log file.
    - Corrects the time.
    - Converts to a pandas dataframe.
    - Processes and cleans up sensor data.

    Args:
        log_files (list): A list of log file paths.
    """
    LOG.logger.info(f"Processing {log_files[0]} with {len(log_files)} files")

    pandas_file = extract_pandas_file_name(log_files) + ".parquet"
    pandas_path = os.path.join(G.PANDAS_LOGS_DIRECTORY, pandas_file)

    if not force and os.path.exists(pandas_path):
        LOG.logger.info(f"Skipping {pandas_file} as it already exists")
    else:
        # Skip zero-byte input files early: they yield no NMEA records and the
        # dataframe build then throws "list index out of range". Because no
        # parquet is ever written, the "skip if parquet exists" guard above
        # never trips, so this would re-crash on every run (not idempotent).
        nonempty = [f for f in log_files if os.path.getsize(f) > 0]
        if not nonempty:
            LOG.logger.info(f"Skipping {pandas_file}: all {len(log_files)} log file(s) are empty")
            return None
        log_files = nonempty
        try:
            nmea_messages = read_log_files(log_files, 16)
            if not nmea_messages:
                # A non-empty file can still yield zero whitelisted records.
                LOG.logger.info(f"Skipping {pandas_file}: no NMEA records in {len(log_files)} log file(s)")
                return None
            time_messages = extract_time_messages(nmea_messages)
            params = find_time_correction(time_messages)
            if params is not None:
                correct_time_messages(time_messages, **params)
                time_stamped_messages = assign_time_to_messages(nmea_messages)
            else:
                time_stamped_messages = assign_time_to_messages(nmea_messages, use_gps_only=True)
            df = messages_to_dataframe(time_stamped_messages, pandas_time_step=0.1)
            # Scrub the unixtime column into a strictly-monotonic sequence
            # using the same PID-clamp the read side applies. This turns
            # any backward-time jitter (from assign_time_to_messages'
            # linear interpolation or occasional BB clock glitches) into
            # bounded forward drift so downstream code that slices by
            # unixtime (insta360.log_extract_slice, find_starts, etc.) is
            # protected without having to re-run compute_record_times.
            from Libraries import race_logs as _race_logs
            pre_backward = int((np.diff(df['unixtime'].to_numpy()) < 0).sum())
            base = float(df['unixtime'].iloc[0])
            deltas = _race_logs.clamp_monotonic_deltas(df['unixtime'].to_numpy(),
                                                       df['row_seconds'].to_numpy())
            df['unixtime'] = base + deltas
            post_backward = int((np.diff(df['unixtime'].to_numpy()) < 0).sum())
            if pre_backward:
                LOG.logger.info(f"unixtime monotonicity: {pre_backward} backward steps in raw stream, "
                                f"{post_backward} after clamp")
            LOG.logger.info(f"Writing {pandas_file}")
            # keep only the columns we want, but some might be missing!
            cols = set(df.columns)
            # union 
            cols_to_keep = COLS_TO_KEEP.intersection(cols)
            cols_missing = COLS_TO_KEEP - cols_to_keep
            LOG.logger.info(f"{cols_missing}, missing columns")
            cdf = df[list(cols_to_keep)].copy()
            # Handle zeus_source column if it exists
            if 'zeus_source' in cdf.columns:
                try:
                    cdf['zeus_source'] = cdf['zeus_source'].astype(str)
                except Exception as e:
                    LOG.logger.warning(f"Could not convert zeus_source to string: {e}. Dropping column.")
                    cdf = cdf.drop(columns=['zeus_source'])
            # Atomic write: stage to .tmp then os.replace so SIGINT mid-write
            # never leaves a truncated parquet that the existence-only skip-
            # guard above would silently re-use on the next run.
            tmp_path = pandas_path + ".tmp"
            try:
                cdf.to_parquet(tmp_path)
                os.replace(tmp_path, pandas_path)
            except BaseException:
                try:
                    os.unlink(tmp_path)
                except FileNotFoundError:
                    pass
                raise
            return cdf
        except Exception as e:
            LOG.logger.error(f"Error processing {log_files[0]}: {e}")

