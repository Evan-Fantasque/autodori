import base64
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
from chart import Chart
from util import get_color_eval_in_range, get_runtime_info

# --- Global Variables & Constants ---
MIN_LIVEBOOST = 1
DIFFICULTY = "hard"
IS_FULL_SONG = False  # New global flag for FULL song support
OFFSET = {"up": 0, "down": 0, "move": 0, "wait": 0.0, "interval": 0.0}
PHOTOGATE_LATENCY = 30
DEFAULT_MOVE_SLICE_SIZE = 10
CMD_SLICE_SIZE = 100

# --- MAA & System Components ---
config_path = Path("data/config.yml")
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
    current_chart.notes_to_actions(current_player.resolution, DEFAULT_MOVE_SLICE_SIZE)
    current_orientation = _get_orientation()
    current_chart.actions_to_MNTcmd(
        (mnt.max_x, mnt.max_y), current_orientation, OFFSET, CMD_SLICE_SIZE
    )
    logging.info(f"Saved song: {name}")


def play_song():
    """
    重构后的play_song函数。
    集成了“等待加载”、“等待画面静止”和“光电门检测”三大功能。
    模板匹配被限制在屏幕的右上角区域，以提高效率和准确性。
    """
    cmd_log_list.clear()
    reset_callback_data()

    # --- STAGE 1: 等待游戏加载 (检测暂停按钮) ---
    logging.info("==> [阶段1] 等待游戏加载，正在检测暂停按钮...")

    CONFIDENCE_THRESHOLD = 0.4
    template_path = Path("assets/resource/image/live/button/pause.png")

    if not template_path.exists():
        logging.error(f"暂停按钮模板图片未找到: {template_path}")
        return

    template = cv2.imread(str(template_path), 0)
    if template is None:
        logging.error(f"无法加载模板图片: {template_path}")
        return

    pause_button_found = False
    wait_start_time = time.time()

    # 新增一个标志位，确保ROI截图只保存一次
    roi_screenshot_saved = False

    while not pause_button_found:
        if time.time() - wait_start_time > 30:
            logging.error("等待暂停按钮超时 (30秒)，演奏任务中止。")
            return

        screen = current_player.ipc_capture_display()
        if screen is None:
            time.sleep(0.5)
            continue

        height, width, _ = screen.shape

        roi_y_start = 0
        roi_y_end = int(height * 0.15)
        roi_x_start = int(width * 0.95)
        roi_x_end = width

        roi_screen = screen[roi_y_start:roi_y_end, roi_x_start:roi_x_end]
        gray_roi = cv2.cvtColor(roi_screen, cv2.COLOR_BGR2GRAY)

        # 1. 灰度模板匹配
        result = cv2.matchTemplate(gray_roi, template, cv2.TM_CCOEFF_NORMED)
        min_val, max_val, min_loc, max_loc = cv2.minMaxLoc(result)

        confidence = max_val
        best_match_loc = max_loc

        logging.info(f"等待暂停按钮... 匹配度: {confidence:.2f}")

        if confidence >= CONFIDENCE_THRESHOLD:
            pause_button_found = True
        else:
            time.sleep(0.2)

    # --- STAGE 2 & 3: 等待画面静止 & 光电门检测 ---
    logging.info("==> [阶段2] 等待画面静止...")

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

    # --- Wait for the first note (Photogate logic) ---
    last_color = None
    waited_frames = 0
    info = get_runtime_info(current_player.resolution)["wait_first"]
    from_row, to_row = info["from"], info["to"]
    freezed = False

    while True:
        try:
            screen = current_player.ipc_capture_display()
            cur_color, _ = get_color_eval_in_range(screen, from_row, to_row)
            if last_color is not None:
                change_score = np.sum(np.abs(cur_color[:3].astype(int) - last_color[:3].astype(int)))

                # 调试日志：打印颜色变化量
                logging.info(f"颜色变化量: {change_score}")

                if change_score > 3:  # 阈值可根据需要调整
                    if freezed:
                        logging.info("==> [阶段3] 检测到第一个音符！开始演奏！")
                        time.sleep(PHOTOGATE_LATENCY / 1000)
                        break  # 跳出循环，开始执行命令
                elif not freezed:
                    waited_frames += 1

                if not freezed and waited_frames >= 150:  # 静止帧数要求，可调整
                    freezed = True
                    logging.info("画面已静止，光电门已准备就绪！")
            last_color = cur_color
        except Exception as e:
            logging.error(f"在光电门检测期间发生错误: {e}")
            time.sleep(0.1)

    # --- STAGE 4: 命令执行循环 ---
    logging.info("==> [阶段4] 开始执行演奏指令...")
    while True:
        current_chart.command_builder.publish(mnt, block=False)
        wait_time = _get_wait_time()
        time.sleep(max(0, wait_time - 3) / 1000)
        index = current_chart.actions_to_cmd_index
        if current_chart.actions[index: index + CMD_SLICE_SIZE]:
            with callback_data_lock:
                _adjust_offset()
                reset_callback_data()
            current_chart.actions_to_MNTcmd(
                (mnt.max_x, mnt.max_y), current_orientation, OFFSET, CMD_SLICE_SIZE
            )
        else:
            break
    logging.info("Playback finished.")


