import argparse
import datetime
import json
import logging
import random
import re
import string
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional, Union

import requests


def data_path(relative_path):
    """
    Get the path for persistent data, ensuring files are saved in the same directory as the exe after packaging, rather than in a temporary folder.
    """
    if getattr(sys, 'frozen', False):
        base_path = Path(sys.executable).parent
    else:
        base_path = Path(__file__).parent.parent

    return base_path / relative_path


config_path = data_path("config/config.yml")
"""
data_path = Path("data")
data_path.mkdir(exist_ok=True)
cache_path = Path("cache")
cache_path.mkdir(exist_ok=True)
config_path = Path("data/config.yml")
Path("debug").mkdir(exist_ok=True)
"""
if not config_path.exists():
    config_path.parent.mkdir(exist_ok=True)
    config_path.touch()
    config_path.write_text("{}", encoding="utf-8")

import numpy as np
from fuzzywuzzy import process as fzwzprocess
from maa.context import Context
from maa.controller import AdbController
from maa.custom_action import CustomAction, CustomRecognitionResult
from maa.custom_recognition import CustomRecognition
from maa.define import RectType
from maa.resource import Resource
from maa.tasker import Tasker
from maa.toolkit import AdbDevice, Toolkit
from minitouchpy import (
    MNT,
    MNTEvATive7LogEventData,
    MNTEvent,
    MNTEventData,
    MNTServerCommunicateType,
)

import player
from api import BestdoriAPI
from chart import Chart, PlayRecord
from util import *
import cv2

MIN_LIVEBOOST = 1
LIVEMODE = "freelive"
DIFFICULTY = "hard"
OFFSET = {"up": 0, "down": 0, "move": 0, "wait": 0.0, "interval": 0.0}
PHOTOGATE_LATENCY = 30
DEFAULT_MOVE_SLICE_SIZE = 10
MAX_FAILED_TIMES = 10
CMD_SLICE_SIZE = 100
MAX_CONTINUOUS_FAILED_TIMES = 10
STABLE_THRESHOLD = 3
CONSECUTIVE_FRAMES_NEEDED = 160
FREEZE_SLEEP_TIME = 0.005
CONFIDENCE_THRESHOLD_FAILURE = 0.8
CONFIDENCE_THRESHOLD_PLAY = 0.9
MAX_CONTINUOUS_NOT_FC_COUNT = 1  # A song will be skipped after failing to achieve a Full Combo this many times.
MAX_ATTEMPT_COUNT = 1  # Maximum number of attempts per song in a single task session
PLAY_FAILED_TIMES = 0
HUMAN_DELAY_ENABLED = False
IS_FULL_SONG = False
IS_HIGH_DIFFICULTY = False
SUPPORTED_DIFFICULTIES = ['easy', 'normal', 'hard', 'expert', 'special']
NOT_FC_SONG_COUNT_DICT: dict[str, int] = {}  # Global variable to track songs that were not Full Combo'd.
LAST_PLAYED_SONG_ID: Optional[str] = None  # Variable to record the last played song ID
SONG_ATTEMPT_COUNT_DICT: dict[str, int] = {}  # Record total attempt count per song
IS_INITIALISED = False  # Global initialisation status flag

config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
maaresource = Resource()
maatasker = Tasker()
maacontroller: AdbController = None
device: AdbDevice = None
current_player: player.Player = None
current_orientation: int = 0
mnt: MNT = None
all_songs: dict = BestdoriAPI.get_song_list()
all_song_name_indexes: dict[str, str] = {
    list(filter(lambda title: title is not None, sinfo["musicTitle"]))[0]: sid
    for sid, sinfo in all_songs.items()
}
current_song_name: str = None
current_song_id: str = None
current_chart: Chart = None
play_failed_times: int = 0
callback_data: dict = {}
callback_data_lock = threading.Lock()
cmd_log_list: list = []
cmd_log_list_lock = threading.Lock()
current_version = None
stop_event = threading.Event()
playback_started_event = threading.Event()


