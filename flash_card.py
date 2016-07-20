import glob
import os
import random
import string
import shutil
import subprocess
import tarfile
import argparse

parser = argparse.ArgumentParser(description="Flash spc-eyepi sd card")
parser.add_argument("blockdevice", metavar='d', type=str, nargs=1,
                    help="Block device/devices to copy data to")
parser.add_argument("--tarfile", metavar="t", type=argparse.FileType('rb'),
                    help="tar/tar.gz file to flash to the card")
parser.add_argument("--api-token", metavar='k', type=str,
                    help="traitcapture api token for automated addition to database")
parser.add_argument("--update", default=False,action='store_true',
                    help="dont flash new data to the card, update the software and set the name if required.")
parser.add_argument("--backup", metavar='b',
                    help="backup the tor encryption keys, ssh encryption keys, and camera config files to a directory.")
parser.add_argument("--restore", metavar='r',
                    help="restore tor encryption keys, ssh encryption keys, and camera config files from a directory.")
parser.add_argument("--name", metavar='n', type=str,
                    help="name of the raspberry pi (will be autogenerated if not provided)")

args = parser.parse_args()

RAND = "".join((random.choice(string.ascii_letters) for _ in range(6)))
if not args.name:
    print("No name provided. autogenerated: {} for a name".format("Picam-"+RAND))

gname = "Picam-"+RAND if not args.name else args.name



def mkdir_mount():
    temp_directory = os.path.join('/tmp/spc_os/', RAND)
    print("temp dir: {}".format(temp_directory))
    d = glob.glob(args.blockdevice[0]+"*")
    d.sort()
    d.pop(0)
    os.makedirs(os.path.join(temp_directory, "boot"), exist_ok=True)
    os.makedirs(os.path.join(temp_directory, "root"), exist_ok=True)
    if not len(d) >= 2:
        print("didnt find 2 partitions on the block device to mount...")
        return temp_directory
    try:
        print("Mounting {}".format(" ".join(d)))
        os.system("mount {} {}".format(d[0], os.path.join(temp_directory, "boot")))
        os.system("mount {} {}".format(d[1], os.path.join(temp_directory, "root")))
    except Exception as e:
        print(str(e))
    return temp_directory


def cleanup(tmpdir):
    dirs = glob.glob(os.path.join(tmpdir, "*"))
    for mp in dirs:
        print("umount {}".format(mp))
        os.system("umount {}".format(mp))
    shutil.rmtree(tmpdir)
    os.sync()


def write_api_token(tmpdir):
    import requests, json
    resp = requests.get("https://traitcapture.org/api/code/new/14?token=" + args.api_token)
    if resp.status_code == 200:
        try:
            js = json.loads(resp.text)
            ssh_dir = os.path.join(tmpdir, "root", "home", ".ssh")
            os.makedirs(ssh_dir, exist_ok=True)
            with open(os.path.join(ssh_dir, "token"), 'w') as f:
                f.write(js['code'])
        except Exception as e:
            print("Couldnt get token using key")

    else:
        print("invalid response from server")


def backup_old(tmpdir):
    print("copying old files over...")
    with open(os.path.join(tmpdir, "root", "hostname"), 'r') as f:
        hostname = f.read().strip()
    bakdir = "{}.bak".format(hostname)
    os.makedirs(bakdir)
    os.makedirs(os.path.join(bakdir, "configs"), exist_ok=True)
    os.makedirs(os.path.join(bakdir, "tor_private"), exist_ok=True)
    os.makedirs(os.path.join(bakdir, ".ssh"), exist_ok=True)
    shutil.copy(os.path.join(tmpdir, "root", "etc", "hostname"), os.path.join(bakdir, "hostname"))
    shutil.copy(os.path.join(tmpdir, "root", "home", "spc-eyepi", "configs_byserial"), os.path.join(bakdir, "configs"))
    shutil.copy(os.path.join(tmpdir, "root", "home", "spc-eyepi", "tor_private"), os.path.join(bakdir, "tor_private"))
    shutil.copy(os.path.join(tmpdir, "root", "home", ".ssh"), os.path.join(bakdir, "ssh"))


