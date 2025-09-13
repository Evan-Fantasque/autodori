# autodori_ui.py

import random
import re
import string
import subprocess
import threading
from logging.handlers import QueueHandler
from typing import Optional, Union

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

# 导入项目内的模块
import player
from api import BestdoriAPI
from chart import Chart, PlayRecord
from util import *

# ---- 模块级变量和常量 ----
MIN_LIVEBOOST = 1
LIVEMODE = "freelive"
DIFFICULTY = "hard"
OFFSET = {"up": 0, "down": 0, "move": 0, "wait": 0.0, "interval": 0.0}
PHOTOGATE_LATENCY = 30
DEFAULT_MOVE_SLICE_SIZE = 10
MAX_FAILED_TIMES = 10
CMD_SLICE_SIZE = 100

config_path = Path("data/config.yml")
if not config_path.exists():
    config_path.touch()
    config_path.write_text("{}", encoding="utf-8")
config = yaml.safe_load(config_path.read_text(encoding="utf-8"))

# MAA 全局对象
maaresource = Resource()
maatasker: Optional[Tasker] = None
maacontroller: Optional[AdbController] = None
device: Optional[AdbDevice] = None

# 状态变量
current_player: Optional[player.Player] = None
current_orientation: int = 0
mnt: Optional[MNT] = None
all_songs: dict = BestdoriAPI.get_song_list()
all_song_name_indexes: dict[str, str] = {
    list(filter(lambda title: title is not None, sinfo["musicTitle"]))[0]: sid
    for sid, sinfo in all_songs.items()
}
current_song_name: Optional[str] = None
current_song_id: Optional[str] = None
current_chart: Optional[Chart] = None
play_failed_times: int = 0
callback_data: dict = {}
callback_data_lock = threading.Lock()
cmd_log_list: list[MNTEvATive7LogEventData] = []
cmd_log_list_lock = threading.Lock()

# ---- 用于从一次性任务中传回结果的全局变量 ----
_one_off_ocr_result: Optional[str] = None


# ---- MAA 自定义识别与动作 (与原文件一致，未作修改) ----

@maaresource.custom_recognition("SongRecognition")
class SongRecognition(CustomRecognition):
    def analyze(
            self, context: Context, argv: CustomRecognition.AnalyzeArg
    ) -> Union[CustomRecognition.AnalyzeResult, Optional[RectType]]:

        roi = [200, 332, 368, 29]

        def match(model=None):
            pplname = "_ocrsong_" + "".join(random.choices(string.ascii_lowercase, k=7))
            pipeline = {
                pplname: {
                    "recognition": "OCR",
                    "only_rec": True,
                    "roi": roi,
                },
            }
            if model != None:
                pipeline[pplname]["model"] = model
            try:
                song_fuzzyname = context.run_recognition(
                    pplname,
                    argv.image,
                    pipeline,
                ).best_result.text
            except:
                song_fuzzyname = ""
            return fuzzy_match_song(song_fuzzyname)

        jpmatch = match("ppocr_v3/ja_jp")
        commonmatch = match()
        logging.debug(
            "Match result with ppocr_v3/ja_jp: {}, Match result with default: {}".format(
                jpmatch, commonmatch
            )
        )
        result = sorted([jpmatch, commonmatch], key=lambda x: x[1], reverse=True)
        if all([r[1] < 50 for r in result]):
            return CustomRecognition.AnalyzeResult(None, "")
        result_music_name = result[0][0]

        if not check_song_available(
                result_music_name, all_song_name_indexes[result_music_name], DIFFICULTY
        ):
            return CustomRecognition.AnalyzeResult(None, "")

        return CustomRecognition.AnalyzeResult(roi, result_music_name)