def reset_callback_data():
    global callback_data
    callback_data = {
        "wait": {"total": 0, "total_offset": 0.0},
        "move": {"uncommited": 0, "total": 0, "total_offset": 0.0},
        "up": {"uncommited": 0, "total": 0, "total_offset": 0.0},
        "down": {"uncommited": 0, "total": 0, "total_offset": 0.0},
        "interval": {"total": 0, "total_offset": 0.0},
        "last_cmd_endtime": -1,
    }


reset_callback_data()


def check_song_available(name, id_, difficulty):
    if name.startswith("[FULL]") and not IS_FULL_SONG:
        return False

    if id_:
        attempt_count = SONG_ATTEMPT_COUNT_DICT.get(id_, 0)
        if attempt_count >= MAX_ATTEMPT_COUNT:
            logging.warning(f"Song '{name}' has reached max attempt count ({attempt_count}).")
            return False

        not_fc_count = NOT_FC_SONG_COUNT_DICT.get(id_, 0)
        if not_fc_count >= MAX_CONTINUOUS_NOT_FC_COUNT:
            logging.warning(f"Song '{name}' has reached the non-FC limit ({not_fc_count}).")
            NOT_FC_SONG_COUNT_DICT[id_] = 0
            return False

    """
    lastmatched = PlayRecord.get_or_none(chart_id=id_, difficulty=difficulty)
    if lastmatched:
        if not lastmatched.succeed:
            return True
    """

    return True


@maaresource.custom_recognition("SongRecognition")
class SongRecognition(CustomRecognition):
    def analyze(
        self, context: Context, argv: CustomRecognition.AnalyzeArg
    ) -> Union[CustomRecognition.AnalyzeResult, Optional[RectType]]:

        if LIVEMODE == "medley_single":
            roi = [110, 545, 370, 30]
        elif LIVEMODE == "free_single":
            roi = [220, 545, 570, 30]
        elif LIVEMODE == "free_auto":
            roi = [200, 330, 370, 30]

        models_to_try = ["ppocr_v5/zh_cn"]

        def match(model=None):
            pipeline = {
                "_ocr_song": {
                    "recognition": "OCR",
                    "only_rec": True,
                    "roi": roi,
                },
            }
            if model != None:
                pipeline["_ocr_song"]["model"] = model
            try:
                ocr_text = context.run_recognition("_ocr_song", argv.image, pipeline).best_result.text
                logging.info(f"OCR ({model or 'default'}) raw text: '{ocr_text}'")

                if LIVEMODE not in ["medley_single", "free_single"] and "full" in ocr_text.lower():
                    return 100

                match = fuzzy_match_song(ocr_text)
                if not match:
                    return None

                matched_name, raw_score = match[0], match[1]

                len_ocr = len(ocr_text)
                len_matched = len(matched_name)
                length_ratio = min(len_ocr, len_matched) / max(len_ocr, len_matched) if len_ocr and len_matched else 0
                adjusted_score = raw_score * length_ratio

                logging.info(
                    f"Adjusted score for '{matched_name}' with length penalty ({model or 'default'}): "
                    f"{adjusted_score:.2f} (raw: {raw_score}, len_ratio: {length_ratio:.2f})"
                )
                return (matched_name, adjusted_score)

            except Exception as e:
                logging.error(f"OCR ({model or 'default'}) execution failed: {e}")
                return None

        results = [m for m in [match(model) for model in models_to_try] if m]

        if not results or 100 in results:
            return self.AnalyzeResult(None, "")

        best_match = max(results, key=lambda x: x[1])

        if best_match and best_match[1] > 50:
            matched_song_name = best_match[0]
            adjusted_confidence = best_match[1]

            if LIVEMODE == "free_single":
                if IS_FULL_SONG:
                    matched_song_name = "[FULL] " + matched_song_name
                elif IS_HIGH_DIFFICULTY:
                    matched_song_name = "[超高難易度 SPECIAL] " + matched_song_name

            song_id = all_song_name_indexes.get(best_match[0])

            if not check_song_available(matched_song_name, song_id, DIFFICULTY):
                return self.AnalyzeResult(None, "")

            logging.info(f"Song recognised: '{matched_song_name}' (Adjusted Confidence: {adjusted_confidence:.2f}%)")
            return self.AnalyzeResult(roi, matched_song_name)

        return self.AnalyzeResult(None, "")



