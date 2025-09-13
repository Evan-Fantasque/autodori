import logging
import queue
import socket
import sys
import threading
import time
import webbrowser
from logging.handlers import QueueHandler
from threading import Timer

from flask import Flask, jsonify, render_template, request, Response

# 导入改造后的主模块
import autodori_master

# ---- 全局变量 ----
app = Flask(__name__, template_folder="../templates")
log_queue = queue.Queue()
autodori_thread = None
current_status = "idle"  # idle, running, stopping, stopped, error

# ---- 日志配置 ----
# 配置一个处理器，将日志消息放入队列中
queue_handler = QueueHandler(log_queue)
logging.getLogger().addHandler(queue_handler)
# 同时保留控制台输出
stream_handler = logging.StreamHandler(sys.stdout)
formatter = logging.Formatter('%(asctime)s[%(levelname)s][%(name)s] %(message)s')
stream_handler.setFormatter(formatter)
logging.getLogger().addHandler(stream_handler)
logging.getLogger().setLevel(logging.INFO)


def run_autodori_task(config):
    """在单独的线程中运行 Autodori 任务"""
    global current_status
    try:
        current_status = "running"
        # 直接调用重构后的 main 函数
        autodori_master.main(
            difficulty=config["difficulty"],
            livemode=config["livemode"],
            min_liveboost=int(config["liveboost"]),
        )
        if current_status != "stopping":
            current_status = "stopped"
    except SystemExit:
        # autodori_master.main() 在结束时会调用 sys.exit()，这会触发 SystemExit 异常
        # 这是正常的任务结束流程，我们捕获它以正确更新状态
        logging.info("Autodori 任务已正常结束。")
        current_status = "stopped"
    except Exception as e:
        logging.error(f"Autodori 任务发生致命错误: {e}", exc_info=True)
        current_status = "error"


# ---- Flask 路由 ----
@app.route("/")
def index():
    """渲染主页面"""
    return render_template("index.html")


@app.route("/start", methods=["POST"])
def start_task():
    """启动自动化任务"""
    global autodori_thread, current_status
    if autodori_thread and autodori_thread.is_alive():
        return jsonify({"status": "error", "message": "任务已经在运行中。"})

    config = request.json
    logging.info(f"收到启动请求，配置: {config}")

    autodori_thread = threading.Thread(target=run_autodori_task, args=(config,))
    autodori_thread.start()

    return jsonify({"status": "success", "message": "任务已启动。"})


@app.route("/stop", methods=["POST"])
def stop_task():
    """停止自动化任务"""
    global current_status
    if not autodori_thread or not autodori_thread.is_alive():
        return jsonify({"status": "error", "message": "当前没有正在运行的任务。"})

    logging.info("收到停止请求...")
    current_status = "stopping"
    # 直接通过模块访问 maatasker 来停止任务
    if hasattr(autodori_master, 'maatasker'):
        autodori_master.maatasker.stop()

    return jsonify({"status": "success", "message": "正在停止任务..."})


@app.route("/status")
def status():
    """获取当前状态"""
    is_alive = autodori_thread and autodori_thread.is_alive()
    # 如果线程死了但状态还是running, 说明是异常退出
    if not is_alive and current_status == "running":
        status_to_report = "error"
    else:
        status_to_report = current_status

    return jsonify({"status": status_to_report, "is_alive": is_alive})


@app.route("/stream")
def stream():
    """使用 SSE 流式传输日志"""

    def event_stream():
        while True:
            try:
                log_record = log_queue.get(timeout=1)
                message = stream_handler.format(log_record)
                yield f"data: {message}\n\n"
            except queue.Empty:
                # 发送一个心跳，防止连接被一些代理关闭
                yield ": heartbeat\n\n"
            time.sleep(0.1)

    return Response(event_stream(), mimetype="text/event-stream")


def find_free_port(start_port=5000):
    """查找一个空闲的端口"""
    port = start_port
    while port < 65535:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                port += 1
    raise IOError("在 5000-65535 范围内未找到可用端口")


if __name__ == "__main__":
    host = "127.0.0.1"
    port = find_free_port(5000)
    url = f"http://{host}:{port}"

    print(f"WebUI 已启动，请在浏览器中打开 {url}")

    # 使用计时器延迟打开浏览器，确保 Flask 服务已启动
    Timer(1, lambda: webbrowser.open_new_tab(url)).start()

    app.run(host=host, port=port, debug=False)

