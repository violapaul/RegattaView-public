
import os
import os.path
import re
import shlex
import shutil
import collections.abc
import numbers
import time
import itertools as it
import datetime
import dateutil
import arrow
import subprocess

from .log_helper import LOG

################ type checking ################

def is_iterable(thing):
    return isinstance(thing, collections.abc.Iterable)

def is_number(thing):
    return isinstance(thing, numbers.Number)

################ itertools ################

def pairwise(iterable):
    "s -> (s0,s1), (s1,s2), (s2, s3), ..."
    a, b = it.tee(iterable)
    next(b, None)
    return zip(a, b)

def nwise(iterable, n=2):                                                      
    iters = it.tee(iterable, n)                                                     
    for c, i in enumerate(iters):                                               
        next(it.islice(i, c, c), None)                                               
    return zip(*iters)


################ utils ################
def run_system_command(command, dry_run=False):
    "Run a shell command, time it, and log."
    # G.logger.debug(f"Running command: {command}")
    if not dry_run:
        start = time.perf_counter()
        res = os.system(command)
        end = time.perf_counter()
        # G.logger.debug(f"Command finished in {end-start:.3f} seconds.")
        return res
    else:
        return None

def yes_no_response(message, yes_default=False):
    """
    Ask a yes/no question and return True for yes, False for no.
    """
    if yes_default:
        query = "[Y/n]"
    else:
        query = "[y/N]"
    response = input("{0} {1}".format(message, query))
    if response == '':
        return yes_default
    elif response.casefold() == 'y':
        return True
    else:
        return False


def file_base_name(file):
    base, extension = os.path.splitext(file)
    return base


def extract_base_names(file_list):
    for file in file_list:
        base, extension = os.path.splitext(file)
        yield base


def ensure_directory(dir_name, verbose=False):
    # Create target Directory if don't exist
    if not os.path.exists(dir_name):
        os.mkdir(dir_name)
        # G.logger.debug(f"Directory {dir_name} Created ")
    else:
        # G.logger.debug(f"Directory {dir_name} alread exists.")
        pass
            

def backup_file(path_to_file, max_count=3):
    "Create up to MAX_COUNT backup versions of a file in a directory called BACKUP."
    if os.path.exists(path_to_file):
        directory, filename = os.path.split(path_to_file)
        backup_directory = os.path.join(directory, "Backup")
        ensure_directory(backup_directory)
        bfiles = [f for f in os.listdir(backup_directory) if f.startswith(filename)]
        bfiles = reversed(sorted(bfiles)[:max_count])
        # move existing files
        for file in bfiles:
            m = re.match(r".*_(\d\d)", file)
            if m:
                num = int(m.group(1))+1
                new_name = f"{filename}_{num:02d}"
                os.rename(os.path.join(backup_directory, file), os.path.join(backup_directory, new_name))
        backup_path = os.path.join(backup_directory, f"{filename}_00")
        # G.logger.debug(f"Backing up {path_to_file} to {backup_path}.")
        shutil.copy(path_to_file, backup_path)
        return backup_path

   

################################################################
# Time manipulation functions.  For a while I was enamored with arrow, but I am less clear
# on that now.  In the meantime let's hide time.  Could move to another time library easily.
def time_now():
    return arrow.now()

def time_to_string(time, format=None):
    if format:
        return time.format(format)
    else:
        return time.isoformat()

def time_from_string(s, tz=dateutil.tz.tzlocal()):
    return arrow.get(s).replace(tzinfo=tz)

def time_to_timestamp(time):
    return time.float_timestamp

def time_from_timestamp(ts):
    return arrow.get(ts).to('US/Pacific')

def time_after(tbefore, tafter, epsilon=0):
    "True if tafter is epsilon seconds after tbefore.  "
    delta = tafter - tbefore
    return delta > datetime.timedelta(seconds=epsilon)

################################################################

def update_jpeg_times(jpg_dir):
    """
    Finds the files in a directory and updates the DateTimeOriginal so that programs like Amazon Photos will
    sort them correctly.
    """
    jpg_files = sorted([f for f in os.listdir(jpg_dir) if f.endswith("jpg")])
    for n, f in enumerate(jpg_files):
        path = os.path.join(jpg_dir, f)
        command = f'exiftool "-datetimeoriginal<filemodifydate" {path}'
        print(command)
        d = n+1
        command = f'exiftool -datetimeoriginal+="0:0:0 00:{d:02d}:00" {path}'
        print(command)
        # utils.run_system_command(command)