@maaresource.custom_recognition("LiveBoostEnoughRecognition")
class LiveBoostEnoughRecognition(CustomRecognition):
    def analyze(
        self, context: Context, argv: CustomRecognition.AnalyzeArg
    ) -> Union[CustomRecognition.AnalyzeResult, Optional[RectType]]:
        # roi = [970, 29, 39, 21]
        roi = [979, 30, 61, 20]

        pipeline = {
            "live_boost_enough_ocr": {
                "recognition": "OCR",
                "only_rec": True,
                "roi": roi,
            },
        }
        live_boost = context.run_recognition(
            "live_boost_enough_ocr",
            argv.image,
            pipeline,
        ).best_result.text

        logging.debug("Live boost rec result: {}".format(live_boost))
        pattern = r"^\s*(\d+)\s*/"
        match = re.match(pattern, live_boost.replace(" ", ""))

        if match:
            try:
                live_boost = int(match.group(1))
            except:
                live_boost = -1
        else:
            live_boost = -1

        logging.debug("Live boost: {}".format(live_boost))
        return CustomRecognition.AnalyzeResult(roi, str(live_boost))


@maaresource.custom_action("HandleLiveBoost")
class HandleLiveBoost(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg):
        liveboost = int(argv.reco_detail.best_result.detail)
        if liveboost < MIN_LIVEBOOST:
            logging.debug("Live boost not enough, ready to exit")
            context.run_action("close_app")
            context.run_action("stop")
        return CustomAction.RunResult(True)


@maaresource.custom_recognition("PlayResultRecognition")
class PlayResultRecognition(CustomRecognition):
    def analyze(
        self, context: Context, argv: CustomRecognition.AnalyzeArg
    ) -> Union[CustomRecognition.AnalyzeResult, Optional[RectType]]:

        types = {
            "score": {
                "roi": [1028, 192, 144, 35],
            },
            "maxcombo": {
                "roi": [1009, 391, 91, 28],
            },
            "perfect": {
                "roi": [829, 282, 90, 28],
            },
            "great": {
                "roi": [828, 322, 91, 27],
            },
            "good": {
                "roi": [829, 363, 91, 27],
            },
            "bad": {
                "roi": [829, 401, 90, 27],
            },
            "miss": {
                "roi": [830, 438, 91, 28],
            },
            "fast": {
                "roi": [1088, 283, 90, 27],
            },
            "slow": {
                "roi": [1088, 323, 91, 28],
            },
        }
        result = {type_: {} for type_ in types.keys()}
        pipeline = {
            f"_PlayResultRecognition_ocr_{type_}": {
                "recognition": "OCR",
                "only_rec": True,
                "roi": type_value["roi"],
            }
            for type_, type_value in types.items()
        }
        for type_, _ in types.items():
            try:
                ocrtext = context.run_recognition(
                    f"_PlayResultRecognition_ocr_{type_}",
                    argv.image,
                    pipeline,
                ).best_result.text
                type_result = int(ocrtext)
            except:
                type_result = -1
            result[type_] = type_result

        logging.debug("Play result: {}".format(result))
        return CustomRecognition.AnalyzeResult([0, 0, 0, 0], json.dumps(result))


