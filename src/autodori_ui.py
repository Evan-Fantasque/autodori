import base64
import datetime
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

# --- Global Variables & Constants ---
MIN_LIVEBOOST = 1
DIFFICULTY = "hard"
IS_FULL_SONG = False
OFFSET = {"up": 0, "down": 0, "move": 0, "wait": 0.0, "interval": 0.0}
PHOTOGATE_LATENCY = 10
DEFAULT_MOVE_SLICE_SIZE = 10
CMD_SLICE_SIZE = 100
HUMAN_DELAY_ENABLED = True
MAX_FAILED_TIMES = 10
play_failed_times: int = 0


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
    current_chart.notes_to_actions(current_player.resolution, DEFAULT_MOVE_SLICE_SIZE, humanize=HUMAN_DELAY_ENABLED)
    current_orientation = _get_orientation()
    current_chart.actions_to_MNTcmd(
        (mnt.max_x, mnt.max_y), current_orientation, OFFSET, CMD_SLICE_SIZE
    )
    logging.info(f"Saved song: {name}")


def play_song():
    """
    重构后的play_song函数，保留UI版的核心优化。
    """
    cmd_log_list.clear()
    reset_callback_data()

    # STAGE 1: 等待游戏加载 (检测暂停按钮)
    time.sleep(3)
    logging.info("==> [阶段1] 等待游戏加载，正在检测暂停按钮...")
    CONFIDENCE_THRESHOLD = 0.4
    template_path = Path("assets/resource/image/live/button/pause.png")
    if not template_path.exists():
        logging.error(f"暂停按钮模板图片未找到: {template_path}"); return
    template = cv2.imread(str(template_path), 0)
    if template is None:
        logging.error(f"无法加载模板图片: {template_path}"); return
    pause_button_found = False
    wait_start_time = time.time()
    while not pause_button_found:
        if time.time() - wait_start_time > 30:
            logging.error("等待暂停按钮超时 (30秒)，演奏任务中止。"); return
        screen = current_player.ipc_capture_display()
        if screen is None: time.sleep(0.5); continue
        height, width, _ = screen.shape
        roi_screen = screen[0:int(height * 0.15), int(width * 0.95):width]
        gray_roi = cv2.cvtColor(roi_screen, cv2.COLOR_BGR2GRAY)
        result = cv2.matchTemplate(gray_roi, template, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, _ = cv2.minMaxLoc(result)
        logging.info(f"等待暂停按钮... 匹配度: {max_val:.2f}")
        if max_val >= CONFIDENCE_THRESHOLD:
            pause_button_found = True
        else:
            time.sleep(0.2)

    # STAGE 2 & 3: 等待画面静止 & 光电门检测
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

    last_color, waited_frames, freezed = None, 0, False
    info = get_runtime_info(current_player.resolution)["wait_first"]
    from_row, to_row = info["from"], info["to"]
    while True:
        try:
            screen = current_player.ipc_capture_display()
            cur_color, _ = get_color_eval_in_range(screen, from_row, to_row)
            if last_color is not None:
                change_score = np.sum(np.abs(cur_color[:3].astype(int) - last_color[:3].astype(int)))
                logging.info(f"颜色变化量: {change_score}")
                if change_score > 3 and freezed:
                    logging.info("==> [阶段3] 检测到第一个音符！开始演奏！")
                    time.sleep(PHOTOGATE_LATENCY / 1000)
                    break
                elif not freezed:
                    waited_frames += 1
                if not freezed and waited_frames >= 150:
                    freezed = True
                    logging.info("画面已静止，光电门已准备就绪！")
            last_color = cur_color
        except Exception as e:
            logging.error(f"在光电门检测期间发生错误: {e}"); time.sleep(0.1)

    # STAGE 4: 命令执行循环
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
                callback_data[type_]["uncommited"] += 1; callback_data[type_]["total"] += 1
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
    if not adb_devices: raise RuntimeError("未找到 ADB 设备。")
    supported_devices = [d for d in adb_devices if "mumu" in d.config.get("extras", {}) or "ld" in d.config.get("extras", {})]
    if not supported_devices: raise RuntimeError("未找到支持的模拟器 (MuMu, 雷电)。")
    device = supported_devices[0]
    logging.info(f"正在使用设备: {device.name} at {device.address}")
    maacontroller = AdbController(adb_path=device.adb_path, address=device.address, config=device.config)
    if not maacontroller.post_connection().wait().succeeded:
        raise RuntimeError(f"连接控制器到设备 {device.name} 失败。")
    maatasker.bind(maaresource, maacontroller)
    if not maatasker.inited: raise RuntimeError("初始化 MAA 任务模块失败。")
    logging.info("MAA 初始化成功。")


def init_player_and_mnt():
    global current_player, mnt
    if not device: raise RuntimeError("在初始化播放器之前 MAA 设备尚未初始化。")
    extra_config = device.config["extras"]
    if "mumu" in extra_config:
        type_, config_key = "mumu", "mumu"
        if "v4" in device.name or "v5" in device.name: type_ += device.name[-2:]
    elif "ld" in extra_config:
        type_, config_key = "ld", "ld"
    else:
        raise RuntimeError(f"不支持的模拟器类型: {list(extra_config.keys())}")
    player_config = extra_config[config_key]
    current_player = player.Player(type_, Path(player_config["path"]), player_config["index"])
    mnt = MNT(
        device.address, type_="EvATive7", communicate_type=MNTServerCommunicateType.STDIO,
        mnt_asset_path=Path("./assets/minitouch_EvATive7"), callback=mnt_callback,
        adb_executor=str(device.adb_path.absolute()),
    )
    logging.info(f"{type_} 播放器和 Minitouch 初始化成功。")


# =================================================================
# ===================== MAA 自定义模块区域 =====================
# =================================================================

@maaresource.custom_recognition("UISongRecognitionMedley")
class UISongRecognitionMedley(CustomRecognition):
    def analyze(self, context: Context, argv: CustomRecognition.AnalyzeArg):
        roi = [110, 545, 368, 29] # 这里的ROI可能需要根据实际情况微调
        def ocr_and_match(model=None):
            try:
                pipeline = {"_ocr_song": {"recognition": "OCR", "roi": roi, "only_rec": True}}
                if model: pipeline["_ocr_song"]["model"] = model
                ocr_text = context.run_recognition("_ocr_song", argv.image, pipeline).best_result.text
                logging.info(f"OCR ({model or 'default'}) 原始文本: '{ocr_text}'")
                match = fuzzy_match_song(ocr_text)
                logging.info(f"模糊匹配结果 ({model or 'default'}): {match}")
                return match
            except Exception as e:
                logging.error(f"OCR ({model or 'default'}) 执行失败: {e}"); return None
        results = [m for m in [ocr_and_match("ppocr_v3/ja_jp"), ocr_and_match()] if m]
        if not results: return self.AnalyzeResult(None, "")
        best_match = max(results, key=lambda x: x[1])
        if best_match and best_match[1] > 50:
            song_name = "[FULL] " + best_match[0] if IS_FULL_SONG else best_match[0]
            logging.info(f"歌曲识别成功: '{song_name}' (置信度: {best_match[1]}%)")
            return self.AnalyzeResult(roi, song_name)
        return self.AnalyzeResult(None, "")


@maaresource.custom_recognition("UISongRecognition")
class UISongRecognition(CustomRecognition):
    def analyze(self, context: Context, argv: CustomRecognition.AnalyzeArg):
        roi = [200, 332, 368, 29]  # 这里的ROI可能需要根据实际情况微调

        def ocr_and_match(model=None):
            try:
                pipeline = {"_ocr_song": {"recognition": "OCR", "roi": roi, "only_rec": True}}
                if model: pipeline["_ocr_song"]["model"] = model
                ocr_text = context.run_recognition("_ocr_song", argv.image, pipeline).best_result.text
                logging.info(f"OCR ({model or 'default'}) 原始文本: '{ocr_text}'")
                if "FULL" in ocr_text and not IS_FULL_SONG:
                    return None
                match = fuzzy_match_song(ocr_text)
                logging.info(f"模糊匹配结果 ({model or 'default'}): {match}")
                return match
            except Exception as e:
                logging.error(f"OCR ({model or 'default'}) 执行失败: {e}");
                return None

        results = [m for m in [ocr_and_match("ppocr_v3/ja_jp"), ocr_and_match()] if m]
        if not results: return self.AnalyzeResult(None, "")

        best_match = max(results, key=lambda x: x[1])

        if best_match and best_match[1] > 50:
            matched_song_name = best_match[0]
            # 仅在非FULL歌曲或IS_FULL_SONG为True时继续
            song_name_to_return = "[FULL] " + matched_song_name if IS_FULL_SONG else matched_song_name
            logging.info(f"歌曲识别成功: '{song_name_to_return}' (置信度: {best_match[1]}%)")
            return self.AnalyzeResult(roi, song_name_to_return)

        return self.AnalyzeResult(None, "")

@maaresource.custom_action("UISaveSong")
class UISaveSong(CustomAction):
    def run(self, context, argv):
        save_song(argv.reco_detail.best_result.detail); return self.RunResult(True)

@maaresource.custom_action("UIPlay")
class UIPlay(CustomAction):
    def run(self, context, argv):
        try:
            play_song(); return self.RunResult(True)
        except Exception as e:
            logging.error(f"歌曲演奏期间出错: {e}", exc_info=True); return self.RunResult(False)


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

        # ==================== 新增：保存ROI调试截图的逻辑 ====================
        try:
            # 1. 创建一个截图副本用于绘制，以免影响原始图像
            image_to_save = argv.image.copy()

            # 定义绘制参数
            box_color = (0, 255, 0)  # 绿色方框
            box_thickness = 2
            font = cv2.FONT_HERSHEY_SIMPLEX
            font_scale = 0.5
            font_color = (255, 255, 255)  # 白色文字
            font_thickness = 1

            # 2. 遍历所有ROI并在图上绘制
            for name, data in types.items():
                roi = data['roi']
                x, y, w, h = roi

                # 绘制方框
                pt1 = (x, y)
                pt2 = (x + w, y + h)
                cv2.rectangle(image_to_save, pt1, pt2, box_color, box_thickness)

                # 在方框左上角上方添加文字标签
                text_pos = (x, y - 10)
                cv2.putText(image_to_save, name, text_pos, font, font_scale, font_color, font_thickness)

            # 3. 确保 debug 目录存在
            debug_dir = Path("debug")
            debug_dir.mkdir(exist_ok=True)

            # 4. 生成唯一文件名并保存
            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            filename = f"UIPlayResult_ROI_{timestamp}.png"
            filepath = debug_dir / filename

            cv2.imwrite(str(filepath), image_to_save)
            logging.info(f"成功保存ROI调试截图至: {filepath}")

        except Exception as e:
            logging.error(f"保存ROI调试截图时发生错误: {e}")
        # ========================== 调试代码结束 ==========================

        # --- 原有的OCR识别逻辑保持不变 ---
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
        global play_failed_times

        # 修复：简化参数处理，直接判断succeed的值
        try:
            param_data = json.loads(argv.custom_action_param)
            succeed = param_data.get("succeed", False)
        except (json.JSONDecodeError, AttributeError):
            # 如果参数不是预期的JSON格式，则默认失败
            succeed = False

        play_result = {}
        if succeed and argv.reco_detail and argv.reco_detail.best_result:
            play_result = json.loads(argv.reco_detail.best_result.detail)

        if not succeed:
            play_failed_times += 1

        PlayRecord.create(
            play_time=int(time.time()), play_offset=OFFSET, result=play_result,
            succeed=succeed, chart_id=current_song_id, difficulty=DIFFICULTY,
        )
        if play_failed_times >= MAX_FAILED_TIMES:
            logging.error(f"失败次数达到上限 ({MAX_FAILED_TIMES})，自动停止。")
            context.run_action("stop")
        return self.RunResult(True)


# =================================================================
# ===================== 画面串流相关 (不变) =====================
# =================================================================
def _stream_loop(socketio):
    while streaming_active.is_set():
        try:
            if not current_player: time.sleep(1); continue
            with stream_settings_lock: fps, res_width = STREAM_SETTINGS["fps"], STREAM_SETTINGS["resolution"]
            img_bgr = current_player.ipc_capture_display()
            if img_bgr is None: time.sleep(1); continue
            img_rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
            h, w = img_rgb.shape[:2]
            thumb = cv2.resize(img_rgb, (res_width, int(res_width * (h / w))))
            _, buffer = cv2.imencode(".jpg", thumb, [cv2.IMWRITE_JPEG_QUALITY, 80])
            socketio.emit("update_frame", {"image": base64.b64encode(buffer).decode("utf-8")})
            time.sleep(1 / fps)
        except Exception as e:
            logging.error(f"画面传输线程出错: {e}"); time.sleep(1)

def update_stream_settings(settings):
    with stream_settings_lock:
        STREAM_SETTINGS["fps"] = int(settings.get("fps", 1))
        STREAM_SETTINGS["resolution"] = int(settings.get("resolution", 480))

def start_streaming(socketio):
    global streaming_thread
    if not streaming_thread or not streaming_thread.is_alive():
        streaming_active.set()
        streaming_thread = threading.Thread(target=_stream_loop, args=(socketio,)); streaming_thread.daemon = True; streaming_thread.start()

def stop_streaming():
    global streaming_thread
    streaming_active.clear()
    if streaming_thread and streaming_thread.is_alive(): streaming_thread.join(timeout=1)
    streaming_thread = None


# =================================================================
# ===================== 任务启动函数区域 =====================
# =================================================================
def init(log_callback=None):
    try:
        init_maa(); init_player_and_mnt()
    except Exception as e:
        raise e

def run_simplified_autodori(config_data):
    """单曲模式：只打一首歌就停止。"""
    global DIFFICULTY, IS_FULL_SONG, HUMAN_DELAY_ENABLED
    DIFFICULTY = config_data.get("difficulty", "expert")
    IS_FULL_SONG = config_data.get("is_full_song", False)
    HUMAN_DELAY_ENABLED = config_data.get("human_delay", False)
    if not maacontroller or not mnt: raise RuntimeError("MAA is not initialized.")
    override_pipeline = {
        "ui_simplified_entry": {"recognition": "Custom", "custom_recognition": "UISongRecognitionMedley", "action": "Custom", "custom_action": "UISaveSong", "next": ["startlive"], "timeout": 15000, "on_error": ["stop"]},
        "startlive": {"action": "Click", "next": ["playsong"], "on_error": ["stop"], "recognition": "TemplateMatch", "template": "live/button/live_medley.png", "threshold": 0.5},
        "playsong": {"action": "Custom", "custom_action": "UIPlay", "next": ["stop"], "timeout": 500000},
    }
    logging.info("正在提交【单曲模式】自动演奏任务...")
    maatasker.post_task("ui_simplified_entry", override_pipeline).wait()
    logging.info("【单曲模式】自动演奏任务完成。")


def run_full_auto_mode(config_data):
    """【V10 结算等待优化版】全自动模式，增加等待结算界面出现的步骤以提高稳定性。"""
    global DIFFICULTY, IS_FULL_SONG, HUMAN_DELAY_ENABLED, play_failed_times
    DIFFICULTY = config_data.get("difficulty", "expert")
    IS_FULL_SONG = config_data.get("is_full_song", False)
    HUMAN_DELAY_ENABLED = config_data.get("human_delay", False)
    use_random_song = config_data.get("use_random_song", True)
    play_failed_times = 0
    if not maacontroller or not mnt: raise RuntimeError("MAA is not initialized.")

    # --- 加载 pipeline 定义文件 ---
    pipeline_def_path = Path("assets/resource/pipeline")
    with open(pipeline_def_path / "live.json", 'r', encoding='utf-8') as f:
        live_pipeline_def = json.load(f)
    with open(pipeline_def_path / "common.json", 'r', encoding='utf-8') as f:
        common_pipeline_def = json.load(f)

    # --- 构建复刻原始JSON逻辑的自动化任务流 ---
    global_interrupts = ["login_expired_action", "connect_failed_action"]
    result_screen_interrupts = [
        *global_interrupts, "next_button", "ok_button",
        "close_button", "confirm_button", "read_after"
    ]

    override_pipeline = {
        # --- 选曲到开始的流程保持不变 ---
        "select_song_entry": {
            **live_pipeline_def["select_song"],
            "next": ["get_song_name", "random_choice_song_action"],
            "interrupt": global_interrupts
        },
        "get_song_name": {
            "recognition": "Custom", "custom_recognition": "UISongRecognition",
            "action": "Custom", "custom_action": "UISaveSong",
            "next": ["click_confirm_on_song_select"],
            "timeout": 15000,
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
            "interrupt": global_interrupts
        },
        "disable_liveplay": {
            **live_pipeline_def["disable_liveplay"],
        },
        "startlive": {
            **live_pipeline_def["startlive"],
            "next": ["playsong"]
        },

        # --- 演奏及后续流程修改 ---
        "playsong": {
            "action": "Custom", "custom_action": "UIPlay",
            "next": ["wait_playresult"],  # 核心修改: 演奏后先进入等待步骤
            "timeout": 500000,
            "on_error": ["save_failed_result"],
            "interrupt": global_interrupts
        },

        # 新增步骤1: 等待结算界面图片出现
        "wait_playresult": {
            **live_pipeline_def["wait_playresult"],
            # live.json 中此项 next 为 wait_playresult1，我们直接沿用
        },

        # 新增步骤2: 再次确认界面，确保稳定
        "wait_playresult1": {
            **live_pipeline_def["wait_playresult1"],
            "next": ["get_result"]  # 确认稳定后，才去获取结果
        },

        # 获取结果的步骤现在由等待步骤触发
        "get_result": {
            "recognition": "Custom", "custom_recognition": "UIPlayResult",
            "action": "Custom", "custom_action": "UISavePlayResult",
            "custom_action_param": json.dumps({"succeed": True}),
            "next": ["liveagain"],
            "timeout": 30000,
            "on_error": ["stop"],
            "interrupt": result_screen_interrupts
        },
        "save_failed_result": {
            "action": "Custom", "custom_action": "UISavePlayResult",
            "custom_action_param": json.dumps({"succeed": False}),
            "next": ["liveagain"],
        },
        "liveagain": {
            **live_pipeline_def["liveagain"],
            "next": ["select_song_entry"],
            "interrupt": result_screen_interrupts
        },

        # --- 错误处理部分 ---
        "login_expired_action": {
            **common_pipeline_def["login_expired"],
            "next": ["stop"]
        },
        "connect_failed_action": {
            **common_pipeline_def["connect_failed"],
            "action": "StopTask",
            "next": ["stop"]
        },
    }
    override_pipeline.update(common_pipeline_def)

    logging.info("正在提交【V10-结算等待优化版】自动演奏任务...")
    maatasker.post_task("select_song_entry", override_pipeline).wait()
    logging.info("【V10-结算等待优化版】自动演奏任务完成或已停止。")