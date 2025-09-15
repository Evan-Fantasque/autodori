import base64
import io
import logging
import sys
import threading
import time
import webbrowser
import socket
from pathlib import Path

from flask import Flask, render_template, request
from flask_socketio import SocketIO

import autodori_ui

# --- Fix for Template Path ---
template_dir = str(Path(__file__).parent.parent / "templates")
app = Flask(__name__, template_folder=template_dir)

# --- Flask App and SocketIO Setup ---
app.config["SECRET_KEY"] = "secret!"
socketio = SocketIO(app, async_mode="threading", ping_timeout=120, ping_interval=60)


# --- Logging Setup ---
class SocketIOHandler(logging.Handler):
    def emit(self, record):
        log_entry = self.format(record)
        socketio.emit("log", {"data": log_entry})


logger = logging.getLogger()
logger.setLevel(logging.INFO)
# Keep this formatter for the console output
console_formatter = logging.Formatter("[%(asctime)s][%(levelname)s] %(message)s", datefmt="%H:%M:%S")

# Use a simplified formatter for SocketIO to avoid duplicate timestamps on the frontend
socketio_formatter = logging.Formatter("[%(levelname)s] %(message)s")

socketio_handler = SocketIOHandler()
socketio_handler.setFormatter(socketio_formatter)
logger.addHandler(socketio_handler)

console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(console_formatter)
logger.addHandler(console_handler)

# --- Global State ---
task_thread = None


# --- HTTP Routes ---
@app.route("/")
def index():
    return render_template("index.html")


# --- SocketIO Event Handlers ---
@socketio.on("connect")
def handle_connect():
    logging.info("WebUI 已连接")


@socketio.on("disconnect")
def handle_disconnect():
    logging.info("WebUI 已断开")
    autodori_ui.stop_streaming()


@socketio.on("initialize")
def handle_initialize():
    global task_thread
    if task_thread and task_thread.is_alive():
        logging.warning("一个任务正在进行中，请等待其完成。")
        return

    def init_task():
        try:
            autodori_ui.init()
            socketio.emit("initialization_status", {"success": True})
        except Exception as e:
            logging.error(f"初始化失败: {e}")
            socketio.emit("initialization_status", {"success": False, "error": str(e)})

    task_thread = threading.Thread(target=init_task)
    task_thread.start()


@socketio.on("start_simplified_auto")
def handle_run_task(data):
    global task_thread
    if task_thread and task_thread.is_alive():
        logging.warning("一个任务正在进行中，请等待其完成。")
        return

    def run_task_in_thread():
        try:
            config = {
                "difficulty": data.get("difficulty", "hard"),
                "is_full_song": data.get("is_full_song", False)
            }
            autodori_ui.run_simplified_autodori(config)
        except Exception as e:
            logging.error(f"简化版自动演奏失败: {e}")
        finally:
            socketio.emit("task_finished")

    task_thread = threading.Thread(target=run_task_in_thread)
    task_thread.start()


@socketio.on("start_stream")
def handle_start_stream(settings=None):
    try:
        if settings:
            autodori_ui.update_stream_settings(settings)
        autodori_ui.start_streaming(socketio)
        logging.info("已开启实时画面传输。")
    except Exception as e:
        logging.error(f"开启画面传输失败: {e}")


@socketio.on("stop_stream")
def handle_stop_stream():
    autodori_ui.stop_streaming()
    logging.info("已停止实时画面传输。")


@socketio.on("update_stream_settings")
def handle_update_stream_settings(settings):
    """Handle stream settings update from client."""
    try:
        autodori_ui.update_stream_settings(settings)
        logging.info(f"画面传输设置已更新: {settings}")
    except Exception as e:
        logging.error(f"更新画面传输设置失败: {e}")


# --- Main Execution ---
def find_free_port():
    """Finds a free port on the local machine."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def open_browser_if_needed(url):
    """
    等待1.5秒，然后检查是否有WebUI连接。如果没有，则打开浏览器。
    """
    time.sleep(1.5)
    if not webui_connect:
        logging.info("WebUI未连接，将在1秒后自动打开浏览器...")
        # 使用一个新的线程来打开浏览器，以避免阻塞主线程
        threading.Timer(1, lambda: webbrowser.open_new_tab(url)).start()


if __name__ == "__main__":
    port = find_free_port()
    url = f"http://127.0.0.1:{port}"

    # 在后台启动一个线程，用于检查是否需要打开浏览器
    # 设置为守护线程(daemon=True)，这样主程序退出时该线程也会随之结束
    browser_opener_thread = threading.Thread(target=open_browser_if_needed, args=(url,))
    browser_opener_thread.daemon = True
    browser_opener_thread.start()

    logging.info(f"AutoDori WebUI 将在 {url} 启动")
    logging.info("将在1秒后尝试自动打开浏览器...")

    threading.Timer(1, lambda: webbrowser.open_new_tab(url)).start()

    socketio.run(app, host="127.0.0.1", port=port, allow_unsafe_werkzeug=True)