@maaresource.custom_action("SavePlayResult")
class SavePlayResult(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg):
        try:
            global PLAY_FAILED_TIMES, NOT_FC_SONG_COUNT_DICT, LAST_PLAYED_SONG_ID, SONG_ATTEMPT_COUNT_DICT

            param = argv.custom_action_param
            try:
                succeed = json.loads(param).get("succeed", False)
            except (json.JSONDecodeError, TypeError):
                logging.error("Failed to parse custom_action_param.")
                succeed = False

            playresult = {}
            if succeed and argv.reco_detail and argv.reco_detail.best_result:
                try:
                    playresult = argv.reco_detail.best_result.detail

                    perfect = playresult.get('perfect', -1)
                    great = playresult.get('great', -1)
                    good = playresult.get('good', -1)
                    bad = playresult.get('bad', -1)
                    miss = playresult.get('miss', -1)
                    maxcombo = playresult.get('maxcombo', -1)

                    is_not_fc = False
                    reasons = []

                    is_ap_by_sum = (
                            perfect != -1 and
                            great != -1 and
                            maxcombo != -1 and
                            (perfect + great) == maxcombo
                    )

                    if is_ap_by_sum:
                        is_not_fc = False
                    else:
                        if -1 in [perfect, great, good, bad, miss, maxcombo]:
                            is_not_fc = True
                            reasons.append("OCR Failed")
                        else:
                            if bad > 0:
                                reasons.append(f"Bad: {bad}")
                            if miss > 0:
                                reasons.append(f"Miss: {miss}")
                            if good > 0:
                                reasons.append(f"Good: {good}")

                            sum_of_judgements = perfect + great
                            if maxcombo != sum_of_judgements:
                                reasons.append(f"P+G={sum_of_judgements}, MaxCombo={maxcombo}")

                            if reasons:
                                is_not_fc = True

                    if is_not_fc and current_song_id:
                        current_count = NOT_FC_SONG_COUNT_DICT.get(current_song_id, 0)
                        NOT_FC_SONG_COUNT_DICT[current_song_id] = current_count + 1
                        logging.warning(
                            f"Song '{current_song_name}' did not achieve a Full Combo. "
                            f"Total count: {NOT_FC_SONG_COUNT_DICT[current_song_id]} "
                            f"Reasons: {', '.join(reasons)}"
                        )

                except json.JSONDecodeError:
                    logging.error("Failed to parse play result JSON.")
                    playresult = {}

            # Increment failure count only on explicit failures (e.g., live failed, pipeline error).
            if succeed:
                # If task is successful and there were previous failures, log and reset counter
                if PLAY_FAILED_TIMES > 0:
                    logging.info(
                        f"Task successful, resetting continuous failure count from {PLAY_FAILED_TIMES} to zero.")
                PLAY_FAILED_TIMES = 0
            else:  # 'not succeed' case
                # If task failed, increment counter unconditionally
                PLAY_FAILED_TIMES += 1
                logging.info(f"Recording one task failure, current continuous failure count: {PLAY_FAILED_TIMES}")

            PlayRecord.create(
                play_time=int(time.time()),
                play_offset=OFFSET,
                result=playresult,
                succeed=succeed,
                chart_id=current_song_id,
                difficulty=DIFFICULTY,
            )

            if current_song_id:
                current_attempts = SONG_ATTEMPT_COUNT_DICT.get(current_song_id, 0)
                SONG_ATTEMPT_COUNT_DICT[current_song_id] = current_attempts + 1
                logging.info(
                    f"Song '{current_song_name}' has been attempted {SONG_ATTEMPT_COUNT_DICT[current_song_id]} times.")

            LAST_PLAYED_SONG_ID = current_song_id

            if PLAY_FAILED_TIMES >= MAX_CONTINUOUS_FAILED_TIMES:
                logging.error(
                    f"Continuous failure limit reached ({MAX_CONTINUOUS_FAILED_TIMES}). Stopping automatically.")
                context.run_action("close_app")
                context.run_action("stop")
            return CustomAction.RunResult(True)
        except Exception as e:
            logging.error(f"Failed to save play result: {e}")
            return CustomAction.RunResult(False)


@maaresource.custom_action("Play")
class Play(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg):
        global stop_event, playback_started_event
        stop_event.clear()
        playback_started_event.clear()
        monitor = threading.Thread(
            target=monitor_failure_thread,
            args=(stop_event, playback_started_event),
            daemon=True
        )
        try:
            monitor.start()
            play_song(stop_event, playback_started_event)
            stop_event.set()
            return CustomAction.RunResult(True)
        except Exception as e:
            stop_event.set()
            logging.error(f"Error during song playback: {e}", exc_info=True)
            return CustomAction.RunResult(False)
        finally:
            monitor.join(timeout=5)


@maaresource.custom_action("SaveSong")
class SaveSong(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg):
        name: CustomRecognitionResult = argv.reco_detail.best_result.detail
        save_song(name)
        return CustomAction.RunResult(True)


