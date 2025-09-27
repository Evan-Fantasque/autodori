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
console_formatter = logging.Formatter("[%(asctime)s][%(levelname)s] %(message)s", datefmt="%H:%M:%S")
socketio_formatter = logging.Formatter("[%(levelname)s] %(message)s")

socketio_handler = SocketIOHandler()
socketio_handler.setFormatter(socketio_formatter)
logger.addHandler(socketio_handler)

console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(console_formatter)
logger.addHandler(console_handler)

# --- Global State ---
task_thread = None
webui_connect = False
is_autodori_initialized = False
is_task_running = False


def broadcast_status():
    """Broadcasts the current status to all connected clients."""
    status = {
        "initialized": is_autodori_initialized,
        "running": is_task_running
    }
    socketio.emit("status_update", status)


# --- HTTP Routes ---
@app.route("/")
def index():
    return render_template("index.html")


# --- SocketIO Event Handlers ---
@socketio.on("connect")
def handle_connect():
    global webui_connect
    logging.info("WebUI 已连接")
    webui_connect = True


@socketio.on("disconnect")
def handle_disconnect():
    global webui_connect
    logging.info("WebUI 已断开")
    webui_connect = False
    autodori_ui.stop_streaming()


@socketio.on("request_status_update")
def handle_status_request():
    """Sends the current application state to the requesting client."""
    sid = request.sid
    status = {
        "initialized": is_autodori_initialized,
        "running": is_task_running
    }
    socketio.emit("status_update", status, room=sid)


@socketio.on("initialize")
def handle_initialize():
    global task_thread, is_autodori_initialized, is_task_running
    if is_task_running:
        logging.warning("一个任务正在进行中，请等待其完成。")
        return

    def init_task():
        global is_autodori_initialized, is_task_running
        is_task_running = True
        broadcast_status()
        try:
            autodori_ui.init()
            is_autodori_initialized = True
            logging.info("autodori 初始化成功!")
        except Exception as e:
            is_autodori_initialized = False
            logging.error(f"初始化失败: {e}")
        finally:
            is_task_running = False
            broadcast_status()

    task_thread = threading.Thread(target=init_task)
    task_thread.start()


@socketio.on("start_simplified_auto")
def handle_run_task(data):
    global task_thread, is_task_running
    if is_task_running:
        logging.warning("一个任务正在进行中，请等待其完成。")
        return

    def run_task_in_thread():
        global is_task_running
        is_task_running = True
        broadcast_status()
        overall_success = False  # Flag to determine the final message
        try:
            config = {
                "difficulty": data.get("difficulty", "hard"),
                "is_full_song": data.get("is_full_song", False),
                "human_delay": data.get("human_delay", False)
            }
            auto_replay = data.get("auto_replay", False)
            runs = 3 if auto_replay else 1

            for i in range(runs):
                if i > 0:
                    logging.info(f"连战: 开始第 {i + 1}/{runs} 次演奏...")

                success = autodori_ui.run_simplified_autodori(config)

                if not success:
                    logging.error(f"第 {i + 1} 次演奏失败，停止连战。")
                    socketio.emit("task_finished", {"message": f"第 {i+1} 次演奏时任务失败，连战已中止。", "level": "ERROR"})
                    overall_success = False
                    break

                if i < runs - 1:
                    logging.info(f"第 {i + 1} 次演奏完成，等待 15 秒进行下一次...")
                    time.sleep(15)
            else:
                # This 'else' belongs to the 'for' loop.
                # It executes only if the loop completed without a 'break'.
                overall_success = True

            if overall_success:
                socketio.emit("task_finished", {"message": "自动演奏任务已完成。", "level": "SUCCESS"})
        except Exception as e:
            logging.error(f"自动演奏任务执行期间发生意外错误: {e}", exc_info=True)
            socketio.emit("task_finished", {"message": f"任务执行失败: {e}", "level": "ERROR"})
        finally:
            is_task_running = False
            broadcast_status()

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
    try:
        autodori_ui.update_stream_settings(settings)
        logging.info(f"画面传输设置已更新: {settings}")
    except Exception as e:
        logging.error(f"更新画面传输设置失败: {e}")

def find_free_port(preferred_port=None):
    # 步骤 1: 如果指定了优先端口，则尝试使用它
    if preferred_port:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                # 尝试绑定端口
                s.bind(("127.0.0.1", preferred_port))

                # 如果绑定成功，说明该端口可用，直接返回
                return preferred_port
        except OSError:
            # 如果端口确实已被其他活动进程占用，则会捕获到 OSError
            print(f"警告: 优先端口 {preferred_port} 已被占用，将查找其他可用端口。")

    # 步骤 2: 如果没有指定优先端口，或者优先端口被占用，则查找一个随机可用端口
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))  # 绑定到端口0，由系统自动分配一个临时端口
        return s.getsockname()[1]


def open_browser_if_needed(url):
    time.sleep(5)
    if not webui_connect:
        logging.info("WebUI未连接，将在1秒后自动打开浏览器...")
        threading.Timer(1, lambda: webbrowser.open_new_tab(url)).start()


if __name__ == "__main__":
    port = find_free_port(9000)
    url = f"http://127.0.0.1:{port}"

    browser_opener_thread = threading.Thread(target=open_browser_if_needed, args=(url,))
    browser_opener_thread.daemon = True
    browser_opener_thread.start()

    logging.info(f"AutoDori WebUI 将在 {url} 启动")
    socketio.run(app, host="127.0.0.1", port=port, allow_unsafe_werkzeug=True)