def mnt_callback(event: MNTEvent, data: MNTEventData):
    global callback_data
    if event == MNTEvent.EVATIVE7_LOG:
        data: MNTEvATive7LogEventData = data

        cmd = data.cmd
        cost = data.cost

        with cmd_log_list_lock:
            cmd_log_list.append(data)
        cmd_type = cmd.split(" ")[0]

        callback_data_lock.acquire()

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
        callback_data_lock.release()


def init_maa():
    global device, maacontroller
    maaresource.post_bundle("assets/resource").wait()
    Toolkit.init_option("./")
    adb_devices = Toolkit.find_adb_devices()
    if not adb_devices:
        raise RuntimeError("未找到 ADB 设备。请确保模拟器正在运行并已启用 ADB 调试。")
    supported_devices = [
        d
        for d in adb_devices
        if "mumu" in d.config.get("extras", {}) or "ld" in d.config.get("extras", {})
    ]
    if not supported_devices:
        raise RuntimeError("未找到支持的模拟器 (MuMu, 雷电)。")
    device = supported_devices[0]
    logging.info(f"正在使用设备: {device.name} at {device.address}")
    maacontroller = AdbController(
        adb_path=device.adb_path, address=device.address, config=device.config
    )
    if not maacontroller.post_connection().wait().succeeded:
        raise RuntimeError(f"连接控制器到设备 {device.name} 失败。")
    maatasker.bind(maaresource, maacontroller)
    if not maatasker.inited:
        raise RuntimeError("初始化 MAA 任务模块失败。")
    logging.info("MAA 初始化成功。")


def init_player_and_mnt():
    global current_player, mnt
    if not device:
        raise RuntimeError("在初始化播放器之前 MAA 设备尚未初始化。")
    extra_config = device.config["extras"]
    if "mumu" in extra_config:
        type_, config_key = "mumu", "mumu"
        if "v4" in device.name or "v5" in device.name:
            type_ += device.name[-2:]
    elif "ld" in extra_config:
        type_, config_key = "ld", "ld"
    else:
        raise RuntimeError(f"不支持的模拟器类型，extras: {list(extra_config.keys())}")
    player_config = extra_config[config_key]
    current_player = player.Player(
        type_, Path(player_config["path"]), player_config["index"]
    )
    mnt = MNT(
        device.address,
        type_="EvATive7",
        communicate_type=MNTServerCommunicateType.STDIO,
        mnt_asset_path=Path("./assets/minitouch_EvATive7"),
        callback=mnt_callback,
        adb_executor=str(device.adb_path.absolute()),
    )
    logging.info(f"{type_} 播放器和 Minitouch 初始化成功。")