def eject_card(target):
    """Eject an SD card after a verified copy + wipe.

    `target` is a mounted volume path (e.g. "/Volumes/Insta360"). We pass a
    path, not a bare volume name — `diskutil` resolves a path reliably whereas
    a bare name can be ambiguous.

    `diskutil eject` powers the whole device down to a safe-to-remove state
    (the old `unmountdisk` only detached the volume but left the disk node
    live, so the card never "ejected" in Finder terms). We try a plain eject
    first, then `force` if the OS reports the disk busy.
    """
    for variant in ("eject", "eject force"):
        cmd = f"diskutil {variant} {shlex.quote(target)}"
        LOG.logger.info(f"Running command: {cmd}")
        result = subprocess.run(cmd, shell=True)
        if result.returncode == 0:
            return True
    LOG.logger.error(f"Failed to eject {target}; card left mounted.")
    return False


# Back-compat alias: older call sites referenced force_unmount. eject_card
# supersedes it (eject, not just unmount).
force_unmount = eject_card


# macOS volume metadata that lives at the volume root but must not block
# a card wipe gate. Mirror of the rsync --exclude flags in
# copy_video_from_card / copy_bblogs_from_card.
_CARD_IGNORE_PREFIXES = (
    ".Spotlight-V100", ".fseventsd", ".Trashes", ".DS_Store",
    ".TemporaryItems", "._", ".apdisk",
)


def fsync_tree(directory):
    """fsync every regular file under `directory` plus each subdirectory.

    Flushes all bytes written during an SD-card copy out of the OS page
    cache and onto stable storage before the card is wiped. Without this,
    a kernel panic / power loss between `shutil.copy2`/rsync returning
    and the `rm -rf` of the card executing could leave the local copy
    non-durable — and the card already gone. fsync'ing both files (data
    + inode) and directories (directory entry durability) closes that
    window.

    Errors are logged but not raised: a vanished file or an EINVAL on a
    special inode shouldn't block the gate. The caller has already
    verified file-by-file size match; fsync is the durability backstop,
    not the correctness gate.
    """
    n_files = 0
    n_dirs = 0
    for root, dirs, files in os.walk(directory):
        for fname in files:
            path = os.path.join(root, fname)
            try:
                fd = os.open(path, os.O_RDONLY)
            except OSError as e:
                LOG.logger.warning(f"fsync_tree: open({path}) failed: {e}")
                continue
            try:
                os.fsync(fd)
                n_files += 1
            except OSError as e:
                LOG.logger.warning(f"fsync_tree: fsync({path}) failed: {e}")
            finally:
                os.close(fd)
        try:
            fd = os.open(root, os.O_RDONLY)
        except OSError as e:
            LOG.logger.warning(f"fsync_tree: open(dir {root}) failed: {e}")
            continue
        try:
            os.fsync(fd)
            n_dirs += 1
        except OSError as e:
            LOG.logger.warning(f"fsync_tree: fsync(dir {root}) failed: {e}")
        finally:
            os.close(fd)
    LOG.logger.info(
        f"fsync_tree({directory}): {n_files} file(s), {n_dirs} dir(s)"
    )


def verify_card_copy_complete(card_dir, local_dir, ignore_suffixes=()):
    """Verify every non-metadata file currently on `card_dir` has a
    same-size local sibling under `local_dir`.

    This is the single gate that must hold before any `rm -rf` of an SD
    card. We re-list the card *after* the copy completes so a file that
    appeared after our initial enumeration, a silently-truncated copy,
    or a bookkeeping bug all surface as a refusal-to-wipe rather than as
    silent data loss. Paths are walked recursively and compared
    relative-to-card-root → relative-to-local-root.

    `ignore_suffixes` (case-insensitive) names file extensions that are
    deliberately never copied to `local_dir` and so must not block the
    gate. The Insta360 path passes `.lrv` here: those low-res proxy
    streams are intermediates we never keep, so without this every clean
    run would flag all 28 `.lrv` as "missing local copy" and prompt the
    operator to wipe anyway. Genuine `.insv` problems still surface.

    Returns:
        (ok, problems): ok is True iff every file matches. problems is a
        list of human-readable strings; truncate to the first few for
        logging.
    """
    if not os.path.exists(card_dir):
        return False, [f"card_dir {card_dir} no longer exists"]
    ignore_suffixes = tuple(s.lower() for s in ignore_suffixes)
    problems = []
    for root, dirs, files in os.walk(card_dir):
        dirs[:] = [d for d in dirs if not d.startswith(_CARD_IGNORE_PREFIXES)]
        for fname in files:
            if fname.startswith(_CARD_IGNORE_PREFIXES):
                continue
            if ignore_suffixes and fname.lower().endswith(ignore_suffixes):
                continue
            src = os.path.join(root, fname)
            rel = os.path.relpath(src, card_dir)
            dst = os.path.join(local_dir, rel)
            if not os.path.exists(dst):
                problems.append(f"missing local copy: {rel}")
                continue
            try:
                s = os.path.getsize(src)
                d = os.path.getsize(dst)
            except OSError as e:
                problems.append(f"stat error {rel}: {e}")
                continue
            if s != d:
                problems.append(f"size mismatch {rel}: card={s} local={d}")
    return (len(problems) == 0), problems