@maaresource.custom_recognition("LiveBoostEnoughRecognition")
class LiveBoostEnoughRecognition(CustomRecognition):
    def analyze(
            self, context: Context, argv: CustomRecognition.AnalyzeArg
    ) -> Union[CustomRecognition.AnalyzeResult, Optional[RectType]]:
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
            global current_song_id, play_failed_times
            succeed: bool = json.loads(argv.custom_action_param).get("succeed")
            if succeed:
                playresult = argv.reco_detail.best_result.detail
                if isinstance(playresult, str):
                    playresult = json.loads(argv.reco_detail.best_result.detail)
            else:
                play_failed_times += 1
                playresult = {}
            PlayRecord.create(
                play_time=int(time.time()),
                play_offset=OFFSET,
                result=playresult,
                succeed=succeed,
                chart_id=current_song_id,
                difficulty=DIFFICULTY,
            )
            if play_failed_times >= MAX_FAILED_TIMES:
                logging.error("Failed attempts exceed max failed times")
                context.run_action("close_app")
                context.run_action("stop")
            return CustomAction.RunResult(True)
        except Exception as e:
            logging.error(f"Failed to save play result: {e}")
            return CustomAction.RunResult(False)


@maaresource.custom_action("Play")
class Play(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg):
        try:
            play_song()
            return CustomAction.RunResult(True)
        except Exception as e:
            logging.error(f"Failed when play song: {e}", stack_info=True)
            return CustomAction.RunResult(False)


@maaresource.custom_action("SaveSong")
class SaveSong(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg):
        name: CustomRecognitionResult = argv.reco_detail.best_result.detail
        save_song(name)
        return CustomAction.RunResult(True)


@maaresource.custom_action("StoreRecognitionResult")
class StoreRecognitionResult(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg):
        global _one_off_ocr_result
        if argv.reco_detail and argv.reco_detail.best_result:
            _one_off_ocr_result = argv.reco_detail.best_result.text
        else:
            _one_off_ocr_result = ""
        return CustomAction.RunResult(True)


# ---- 核心功能函数 (与原文件一致，未作修改) ----

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


def check_song_available(name, id_, difficulty):
    if name.startswith("[FULL]"):
        return False

    lastmatched = PlayRecord.get_or_none(chart_id=id_, difficulty=difficulty)
    if lastmatched:
        if not lastmatched.succeed:
            return True
    return True


def fuzzy_match_song(name):
    return fzwzprocess.extractOne(name, list(all_song_name_indexes.keys()))


def _get_orientation():
    try:
        command_list = [
            str(device.adb_path.absolute()),
            "-s",
            device.address,
            "shell",
            "dumpsys input|grep SurfaceOrientation",
        ]
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
    current_chart.notes_to_actions(current_player.resolution, DEFAULT_MOVE_SLICE_SIZE)
    current_orientation = _get_orientation()
    current_chart.actions_to_MNTcmd(
        (mnt.max_x, mnt.max_y), current_orientation, OFFSET, CMD_SLICE_SIZE
    )
    logging.debug("Save song: {}".format(name))


def play_song():
    logging.info("Start play")
    cmd_log_list.clear()
    reset_callback_data()

    def _get_wait_time():
        wait_for = 0.0
        index = current_chart.actions_to_cmd_index
        for action in current_chart.actions[index - CMD_SLICE_SIZE: index]:
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

    wait_first_note()

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
    time.sleep(2)


def wait_first_note():
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
                change_score = np.sum(cur_color[0:3] - last_color[0:3])
                logging.debug(f"Picture changed: {change_score}")
                if change_score > 3:
                    if freezed:
                        logging.debug(
                            f"The first note falls between {from_row}-{to_row}"
                        )
                        time.sleep(PHOTOGATE_LATENCY / 1000)
                        break
                else:
                    if not freezed:
                        waited_frames += 1

                if not freezed and waited_frames >= 200:
                    freezed = True
                    logging.debug("Picture freezed, waiting for the first note...")

            last_color = cur_color
        except Exception as e:
            logging.error(f"Failed to get screen: {e}")


# ---- 初始化与销毁 ----

