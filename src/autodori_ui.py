import base64
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
MIN_LIVEBOOST = 1
DIFFICULTY = "hard"
IS_FULL_SONG = False
OFFSET = {"up": 0, "down": 0, "move": 0, "wait": 0.0, "interval": 0.0}
PHOTOGATE_LATENCY = 30
DEFAULT_MOVE_SLICE_SIZE = 10
CMD_SLICE_SIZE = 100
HUMAN_DELAY_ENABLED = True
MAX_FAILED_TIMES = 10
play_failed_times: int = 0

# Global variable to track songs that were not Full Combo'd.
not_fc_song_counts: dict[str, int] = {}
# A song will be skipped after failing to achieve a Full Combo this many times.
MAX_NOT_FC_COUNT = 1

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

# --- Real-time Streaming Components ---
streaming_thread: Optional[threading.Thread] = None
streaming_active = threading.Event()
STREAM_SETTINGS = {"fps": 1, "resolution": 480}
stream_settings_lock = threading.Lock()


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


def play_song():
    """
    Core playback function with performance optimizations.
    """
    cmd_log_list.clear()
    reset_callback_data()

    # STAGE 1: Wait for the game to load by detecting the pause button
    logging.info("Waiting for game to load, detecting pause button...")
    CONFIDENCE_THRESHOLD = 0.9
    template_path = resource_path("assets/resource/image/live/button/pause.png")
    if not template_path.exists():
        logging.error(f"Pause button template image not found: {template_path}")
        return
    template = cv2.imread(str(template_path), 0)
    if template is None:
        logging.error(f"Failed to load template image: {template_path}")
        return
    pause_button_found = False
    wait_start_time = time.time()
    while not pause_button_found:
        if time.time() - wait_start_time > 30:
            logging.error("Timeout (30s) waiting for pause button. Aborting playback.")
            return
        screen = current_player.ipc_capture_display()
        height, width, _ = screen.shape
        roi_screen = screen[0:int(height * 0.15), int(width * 0.95):width]
        gray_roi = cv2.cvtColor(roi_screen, cv2.COLOR_BGR2GRAY)
        result = cv2.matchTemplate(gray_roi, template, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, _ = cv2.minMaxLoc(result)
        logging.debug(f"Waiting for pause button... match confidence: {max_val:.2f}")
        if max_val >= CONFIDENCE_THRESHOLD:
            pause_button_found = True
        else:
            time.sleep(0.5)

    # STAGE 2 & 3: Wait for screen to freeze & photogate detection
    logging.info("Waiting for screen to freeze...")

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
    while True:
        try:
            screen = current_player.ipc_capture_display()
            cur_color, _ = get_color_eval_in_range(screen, from_row, to_row)
            if last_color is not None:
                change_score = np.sum(np.abs(cur_color[:3].astype(int) - last_color[:3].astype(int)))
                logging.debug(f"Color change delta: {change_score}")
                if change_score > 3 and freezed:
                    logging.info("First note detected, starting playback.")
                    time.sleep(PHOTOGATE_LATENCY / 1000)
                    break
                elif not freezed:
                    waited_frames += 1
                if not freezed and waited_frames >= 200:
                    freezed = True
                    logging.info("Screen has frozen. Photogate is ready.")
            last_color = cur_color
        except Exception as e:
            logging.error(f"Error during photogate detection: {e}")
            time.sleep(0.1)

    # STAGE 4: Command execution loop
    logging.info("Starting command execution...")
    while True:
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
    time.sleep(2)


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
    if not maatasker.inited: raise RuntimeError("Failed to initialize MAA tasker module.")
    logging.info("MAA initialized successfully.")


def init_player_and_mnt():
    global current_player, mnt
    if not device: raise RuntimeError("MAA device not initialized before initializing player.")
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
    logging.info(f"{type_} player and Minitouch initialized successfully.")


# --- MAA Custom Modules ---

@maaresource.custom_recognition("UICheckFCStatusRecognition")
class UICheckFCStatusRecognition(CustomRecognition):
    """
    Custom recognition module for decision nodes.
    This recognition succeeds if the current song's "non-FC" count has reached the limit.
    """

    def analyze(self, context: Context, argv: CustomRecognition.AnalyzeArg):
        global not_fc_song_counts, current_song_id, current_song_name, MAX_NOT_FC_COUNT

        if not current_song_id:
            return self.AnalyzeResult(None, "")

        count = not_fc_song_counts.get(current_song_id, 0)

        if count >= MAX_NOT_FC_COUNT:
            logging.warning(
                f"Condition check: Song '{current_song_name}' has reached the non-FC limit ({count})."
            )
            return self.AnalyzeResult([0, 0, 0, 0], str(count))

        return self.AnalyzeResult(None, "")


@maaresource.custom_recognition("UISongRecognitionMedley")
class UISongRecognitionMedley(CustomRecognition):
    def analyze(self, context: Context, argv: CustomRecognition.AnalyzeArg):
        # This ROI might need adjustment based on the actual screen layout.
        roi = [110, 545, 368, 29]

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
            logging.info(f"Song recognized: '{song_name}' (Confidence: {best_match[1]}%)")
            return self.AnalyzeResult(roi, song_name)
        return self.AnalyzeResult(None, "")


@maaresource.custom_recognition("UISongRecognition")
class UISongRecognition(CustomRecognition):
    def analyze(self, context: Context, argv: CustomRecognition.AnalyzeArg):
        # This ROI might need adjustment based on the actual screen layout.
        roi = [200, 332, 368, 29]

        def ocr_and_match(model=None):
            try:
                pipeline = {"_ocr_song": {"recognition": "OCR", "roi": roi, "only_rec": True}}
                if model: pipeline["_ocr_song"]["model"] = model
                ocr_text = context.run_recognition("_ocr_song", argv.image, pipeline).best_result.text
                logging.info(f"OCR ({model or 'default'}) raw text: '{ocr_text}'")
                if "FULL" in ocr_text and not IS_FULL_SONG:
                    return None
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
            matched_song_name = best_match[0]
            song_name_to_return = "[FULL] " + matched_song_name if IS_FULL_SONG else matched_song_name
            logging.info(f"Song recognized: '{song_name_to_return}' (Confidence: {best_match[1]}%)")
            return self.AnalyzeResult(roi, song_name_to_return)

        return self.AnalyzeResult(None, "")


@maaresource.custom_action("UISaveSong")
class UISaveSong(CustomAction):
    def run(self, context, argv):
        save_song(argv.reco_detail.best_result.detail)
        return self.RunResult(True)


@maaresource.custom_action("UIPlay")
class UIPlay(CustomAction):
    def run(self, context, argv):
        try:
            play_song()
            return self.RunResult(True)
        except Exception as e:
            logging.error(f"Error during song playback: {e}", exc_info=True)
            return self.RunResult(False)


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
        global play_failed_times, not_fc_song_counts

        # Improved handling of the 'succeed' parameter.
        succeed = False
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
                # "Not Full Combo" logic
                is_not_fc = (
                        play_result.get("good", 0) > 0 or
                        play_result.get("bad", 0) > 0 or
                        play_result.get("miss", 0) > 0
                )
                if is_not_fc and current_song_id:
                    current_count = not_fc_song_counts.get(current_song_id, 0)
                    not_fc_song_counts[current_song_id] = current_count + 1
                    logging.warning(
                        f"Song '{current_song_name}' did not achieve a Full Combo. "
                        f"Total count: {not_fc_song_counts[current_song_id]}"
                    )
            except json.JSONDecodeError:
                logging.error("Failed to parse play result JSON.")
                play_result = {}

        # Increment failure count only on explicit failures (e.g., live failed, pipeline error).
        if not succeed:
            play_failed_times += 1
            logging.info(f"Recorded a task failure. Current failure count: {play_failed_times}")

        PlayRecord.create(
            play_time=int(time.time()), play_offset=OFFSET, result=play_result,
            succeed=succeed, chart_id=current_song_id, difficulty=DIFFICULTY,
        )

        if play_failed_times >= MAX_FAILED_TIMES:
            logging.error(f"Failure limit reached ({MAX_FAILED_TIMES}). Stopping automatically.")
            context.run_action("stop")

        return self.RunResult(True)


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


# --- Task Entrypoints ---
def init():
    try:
        init_maa()
        init_player_and_mnt()
    except Exception as e:
        raise e


def run_simplified_autodori(config_data):
    """Single song mode: Plays one song and then stops."""
    global DIFFICULTY, IS_FULL_SONG, HUMAN_DELAY_ENABLED
    DIFFICULTY = config_data.get("difficulty", "expert")
    IS_FULL_SONG = config_data.get("is_full_song", False)
    HUMAN_DELAY_ENABLED = config_data.get("human_delay", False)
    if not maacontroller or not mnt: raise RuntimeError("MAA is not initialized.")
    override_pipeline = {
        "ui_simplified_entry": {"recognition": "Custom", "custom_recognition": "UISongRecognitionMedley",
                                "action": "Custom", "custom_action": "UISaveSong", "next": ["startlive"],
                                "timeout": 15000, "on_error": ["stop"]},
        "startlive": {"action": "Click", "next": ["playsong"], "on_error": ["stop"], "recognition": "TemplateMatch",
                      "template": "live/button/live_medley.png", "threshold": 0.5},
        "playsong": {"action": "Custom", "custom_action": "UIPlay", "next": ["stop"], "timeout": 500000},
    }
    logging.info("Submitting Single Song Mode auto-play task...")
    maatasker.post_task("ui_simplified_entry", override_pipeline).wait()
    logging.info("Single Song Mode auto-play task finished.")


def run_full_auto_mode(config_data):
    """Full auto mode with failure stop and non-FC skip functionality."""
    global DIFFICULTY, IS_FULL_SONG, HUMAN_DELAY_ENABLED, play_failed_times, not_fc_song_counts, MAX_NOT_FC_COUNT

    DIFFICULTY = config_data.get("difficulty", "hard")
    IS_FULL_SONG = config_data.get("is_full_song", False)
    HUMAN_DELAY_ENABLED = config_data.get("human_delay", False)

    # Get the value from config_data and update the global variable
    # Use .get() with a default value of 1 for safety
    MAX_NOT_FC_COUNT = config_data.get("max_not_fc_count", 1)

    play_failed_times = 0
    not_fc_song_counts.clear()

    # Add a log to confirm the setting was received
    logging.info(f"Non-FC Skip Limit set to: {MAX_NOT_FC_COUNT}")

    if not maacontroller or not mnt: raise RuntimeError("MAA is not initialized.")

    pipeline_def_path = resource_path("assets/resource/pipeline")
    with open(pipeline_def_path / "live.json", 'r', encoding='utf-8') as f:
        live_pipeline_def = json.load(f)
    with open(pipeline_def_path / "common.json", 'r', encoding='utf-8') as f:
        common_pipeline_def = json.load(f)

    result_screen_interrupts = [
        "next_button", "ok_button", "close_button", "confirm_button", "read_after"
    ]

    override_pipeline = {
        # --- Song Selection Flow ---
        "select_song_entry": {
            **live_pipeline_def["select_song"],
            "next": "get_song_name",
        },
        "get_song_name": {
            "recognition": "Custom", "custom_recognition": "UISongRecognition",
            "action": "Custom", "custom_action": "UISaveSong",
            "next": ["decide_play_or_skip", "click_confirm_on_song_select"],
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
            "next": ["select_song_entry"]
        },
        "click_confirm_on_song_select": {
            **common_pipeline_def["confirm_button"],
            "next": ["wait_for_final_confirmation"]
        },
        "wait_for_final_confirmation": {
            **live_pipeline_def["comfirm_song"],
            "next": ["disable_liveplay"],
        },
        "disable_liveplay": {
            **live_pipeline_def["disable_liveplay"],
        },
        "startlive": {
            **live_pipeline_def["startlive"],
            "next": ["playsong"]
        },

        # --- Playback and Post-Live Flow ---
        "playsong": {
            "action": "Custom", "custom_action": "UIPlay",
            "next": ["wait_for_result_screen"],
            "timeout": 500000,
            "on_error": ["save_failed_result"],
            "interrupt": [
                "live_failed_handler", "next_button", "close_button", "ok_button",
                "confirm_button", "login_expired", "connect_failed"
            ]
        },
        "live_failed_handler": {
            **live_pipeline_def["live_failed"],
            "next": ["save_failed_result_and_stop"]
        },
        "save_failed_result_and_stop": {
            "action": "Custom", "custom_action": "UISavePlayResult",
            "custom_action_param": {"succeed": False},
            "next": ["stop"],
        },

        # --- Optimized Results Screen Flow ---
        "wait_for_result_screen": {
            "recognition": "TemplateMatch",
            "template": [
                "live/scored.png",
                "live/activity_scored.png"
            ],
            "pre_wait_freezes": {"threshold": 0.9, "time": 3000},
            "next": ["get_result"],
            "interrupt": [
                "live_failed_handler", "next_button", "close_button",
                "ok_button", "confirm_button"
            ]
        },
        "get_result": {
            "recognition": "Custom", "custom_recognition": "UIPlayResult",
            "action": "Custom", "custom_action": "UISavePlayResult",
            "custom_action_param": {"succeed": True},
            "next": ["liveagain"],
            "timeout": 30000,
            "on_error": ["stop"],
            "interrupt": result_screen_interrupts
        },
        "save_failed_result": {
            "action": "Custom", "custom_action": "UISavePlayResult",
            "custom_action_param": {"succeed": False},
            "next": ["liveagain"],
        },
        "liveagain": {
            **live_pipeline_def["liveagain"],
            "next": ["select_song_entry"],
            "interrupt": result_screen_interrupts
        },
    }
    # Merge common definitions into the pipeline
    override_pipeline.update(common_pipeline_def)

    logging.info("Submitting Full Auto Mode auto-play task...")
    maatasker.post_task("select_song_entry", override_pipeline).wait()
    logging.info("Full Auto Mode auto-play task finished or stopped.")