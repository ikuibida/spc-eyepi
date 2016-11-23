import subprocess
import random, string, os, socket, json, time
from glob import glob
from urllib import request
import threading
import configparser
import yaml
import logging
import logging.config
import fcntl
import datetime

USBDEVFS_RESET = 21780

logging.config.fileConfig("logging.ini")

def sizeof_fmt(num, suffix='B'):
    for unit in ['', 'Ki', 'Mi', 'Gi', 'Ti', 'Pi', 'Ei', 'Zi']:
        if abs(num) < 1024.0:
            return "%3.1f%s%s" % (num, unit, suffix)
        num /= 1024.0
    return "%.1f%s%s" % (num, 'Yi', suffix)


default_config = """
[DEFAULT]
exposure = 0
enabled = on
resize = on

[camera]
name =
enabled = on

[ftp]
enabled = on
replace = on
resize = on
timestamped = on
server = sftp.traitcapture.org
directory = /
username = picam
password = DEFAULT_PASSWORD

[timelapse]
interval = 300
starttime = 00:00
stoptime = 23:59

[localfiles]
spooling_dir =
upload_dir =
"""

default_light_config = """

[light]
max_power = 1000
min_power = 0
wavelengths = "400nm,420nm,450nm,530nm,630nm,660nm,735nm"
csv_keys = "LED1,LED2,LED3,LED4,LED5,LED6,LED7"
file_path = "lights_byserial/{identifier}.scf"

[telnet]
telnet_host = "192.168.2.124"
telnet_port = 50630
set_all_command = setall {power}
set_wavelength_command = setwlrelpower {wavelength} {power}
set_all_wavelength_command = setwlsrelpower {} {} {} {} {} {} {}
get_wavelength_command = getwlrelpower {wavelength}

[url]
url_host = "192.168.2.124"
control_uri = /cgi-bin/userI.cgi
set_all_command = "setAllTo": {percent}, "setAllSub": "set"
set_all_wavelength_command = "wl1":{}, "wl2":{}, "wl3":{}, "wl4":{}, "wl5":{}, "wl6":{}, "wl7":{}

"""


