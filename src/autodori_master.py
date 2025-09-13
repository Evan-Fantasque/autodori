# This file is a refactored version of the original autodori.py to be used as a module.
import random
import re
import string
import subprocess
import threading
from pathlib import Path
from typing import Optional, Union

data_path = Path("data")
data_path.mkdir(exist_ok=True)
cache_path = Path("cache")
cache_path.mkdir(exist_ok=True)
config_path = Path("data/config.yml")
Path("debug").mkdir(exist_ok=True)
if not config_path.exists():
    config_path.touch()
    config_path.write_text("{}", encoding="utf-8")

from fuzzywuzzy import process as fzwzprocess
from maa.context import Context
from maa.controller import AdbController
from maa.custom_action import CustomAction
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

# Global scope variables that might be modified by the class instance
config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
maaresource = Resource()
maatasker = Tasker()


class AutoDoriMaster:
    def __init__(self, logger_queue):
        self.logger_queue = logger_queue

        # Instance variables
        self.MIN_LIVEBOOST = 1
        self.LIVEMODE = "freelive"
        self.DIFFICULTY = "hard"
        self.OFFSET = {"up": 0, "down": 0, "move": 0, "wait": 0.0, "interval": 0.0}
        self.PHOTOGATE_LATENCY = 30
        self.DEFAULT_MOVE_SLICE_SIZE = 10
        self.MAX_FAILED_TIMES = 10
        self.CMD_SLICE_SIZE = 100

        self.maacontroller: AdbController = None
        self.device: AdbDevice = None
        self.current_player: player.Player = None
        self.current_orientation: int = 0
        self.mnt: MNT = None
        self.all_songs: dict = BestdoriAPI.get_song_list()
        self.all_song_name_indexes: dict[str, str] = {
            list(filter(lambda title: title is not None, sinfo["musicTitle"]))[0]: sid
            for sid, sinfo in self.all_songs.items()
        }
        self.current_song_name: str = None
        self.current_song_id: str = None
        self.current_chart: Chart = None
        self.play_failed_times: int = 0
        self.callback_data: dict = {}
        self.callback_data_lock = threading.Lock()
        self.cmd_log_list: list[MNTEvATive7LogEventData] = []
        self.cmd_log_list_lock = threading.Lock()
        self.current_version = None

        self._stop_requested = threading.Event()
        self.reset_callback_data()
        self._register_custom_components()

    def reset_callback_data(self):
        self.callback_data = {
            "wait": {"total": 0, "total_offset": 0.0},
            "move": {"uncommited": 0, "total": 0, "total_offset": 0.0},
            "up": {"uncommited": 0, "total": 0, "total_offset": 0.0},
            "down": {"uncommited": 0, "total": 0, "total_offset": 0.0},
            "interval": {"total": 0, "total_offset": 0.0},
            "last_cmd_endtime": -1,
        }

    def _register_custom_components(self):
        # Pass self to custom actions/recognitions so they can access instance state
        SongRecognition.master = self
        maaresource.custom_recognition("SongRecognition")(SongRecognition)
        LiveBoostEnoughRecognition.master = self
        maaresource.custom_recognition("LiveBoostEnoughRecognition")(LiveBoostEnoughRecognition)
        HandleLiveBoost.master = self
        maaresource.custom_action("HandleLiveBoost")(HandleLiveBoost)
        PlayResultRecognition.master = self
        maaresource.custom_recognition("PlayResultRecognition")(PlayResultRecognition)
        SavePlayResult.master = self
        maaresource.custom_action("SavePlayResult")(SavePlayResult)
        Play.master = self
        maaresource.custom_action("Play")(Play)
        SaveSong.master = self
        maaresource.custom_action("SaveSong")(SaveSong)

    def initialize(self):
        """Initializes MAA, MNT, and the player."""
        self.init_maa()
        self.init_player_and_mnt()

    def run(self, difficulty, livemode, min_liveboost):
        """Starts the main automation task."""
        self.DIFFICULTY = difficulty
        self.LIVEMODE = livemode
        self.MIN_LIVEBOOST = min_liveboost

        logging.info(f"任务开始，难度: {self.DIFFICULTY}, 模式: {self.LIVEMODE}, 最低火量: {self.MIN_LIVEBOOST}")

        try:
            maatasker.post_task("main", self._get_override_pipeline()).wait().get()
        finally:
            if self.mnt:
                self.mnt.stop()
            logging.info("任务已结束。")

    def stop(self):
        """Requests to stop the current task."""
        logging.info("正在尝试停止 MAA 任务...")
        self._stop_requested.set()
        maatasker.stop()  # Use MAA's built-in stop function

    def init_maa(self):
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
            logging.fatal("未找到 ADB 设备。")
            raise ConnectionError("未找到 ADB 设备。")

        _device: list[AdbDevice] = []
        for device in adb_devices:
            extra_names = device.config.get("extras", {}).keys()
            if "mumu" in extra_names or "ld" in extra_names:
                if (device.name, device.address) not in [
                    (d.name, d.address) for d in _device
                ]:
                    _device.append(device)
        filter_str = config.get("device", {}).get("filter", "devices")
        _device = eval(filter_str, {}, {"devices": _device})

        if not _device:
            logging.fatal("未找到支持的模拟器。")
            raise ConnectionError("未找到支持的模拟器。")
        elif len(_device) >= 1:
            self.device = _device[0]  # Default to first device
            logging.info(f"已自动选择设备: {self.device.name}({self.device.address})")

        self.maacontroller = AdbController(
            adb_path=self.device.adb_path,
            address=self.device.address,
            screencap_methods=self.device.screencap_methods,
            input_methods=self.device.input_methods,
            config=self.device.config,
        )

        for i in range(3):
            if self.maacontroller.post_connection().wait().succeeded:
                break

        maatasker.bind(maaresource, self.maacontroller)

        if not maatasker.inited:
            logging.fatal("MAA 初始化失败。")
            raise RuntimeError("MAA 初始化失败。")

        logging.info("MAA 初始化成功。")

    def init_player_and_mnt(self):
        extra_config = self.device.config["extras"]
        if "mumu" in extra_config.keys():
            extra_config = extra_config["mumu"]
            type_ = "mumu"
            if self.device.name == "MuMuPlayer12":
                type_ += "v4"
            if self.device.name == "MuMuPlayer12 v5":
                type_ += "v5"
        elif "ld" in extra_config.keys():
            extra_config = extra_config["ld"]
            type_ = "ld"

        path = extra_config["path"]
        index = extra_config["index"]

        self.current_player = player.Player(type_, Path(path), index)
        self.mnt = MNT(
            self.device.address,
            type_="EvATive7",
            communicate_type=MNTServerCommunicateType.STDIO,
            mnt_asset_path=Path("./assets/minitouch_EvATive7"),
            callback=self.mnt_callback,
            adb_executor=str(self.device.adb_path.absolute()),
        )
        logging.info("模拟器 IPC 和 MNT 初始化成功。")

    def mnt_callback(self, event: MNTEvent, data: MNTEventData):
        if event == MNTEvent.EVATIVE7_LOG:
            data: MNTEvATive7LogEventData = data
            cmd = data.cmd
            cost = data.cost
            with self.cmd_log_list_lock:
                self.cmd_log_list.append(data)
            cmd_type = cmd.split(" ")[0]
            with self.callback_data_lock:
                if (last_cmd_endtime := self.callback_data.get("last_cmd_endtime")) != -1:
                    self.callback_data["interval"]["total"] += 1
                    self.callback_data["interval"]["total_offset"] += (
                            data.start_time - last_cmd_endtime
                    )
                self.callback_data["last_cmd_endtime"] = data.end_time
                if cmd_type in ["w"]:
                    self.callback_data["wait"]["total"] += 1
                    self.callback_data["wait"]["total_offset"] += cost - int(cmd.split(" ")[-1])
                elif cmd_type in ["u", "d", "m"]:
                    type_ = {"u": "up", "d": "down", "m": "move"}[cmd_type]
                    self.callback_data[type_]["uncommited"] += 1
                    self.callback_data[type_]["total"] += 1
                    self.callback_data[type_]["total_offset"] += cost
                elif cmd_type in ["c"]:
                    total_uncommited = sum(self.callback_data[type_]["uncommited"] for type_ in ["up", "down", "move"])
                    if total_uncommited != 0:
                        for type_ in ["up", "down", "move"]:
                            self.callback_data[type_]["total_offset"] += cost * (
                                    self.callback_data[type_]["uncommited"] / total_uncommited
                            )
                            self.callback_data[type_]["uncommited"] = 0

    def _get_override_pipeline(self):
        all_pipelines = {}
        difficulty: str = self.DIFFICULTY
        roi = {
            "easy": [659, 495, 107, 97], "normal": [768, 494, 107, 97],
            "hard": [886, 494, 105, 97], "expert": [996, 493, 107, 97],
            "special": [1086, 449, 192, 184],
        }[difficulty]
        all_pipelines["set_difficulty"] = {
            "action": "Click", "recognition": "TemplateMatch",
            "template": [
                f"live/difficulty/{difficulty}_active.png",
                f"live/difficulty/{difficulty}_inactive.png",
            ],
            "next": "get_song_name", "target": roi, "timeout": 5000,
            "interrupt": ["random_choice_song"],
        }
        livemode_pipeline = {
            "recognition": "OCR", "expected": "", "roi": [679, 183, 257, 354],
            "action": "Click", "post_delay": 1000,
            "next": ["select_song", "select_live_mode", "live_home_button"],
            "interrupt": ["login_expired", "connect_failed"],
        }
        if self.LIVEMODE == "freelive":
            livemode_pipeline["expected"] = "自由演出"
        elif self.LIVEMODE == "challengelive":
            livemode_pipeline["expected"] = "挑战演出"
        all_pipelines["select_live_mode"] = livemode_pipeline
        return all_pipelines


