import json
import logging
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

import cv2
import numpy as np
import yaml
from fuzzywuzzy import process as fzwzprocess
from maa.context import Context
from maa.controller import AdbController
from maa.custom_action import CustomAction
from maa.custom_recognition import CustomRecognition
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
from util import get_color_eval_in_range, get_runtime_info


def resource_path(relative_path):
    """
    Get the absolute path to a resource, compatible with both development environments
    (including the 'src' directory layout) and PyInstaller-packaged environments.
    """
    if getattr(sys, 'frozen', False):
        # When running in a packaged bundle, the base path is the temporary folder created by PyInstaller.
        base_path = Path(sys._MEIPASS)
    else:
        # In a development environment, trace up one level from the current file's location (__file__)
        # to the project root.
        base_path = Path(__file__).parent.parent

    return base_path / relative_path


# --- Global Variables & Constants ---
PHOTOGATE_LATENCY = 30
MIN_LIVEBOOST = 1
DEFAULT_MOVE_SLICE_SIZE = 10
CMD_SLICE_SIZE = 100
MAX_CONTINUOUS_FAILED_TIMES = 10
STABLE_THRESHOLD = 3
CONSECUTIVE_FRAMES_NEEDED = 150
FREEZE_SLEEP_TIME = 0.005
CONFIDENCE_THRESHOLD_FAILURE = 0.9
CONFIDENCE_THRESHOLD_PLAY = 0.9
MAX_CONTINUOUS_NOT_FC_COUNT = 2 # A song will be skipped after failing to achieve a Full Combo this many times.
MAX_ATTEMPT_COUNT = 3 # 每首歌在一次任务中最多尝试3次
PLAY_FAILED_TIMES = 0
DIFFICULTY = "hard"
HUMAN_DELAY_ENABLED = False
IS_FULL_SONG = False
IS_HIGH_DIFFICULTY = False
SUPPORTED_DIFFICULTIES = ['easy', 'normal', 'hard', 'expert', 'special']
OFFSET = {"up": 0, "down": 0, "move": 0, "wait": 0.0, "interval": 0.0}
NOT_FC_SONG_COUNT_DICT: dict[str, int] = {} # Global variable to track songs that were not Full Combo'd.
LAST_PLAYED_SONG_ID: Optional[str] = None # <-- 新增：记录上一首歌曲ID的变量
SONG_ATTEMPT_COUNT_DICT: dict[str, int] = {} # <-- 新增：记录总尝试次数
IS_INITIALISED = False # <-- 新增：全局初始化状态标志

# Playback monitor thread
stop_event = threading.Event()
playback_started_event = threading.Event()

# --- MAA & System Components ---
config_path = resource_path("data/config.yml")
if not config_path.exists():
    config_path.parent.mkdir(exist_ok=True)
    config_path.touch()
    config_path.write_text("{}", encoding="utf-8")

config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
maaresource = Resource()
maatasker = Tasker()
maacontroller: Optional[AdbController] = None
device: Optional[AdbDevice] = None
current_player: Optional[player.Player] = None
mnt: Optional[MNT] = None

# --- Song & Chart Data ---
all_songs: dict = BestdoriAPI.get_song_list()
all_song_name_indexes: dict[str, str] = {
    list(filter(lambda title: title is not None, sinfo["musicTitle"]))[0]: sid
    for sid, sinfo in all_songs.items()
}
current_song_name: Optional[str] = None
current_song_id: Optional[str] = None
current_chart: Optional[Chart] = None
current_orientation: int = 0

# --- Latency Compensation & Real-time Data ---
callback_data: dict = {}
callback_data_lock = threading.Lock()
cmd_log_list: list = []
cmd_log_list_lock = threading.Lock()
"""
# --- Real-time Streaming Components ---
streaming_thread: Optional[threading.Thread] = None
streaming_active = threading.Event()
STREAM_SETTINGS = {"fps": 1, "resolution": 480}
stream_settings_lock = threading.Lock()
"""

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


def fuzzy_match_song(name):
    return fzwzprocess.extractOne(name, list(all_song_name_indexes.keys()))


def _get_orientation():
    try:
        creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        output = subprocess.check_output(
            [
                str(device.adb_path.absolute()),
                "-s",
                device.address,
                "shell",
                "dumpsys input|grep SurfaceOrientation",
            ],
            text=True,
            creationflags=creationflags,
        )
        match = re.search(r"SurfaceOrientation:\s*(\d+)", output)
        return int(match.group(1))
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
        fail_template_path = resource_path("assets/resource/image/live/live_failed.png")
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