def destroy_maa_instances():
    """销毁并重置所有 MAA 和 MNT 全局实例"""
    global maacontroller, device, mnt, maatasker
    logging.info("Destroying and resetting MAA/MNT instances...")

    if mnt and mnt.is_running:
        try:
            mnt.stop()
            logging.info("MNT server stopped.")
        except Exception as e:
            logging.error(f"Error while stopping MNT server: {e}")

    if maatasker:
        maatasker.stop()

    maacontroller = None
    device = None
    mnt = None
    maatasker = None

    logging.info("MAA/MNT instances have been reset.")


def init_maa():
    global device, maacontroller, maatasker

    # 每次都创建一个新的Tasker实例以保证环境干净
    maatasker = Tasker()

    user_path = "./"
    resource_path = "assets/resource"

    res_job = maaresource.post_bundle(resource_path)
    res_job.wait()

    Toolkit.init_option(user_path)
    for i in range(3):
        adb_devices = Toolkit.find_adb_devices()
        if adb_devices:
            break
    if not adb_devices:
        raise RuntimeError("No ADB device found.")

    _device: list[AdbDevice] = []
    for d in adb_devices:
        extra_names = d.config.get("extras", {}).keys()
        if "mumu" in extra_names or "ld" in extra_names:
            if (d.name, d.address) not in [
                (dev.name, dev.address) for dev in _device
            ]:
                _device.append(d)

    filter_str = config.get("device", {}).get("filter", "devices")
    _device = eval(filter_str, {}, {"devices": _device})

    if not _device:
        raise RuntimeError("No supported devices were found.")
    elif len(_device) == 1:
        device = _device[0]
    else:
        print("Multiple devices were found:")
        for i, d in enumerate(_device):
            print(f"{i}: {d.name}({d.address})")
        selected = input("Select a device: ")
        device = _device[int(selected)]

    maacontroller = AdbController(
        adb_path=device.adb_path, address=device.address,
        screencap_methods=device.screencap_methods,
        input_methods=device.input_methods, config=device.config,
    )

    for i in range(3):
        if maacontroller.post_connection().wait().succeeded:
            break
    else:
        raise RuntimeError("Failed to connect to device.")

    maatasker.bind(maaresource, maacontroller)

    if not maatasker.inited:
        raise RuntimeError("Failed to init MAA.")

    logging.info("MAA inited.")


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
            if (last_cmd_endtime := callback_data.get("last_cmd_endtime", -1)) != -1:
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


def init_player_and_mnt():
    global current_player, mnt

    extra_config = device.config["extras"]
    if "mumu" in extra_config.keys():
        extra_config = extra_config["mumu"]
        type_ = "mumu"
        if device.name == "MuMuPlayer12":
            type_ += "v4"
        if device.name == "MuMuPlayer12 v5":
            type_ += "v5"
    elif "ld" in extra_config.keys():
        extra_config = extra_config["ld"]
        type_ = "ld"
    else:
        raise RuntimeError("Unsupported device type")

    path = extra_config["path"]
    index = extra_config["index"]

    current_player = player.Player(type_, Path(path), index)
    mnt = MNT(
        device.address,
        type_="EvATive7",
        communicate_type=MNTServerCommunicateType.STDIO,
        mnt_asset_path=Path("./assets/minitouch_EvATive7"),
        callback=mnt_callback,
        adb_executor=str(device.adb_path.absolute()),
    )
    logging.info("Player and MNT inited.")