# --- Custom Recognition and Action Classes ---
# Note: These are defined outside the master class but linked via the `master` class variable.

class SongRecognition(CustomRecognition):
    master: "AutoDoriMaster" = None

    def analyze(self, context: Context, argv: CustomRecognition.AnalyzeArg) -> Union[
        CustomRecognition.AnalyzeResult, Optional[RectType]]:
        roi = [200, 332, 368, 29]

        def match(model=None):
            pplname = "_ocrsong_" + "".join(random.choices(string.ascii_lowercase, k=7))
            pipeline = {pplname: {"recognition": "OCR", "only_rec": True, "roi": roi}}
            if model:
                pipeline[pplname]["model"] = model
            try:
                song_fuzzyname = context.run_recognition(pplname, argv.image, pipeline).best_result.text
            except:
                song_fuzzyname = ""
            return fzwzprocess.extractOne(song_fuzzyname, list(self.master.all_song_name_indexes.keys()))

        jpmatch = match("ppocr_v3/ja_jp")
        commonmatch = match()
        logging.debug(f"歌曲识别结果 (日文模型): {jpmatch}, (默认模型): {commonmatch}")
        result = sorted([jpmatch, commonmatch], key=lambda x: x[1], reverse=True)
        if all([r[1] < 50 for r in result]): return CustomRecognition.AnalyzeResult(None, "")
        result_music_name = result[0][0]

        song_id = self.master.all_song_name_indexes[result_music_name]
        last_played = PlayRecord.get_or_none(chart_id=song_id, difficulty=self.master.DIFFICULTY)
        if last_played and last_played.succeed:
            logging.info(f"歌曲 '{result_music_name}' 已成功完成过，跳过。")
            return CustomRecognition.AnalyzeResult(None, "")

        return CustomRecognition.AnalyzeResult(roi, result_music_name)