@maaresource.custom_recognition("UISongRecognition")
class UISongRecognition(CustomRecognition):
    def analyze(self, context: Context, argv: CustomRecognition.AnalyzeArg):
        roi = [110, 545, 368, 29]
        """
        try:
            debug_path = Path("debug")
            debug_path.mkdir(exist_ok=True)
            img_np = np.array(argv.image, copy=False)
            img_bgr = cv2.cvtColor(img_np, cv2.COLOR_BGRA2BGR)
            x, y, w, h = roi
            cv2.rectangle(img_bgr, (x, y), (x + w, y + h), (0, 0, 255), 2)
            filename = (
                    debug_path
                    / f"roi_debug_{datetime.datetime.now().strftime('%Y%m%d-%H%M%S')}.png"
            )
            cv2.imwrite(str(filename), img_bgr)
            logging.debug(f"调试图像已保存: {filename}")
        except Exception as e:
            logging.error(f"保存调试图像失败: {e}")
        """

        def ocr_and_match(model=None):
            """Helper function to run OCR with a specific model and get the match result."""
            try:
                pipeline = {
                    "_ocr_song": {
                        "recognition": "OCR",
                        "roi": roi,
                        "only_rec": True,
                    }
                }
                if model:
                    pipeline["_ocr_song"]["model"] = model

                ocr_text = context.run_recognition(
                    "_ocr_song", argv.image, pipeline
                ).best_result.text

                logging.info(f"OCR ({model or 'default'}) 识别原始文本: '{ocr_text}'")
                match = fuzzy_match_song(ocr_text)
                logging.info(f"模糊匹配结果 ({model or 'default'}): {match}")
                return match
            except Exception as e:
                logging.error(f"OCR ({model or 'default'}) 执行失败: {e}")
                return None

        # --- Dual-engine OCR process ---
        jp_match = ocr_and_match("ppocr_v3/ja_jp")
        common_match = ocr_and_match()

        # --- Compare results and select the best one ---
        results = [m for m in [jp_match, common_match] if m is not None]
        if not results:
            logging.warning("UI 流程歌曲识别失败：所有 OCR 引擎均未返回有效结果。")
            return self.AnalyzeResult(None, "")

        best_match = max(results, key=lambda x: x[1])

        if best_match and best_match[1] > 50:
            if IS_FULL_SONG:
                logging.info(f"歌曲识别成功: '[FULL] {best_match[0]}' (最高置信度: {best_match[1]}%)")
                return self.AnalyzeResult(roi, "[FULL] "+best_match[0])
            else:
                logging.info(f"歌曲识别成功: '{best_match[0]}' (最高置信度: {best_match[1]}%)")
                return self.AnalyzeResult(roi, best_match[0])
        else:
            logging.warning(f"UI 流程歌曲识别失败：最高匹配度未超过 50% ({best_match})。")
            return self.AnalyzeResult(None, "")


@maaresource.custom_recognition("UILiveBoostEnough")
class UILiveBoostEnough(CustomRecognition):
    def analyze(self, context, argv):
        return self.AnalyzeResult(None, "10")


@maaresource.custom_action("UIHandleLiveBoost")
class UIHandleLiveBoost(CustomAction):
    def run(self, context, argv):
        return self.RunResult(True)


@maaresource.custom_recognition("UIPlayResult")
class UIPlayResult(CustomRecognition):
    def analyze(self, context, argv):
        return self.AnalyzeResult([0, 0, 0, 0], "{}")


@maaresource.custom_action("UISavePlayResult")
class UISavePlayResult(CustomAction):
    def run(self, context, argv):
        logging.info("歌曲完成，简化流程中跳过保存结果。")
        return self.RunResult(True)


@maaresource.custom_action("UIPlay")
class UIPlay(CustomAction):
    def run(self, context, argv):
        try:
            play_song()
            return self.RunResult(True)
        except Exception as e:
            logging.error(f"歌曲演奏期间出错: {e}", exc_info=True)
            return self.RunResult(False)


@maaresource.custom_action("UISaveSong")
class UISaveSong(CustomAction):
    def run(self, context, argv):
        save_song(argv.reco_detail.best_result.detail)
        return self.RunResult(True)


