#!/usr/bin/python3

import logging
import logging.config
import os
import re
import subprocess
import sys
import time

import pyudev

from libs.Camera import *
from libs.Updater import Updater
from libs.Uploader import Uploader

logging.config.fileConfig("logging.ini")
logging.getLogger("paramiko").setLevel(logging.WARNING)


def detect_cameras(type):
    """
    detects cameras connected via usb/gphoto2.
    locks gphoto2, so this will cause errors if a camera is attempting to capture for the split second that it tries
    to detect

    :param type:
    :return: a dict of port:serialnumber values corresponding to the currently connected gphoto2 cameras.
    """
    try:
        a = subprocess.check_output("gphoto2 --auto-detect", shell=True).decode()
        a = a.replace(" ", "").replace("\n", "").replace("-", "")
        cams = {}
        for pstring in re.finditer("usb:", a):
            port = a[pstring.start():pstring.end() + 7]
            cmdret = subprocess.check_output(
                'gphoto2 --port "' + port + '" --get-config serialnumber',
                shell=True).decode()
            cur = cmdret.split("\n")[-2]
            cams[port] = cur.split(" ")[-1]
        return cams
    except Exception as e:
        logger.error("Could not detect camera for some reason: {}".format(str(e)))
    return None


def redetect_cameras(camera_workers):
    """
    this isnt used yet, but it may be in the future to reassign port numbers to cameras when they are unplugged.
    :param camera_workers:
    :return:
    """
    try:
        a = subprocess.check_output("gphoto2 --auto-detect", shell=True).decode()
        for port in re.finditer("usb:", a):
            cmdret = subprocess.check_output(
                'gphoto2 --port "' + a[port.start():port.end() + 7] + '" --get-config serialnumber',
                shell=True).decode()
            serialnumber = cmdret[cmdret.find("Current: ") + 9: len(cmdret) - 1]
            port = a[port.start():port.end() + 7]
            for camera_worker in camera_workers:
                if camera_worker.__name__ == serialnumber:
                    camera_worker.camera_port = port
                    logger.info("redetected camera: " + str(serialnumber) + " : " + str(port))
        return True
    except Exception as e:
        print((str(e)))
        logger.error("Could not detect camera for some reason: " + str(e))
        return False


def detect_picam():
    """
    detects whether the pi has a picam installed and enabled.
    on all SPC-OS devices this will return true if the picam is installed
    on other rpis it may return false if the raspberrypi-firmware-tools is not installed or the boot.cfg flag
    for the camera is not set.
    :return:
    """
    try:
        cmdret = subprocess.check_output("/opt/vc/bin/vcgencmd get_camera", shell=True).decode()
        if cmdret[cmdret.find("detected=") + 9: len(cmdret) - 1] == "1":
            return [PiCamera("picam.ini", name="PiCam",serialnumber="picam"), Uploader("picam.ini", name="PiCam-Uploader")]
        else:
            return None
    except subprocess.CalledProcessError:
        pass
    return None


def create_workers(cameras):
    """
    Creates thread workers from a dict of port:serialnumber strings.
    creates workers for uploaders as well as captures.
    :param cameras:
    :return:
    """

    camthreads = []
    uploadthreads = []
    for port, serialnumber in list(cameras.items()):
        camthreads.append(
            GphotoCamera(os.path.join("configs_byserial", serialnumber + ".ini"), camera_port=port,
                         serialnumber=serialnumber,
                         name=serialnumber))
        uploadthreads.append(
            Uploader(os.path.join("configs_byserial", serialnumber + ".ini"), name=serialnumber + "-Uploader"))
    return (camthreads, uploadthreads)


def start_workers(objects):
    if not objects:
        logger.warning("Tried to start workers that didnt exist")
        return
    for thread in objects:
        thread.daemon = True
        thread.start()


def kill_workers(objects):
    for thread in objects:
        thread.stop()
        thread.join()


def get_usb_dev_list():
    context = pyudev.Context()
    ret = ""
    for device in context.list_devices(subsystem='usb'):
        ret += str(device)


if __name__ == "__main__":
    logger = logging.getLogger("Worker_dispatch")
    logger.info("Program startup")
    # The main loop for capture
    cameras = None
    raspberry = None
    # TODO: Fix storage for multiple cameras
    try:
        raspberry = detect_picam()
        start_workers(raspberry)

        updater = None
        if os.path.isfile("picam.ini"):
            updater = Updater()
            updater.start()

        cameras = detect_cameras("usb")
        tries = 0
        while not cameras and tries < 10:
            logger.debug("detecting Cameras")
            cameras = detect_cameras("usb")
            time.sleep(2)
            tries += 1

        if cameras is not None:
            workers = create_workers(cameras)
            for worker in workers:
                start_workers(worker)

        # TODO: detect and classify with serialnumber /dev/videoX devices.
        # webcam = (WebCamera("webcam.ini", name="WebCam", serialnumber="webcam"),
        #           Uploader("webcam.ini", name="Webcam-Uploader"))
        # start_workers(webcam)

        # ivportcam = (IVPortCamera("ivport.ini", name="IVPort", serialnumber="ivport"),
        #           Uploader("ivport.ini", name="IVport-Uploader"))
        # start_workers(ivportcam)

        usb_dev_list = get_usb_dev_list()

        while True:
            try:
                if usb_dev_list != get_usb_dev_list():
                    if cameras is not None:
                        for worker in workers:
                            kill_workers(worker)
                    cameras = detect_cameras("usb")
                    # start workers again
                    time.sleep(60)
                    workers = create_workers(cameras)
                    for worker in workers:
                        start_workers(worker)
                    usb_dev_list = get_usb_dev_list()
                time.sleep(1)
            except (KeyboardInterrupt, SystemExit):
                if cameras is not None:
                    for worker in workers:
                        kill_workers(worker)
                # if webcam:
                #     kill_workers(webcam)

                # if ivportcam:
                #     kill_workers(ivportcam)
                if raspberry:
                    kill_workers(raspberry)
                updater.join()
                sys.exit()

    except (KeyboardInterrupt, SystemExit):
        print("exiting...")
        if cameras is not None:
            for worker in workers:
                kill_workers(worker)

        # if webcam:
        #     kill_workers(webcam)
        # if ivportcam:
        #     kill_workers(ivportcam)
        if raspberry:
            kill_workers(raspberry)
        if updater:
            kill_workers([updater])

        sys.exit()