class LiveBoostEnoughRecognition(CustomRecognition):
    master: "AutoDoriMaster" = None

    def analyze(self, context: Context, argv: CustomRecognition.AnalyzeArg) -> Union[
        CustomRecognition.AnalyzeResult, Optional[RectType]]:
        roi = [979, 30, 61, 20]
        pipeline = {"live_boost_enough_ocr": {"recognition": "OCR", "only_rec": True, "roi": roi}}
        live_boost_text = context.run_recognition("live_boost_enough_ocr", argv.image, pipeline).best_result.text
        logging.debug(f"Live boost OCR 结果: {live_boost_text}")
        match = re.match(r"^\s*(\d+)\s*/", live_boost_text.replace(" ", ""))
        live_boost = int(match.group(1)) if match else -1
        logging.debug(f"当前 Live boost: {live_boost}")
        return CustomRecognition.AnalyzeResult(roi, str(live_boost))


class HandleLiveBoost(CustomAction):
    master: "AutoDoriMaster" = None

    def run(self, context: Context, argv: CustomAction.RunArg):
        liveboost = int(argv.reco_detail.best_result.detail)
        if liveboost < self.master.MIN_LIVEBOOST:
            logging.warning("Live boost 不足，准备退出。")
            context.run_action("close_app")
            context.run_action("stop")
        return CustomAction.RunResult(True)