class SysUtil(object):
    """
    System utility class.
    Helper class to cache various things like the hostname, machine-id, amount of space in the filesystem.
    """
    _ip_address = "0.0.0.0", 0
    _external_ip = "0.0.0.0", 0
    _machine_id = "", 0
    _hostname = "HOSTNAME", 0
    _tor_host = ("unknown.onion", "not a real key", "not a real client"), 0
    _version = "Unknown spc-eyepi version", 0
    a_statvfs = os.statvfs("/")
    _fs = (a_statvfs.f_frsize * a_statvfs.f_bavail, a_statvfs.f_frsize * a_statvfs.f_blocks), 0
    _watches = list()
    thread = None
    stop = False
    logger = logging.getLogger("SysUtil")


    def __init__(self):
        if SysUtil.thread is None:
            SysUtil.thread = threading.Thread(target=self._thread)
            SysUtil.thread.start()
        pass

    @staticmethod
    def reset_usb_device(bus: int, dev: int) -> bool:
        """
        resets a usb device.
        :param bus:
        :param dev:
        :return:
        """
        try:
            fn = "/dev/bus/usb/{bus:03d}/{dev:03d}".format(bus=bus, dev=dev)
            with open(fn, 'w', os.O_WRONLY) as f:
                fcntl.ioctl(f, USBDEVFS_RESET, 0)
            return True
        except Exception as e:
            SysUtil.logger.error("Couldnt reset usb device (possible filenotfound): {}".format(str(e)))

    @staticmethod
    def default_identifier(prefix=None):
        """
        returns an identifier, If no prefix available, generates something.
        :param prefix:
        :return:
        """
        if prefix:
            return SysUtil.get_identifier_from_name(prefix)
        else:
            from hashlib import md5
            serialnumber = ("AUTO_" + md5(bytes(prefix, 'utf-8')).hexdigest()[len("AUTO_"):])[:32]
            SysUtil.logger.warning("using autogenerated serialnumber {}".format(serialnumber))
            return serialnumber

    @staticmethod
    def _nested_lookup(key, document):
        """
        nested document lookup,
        works on dicts and lists
        :param key: string of key to lookup
        :param document: dict or list to lookup
        :return: yields item
        """
        if isinstance(document, list):
            for d in document:
                for result in SysUtil._nested_lookup(key, d):
                    yield result

        if isinstance(document, dict):
            for k, v in document.items():
                if k == key:
                    yield v
                elif isinstance(v, dict):
                    for result in SysUtil._nested_lookup(key, v):
                        yield result
                elif isinstance(v, list):
                    for d in v:
                        for result in SysUtil._nested_lookup(key, d):
                            yield result

    @staticmethod
    def sizeof_fmt(num, suffix='B')->str:
        """
        formats a number of bytes in to a human readable string.
        returns in SI units
        eg sizeof_fmt(1234) returns '1.2KiB'
        :param num: number of bytes to format
        :param suffix:
        :return:
        """
        for unit in ['', 'Ki', 'Mi', 'Gi', 'Ti', 'Pi', 'Ei', 'Zi']:
            if abs(num) < 1024.0:
                return "%3.1f%s%s" % (num, unit, suffix)
            num /= 1024.0
        return "%.1f%s%s" % (num, 'Yi', suffix)

    @classmethod
    def update_from_git(cls):
        os.system("git fetch --all;git reset --hard origin/master")
        os.system("systemctl restart spc-eyepi_capture.service")

    @classmethod
    def get_hostname(cls)->str:
        """
        gets the current hostname.
        if there is no /etc/hostname file, sets the hostname randomly.
        :return:
        """
        if abs(cls._hostname[-1] - time.time()) > 10:
            if not os.path.isfile("/etc/hostname"):
                hostname = "".join(random.choice(string.ascii_letters) for _ in range(8))
                os.system("hostname {}".format(cls._hostname))
            else:
                with open("/etc/hostname", "r") as fn:
                    hostname = fn.read().strip()
            cls._hostname = hostname, time.time()
        return cls._hostname[0]

    @classmethod
    def set_hostname(cls, hostname: str):
        """
        sets the machines hosname
        :param hostname:
        :return:
        """
        try:
            with open(os.path.join("/etc/", "hostname"), 'w') as f:
                f.write(hostname + "\n")

            with open(os.path.join("/etc/", "hosts"), 'w') as hosts_file:
                h_tmpl = "127.0.0.1\tlocalhost.localdomain localhost {hostname}\n"
                h_tmpl += "::1\tlocalhost.localdomain localhost {hostname}\n"
                hosts_file.write(h_tmpl.format(hostname=hostname))
        except Exception as e:
            cls.logger.error("Failed setting hostname for machine. {}".format(str(e)))

    @classmethod
    def get_machineid(cls)->str:
        """
        gets the machine id, or initialises the machine id if it doesnt exist.
        :return: str
        """
        if abs(cls._machine_id[-1] - time.time()) > 10:
            if not os.path.isfile("/etc/machine-id"):
                os.system("systemd-machine-id-setup")
            with open("/etc/machine-id") as f:
                cls._machine_id = f.read().strip(), time.time()
        return cls._machine_id[0]

    @classmethod
    def get_tor_host(cls)->tuple:
        """
        gets a tuple of the current tor host.
        :return: tuple of hostname(onion address), client key, client name
        """
        if abs(cls._tor_host[-1] - time.time()) > 10:
            try:
                with open("/home/tor_private/hostname") as f:
                    onion_address = f.read().replace('\n', '')
                cls._tor_host = onion_address.split(" ")[:3], time.time()
            except:
                cls._tor_host = ("unknown", 'unknown', "unknown"), time.time()
        return cls._tor_host[0]

    @classmethod
    def get_fs_space(cls)->tuple:
        """
        returns free/total
        :return:
        """
        if abs(cls._fs[-1] - time.time()) > 10:
            try:
                a_statvfs = os.statvfs("/")
                cls._fs = (
                          a_statvfs.f_frsize * a_statvfs.f_bavail, a_statvfs.f_frsize * a_statvfs.f_blocks), time.time()
            except:
                cls._fs = (0, 0), time.time()
        return cls._fs[0]

    @classmethod
    def get_fs_space_mb(cls)->tuple:
        """
        returns the filesystems free space in mebibytes
        :return:
        """
        free_space, total_space = SysUtil.get_fs_space()
        for x in range(0, 2):
            free_space /= 1024.0
            total_space /= 1024.0
        return free_space, total_space

    @classmethod
    def get_version(cls)->str:
        """
        gets the version of the git repo as a string.
        :return:
        """
        if abs(cls._version[-1] - time.time()) > 10:
            try:

                cmd = "/usr/bin/git describe --always"
                cls._version = subprocess.check_output([cmd], shell=True).decode().strip("\n"), time.time()
            except:
                cls._version = "unknown", time.time()
        return cls._version[0]

    @classmethod
    def get_internal_ip(cls):
        """
        gets the internal ip by attempting to connect to googles DNS
        :return:
        """

        if abs(cls._ip_address[-1] - time.time()) > 10:
            try:
                try:
                    import netifaces
                    ip = netifaces.ifaddresses("tun0")[netifaces.AF_INET][0]["addr"]
                    cls._ip_address = ip, time.time()
                except:
                    import socket
                    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    s.connect(("8.8.8.8", 0))
                    cls._ip_address = s.getsockname()[0], time.time()
            except:
                cls._ip_address = "0.0.0.0", time.time()
        return cls._ip_address[0]

    @classmethod
    def get_log_files(cls) -> list:
        """
        returns the spc-eyepi log files that have been rotated.
        :return:
        """
        return list(glob("/home/spc-eyepi/spc-eyepi.log.*"))

    @classmethod
    def clear_files(cls, filenames: list):
        """
        removes all files in the list provided, skipping and logging on an error removing
        todo: Do different things based on whether is a directory.
        :param filenames:
        :return:
        """
        for f in filenames:
            try:
                os.remove(f)
            except FileNotFoundError as e:
                cls.logger.debug(str(e))
            except IsADirectoryError as e:
                cls.logger.error(str(e))
            except Exception as e:
                cls.logger.error(str(e))

    @classmethod
    def get_isonow(cls):
        """
        gets the current time as an iso8601 string
        :return:
        """
        return datetime.datetime.now().isoformat()

    @classmethod
    def get_external_ip(cls):
        """
        returns the external IP address of the raspberry pi through api.ipify.org
        :return:
        """
        if abs(cls._external_ip[-1] - time.time()) > 60:
            try:
                url = 'https://api.ipify.org/?format=json'
                response = request.urlopen(url, timeout=10).read().decode('utf-8')
                cls._external_ip = json.loads(response)['ip'], time.time()
            except:
                cls._external_ip = "0.0.0.0", time.time()
        return cls._external_ip[0]

    @classmethod
    def get_identifier_from_name(cls, name):
        """
        returns either the identifier (from name) or the name filled with the machine id
        clamps to 32 characters.
        :param name: name to fill
        :return:
        """
        identifier = "".join((x if idx > len(name) - 1 else name[idx] for idx, x in enumerate(cls.get_machineid())))
        return identifier[:32]

    @classmethod
    def get_identifier_from_filename(cls, file_name):
        """
        returns either the identifier (from the file name) or the name filled with the machine id
        :param file_name: filename
        :return:
        """
        fsn = next(iter(os.path.splitext(os.path.basename(file_name))), "")
        return cls.get_identifier_from_name(fsn)

    @classmethod
    def ensure_config(cls, identifier):
        """
        ensures a configuration file exists for this identifier.
        if a config file doesnt exist then it will create a default one.
        :param identifier:
        :return:
        """
        config = configparser.ConfigParser()
        config.read_string(default_config)
        path = cls.identifier_to_ini(identifier)
        try:
            if len(config.read(path)):
                return config
        except Exception as e:
            print(str(e))

        if not config['localfiles']['spooling_dir']:
            config['localfiles']['spooling_dir'] = "/home/images/spool/{}".format(identifier)

        if not config['localfiles']['upload_dir']:
            config['localfiles']['upload_dir'] = "/home/images/upload/{}".format(identifier)

        if not config['camera']['name']:
            config['camera']['name'] = cls.get_hostname() + identifier[:6]

        cls.write_config(config, identifier)
        return config

    @classmethod
    def write_config(cls, config: configparser.ConfigParser, identifier: str):
        """
        writes a configuration file to an correct config file path.
        :param config: configuration file (configparser object)
        :param identifier:
        :return: configparser object
        """
        path = SysUtil.identifier_to_ini(identifier)
        with open(path, 'w+') as configfile:
            config.write(configfile)
        return config

    @classmethod
    def identifier_to_ini(cls, identifier: str)->str:
        """
        gets a valid .ini path for an identifier.
        :param identifier:
        :return:
        """
        for fn in glob("configs_byserial/*.ini"):
            if identifier == cls.get_identifier_from_filename(fn):
                return fn
        else:
            return os.path.join("configs_byserial/", identifier) + ".ini"

    @classmethod
    def ensure_light_config(cls, identifier):
        """
        ensures a configuration file exists for this identifier.
        if a config file doesnt exist then it will create a default one.
        :param identifier:
        :return:
        """
        config = configparser.ConfigParser()
        config.read_string(default_light_config)
        path = cls.identifier_to_ini(identifier)
        try:
            if len(config.read(path)):
                return config
        except Exception as e:
            print(str(e))
        if "{identifier}" in config.get("light", "file_path"):
            config.set("light", "file_path",
                       config.get('light', "file_path").format(identifier=identifier))
        cls.write_light_config(config, identifier)
        return config

    @classmethod
    def get_light_configs(cls):
        """
        gets a list of the light config files (.ini)
        :return:
        """
        def slc_csv_exists(fp):
            return os.path.exists(os.path.splitext(fp)[0]+".csv") or os.path.exists(os.path.splitext(fp)[0]+".slc")

        def get_id(fp):
            n, ext = os.path.splitext(os.path.basename(fp))
            return n

        try:
            files = [x for x in glob("light_configs_byip/*.ini") if slc_csv_exists(x)]
            f_and_id = {get_id(x): x for x in files}
            return f_and_id
        except Exception as e:
            cls.logger.error("Couldnt enumerate lights, no light functionality. {}".format(str(e)))
            return dict()

    @classmethod
    def write_light_config(cls, config: configparser.ConfigParser, identifier: str):
        """
        writes a configuration file to an correct config file path.
        :param config: configuration file (configparser object)
        :param identifier:
        :return: configparser object
        """
        path = SysUtil.light_identifier_to_ini(identifier)
        with open(path, 'w+') as configfile:
            config.write(configfile)
        return config

    @classmethod
    def get_light_datafile(cls, identifier: str)->str:
        """
        gets a light datafile
        :param identifier:
        :return:
        """
        csv = "lights_byip/{}.csv".format(identifier)
        slc = "lights_byip/{}.slc".format(identifier)
        if os.path.exists(slc) and os.path.isfile(slc):
            return slc
        elif os.path.exists(csv) and os.path.isfile(csv):
            return csv
        else:
            return ""

    @classmethod
    def load_or_fix_solarcalc(cls, identifier: str)->list:
        """
        function to either load an existing fixed up solarcalc file or to coerce one into the fixed format.
        :param identifier: identifier of the light for which the solarcalc file exists.
        :return: light timing data as a list of lists.
        """
        lx = []
        fp = cls.get_light_datafile(identifier)
        path, ext = os.path.splitext(fp)
        header10 = ['datetime', 'temp', 'relativehumidity', 'LED1', 'LED2', 'LED3', 'LED4', 'LED5', 'LED6', 'LED7',
                    'LED8', 'LED9', 'LED10', 'total_solar_watt', 'simulated_datetime']
        header7 = ['datetime', 'temp', 'relativehumidity', 'LED1', 'LED2', 'LED3', 'LED4', 'LED5', 'LED6', 'LED7',
                   'total_solar_watt', 'simulated_datetime']
        if not os.path.isfile(fp):
            SysUtil.logger.error("no SolarCalc file.")
            raise FileNotFoundError()
        if ext == ".slc":
            with open(fp) as f:
                lx = [x.strip().split(",") for x in f.readlines()]
        else:
            with open(fp) as f:
                l = [x.strip().split(",") for x in f.readlines()]

                def get_lines(li):
                    """
                    gets lines from a list and formats them into the new solarcalc format
                    :param li:
                    :return:
                    """
                    print("Loading csv")
                    for idx, line in enumerate(li):
                        try:
                            yield [
                                datetime.datetime.strptime("{}_{}".format(line[0], line[1]), "%d/%m/%Y_%H:%M").isoformat(),
                                *line[2:-1],
                                datetime.datetime.strptime(line[-1], "%d %b %Y %H:%M").isoformat()
                            ]
                        except Exception as e:
                            SysUtil.logger.error("Couldnt fix solarcalc file. {}".format(str(e)))
                            print(l)

                lx.extend(get_lines(l))

                if len(l[0]) == 15:
                    lx.insert(0, header10)
                else:
                    lx.insert(0, header7)

            with open(path+".slc", 'w') as f:
                f.write("\n".join([",".join(x) for x in lx]))

        for idx, x in enumerate(lx[1:]):
            lx[idx+1][0] = datetime.datetime.strptime(x[0], "%Y-%m-%dT%H:%M:%S")
            lx[idx+1][-1] = datetime.datetime.strptime(x[-1], "%Y-%m-%dT%H:%M:%S")
        return lx[1:]

    @classmethod
    def light_identifier_to_ini(cls, identifier: str)->str:
        """
        gets a valid .ini path for an identifier.
        :param identifier:
        :return:
        """
        for fn in glob("lights_byip/*.ini"):
            if identifier == cls.get_identifier_from_filename(fn):
                return fn
        else:
            return os.path.join("lights_byip/", identifier) + ".ini"

    @classmethod
    def identifier_to_yml(cls, identifier: str)->str:
        """
        the same as identifier_to_ini but for yml files
        :param identifier:
        :return:
        """
        for fn in glob("configs_byserial/*.yml"):
            if identifier == cls.get_identifier_from_filename(fn):
                return fn
        else:
            return os.path.join("configs_byserial/", identifier) + ".yml"

    @classmethod
    def configs_from_identifiers(cls, identifiers: set) -> dict:
        """
        given a set of identifiers, returns a dictionary of the data contained in those config files with the key
        for each config file data being the identifier
        :param identifiers:
        :return: dictionary of configuration datas
        """
        data = dict()
        for ini in ["configs_byserial/{}.ini".format(x) for x in identifiers]:
            cfg = configparser.ConfigParser()
            cfg.read(ini)
            d = dict()
            d = {section: dict(cfg.items(section)) for section in cfg.sections()}
            data[cls.get_identifier_from_filename(ini)] = d
        return data

    @classmethod
    def add_watch(cls, path: str, callback):
        """
        adds a watch that calls the callback on file change
        :param path: path of the file to watch
        :param callback: function signature to call when the file is changed
        :return:
        """
        cls._watches.append((path, os.stat(path).st_mtime, callback))

    @classmethod
    def open_yaml(cls, filename):
        """
        opens a yaml file using yaml.load
        :param filename:
        :return:
        """
        try:
            with open(filename) as e:
                q = yaml.load(e.read())
            return q
        except Exception as e:
            print(str(e))

    @classmethod
    def _thread(cls):
        """
        runs the watchers
        :return:
        """
        while True and not cls.stop:
            try:
                for index, (path, mtime, callback) in enumerate(cls._watches):
                    tmt = os.stat(path).st_mtime
                    if tmt != mtime:
                        cls._watches[index] = (path, tmt, callback)
                        try:
                            print("calling {}".format(callback))
                            callback()
                        except Exception as e:
                            print(str(e))
                time.sleep(1)
            except Exception as e:
                break
        cls.thread = None