def play_song(stop_event, playback_started_event):
    """
    Core playback function with performance optimisations.
    """
    cmd_log_list.clear()
    reset_callback_data()
    def check_exit_status():
        if stop_event.is_set():
            logging.warning("Playback failed, exiting.")
            return True
        else:
            return False

    # STAGE 1: Wait for the game to load by detecting the pause button
    logging.info("Waiting for game to load, detecting pause button.")
    template_path = resource_path("assets/resource/image/live/button/pause.png")
    if not template_path.exists():
        logging.error(f"Pause button template image not found: {template_path}")
        return
    template = get_scaled_template(template_path)
    if template is None:
        logging.error(f"Failed to load template image: {template_path}")
        return
    pause_button_found = False
    wait_start_time = time.time()
    while not pause_button_found:
        wait_timeout=30
        wait_current_time=time.time()
        if wait_current_time - wait_start_time > wait_timeout:
            logging.error(f"Waiting for pause button timeout ({wait_current_time - wait_start_time}s), aborting.")
            return
        if check_exit_status():
            return
        screen = current_player.ipc_capture_display()
        height, width, _ = screen.shape
        roi_screen = screen[0:int(height * 0.15), width-int(height * 0.15):width]
        gray_roi = cv2.cvtColor(roi_screen, cv2.COLOR_BGR2GRAY)
        result = cv2.matchTemplate(gray_roi, template, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, _ = cv2.minMaxLoc(result)
        logging.debug(f"Waiting for pause button, match confidence: {max_val:.2f}")
        if max_val >= CONFIDENCE_THRESHOLD_PLAY:
            pause_button_found = True
        else:
            time.sleep(0.5)

    # STAGE 2 & 3: Wait for screen to freeze & photogate detection
    logging.info("Waiting for screen to freeze.")

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

    def _get_wait_time():
        wait_for = 0.0
        index = current_chart.actions_to_cmd_index
        for action in current_chart.actions[index - CMD_SLICE_SIZE: index]:
            if action["type"] == "wait":
                wait_for += action["length"]
        return wait_for

    last_color, waited_frames, freezed = None, 0, False
    info = get_runtime_info(current_player.resolution)["wait_first"]
    from_row, to_row = info["from"], info["to"]
    playback_start_time=time.time()
    while True:
        playback_timeout=500
        playback_current_time=time.time()
        if playback_current_time - playback_start_time > playback_timeout:
            logging.error(f"Playback timeout ({playback_current_time - playback_start_time}s), aborting.")
            return
        if check_exit_status():
            return
        try:
            screen = current_player.ipc_capture_display()
            cur_color, _ = get_color_eval_in_range(screen, from_row, to_row)
            if last_color is not None:
                change_score = np.sum(np.abs(cur_color[:3].astype(int) - last_color[:3].astype(int)))
                if change_score > STABLE_THRESHOLD and freezed:
                    logging.info("First note detected, starting playback.")
                    time.sleep(PHOTOGATE_LATENCY / 1000)
                    break
                elif not freezed:
                    logging.debug(f"Colour change delta: {change_score}, waited_frames: {waited_frames}")
                    if change_score < STABLE_THRESHOLD:
                        waited_frames += 1
                    else:
                        waited_frames = 0
                if not freezed and waited_frames >= CONSECUTIVE_FRAMES_NEEDED:
                    freezed = True
                    logging.info("Screen has frozen. Photogate is ready.")
            last_color = cur_color
            time.sleep(FREEZE_SLEEP_TIME)
        except Exception as e:
            logging.error(f"Error during photogate detection: {e}")
            return

    # STAGE 4: Command execution loop
    playback_started_event.set()
    logging.info("Starting command execution.")
    while True:
        if check_exit_status():
            return
        current_chart.command_builder.publish(mnt, block=False)
        wait_time = _get_wait_time()
        time.sleep(max(0, wait_time - 3) / 1000)
        index = current_chart.actions_to_cmd_index
        if current_chart.actions[index: index + CMD_SLICE_SIZE]:
            with callback_data_lock:
                _adjust_offset()
                reset_callback_data()
            current_chart.actions_to_MNTcmd((mnt.max_x, mnt.max_y), current_orientation, OFFSET, CMD_SLICE_SIZE)
        else:
            break
    logging.info("Playback finished.")

def mnt_callback(event: MNTEvent, data: MNTEventData):
    global callback_data
    if event == MNTEvent.EVATIVE7_LOG:
        data: MNTEvATive7LogEventData = data
        cmd, cost = data.cmd, data.cost
        with cmd_log_list_lock:
            cmd_log_list.append(data)
        cmd_type = cmd.split(" ")[0]
        with callback_data_lock:
            if (last_cmd_endtime := callback_data.get("last_cmd_endtime")) != -1:
                callback_data["interval"]["total"] += 1
                callback_data["interval"]["total_offset"] += (data.start_time - last_cmd_endtime)
            callback_data["last_cmd_endtime"] = data.end_time
            if cmd_type == "w":
                callback_data["wait"]["total"] += 1
                callback_data["wait"]["total_offset"] += cost - int(cmd.split(" ")[-1])
            elif cmd_type in "udm":
                type_ = {"u": "up", "d": "down", "m": "move"}[cmd_type]
                callback_data[type_]["uncommited"] += 1
                callback_data[type_]["total"] += 1
                callback_data[type_]["total_offset"] += cost
            elif cmd_type == "c":
                total_uncommited = sum(callback_data[t]["uncommited"] for t in ["up", "down", "move"])
                if total_uncommited != 0:
                    for t in ["up", "down", "move"]:
                        callback_data[t]["total_offset"] += cost * (callback_data[t]["uncommited"] / total_uncommited)
                        callback_data[t]["uncommited"] = 0


def init_maa():
    global device, maacontroller
    maaresource.post_bundle("assets/resource").wait()
    Toolkit.init_option("./")
    adb_devices = Toolkit.find_adb_devices()
    if not adb_devices: raise RuntimeError("No ADB devices found.")
    supported_devices = [d for d in adb_devices if
                         "mumu" in d.config.get("extras", {}) or "ld" in d.config.get("extras", {})]
    if not supported_devices: raise RuntimeError("No supported emulators found (MuMu, LDPlayer).")
    device = supported_devices[0]
    logging.info(f"Using device: {device.name} at {device.address}")
    maacontroller = AdbController(adb_path=device.adb_path, address=device.address, config=device.config)
    if not maacontroller.post_connection().wait().succeeded:
        raise RuntimeError(f"Failed to connect controller to device {device.name}.")
    maatasker.bind(maaresource, maacontroller)
    if not maatasker.inited: raise RuntimeError("Failed to initialise MAA tasker module.")
    logging.info("MAA initialised successfully.")


def init_player_and_mnt():
    global current_player, mnt
    if not device: raise RuntimeError("MAA device not initialised before initialising player.")
    extra_config = device.config["extras"]
    if "mumu" in extra_config:
        type_, config_key = "mumu", "mumu"
        if "v4" in device.name or "v5" in device.name: type_ += device.name[-2:]
    elif "ld" in extra_config:
        type_, config_key = "ld", "ld"
    else:
        raise RuntimeError(f"Unsupported emulator type: {list(extra_config.keys())}")
    player_config = extra_config[config_key]
    current_player = player.Player(type_, Path(player_config["path"]), player_config["index"])
    mnt = MNT(
        device.address, type_="EvATive7", communicate_type=MNTServerCommunicateType.STDIO,
        mnt_asset_path=resource_path("assets/minitouch_EvATive7"), callback=mnt_callback,
        adb_executor=str(device.adb_path.absolute()),
    )
    logging.info(f"{type_} player and Minitouch initialised successfully.")


# --- MAA Custom Modules ---

@maaresource.custom_recognition("UICheckFCStatusRecognition")
class UICheckFCStatusRecognition(CustomRecognition):
    """
    Custom recognition module for decision nodes.
    This recognition succeeds if the current song's "non-FC" count has reached the limit.
    """

    def analyze(self, context: Context, argv: CustomRecognition.AnalyzeArg):
        global NOT_FC_SONG_COUNT_DICT, current_song_id, current_song_name, MAX_CONTINUOUS_NOT_FC_COUNT

        if not current_song_id:
            return self.AnalyzeResult(None, "")

        """# --- 新增的熔断检查 ---
        # 如果当前选择的歌曲和上一首是同一首，则强制跳过
        if current_song_id == LAST_PLAYED_SONG_ID:
            logging.warning(
                f"Song '{current_song_name}' is the same as the last one, force random song."
            )
            return self.AnalyzeResult([0, 0, 0, 0], "repeated")"""

        # 2. 新增的总尝试次数检查
        attempt_count = SONG_ATTEMPT_COUNT_DICT.get(current_song_id, 0)
        if attempt_count >= MAX_ATTEMPT_COUNT:
            logging.warning(
                f"Song '{current_song_name}' has reached max attempt count ({attempt_count})."
            )
            return self.AnalyzeResult([0, 0, 0, 0], str(attempt_count))

        # --- 原有的次数超限检查 ---
        count = NOT_FC_SONG_COUNT_DICT.get(current_song_id, 0)

        if count >= MAX_CONTINUOUS_NOT_FC_COUNT:
            logging.warning(
                f"Song '{current_song_name}' has reached the non-FC limit ({count})."
            )
            NOT_FC_SONG_COUNT_DICT[current_song_id] = 0
            return self.AnalyzeResult([0, 0, 0, 0], str(count))

        return self.AnalyzeResult(None, "")


@maaresource.custom_recognition("UISongRecognitionMedley")
class UISongRecognitionMedley(CustomRecognition):
    def analyze(self, context: Context, argv: CustomRecognition.AnalyzeArg):
        # This ROI might need adjustment based on the actual screen layout.
        roi = [110, 545, 370, 30]

        def ocr_and_match(model=None):
            try:
                pipeline = {"_ocr_song": {"recognition": "OCR", "roi": roi, "only_rec": True}}
                if model: pipeline["_ocr_song"]["model"] = model
                ocr_text = context.run_recognition("_ocr_song", argv.image, pipeline).best_result.text
                logging.info(f"OCR ({model or 'default'}) raw text: '{ocr_text}'")
                match = fuzzy_match_song(ocr_text)
                logging.info(f"Fuzzy match result ({model or 'default'}): {match}")
                return match
            except Exception as e:
                logging.error(f"OCR ({model or 'default'}) execution failed: {e}")
                return None

        results = [m for m in [ocr_and_match("ppocr_v3/ja_jp"), ocr_and_match()] if m]
        if not results: return self.AnalyzeResult(None, "")
        best_match = max(results, key=lambda x: x[1])
        if best_match and best_match[1] > 50:
            song_name = "[FULL] " + best_match[0] if IS_FULL_SONG else best_match[0]
            logging.info(f"Song recognised: '{song_name}' (Confidence: {best_match[1]}%)")
            return self.AnalyzeResult(roi, song_name)
        return self.AnalyzeResult(None, "")

@maaresource.custom_recognition("UISongRecognitionFreeSingle")
class UISongRecognitionFreeSingle(CustomRecognition):
    def analyze(self, context: Context, argv: CustomRecognition.AnalyzeArg):
        # This ROI might need adjustment based on the actual screen layout.
        roi = [220, 545, 570, 30]

        def ocr_and_match(model=None):
            try:
                pipeline = {"_ocr_song": {"recognition": "OCR", "roi": roi, "only_rec": True}}
                if model: pipeline["_ocr_song"]["model"] = model
                ocr_text = context.run_recognition("_ocr_song", argv.image, pipeline).best_result.text
                logging.info(f"OCR ({model or 'default'}) raw text: '{ocr_text}'")
                match = fuzzy_match_song(ocr_text)
                logging.info(f"Fuzzy match result ({model or 'default'}): {match}")
                return match
            except Exception as e:
                logging.error(f"OCR ({model or 'default'}) execution failed: {e}")
                return None

        results = [m for m in [ocr_and_match("ppocr_v3/ja_jp"), ocr_and_match()] if m]
        if not results: return self.AnalyzeResult(None, "")
        best_match = max(results, key=lambda x: x[1])
        if best_match and best_match[1] > 50:
            if IS_FULL_SONG:
                song_name = "[FULL] " + best_match[0]
            elif IS_HIGH_DIFFICULTY:
                song_name = "[超高難易度 SPECIAL] " + best_match[0]
            else:
                song_name = best_match[0]
            logging.info(f"Song recognised: '{song_name}' (Confidence: {best_match[1]}%)")
            return self.AnalyzeResult(roi, song_name)
        return self.AnalyzeResult(None, "")


@maaresource.custom_recognition("UISongRecognitionFreeAuto")
class UISongRecognitionFreeAuto(CustomRecognition):
    def analyze(self, context: Context, argv: CustomRecognition.AnalyzeArg):
        # This ROI might need adjustment based on the actual screen layout.
        roi = [200, 330, 370, 30]

        def ocr_and_match(model=None):
            try:
                pipeline = {"_ocr_song": {"recognition": "OCR", "roi": roi, "only_rec": True}}
                if model: pipeline["_ocr_song"]["model"] = model
                ocr_text = context.run_recognition("_ocr_song", argv.image, pipeline).best_result.text
                logging.info(f"OCR ({model or 'default'}) raw text: '{ocr_text}'")

                if "FULL" in ocr_text:
                    return None

                match = fuzzy_match_song(ocr_text)
                if not match:
                    return None

                matched_name, raw_score = match[0], match[1]
                logging.info(f"Fuzzy match result ({model or 'default'}): ('{matched_name}', {raw_score})")

                # --- 新增：长度差异惩罚机制 ---
                len_ocr = len(ocr_text)
                len_matched = len(matched_name)

                if len_ocr == 0 or len_matched == 0:
                    length_ratio = 0
                else:
                    # 计算长度相似度，作为惩罚因子 (0.0 a 1.0)
                    length_ratio = min(len_ocr, len_matched) / max(len_ocr, len_matched)

                adjusted_score = raw_score * length_ratio
                logging.info(
                    f"Adjusted score for '{matched_name}' with length penalty ({model or 'default'}): "
                    f"{adjusted_score:.2f} (raw: {raw_score}, len_ratio: {length_ratio:.2f})"
                )
                # --- 惩罚机制结束 ---

                # 返回带有惩罚分数的匹配结果
                return (matched_name, adjusted_score)

            except Exception as e:
                logging.error(f"OCR ({model or 'default'}) execution failed: {e}")
                return None

        jp_match = ocr_and_match("ppocr_v3/ja_jp")
        common_match = ocr_and_match()

        # 收集所有成功的匹配结果
        results = [m for m in [jp_match, common_match] if m]

        if not results:
            return self.AnalyzeResult(None, "")

        # 基于调整后的分数（adjusted_score）选择最佳匹配
        best_match = max(results, key=lambda x: x[1])

        if best_match and best_match[1] > 50:
            matched_song_name = best_match[0]
            adjusted_confidence = best_match[1]
            logging.info(f"Song recognised: '{matched_song_name}' (Adjusted Confidence: {adjusted_confidence:.2f}%)")
            return self.AnalyzeResult(roi, matched_song_name)

        return self.AnalyzeResult(None, "")


@maaresource.custom_action("UISaveSong")
class UISaveSong(CustomAction):
    def run(self, context, argv):
        save_song(argv.reco_detail.best_result.detail)
        return self.RunResult(True)


@maaresource.custom_action("UIPlay")
class UIPlay(CustomAction):
    def run(self, context, argv):
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
            play_song(stop_event,playback_started_event)
            stop_event.set()
            return self.RunResult(True)
        except Exception as e:
            stop_event.set()
            logging.error(f"Error during song playback: {e}", exc_info=True)
            return self.RunResult(False)
        finally:
            monitor.join(timeout=5)


@maaresource.custom_recognition("UIPlayResult")
class UIPlayResult(CustomRecognition):
    def analyze(self, context, argv):
        types = {
            "score": {"roi": [1028, 192, 144, 35]}, "maxcombo": {"roi": [1009, 391, 91, 28]},
            "perfect": {"roi": [829, 282, 90, 28]}, "great": {"roi": [828, 322, 91, 27]},
            "good": {"roi": [829, 363, 91, 27]}, "bad": {"roi": [829, 401, 90, 27]},
            "miss": {"roi": [830, 438, 91, 28]}, "fast": {"roi": [1088, 283, 90, 27]},
            "slow": {"roi": [1088, 323, 91, 28]},
        }
        result, pipeline = {}, {f"_ocr_{t}": {"recognition": "OCR", "only_rec": True, "roi": v["roi"]} for t, v in
                                types.items()}
        for type_ in types:
            try:
                ocr_recognition_result = context.run_recognition(f"_ocr_{type_}", argv.image, pipeline)
                if ocr_recognition_result and ocr_recognition_result.best_result:
                    result[type_] = int(ocr_recognition_result.best_result.text)
                else:
                    result[type_] = -1
            except (ValueError, TypeError):
                result[type_] = -1
        logging.info(f"Play result: {result}")
        return self.AnalyzeResult([0, 0, 0, 0], json.dumps(result))


@maaresource.custom_action("UISavePlayResult")
class UISavePlayResult(CustomAction):
    def run(self, context, argv):
        global PLAY_FAILED_TIMES, NOT_FC_SONG_COUNT_DICT, LAST_PLAYED_SONG_ID, SONG_ATTEMPT_COUNT_DICT

        # Improved handling of the 'succeed' parameter.
        param = argv.custom_action_param
        try:
            succeed = json.loads(param).get("succeed", False)
        except (json.JSONDecodeError, TypeError):
            # If parsing fails, keep 'succeed' as False.
            logging.error("Failed to parse custom_action_param.")
            succeed = False

        play_result = {}
        if succeed and argv.reco_detail and argv.reco_detail.best_result:
            try:
                play_result = argv.reco_detail.best_result.detail
                # --- 优化后的“非Full Combo”健壮性判断逻辑 (V3 - 增加乐观检查) ---

                # 1. 安全地获取所有数值
                perfect = play_result.get('perfect', -1)
                great = play_result.get('great', -1)
                good = play_result.get('good', -1)
                bad = play_result.get('bad', -1)
                miss = play_result.get('miss', -1)
                maxcombo = play_result.get('maxcombo', -1)

                is_not_fc = False
                reasons = []

                # 2. 新增【乐观检查】(Optimistic Check):
                # 检查是否满足 All Perfect (AP) 的强条件 (perfect + great == maxcombo)。
                # 这是一个非常强的FC信号，即使 good/bad/miss 的数据缺失(-1)也可以采信。
                is_ap_by_sum = (
                        perfect != -1 and
                        great != -1 and
                        maxcombo != -1 and
                        (perfect + great) == maxcombo
                )

                if is_ap_by_sum:
                    # 如果满足AP条件，我们就可以100%确信这是一个FC。
                    # 因此，直接判定 is_not_fc 为 False，并跳过所有后续的“非FC”检查。
                    is_not_fc = False
                else:
                    # 3. 如果【乐观检查】不通过，则执行之前的【保守检查】(Pessimistic Check)

                    # a. 检查数据完整性
                    if -1 in [perfect, great, good, bad, miss, maxcombo]:
                        is_not_fc = True
                        reasons.append("OCR Failed")
                    else:
                        # b. 如果所有数值都有效，再进行游戏逻辑判断
                        if bad > 0:
                            reasons.append(f"Bad: {bad}")
                        if miss > 0:
                            reasons.append(f"Miss: {miss}")
                        if good > 0:
                            reasons.append(f"Good: {good}")

                        # c. 交叉验证 (P+G+Gd vs MaxCombo)
                        sum_of_judgements = perfect + great
                        if maxcombo != sum_of_judgements:
                            reasons.append(f"P+G={sum_of_judgements}, MaxCombo={maxcombo}")

                        if reasons:
                            is_not_fc = True

                # --- 判断逻辑结束 ---

                if is_not_fc and current_song_id:
                    current_count = NOT_FC_SONG_COUNT_DICT.get(current_song_id, 0)
                    NOT_FC_SONG_COUNT_DICT[current_song_id] = current_count + 1
                    # 日志现在只在确定为“非FC”时才打印原因
                    logging.warning(
                        f"Song '{current_song_name}' did not achieve a Full Combo. "
                        f"Total count: {NOT_FC_SONG_COUNT_DICT[current_song_id]}"
                        f"Reasons: {', '.join(reasons)}"
                    )

            except json.JSONDecodeError:
                logging.error("Failed to parse play result JSON.")
                play_result = {}

        # Increment failure count only on explicit failures (e.g., live failed, pipeline error).
        if succeed:
            # If task is successful and there were previous failures, log and reset counter
            if PLAY_FAILED_TIMES > 0:
                logging.info(f"Task successful, resetting continuous failure count from {PLAY_FAILED_TIMES} to zero.")
            PLAY_FAILED_TIMES = 0
        else:  # 'not succeed' case
            # If task failed, increment counter unconditionally 
            PLAY_FAILED_TIMES += 1
            logging.info(f"Recording one task failure, current continuous failure count: {PLAY_FAILED_TIMES}")

        PlayRecord.create(
            play_time=int(time.time()), play_offset=OFFSET, result=play_result,
            succeed=succeed, chart_id=current_song_id, difficulty=DIFFICULTY,
        )

        # --- 新增：无条件更新总尝试次数 ---
        if current_song_id:
            current_attempts = SONG_ATTEMPT_COUNT_DICT.get(current_song_id, 0)
            SONG_ATTEMPT_COUNT_DICT[current_song_id] = current_attempts + 1
            logging.info(f"Song '{current_song_name}' has been attempted {SONG_ATTEMPT_COUNT_DICT[current_song_id]} times.")

        # --- 在函数末尾记录上一首歌曲 ---
        LAST_PLAYED_SONG_ID = current_song_id

        if PLAY_FAILED_TIMES >= MAX_CONTINUOUS_FAILED_TIMES:
            logging.error(f"Continuous failure limit reached ({MAX_CONTINUOUS_FAILED_TIMES}). Stopping automatically.")
            context.run_action("stop")

        return self.RunResult(True)

"""
# --- Screen Streaming ---
def _stream_loop(socketio):
    while streaming_active.is_set():
        try:
            if not current_player: time.sleep(1); continue
            with stream_settings_lock:
                fps, res_width = STREAM_SETTINGS["fps"], STREAM_SETTINGS["resolution"]
            img_bgr = current_player.ipc_capture_display()
            if img_bgr is None: time.sleep(1); continue
            img_rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
            h, w = img_rgb.shape[:2]
            thumb = cv2.resize(img_rgb, (res_width, int(res_width * (h / w))))
            _, buffer = cv2.imencode(".jpg", thumb, [cv2.IMWRITE_JPEG_QUALITY, 80])
            socketio.emit("update_frame", {"image": base64.b64encode(buffer).decode("utf-8")})
            time.sleep(1 / fps)
        except Exception as e:
            logging.error(f"Screen streaming thread error: {e}")
            time.sleep(1)


def update_stream_settings(settings):
    with stream_settings_lock:
        STREAM_SETTINGS["fps"] = int(settings.get("fps", 1))
        STREAM_SETTINGS["resolution"] = int(settings.get("resolution", 480))


def start_streaming(socketio):
    global streaming_thread
    if not streaming_thread or not streaming_thread.is_alive():
        streaming_active.set()
        streaming_thread = threading.Thread(target=_stream_loop, args=(socketio,))
        streaming_thread.daemon = True
        streaming_thread.start()


def stop_streaming():
    global streaming_thread
    streaming_active.clear()
    if streaming_thread and streaming_thread.is_alive(): streaming_thread.join(timeout=1)
    streaming_thread = None
"""

# --- Task Entrypoints ---
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
    
# 文件末尾的清理函数
def shutdown_resources():
    """关闭并释放所有全局资源，如 MNT 和 MAA 控制器。"""
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


def run_single_mode_free(config_data):
    """Single song mode: Plays one song and then stops."""
    global DIFFICULTY, HUMAN_DELAY_ENABLED
    DIFFICULTY = config_data.get("difficulty", "expert")
    HUMAN_DELAY_ENABLED = config_data.get("human_delay", False)
    if not maacontroller or not mnt: raise RuntimeError("MAA is not initialised.")
    override_pipeline = {
        "ui_simplified_entry": {
            "recognition": "Custom",
            "custom_recognition": "UISongRecognitionFreeSingle",
            "action": "Custom",
            "custom_action": "UISaveSong",
            "next": [
                "startlive"
            ],
            "timeout": 15000,
            "on_error": [
                "stop"
            ]
        },
        "startlive": {
            "action": "Click",
            "next": [
                "playsong"
            ],
            "on_error": [
                "stop"
            ],
            "recognition": "TemplateMatch",
            "template": "live/button/live_medley.png",
            "threshold": 0.5
        },
        "playsong": {
            "action": "Custom",
            "custom_action": "UIPlay",
            "next": [
                "stop"
            ],
            "timeout": 500000
        },
    }
    logging.info("Submitting Single Song Mode auto-play task.")
    maatasker.post_task("ui_simplified_entry", override_pipeline).wait()
    logging.info("Single Song Mode auto-play task finished.")


def run_full_auto_mode(config_data):
    """Full auto mode with failure stop and non-FC skip functionality."""
    global DIFFICULTY, HUMAN_DELAY_ENABLED, PLAY_FAILED_TIMES, NOT_FC_SONG_COUNT_DICT, MAX_CONTINUOUS_NOT_FC_COUNT, MAX_ATTEMPT_COUNT

    DIFFICULTY = config_data.get("difficulty", "hard")
    HUMAN_DELAY_ENABLED = config_data.get("human_delay", False)

    # Get the value from config_data and update the global variable
    # Use .get() with a default value of 1 for safety
    MAX_CONTINUOUS_NOT_FC_COUNT = config_data.get("max_continuous_not_fc_count", 1)
    MAX_ATTEMPT_COUNT = config_data.get("max_attempt_count", 3)

    PLAY_FAILED_TIMES = 0
    NOT_FC_SONG_COUNT_DICT.clear()

    # Add a log to confirm the setting was received
    logging.info(f"Non-FC Skip Limit set to: {MAX_CONTINUOUS_NOT_FC_COUNT}")

    if not maacontroller or not mnt: raise RuntimeError("MAA is not initialised.")

    pipeline_def_path = resource_path("assets/resource/pipeline")
    with open(pipeline_def_path / "live.json", 'r', encoding='utf-8') as f:
        live_pipeline_def = json.load(f)
    with open(pipeline_def_path / "common.json", 'r', encoding='utf-8') as f:
        common_pipeline_def = json.load(f)

    result_screen_interrupts = [
        "next_button",
        "close_button",
        "ok_button",
        "confirm_button",
        "reader_menu",
        "read_after"
    ]

    override_pipeline = {
        # --- Song Selection Flow ---
        "select_song": {
            "recognition": "OCR",
            "expected": [
                "选择乐曲"
            ],
            "next": [
                "get_song_name",
                "random_choice_song_action"
            ],
            "interrupt": [
                "liveagain",
                "live_home_button"
            ] + result_screen_interrupts,
            "post_delay": 2000
        },
        "get_song_name": {
            "recognition": "Custom",
            "custom_recognition": "UISongRecognitionFreeAuto",
            "action": "Custom",
            "custom_action": "UISaveSong",
            "next": [
                "decide_play_or_skip",
                "click_confirm_on_song_select"
            ],
            "timeout": 15000,
        },
        # Decision node
        "decide_play_or_skip": {
            "recognition": "Custom",
            "custom_recognition": "UICheckFCStatusRecognition",
            "next": "random_choice_song_action",
        },
        "random_choice_song_action": {
            **live_pipeline_def["random_choice_song"],
            "next": [
                "select_song"
            ]
        },
        "click_confirm_on_song_select": {
            **common_pipeline_def["confirm_button"],
            "next": [
                "wait_for_final_confirmation"
            ]
        },
        "wait_for_final_confirmation": {
            "recognition": "OCR",
            "expected": [
                "选择乐队",
                "最终确认"
            ],
            "timeout": 3000,
            "next": [
                "disable_liveplay"
            ],
            "interrupt": [
                "switch_liveplay_mode"
            ]
        },
        "disable_liveplay": {
            **live_pipeline_def["disable_liveplay"],
            "next": "startlive",
        },
        "startlive": {
            "action": "Click",
            "recognition": "TemplateMatch",
            "pre_wait_freezes": {"threshold": 0.65, "time": 3000},
            "template": "live/button/startlive.png",
            "interrupt": [
                "startlive",
                "confirm_button",
                "login_expired",
                "connect_failed"
            ],
            "post_delay": 3000,
            "next": [
                "playsong"
            ]
        },

        # --- Playback and Post-Live Flow ---
        "playsong": {
            "action": "Custom",
            "custom_action": "UIPlay",
            "next": [
                "wait_for_result_screen"
            ],
            "timeout": 500000,
            "interrupt": [
                "live_failed",
                "next_button",
                "close_button",
                "ok_button",
                "confirm_button",
                "login_expired",
                "connect_failed"
            ]
        },
        "save_failed_playresult": {
            "action": "Custom",
            "custom_action": "UISavePlayResult",
            "custom_action_param": {"succeed": False},
            "next": "live_home_button",
            "interrupt": [
                "exit_button"
            ]
        },
        "select_live_mode": {
                "recognition": "OCR",
                "expected": "自由演出",
                "roi": [679, 183, 257, 354],
                "action": "Click",
                "post_delay": 1500,
                "next": [
                    "select_song",
                    "select_live_mode",
                    "live_home_button"
                ],
                "interrupt": [
                    "login_expired",
                    "connect_failed"
                ],
        },
        # --- Optimised Results Screen Flow ---
        "wait_for_result_screen": {
            "recognition": "TemplateMatch",
            "template": [
                "live/scored.png",
                "live/activity_scored.png"
            ],
            "pre_wait_freezes": {"threshold": 0.65, "time": 5000},
            "next": ["wait_playresult"],
            "interrupt": [
                "live_failed",
                "next_button",
                "close_button",
                "ok_button",
                "confirm_button"
            ],
            "post_delay": 3000
        },
        "wait_playresult": {
            "recognition": "TemplateMatch",
            "template": [
                "live/scored.png",
                "live/activity_scored.png"
            ],
            "pre_wait_freezes": 2000,
            "next": "get_result"
        },
        "get_result": {
            "recognition": "Custom",
            "custom_recognition": "UIPlayResult",
            "action": "Custom",
            "custom_action": "UISavePlayResult",
            "custom_action_param": {"succeed": True},
            "next": [
                "liveagain"
            ],
            "timeout": 30000,
            "on_error": [
                "stop"
            ],
            "interrupt": result_screen_interrupts
        },
        "liveagain": {
            "recognition": "TemplateMatch",
            "template": "live/button/liveagain.png",
            "pre_wait_freezes": {"threshold": 0.65, "time": 5000},
            "action": "Click",
            "next": "select_song",
            "post_delay": 2000
        },
    }
    # Merge common definitions into the pipeline
    override_pipeline.update(common_pipeline_def)

    logging.info("Submitting Full Auto Mode auto-play task.")
    maatasker.post_task("select_song", override_pipeline).wait()
    logging.info("Full Auto Mode auto-play task finished or stopped.")