class PlayResultRecognition(CustomRecognition):
    master: "AutoDoriMaster" = None

    def analyze(self, context: Context, argv: CustomRecognition.AnalyzeArg) -> Union[
        CustomRecognition.AnalyzeResult, Optional[RectType]]:
        types = {
            "score": {"roi": [1028, 192, 144, 35]}, "maxcombo": {"roi": [1009, 391, 91, 28]},
            "perfect": {"roi": [829, 282, 90, 28]}, "great": {"roi": [828, 322, 91, 27]},
            "good": {"roi": [829, 363, 91, 27]}, "bad": {"roi": [829, 401, 90, 27]},
            "miss": {"roi": [830, 438, 91, 28]}, "fast": {"roi": [1088, 283, 90, 27]},
            "slow": {"roi": [1088, 323, 91, 28]},
        }
        result = {}
        pipeline = {f"_ocr_{t}": {"recognition": "OCR", "only_rec": True, "roi": v["roi"]} for t, v in types.items()}
        for type_, _ in types.items():
            try:
                ocrtext = context.run_recognition(f"_ocr_{type_}", argv.image, pipeline).best_result.text
                result[type_] = int(ocrtext)
            except:
                result[type_] = -1
        logging.debug(f"结算结果: {result}")
        return CustomRecognition.AnalyzeResult([0, 0, 0, 0], json.dumps(result))


class SavePlayResult(CustomAction):
    master: "AutoDoriMaster" = None

    def run(self, context: Context, argv: CustomAction.RunArg):
        try:
            succeed = json.loads(argv.custom_action_param).get("succeed")
            if succeed:
                playresult = json.loads(argv.reco_detail.best_result.detail)
            else:
                self.master.play_failed_times += 1
                playresult = {}
            PlayRecord.create(
                play_time=int(time.time()), play_offset=self.master.OFFSET, result=playresult,
                succeed=succeed, chart_id=self.master.current_song_id, difficulty=self.master.DIFFICULTY,
            )
            logging.info(f"已保存演奏记录, 成功: {succeed}")
            if self.master.play_failed_times >= self.master.MAX_FAILED_TIMES:
                logging.error("失败次数过多，任务终止。")
                context.run_action("close_app")
                context.run_action("stop")
            return CustomAction.RunResult(True)
        except Exception as e:
            logging.error(f"保存演奏记录失败: {e}")
            return CustomAction.RunResult(False)


class Play(CustomAction):
    master: "AutoDoriMaster" = None

    def run(self, context: Context, argv: CustomAction.RunArg):
        try:
            self.master._play_song()
            return CustomAction.RunResult(True)
        except Exception as e:
            logging.error(f"演奏歌曲时出错: {e}", stack_info=True)
            return CustomAction.RunResult(False)


class SaveSong(CustomAction):
    master: "AutoDoriMaster" = None

    def run(self, context: Context, argv: CustomAction.RunArg):
        name = argv.reco_detail.best_result.detail
        self.master._save_song(name)
        return CustomAction.RunResult(True)