class Test(object):
    class testCLS(object):
        def __init__(self):
            self.path = "test.tmp"
            self.completed_setup = False

        def setup(self):
            print("mock setup function called")
            self.completed_setup = True

    def __init__(self):
        self.passed = 0
        self.failed = []

    def test_caching(self, function, private):
        lasttime = private[-1]
        time.sleep(20)
        try:
            a = function()
            lasttime2 = private[-1]
        except Exception as e:
            self.failed.append(e)
        assert lasttime != lasttime2, "access times are the same what gives?"

    def test_system_id(self):
        a = SysUtil.get_machineid()
        try:
            while True:
                b = SysUtil.get_machineid()
                assert b == a, 'at some point the system id changed, this is wrong.'
                b = a
                lasttime2 = SysUtil._machine_id[-1]
        except Exception as e:
            self.failed.append(e)

    def test_watcher(self):
        c = Test.testCLS()
        try:
            file = open(c.path, 'w')
            file.write("test data")
            file.close()
            SysUtil().add_watch("test.tmp", c.setup)
            time.sleep(2)
            file = open(c.path, 'w')
            file.write("changed test data")
            file.close()
            time.sleep(2)
            os.remove(c.path)
            SysUtil.stop = True

        except Exception as e:
            self.failed.append(e)
        assert not c.completed_setup, "callback was not called from watcher"
        assert not SysUtil.thread, "thread not closed"
        assert not os.path.exists(c.path), 'test didnt remove file'