@maaresource.custom_recognition("RecognizeLevelUp")
class RecognizeLevelUp(CustomRecognition):
    def analyze(self, context: Context, argv: CustomRecognition.AnalyzeArg):
        roi = [590, 640, 100, 30]
        target_text = "LevelUP!"
        confidence_threshold = 85

        try:
            pipeline = {"_ocr_levelup": {"recognition": "OCR", "roi": roi, "only_rec": True}}
            ocr_text = context.run_recognition("_ocr_levelup", argv.image, pipeline).best_result.text

            match = fzwzprocess.extractOne(ocr_text, [target_text])

            if match and match[1] >= confidence_threshold:
                return self.AnalyzeResult(roi, target_text)
            else:
                score = match[1] if match else 0
                return self.AnalyzeResult(None, "")

        except Exception as e:
            return self.AnalyzeResult(None, "")


def fuzzy_match_song(name):
    return fzwzprocess.extractOne(name, list(all_song_name_indexes.keys()))


def _get_orientation():
    """
    0, 1, 2, 3
    0: 0°
    1: 90°
    2: 180°
    3: 270°
    """
    try:
        command_list = [
            str(device.adb_path.absolute()),
            "-s",
            device.address,
            "shell",
            "dumpsys input|grep SurfaceOrientation",
        ]

        logging.debug(
            "get SurfaceOrientation command: {}".format(" ".join(command_list))
        )
        output = subprocess.check_output(command_list, text=True)
        match = re.search(r"SurfaceOrientation:\s*(\d+)", output)
        orientation = int(match.group(1))
        logging.debug("SurfaceOrientation: {}".format(orientation))
        return orientation
    except Exception as e:
        logging.error(f"Failed to get SurfaceOrientation: {e}")
        return 0


def save_song(name):
    global current_song_name, current_song_id, current_chart, current_orientation
    current_song_name = name
    current_song_id = all_song_name_indexes[current_song_name]
    current_chart = Chart((current_song_id, DIFFICULTY), current_song_name)
    current_chart.notes_to_actions(current_player.resolution, DEFAULT_MOVE_SLICE_SIZE, humanize=HUMAN_DELAY_ENABLED)
    current_orientation = _get_orientation()
    current_chart.actions_to_MNTcmd(
        (mnt.max_x, mnt.max_y), current_orientation, OFFSET, CMD_SLICE_SIZE
    )
    logging.info(f"Saved song: {name}")