def restore(tmpdir, bakdir=None):
    if not bakdir:
        with open(os.path.join(tmpdir, "root", "hostname"), 'r') as f:
            hostname = f.read().strip()
        bakdir = "{}.bak".format(hostname)

    if os.path.exists(bakdir):
        shutil.copy(os.path.join(bakdir, "hostname"), os.path.join(tmpdir, "root", "etc", "hostname"))
        shutil.copy(os.path.join(bakdir, "configs"), os.path.join(tmpdir, "root", "home", "spc-eyepi", "configs_byserial"))
        shutil.copy(os.path.join(bakdir, "tor_private"), os.path.join(tmpdir, "root", "home", "spc-eyepi", "tor_private"))
        shutil.copy(os.path.join(bakdir, "ssh"), os.path.join(tmpdir, "root", "home", ".ssh"))
        if args.api_token:
            write_api_token(tmpdir)

        if os.path.isfile(os.path.join(bakdir, "hostname")):
            with open(os.path.join(bakdir, "hostname"),'r') as hostname_file:
                with open(os.path.join(tmpdir, "root", "etc", "hosts"), 'w') as hosts_file:
                    h_tmpl = "127.0.0.1\tlocalhost.localdomain localhost {hostname}\n::1\tlocalhost.localdomain localhost {hostname}\n"
                    hosts_file.write(h_tmpl.format(hostname=hostname_file.read().strip()))
    else:
        print("Error: backup dir doesnt exist...")


def update_via_github(temp_dir):
    eyepi_dir = os.path.join(temp_dir, "root", "home", "spc-eyepi")
    git_dir = os.path.join(eyepi_dir, ".git")
    try:
        os.system("git --git-dir={} --work-tree={} fetch --all".format(git_dir, eyepi_dir))
        os.system('git --git-dir={} --work-tree={} reset --hard origin/master'.format(git_dir, eyepi_dir))
        v = subprocess.check_output(["git --git-dir={} --work-tree={} describe".format(git_dir, eyepi_dir)],
                                    shell=True, universal_newlines=True)
        q = subprocess.check_output(["git --git-dir={} --work-tree={} log -1 --pretty=%B".format(git_dir, eyepi_dir)],
                                    shell=True, universal_newlines=True)

        print("Now at:\n{}\n{}".format(v, q))
    except Exception as e:
        print("Coulndt update from git, {}".format(str(e)))


def format_create_new(tmpdir=None):
    if tmpdir:
        cleanup(tmpdir)
    with subprocess.Popen(["/usr/bin/fdisk", "{}".format(args.blockdevice[0])],
                          stdin=subprocess.PIPE,
                          universal_newlines=True) as proc:
        # create boot partition of size 100M
        proc.stdin.write("o\nn\np\n\n\n+100M\n")
        proc.stdin.flush()
        # make w95 fat for rpi :(
        proc.stdin.write("t\nc\n")
        proc.stdin.flush()
        # create linux partition to fill the rest of the disk.
        proc.stdin.write("n\n\n\n\n\n")
        proc.stdin.flush()

        # write to disk
        proc.stdin.write("w\n")
        proc.stdin.flush()
        # get rid of fdisk.
        proc.communicate()

    with subprocess.Popen(["/usr/bin/mkfs.vfat", "{}".format(args.blockdevice[0]+"1")],
                          stdin=subprocess.PIPE,
                          universal_newlines=True) as proc:
        proc.communicate()

    with subprocess.Popen(["/usr/bin/mkfs.ext4", "{}".format(args.blockdevice[0] + "2")],
                          stdin=subprocess.PIPE,
                          universal_newlines=True) as proc:
        proc.communicate()


def extract_new(tmpdir, tar_file_object):
    print("Extracting: ", tar_file_object.name)
    with tarfile.open(fileobj=tar_file_object, mode='r') as tar:
        try:
            tar.extractall(path=tmpdir)
            print("Tar extraction completed.")
        except KeyError:
            print("tar file doesnt have the correct folders.")
        except Exception as e:
            print("something went very wrong", str(e))

def set_hostname(tmpdir, hostname):
    with open(os.path.join(tmpdir, "root", "hostname"), 'w') as f:
        f.write(hostname+"\n")

    with open(os.path.join(tmpdir, "root", "etc", "hosts"), 'w') as hosts_file:
        h_tmpl = "127.0.0.1\tlocalhost.localdomain localhost {hostname}\n::1\tlocalhost.localdomain localhost {hostname}\n"
        hosts_file.write(h_tmpl.format(hostname=hostname))


if __name__ == '__main__':
    temp_dir = None
    if args.tarfile:
        print("Formatting and extracting")
        format_create_new()
        temp_dir = mkdir_mount()
        extract_new(temp_dir, args.tarfile)
        set_hostname(temp_dir, gname)

    if args.update:
        print("Updating")
        temp_dir = mkdir_mount()
        update_via_github(temp_dir)
        set_hostname(temp_dir, gname)

    if args.restore:
        print("Restoring from backup")
        temp_dir = mkdir_mount()
        restore(temp_dir, bakdir=args.backup_directory)
    elif args.backup:
        print("Backing up")
        temp_dir = mkdir_mount()
        backup_old(temp_dir)

    if args.api_token:
        print("Writing api token")
        write_api_token(temp_dir)


    if temp_dir:
        cleanup(temp_dir)