# --- Helper methods moved inside the class or becoming private methods ---

# These methods are now part of the AutoDoriMaster class
AutoDoriMaster._save_song = lambda self, name: (
    setattr(self, 'current_song_name', name),
    setattr(self, 'current_song_id', self.all_song_name_indexes[name]),
    setattr(self, 'current_chart', Chart((self.all_song_name_indexes[name], self.DIFFICULTY), name)),
    self.current_chart.notes_to_actions(self.current_player.resolution, self.DEFAULT_MOVE_SLICE_SIZE),
    setattr(self, 'current_orientation', self._get_orientation()),
    self.current_chart.actions_to_MNTcmd((self.mnt.max_x, self.mnt.max_y), self.current_orientation, self.OFFSET,
                                         self.CMD_SLICE_SIZE),
    logging.info(f"已选定歌曲: {name}")
)

AutoDoriMaster._get_orientation = lambda self: int(re.search(r"SurfaceOrientation:\s*(\d+)", subprocess.check_output(
    [str(self.device.adb_path.absolute()), "-s", self.device.address, "shell", "dumpsys input|grep SurfaceOrientation"],
    text=True)).group(1))

AutoDoriMaster._play_song = lambda self: (
    logging.info("开始演奏..."),
    self.cmd_log_list.clear(),
    self.reset_callback_data(),
    self._wait_first_note(),
    self._play_loop()
)


def _play_loop(self):
    while not self._stop_requested.is_set():
        self.current_chart.command_builder.publish(self.mnt, block=False)
        index_before_wait = self.current_chart.actions_to_cmd_index
        wait_time = sum(action.get("length", 0) for action in
                        self.current_chart.actions[index_before_wait - self.CMD_SLICE_SIZE: index_before_wait] if
                        action["type"] == "wait")
        time.sleep(max(0, wait_time - 3) / 1000)

        index = self.current_chart.actions_to_cmd_index
        if self.current_chart.actions[index: index + self.CMD_SLICE_SIZE]:
            with self.callback_data_lock:
                self._adjust_offset()
                self.reset_callback_data()
            self.current_chart.actions_to_MNTcmd((self.mnt.max_x, self.mnt.max_y), self.current_orientation,
                                                 self.OFFSET, self.CMD_SLICE_SIZE)
        else:
            break
    time.sleep(2)


AutoDoriMaster._play_loop = _play_loop


def _adjust_offset(self):
    total_cost = 0.0
    for type_ in ["up", "down", "move", "wait", "interval"]:
        type_data = self.callback_data[type_]
        total = type_data["total"]
        if total != 0:
            total_cost += type_data["total_offset"] - self.OFFSET[type_] * total
            self.OFFSET[type_] = type_data["total_offset"] / total
    self.current_chart._a2c_offset += total_cost
    logging.debug(f"动态调整 Offset: {self.OFFSET}")


AutoDoriMaster._adjust_offset = _adjust_offset


def _wait_first_note(self):
    last_color = None
    waited_frames = 0
    info = get_runtime_info(self.current_player.resolution)["wait_first"]
    from_row, to_row = info["from"], info["to"]
    freezed = False
    logging.info("等待第一个音符...")
    while not self._stop_requested.is_set():
        try:
            screen = self.current_player.ipc_capture_display()
            cur_color, _ = get_color_eval_in_range(screen, from_row, to_row)
            if last_color is not None:
                change_score = np.sum(np.abs(cur_color[0:3] - last_color[0:3]))
                # logging.debug(f"画面变化量: {change_score}")
                if change_score > 5:
                    if freezed:
                        logging.info("检测到第一个音符!")
                        time.sleep(self.PHOTOGATE_LATENCY / 1000)
                        break
                else:
                    if not freezed:
                        waited_frames += 1
                if not freezed and waited_frames >= 200:
                    freezed = True
                    logging.info("画面已静止, 等待音符落下...")
            last_color = cur_color
        except Exception as e:
            logging.error(f"等待音符时捕获屏幕失败: {e}")


AutoDoriMaster._wait_first_note = _wait_first_note