def _stream_loop(socketio):
    """The loop that captures, processes, and sends screenshots."""
    while streaming_active.is_set():
        try:
            if not current_player:
                time.sleep(1)
                continue

            with stream_settings_lock:
                current_fps = STREAM_SETTINGS["fps"]
                current_res_width = STREAM_SETTINGS["resolution"]

            # 1. Capture screen using the player's method, which is known to be reliable.
            img_bgr = current_player.ipc_capture_display()

            if img_bgr is None:
                logging.warning("获取截图失败 (播放器返回了 None)。")
                time.sleep(1)
                continue

            # 2. Convert from BGR to RGB for correct web display.
            img_rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)

            # 3. Resize to a thumbnail based on settings
            original_h, original_w = img_rgb.shape[:2]
            aspect_ratio = original_h / original_w
            thumbnail_height = int(current_res_width * aspect_ratio)
            thumbnail = cv2.resize(img_rgb, (current_res_width, thumbnail_height))

            # 4. Encode to JPEG and then Base64
            _, buffer = cv2.imencode(".jpg", thumbnail, [cv2.IMWRITE_JPEG_QUALITY, 80])
            img_base64 = base64.b64encode(buffer).decode("utf-8")

            # 5. Emit to the client
            socketio.emit("update_frame", {"image": img_base64})

            # 6. Control frame rate
            sleep_duration = 1 / current_fps
            time.sleep(sleep_duration)

        except Exception as e:
            logging.error(f"画面传输线程出错: {e}")
            time.sleep(1)


def update_stream_settings(settings):
    """Safely updates the global stream settings."""
    global STREAM_SETTINGS
    with stream_settings_lock:
        STREAM_SETTINGS["fps"] = int(settings.get("fps", 1))
        STREAM_SETTINGS["resolution"] = int(settings.get("resolution", 480))


def start_streaming(socketio):
    """Starts the background thread for screen streaming."""
    global streaming_thread
    if not streaming_thread or not streaming_thread.is_alive():
        streaming_active.set()
        streaming_thread = threading.Thread(target=_stream_loop, args=(socketio,))
        streaming_thread.daemon = True
        streaming_thread.start()


def stop_streaming():
    """Stops the screen streaming thread."""
    global streaming_thread
    streaming_active.clear()
    if streaming_thread and streaming_thread.is_alive():
        streaming_thread.join(timeout=1)  # Wait for a second
    streaming_thread = None


def init(log_callback=None):
    try:
        init_maa()
        init_player_and_mnt()
    except Exception as e:
        raise e


def run_simplified_autodori(config_data):
    global DIFFICULTY, IS_FULL_SONG
    DIFFICULTY = config_data.get("difficulty", "hard")
    IS_FULL_SONG = config_data.get("is_full_song", False)
    if not maacontroller or not mnt:
        raise RuntimeError("MAA is not initialized. Please initialize first.")

    # --- Corrected and Simplified Pipeline Definition ---
    override_pipeline = {
        "ui_simplified_entry": {
            "recognition": "Custom",
            "custom_recognition": "UISongRecognition",
            "action": "Custom",
            "custom_action": "UISaveSong",
            "next": ["comfirm_song"],
            "timeout": 15000,
            "on_error": ["stop"],
        },
        "comfirm_song": {
            "recognition": "TemplateMatch",
            "template": "live/button/live_medley.png",
            # After confirming the start button is visible, go directly to the task that clicks it.
            "next": ["startlive"],
            "timeout": 5000,
            "on_error": ["stop"],
            "threshold": 0.5,
        },
        # We override the 'startlive' task from live.json to ensure it has the correct
        # failure behavior and subsequent step for our simplified flow.
        "startlive": {
            "action": "Click",
            "next": ["wait_live_start"],
            "on_error": ["stop"],
            "recognition": "TemplateMatch",
            "template": "live/button/live_medley.png",
            "timeout": 7500,
            "threshold": 0.5,
        },
        "playsong": {
            "action": "Custom",
            "custom_action": "UIPlay",
            "next": ["stop"],
            "timeout": 50000,
        },
    }

    logging.info("正在提交简化版自动演奏任务...")
    result = maatasker.post_task("ui_simplified_entry", override_pipeline).wait()
    if not result.success:
        logging.error(f"简化版自动演奏任务失败，状态: {result.status}")
    logging.info("简化版自动演奏任务完成。")