def play_song(stop_event, playback_started_event):
    logging.info("Start play")
    cmd_log_list.clear()
    reset_callback_data()

    def _get_wait_time():
        wait_for = 0.0
        index = current_chart.actions_to_cmd_index
        for action in current_chart.actions[index - CMD_SLICE_SIZE : index]:
            if action["type"] == "wait":
                wait_for += action["length"]
        return wait_for

    def _adjust_offset():
        global callback_data
        total_cost = 0.0
        for type_ in ["up", "down", "move", "wait", "interval"]:
            type_data = callback_data[type_]
            total = type_data["total"]
            if total != 0:
                total_cost += type_data["total_offset"] - OFFSET[type_] * total
                OFFSET[type_] = type_data["total_offset"] / total

        current_chart._a2c_offset += total_cost
        logging.debug("Adjust offset: {}".format(OFFSET))
        logging.debug("Adjust _actions_to_cmd_offset: {}".format(total_cost))

    logging.info("Waiting for game to load, detecting pause button.")
    template_path = data_path("assets/resource/image/live/button/pause.png")
    if not template_path.exists():
        logging.error(f"Pause button template image not found: {template_path}")
        return
    template = get_scaled_template(template_path)
    if template is None:
        logging.error(f"Failed to load template image: {template_path}")
        return

    pause_button_found = False
    wait_start_time = time.time()
    playback_started_event.set()

    while not pause_button_found:
        wait_timeout = 30
        wait_current_time = time.time()
        if wait_current_time - wait_start_time > wait_timeout:
            logging.error(f"Waiting for pause button timeout ({wait_current_time - wait_start_time}s), aborting.")
            return
        if check_exit_status(stop_event):
            return

        screen = current_player.ipc_capture_display()
        height, width, _ = screen.shape
        roi_screen = screen[0:int(height * 0.15), width - int(height * 0.15):width]
        gray_roi = cv2.cvtColor(roi_screen, cv2.COLOR_BGR2GRAY)
        result = cv2.matchTemplate(gray_roi, template, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, _ = cv2.minMaxLoc(result)

        logging.debug(f"Waiting for pause button, match confidence: {max_val:.2f}")
        if max_val >= CONFIDENCE_THRESHOLD_PLAY:
            logging.info("Pause button detected, waiting for screen frozen.")
            pause_button_found = True
        else:
            time.sleep(0.5)

    if not wait_first_note(stop_event):
        return

    while True:
        if check_exit_status(stop_event):
            return

        current_chart.command_builder.publish(mnt, block=False)
        wait_time = _get_wait_time()
        time.sleep(max(0, wait_time - 3) / 1000)

        index = current_chart.actions_to_cmd_index
        if current_chart.actions[index : index + CMD_SLICE_SIZE]:
            with callback_data_lock:
                _adjust_offset()
                reset_callback_data()
            current_chart.actions_to_MNTcmd(
                (mnt.max_x, mnt.max_y), current_orientation, OFFSET, CMD_SLICE_SIZE
            )
        else:
            break

    time.sleep(2)
    logging.info("Playback finished.")


def wait_first_note(stop_event):
    last_color = None
    waited_frames = 0
    info = get_runtime_info(current_player.resolution)["wait_first"]
    from_row, to_row = info["from"], info["to"]
    freezed = False
    playback_start_time = time.time()

    while True:
        playback_timeout = 500
        playback_current_time = time.time()
        if playback_current_time - playback_start_time > playback_timeout:
            logging.error(f"Playback timeout ({playback_current_time - playback_start_time}s), aborting.")
            return False
        if check_exit_status(stop_event):
            return False

        try:
            screen = current_player.ipc_capture_display()
            cur_color, _ = get_color_eval_in_range(screen, from_row, to_row)

            if last_color is not None:
                change_score = np.sum(np.abs(cur_color[:3].astype(int) - last_color[:3].astype(int)))

                if change_score > STABLE_THRESHOLD:
                    if freezed:
                        logging.info("First note detected, starting playback.")
                        time.sleep(PHOTOGATE_LATENCY / 1000)
                        return True
                else:
                    if not freezed:
                        waited_frames += 1

                if not freezed and waited_frames >= CONSECUTIVE_FRAMES_NEEDED:
                    freezed = True
                    logging.info("Screen has frozen. Photogate is ready.")

            last_color = cur_color
            time.sleep(FREEZE_SLEEP_TIME)

        except Exception as e:
            logging.error(f"Error during photogate detection: {e}")
            return False


def check_exit_status(stop_event):
    if stop_event.is_set():
        logging.warning("Playback failed, exiting.")
        return True
    return False


def init_maa():
    res_job = maaresource.post_bundle(data_path("assets/resource"))
    res_job.wait()
    Toolkit.init_option(data_path(""))
    for i in range(3):
        adb_devices = Toolkit.find_adb_devices()
        if adb_devices:
            break
    if not adb_devices:
        raise RuntimeError("No ADB devices found.")

    global device, maacontroller
    _device: list[AdbDevice] = []
    for device in adb_devices:
        extra_names = device.config.get("extras", {}).keys()
        if "mumu" in extra_names:
            if (device.name, device.address) not in [
                (x.name, x.address) for x in _device
            ]:
                _device.append(device)

    """
    filter_str = config.get("device", {}).get("filter", "devices")
    _device = eval(filter_str, {}, {"devices": _device})
    """

    if not _device:
        raise RuntimeError("No supported emulators found.")
    else:
        device = _device[0]

    """
    elif len(_device) == 1:
        device = _device[0]
    elif len(_device) > 1:
        print("Multiple devices were found:")
        for i, d in enumerate(_device):
            print(f"{i}: {d.name}({d.address})")
        selected = input("Select a device: ")
        device = _device[int(selected)]
    """

    logging.info(f"Using device: {device.name} at {device.address}")

    maacontroller = AdbController(
        adb_path=device.adb_path,
        address=device.address,
        config=device.config,
    )
    """
        screencap_methods=device.screencap_methods,
        input_methods=device.input_methods,
    """

    is_connected = False
    for i in range(3):
        if maacontroller.post_connection().wait().succeeded:
            is_connected = True
            break

    if not is_connected:
        raise RuntimeError(f"Failed to connect controller to device {device.name}.")

    # tasker = Tasker(notification_handler=MyNotificationHandler())
    maatasker.bind(maaresource, maacontroller)

    if not maatasker.inited:
        raise RuntimeError("Failed to initialise MAA tasker module.")

    logging.info("MAA initialised successfully.")


def mnt_callback(event: MNTEvent, data: MNTEventData):
    global callback_data
    if event == MNTEvent.EVATIVE7_LOG:
        data: MNTEvATive7LogEventData = data

        cmd = data.cmd
        cost = data.cost

        with cmd_log_list_lock:
            cmd_log_list.append(data)
        cmd_type = cmd.split(" ")[0]

        with callback_data_lock:
            if (last_cmd_endtime := callback_data.get("last_cmd_endtime")) != -1:
                callback_data["interval"]["total"] += 1
                callback_data["interval"]["total_offset"] += (
                        data.start_time - last_cmd_endtime
                )
            callback_data["last_cmd_endtime"] = data.end_time
            if cmd_type in ["w"]:
                callback_data["wait"]["total"] += 1
                callback_data["wait"]["total_offset"] += cost - int(cmd.split(" ")[-1])
            elif cmd_type in ["u", "d", "m"]:
                type_ = {
                    "u": "up",
                    "d": "down",
                    "m": "move",
                }[cmd_type]
                callback_data[type_]["uncommited"] += 1
                callback_data[type_]["total"] += 1
                callback_data[type_]["total_offset"] += cost
            elif cmd_type in ["c"]:
                total_uncommited = 0
                for type_ in ["up", "down", "move"]:
                    total_uncommited += callback_data[type_]["uncommited"]

                if total_uncommited != 0:
                    for type_ in ["up", "down", "move"]:
                        callback_data[type_]["total_offset"] += cost * (
                                callback_data[type_]["uncommited"] / total_uncommited
                        )
                        callback_data[type_]["uncommited"] = 0


def init_player_and_mnt():
    global current_player, mnt

    if not device: raise RuntimeError("MAA device not initialised before initialising player.")

    extra_config = device.config["extras"]
    if "mumu" in extra_config.keys():
        extra_config = extra_config["mumu"]
        type_ = "mumu"
        """
        if device.name == "MuMuPlayer12":
            type_ += "v4"
        """
        if device.name == "MuMuPlayer12 v5":
            type_ += "v5"
    else:
        raise RuntimeError(f"Unsupported emulator type: {list(extra_config.keys())}")

    path = extra_config["path"]
    index = extra_config["index"]

    current_player = player.Player(type_, Path(path), index)
    mnt = MNT(
        device.address,
        type_="EvATive7",
        communicate_type=MNTServerCommunicateType.STDIO,
        mnt_asset_path=data_path("assets/minitouch_EvATive7"),
        callback=mnt_callback,
        adb_executor=str(device.adb_path.absolute()),
    )

    logging.info(f"{type_} player and Minitouch initialised successfully.")


def get_scaled_template(template_path):
    template = cv2.imread(template_path, 0)
    runtime_h, runtime_w, _ = current_player.ipc_capture_display().shape
    scale_factor = runtime_w / 1920
    if np.isclose(scale_factor, 1.0):
        return template
    original_h, original_w = template.shape[:2]
    new_w = int(original_w * scale_factor)
    new_h = int(original_h * scale_factor)
    if new_w < 1 or new_h < 1:
        return template
    resized_template = cv2.resize(template, (new_w, new_h), interpolation=cv2.INTER_AREA)
    return resized_template


def monitor_failure_thread(stop_event, playback_started_event):
    """
    A background monitoring thread.
    It waits for the playback start signal, then continuously monitors for the "Live Failed" screen through image matching.
    """
    try:
        logging.info("Monitor thread started, waiting for playback start signal.")

        # Wait for "playback started" signal from play_song function, timeout after 60s
        playback_started_event.wait(timeout=30)

        if not playback_started_event.is_set():
            logging.warning("Timeout waiting for playback start signal, monitor thread exiting.")
            stop_event.set()
            return

        logging.info("Received playback start signal, starting screen monitoring.")

        # Load template image once for efficiency
        # Note: Please ensure this path matches your project resource path
        fail_template_path = data_path("assets/resource/image/live/live_failed.png")
        if not fail_template_path.exists():
            logging.error(
                f"Live Failed template image not found: {fail_template_path}, monitor thread cannot work.")
            stop_event.set()
            return

        template = get_scaled_template(fail_template_path)

        # Monitor loop until stop signal received
        while not stop_event.is_set():
            screen_bgr = current_player.ipc_capture_display()
            if screen_bgr is None:
                time.sleep(1)
                continue

            screen_gray = cv2.cvtColor(screen_bgr, cv2.COLOR_BGR2GRAY)

            # Perform template matching
            result = cv2.matchTemplate(screen_gray, template, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, _ = cv2.minMaxLoc(result)

            if max_val >= CONFIDENCE_THRESHOLD_FAILURE:
                logging.error(f"Detected 'Live Failed' screen (match: {max_val:.2f})! Sending stop signal!")
                stop_event.set()  # Key: Set stop event to notify other threads
                break  # Task complete, exit loop

            # Monitor every 1s to avoid high CPU usage
            time.sleep(1)

    except Exception as e:
        logging.error(f"Monitor thread encountered unexpected error: {e}", exc_info=True)
        stop_event.set()
    finally:
        logging.info("Monitor thread terminated.")


def init():
    global IS_INITIALISED
    if IS_INITIALISED:
        logging.info("Components are already initialised. Skipping.")
        return
    try:
        init_maa()
        init_player_and_mnt()
        IS_INITIALISED = True
    except Exception as e:
        IS_INITIALISED = False
        logging.error("Initialisation failed.", exc_info=True)
        raise e


def shutdown_resources():
    global mnt, maacontroller, maatasker

    if maatasker and maatasker.running:
        logging.info("Final shutdown: Stopping MAA tasker.")
        maatasker.post_stop()

    if mnt:
        logging.info("Disconnecting Minitouch...")
        try:
            mnt.disconnect()
            mnt = None
        except Exception as e:
            logging.error(f"Error disconnecting Minitouch: {e}", exc_info=True)

    if maacontroller:
        logging.info("Disconnecting MAA AdbController...")
        try:
            if maacontroller.post_disconnect().wait().succeeded:
                logging.info("AdbController disconnected successfully.")
            else:
                logging.warning("Failed to disconnect AdbController cleanly.")
            maacontroller = None
        except Exception as e:
            logging.error(f"Error disconnecting AdbController: {e}", exc_info=True)


def run_task_mode():
    global DIFFICULTY, HUMAN_DELAY_ENABLED, LIVEMODE
    global MAX_CONTINUOUS_NOT_FC_COUNT, MAX_ATTEMPT_COUNT, PLAY_FAILED_TIMES, NOT_FC_SONG_COUNT_DICT

    if not maacontroller or not mnt:
        raise RuntimeError("MAA is not initialised.")

    PLAY_FAILED_TIMES = 0
    NOT_FC_SONG_COUNT_DICT.clear()

    mode_configs = {
        "free_single": ("Single", "ui_simplified_entry", ["single.json"]),
        "medley_single": ("Medley", "ui_simplified_entry", ["single.json"]),
        "story": ("story", "read_story", ["story.json"]),
        "rouge": ("rouge", "start_from_main_menu", ["rouge.json"]),
        "free_auto": ("Auto", "select_song", ["full_auto.json"])
    }

    if LIVEMODE not in mode_configs:
        logging.error(f"Unknown mode: {LIVEMODE}")
        return

    display_name, entry_task, json_files = mode_configs[LIVEMODE]

    pipeline_def_path = data_path("assets/resource/pipeline")
    override_pipeline = {}

    for json_filename in json_files:
        file_path = pipeline_def_path / json_filename
        if file_path.exists():
            with open(file_path, 'r', encoding='utf-8') as f:
                try:
                    data = json.load(f)
                    override_pipeline.update(data)
                except json.JSONDecodeError as e:
                    logging.error(f"Parsing {json_filename} failed, please check JSON format: {e}")
                    return
        else:
            logging.error(f"Cannot find required Pipeline configuration file: {file_path}")
            return

    logging.info(f"Submitting {display_name} Mode auto-play task. Entry: {entry_task}")
    maatasker.post_task(entry_task, override_pipeline).wait()
    logging.info(f"{display_name} Mode task finished or stopped.")