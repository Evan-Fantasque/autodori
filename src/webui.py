# webui.py

import datetime
import json
import logging
import queue
import socket
import sys
import time
import webbrowser
from logging.handlers import QueueHandler
from pathlib import Path
from threading import Timer

from flask import Flask, Response, jsonify, render_template, request
from PIL import Image, ImageGrab

# 导入后端控制器
from autodori_ui import AutodoriController

# ---- 全局变量 ----
app = Flask(__name__, template_folder="../templates")
log_queue = queue.Queue()
controller = AutodoriController(log_queue)
current_version = None

# ---- 日志配置 ----
# 配置根Logger，以便所有模块（包括autodori）的日志都能被捕获
# AutodoriController内部会用QueueHandler将日志放入log_queue
stream_handler = logging.StreamHandler(sys.stdout)
formatter = logging.Formatter("%(asctime)s[%(levelname)s][%(name)s] %(message)s")
stream_handler.setFormatter(formatter)
file_handler = logging.FileHandler(
    "debug/autodori-webui-{}.log".format(
        datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    ),
    mode="w",
    encoding="utf-8",
)
file_handler.setFormatter(formatter)
root_logger = logging.getLogger()
# 清除已有handler，避免重复添加
root_logger.handlers.clear()
root_logger.addHandler(stream_handler)
root_logger.addHandler(file_handler)
root_logger.setLevel(logging.INFO)
# 将autodori模块的日志也导向这里
logging.getLogger('autodori').addHandler(stream_handler)
logging.getLogger('autodori').addHandler(file_handler)
logging.getLogger('autodori').setLevel(logging.INFO)

werkzeug_logger = logging.getLogger("werkzeug")
werkzeug_logger.setLevel(logging.ERROR)


# ---- 版本与更新检查 ----
def get_current_version():
    global current_version
    try:
        metadata_text = Path("assets/build_metadata.json").read_text(encoding="utf-8")
        current_version = json.loads(metadata_text)["version"]
    except Exception:
        logging.debug("Failed to get current version")


def check_update():
    # ... (此函数内容不变，为简洁省略)
    pass

# ---- Flask 路由 ----
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/start", methods=["POST"])
def start_task():
    config = request.json
    mode = config.get("mode", "full")
    logging.info(f"Received start request, mode: {mode}, config: {config}")

    if mode == "semiauto":
        if not config.get("song_name"):
            return jsonify({"status": "error", "message": "半自动模式需要歌曲名称"})
        success, message = controller.start_semi_auto(config)
    else:
        success, message = controller.start_full_auto(config)

    if success:
        return jsonify({"status": "success", "message": message})
    else:
        return jsonify({"status": "error", "message": message})


@app.route("/stop", methods=["POST"])
def stop_task():
    success, message = controller.stop()
    if success:
        return jsonify({"status": "success", "message": message})
    else:
        return jsonify({"status": "error", "message": message})


@app.route("/recognize_clipboard", methods=["POST"])
def recognize_clipboard():
    try:
        image = ImageGrab.grabclipboard()
        if not isinstance(image, Image.Image):
            return jsonify({"success": False, "message": "剪贴板中没有找到图像。"})

        result = controller.recognize_song_from_image(image)
        return jsonify(result)

    except Exception as e:
        logging.error(f"从剪贴板识别歌曲时出错: {e}", exc_info=True)
        # Pillow在Linux无头环境下可能会抛出此错误
        if "grabclipboard is not supported" in str(e):
             return jsonify({"success": False, "message": "此操作系统不支持从剪贴板获取图像。"})
        return jsonify({"success": False, "message": str(e)})


@app.route("/status")
def status():
    return jsonify(controller.get_status())


@app.route("/stream")
def stream():
    def event_stream():
        # 添加一个初始消息，表明日志流已连接
        yield f"data: --- Log stream connected ---\n\n"
        while True:
            try:
                log_record = log_queue.get(timeout=1)
                # 使用根logger的formatter来格式化从autodori模块传来的日志
                message = formatter.format(log_record)
                yield f"data: {message}\n\n"
            except queue.Empty:
                yield ": heartbeat\n\n"
            time.sleep(0.1)

    return Response(event_stream(), mimetype="text/event-stream")


def find_free_port(start_port=5000):
    port = start_port
    while port < 65535:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                port += 1
    raise IOError(f"在 {start_port}-65535 范围内未找到可用端口")


if __name__ == "__main__":
    get_current_version()
    # check_update() # 可以按需启用

    host = "127.0.0.1"
    port = find_free_port(5000)
    url = f"http://{host}:{port}"
    print(f"WebUI 已启动，请在浏览器中打开 {url}")
    Timer(1, lambda: webbrowser.open_new_tab(url)).start()
    app.run(host=host, port=port, debug=False)