def _get_override_pipeline():
    all_pipelines = {}
    difficulty: str = DIFFICULTY
    roi = {
        "easy": [659, 495, 107, 97], "normal": [768, 494, 107, 97],
        "hard": [886, 494, 105, 97], "expert": [996, 493, 107, 97],
        "special": [1086, 449, 192, 184],
    }[difficulty]
    all_pipelines["set_difficulty"] = {
        "action": "Click", "recognition": "TemplateMatch",
        "template": [f"live/difficulty/{difficulty}_active.png", f"live/difficulty/{difficulty}_inactive.png"],
        "next": "get_song_name", "target": roi, "timeout": 5000, "interrupt": ["random_choice_song"],
    }
    livemode_pipeline = {
        "recognition": "OCR", "expected": "", "roi": [679, 183, 257, 354], "action": "Click",
        "post_delay": 1000, "next": ["select_song", "select_live_mode", "live_home_button"],
        "interrupt": ["login_expired", "connect_failed"],
    }
    if LIVEMODE == "freelive":
        livemode_pipeline["expected"] = "自由演出"
    elif LIVEMODE == "challengelive":
        livemode_pipeline["expected"] = "挑战演出"
    all_pipelines["select_live_mode"] = livemode_pipeline
    return all_pipelines


# ---- 控制器类 (已重构)----
class AutodoriController:
    def __init__(self, log_queue):
        self.log_queue = log_queue
        self.autodori_thread: Optional[threading.Thread] = None
        self.current_status = "idle"
        self.resource_lock = threading.Lock()  # 用于保护MAA初始化过程
        self._setup_logging()

    def _setup_logging(self):
        queue_handler = QueueHandler(self.log_queue)
        # 获取根logger，以便捕获所有子模块的日志
        root_logger = logging.getLogger()
        if not any(isinstance(h, QueueHandler) for h in root_logger.handlers):
            root_logger.addHandler(queue_handler)
        root_logger.setLevel(logging.INFO)

    def get_status(self):
        is_alive = self.autodori_thread and self.autodori_thread.is_alive()
        if not is_alive and self.current_status in ["running", "stopping"]:
            self.current_status = "error"  # 如果线程意外死亡，状态置为error
        return {"status": self.current_status, "is_alive": is_alive}

    def _ensure_maa_initialized(self):
        """确保MAA核心资源只被初始化一次"""
        with self.resource_lock:
            if maacontroller is None:
                logging.info("MAA core resources are not initialized. Initializing...")
                init_maa()
                logging.info("MAA core resources initialized successfully.")
        return True

    def start_full_auto(self, config):
        if self.autodori_thread and self.autodori_thread.is_alive():
            return False, "任务已经在运行中。"

        try:
            self._ensure_maa_initialized()
        except Exception as e:
            logging.error(f"Failed to initialize MAA resources: {e}", exc_info=True)
            return False, f"初始化MAA失败: {e}"

        self.autodori_thread = threading.Thread(target=self._run_full_auto_task, args=(config,))
        self.autodori_thread.start()
        return True, "全自动任务已启动。"

    def start_semi_auto(self, config):
        if self.autodori_thread and self.autodori_thread.is_alive():
            return False, "任务已经在运行中。"

        try:
            self._ensure_maa_initialized()
        except Exception as e:
            logging.error(f"Failed to initialize MAA resources: {e}", exc_info=True)
            return False, f"初始化MAA失败: {e}"

        self.autodori_thread = threading.Thread(target=self._run_semi_auto_task, args=(config,))
        self.autodori_thread.start()
        return True, "半自动任务已启动。"

    def stop(self):
        if not (self.autodori_thread and self.autodori_thread.is_alive()):
            return False, "当前没有正在运行的任务。"

        logging.info("Received stop request...")
        self.current_status = "stopping"
        if maatasker:
            maatasker.stop()  # 发送停止信号
            logging.info("MAA tasker stop signal sent.")
        return True, "正在停止任务..."

    def recognize_song_from_image(self, image: Image.Image):
        if self.autodori_thread and self.autodori_thread.is_alive():
            return {"success": False, "message": "任务正在运行中，请先停止任务再进行识别。"}

        is_temp_instance = False
        with self.resource_lock:
            if maacontroller is None:
                try:
                    logging.info("Creating temporary MAA instance for clipboard OCR...")
                    init_maa()
                    is_temp_instance = True
                except Exception as e:
                    logging.error(f"Failed to create temporary MAA instance for OCR: {e}", exc_info=True)
                    return {"success": False, "message": f"创建临时MAA实例失败: {e}"}

        original_screencap = None
        try:
            global _one_off_ocr_result
            _one_off_ocr_result = None  # 每次调用前重置

            img_array = np.array(image.convert("RGB"))

            if not maacontroller:
                raise RuntimeError("MAA controller not initialized.")
            original_screencap = maacontroller.screencap
            maacontroller.screencap = lambda: img_array

            temp_pipeline = {
                "entry": {
                    "recognition": "OCR",
                    "roi": [0, 0, image.width, image.height],
                    "next": "store_result"
                },
                "store_result": {"action": "StoreRecognitionResult", "next": "stop"}
            }

            logging.info("Performing OCR on clipboard image via temporary task...")
            maatasker.post_task("entry", temp_pipeline).wait().get()

            ocr_text = _one_off_ocr_result

            if ocr_text is None or not ocr_text.strip():
                return {"success": False, "message": "OCR未能识别出任何文字。"}

            logging.info(f"OCR result: '{ocr_text}'")
            matched_song, score = fuzzy_match_song(ocr_text)

            if score < 80:
                return {"success": False,
                        "message": f"未能为 '{ocr_text}' 找到合适的匹配. 最佳匹配: {matched_song} ({score}%)"}

            return {"success": True, "song_name": matched_song}

        except Exception as e:
            logging.error(f"从图像识别歌曲时出错: {e}", exc_info=True)
            return {"success": False, "message": str(e)}
        finally:
            if maacontroller and original_screencap:
                maacontroller.screencap = original_screencap
            if is_temp_instance:
                logging.info("Destroying temporary MAA instance for OCR.")
                destroy_maa_instances()

    def _run_full_auto_task(self, config):
        global DIFFICULTY, LIVEMODE, MIN_LIVEBOOST, mnt
        try:
            self.current_status = "running"

            # -- 每轮任务独立的资源初始化 --
            reset_callback_data()
            init_player_and_mnt()

            # -- 设置本轮任务的参数 --
            DIFFICULTY = config["difficulty"]
            LIVEMODE = config["livemode"]
            MIN_LIVEBOOST = int(config["liveboost"])

            logging.info(f"Starting full-auto task with config: {config}")
            maatasker.post_task("main", _get_override_pipeline()).wait().get()

            if self.current_status == "running":  # 如果任务正常结束（而不是被中途停止）
                self.current_status = "stopped"
                logging.info("Full-auto task finished successfully.")

        except Exception as e:
            logging.error(f"Autodori full-auto task failed with an exception: {e}", exc_info=True)
            self.current_status = "error"
        finally:
            # -- 每轮任务独立的资源清理 --
            if mnt and mnt.is_running:
                mnt.stop()
                logging.info("MNT server for the task has been stopped.")

            if self.current_status == "stopping":
                self.current_status = "stopped"
                logging.info("Full-auto task has been stopped by user.")

    def _run_semi_auto_task(self, config):
        global DIFFICULTY, mnt
        try:
            self.current_status = "running"

            # -- 每轮任务独立的资源初始化 --
            reset_callback_data()
            init_player_and_mnt()

            # -- 设置本轮任务的参数 --
            DIFFICULTY = config["difficulty"]
            song_name = config["song_name"]

            logging.info(f"Starting semi-auto task for song: {song_name}, difficulty: {DIFFICULTY}")
            save_song(song_name)
            maatasker.post_task("semiauto_start_live").wait().get()

            if self.current_status == "running":
                self.current_status = "stopped"
                logging.info("Semi-auto task finished successfully.")

        except Exception as e:
            logging.error(f"Autodori semi-auto task failed with an exception: {e}", exc_info=True)
            self.current_status = "error"
        finally:
            # -- 每轮任务独立的资源清理 --
            if mnt and mnt.is_running:
                mnt.stop()
                logging.info("MNT server for the task has been stopped.")

            if self.current_status == "stopping":
                self.current_status = "stopped"
                logging.info("Semi-auto task has been stopped by user.")
