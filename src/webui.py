import base64
import io
import logging
import queue
import sys
import threading
import time
from logging.handlers import QueueHandler

from flask import Flask, jsonify, render_template, request, Response
from PIL import Image

# 导入改造后的主模块
import autodori_master

# ---- 全局变量 ----
app = Flask(__name__, template_folder="../templates")
log_queue = queue.Queue()
autodori_thread = None
autodori_instance = None
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
    global autodori_instance, current_status
    try:
        current_status = "running"
        autodori_instance = autodori_master.AutoDoriMaster(log_queue)
        autodori_instance.initialize()
        autodori_instance.run(
            difficulty=config["difficulty"],
            livemode=config["livemode"],
            min_liveboost=config["liveboost"],
        )
        # 正常结束
        if current_status != "stopping":
            current_status = "stopped"
    except Exception as e:
        logging.error(f"Autodori 任务发生致命错误: {e}", exc_info=True)
        current_status = "error"
    finally:
        # 确保任务结束后，实例被清理
        if autodori_instance:
            autodori_instance.cleanup()
            autodori_instance = None


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
    global current_status, autodori_instance
    if not autodori_thread or not autodori_thread.is_alive():
        return jsonify({"status": "error", "message": "当前没有正在运行的任务。"})

    logging.info("收到停止请求...")
    current_status = "stopping"
    if autodori_instance:
        autodori_instance.stop()  # 尝试优雅停止

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


@app.route("/debug/images")
def get_debug_images():
    """获取用于调试的截图"""
    if autodori_instance:
        full_b64 = autodori_instance.last_full_screenshot_b64
        roi_b64 = autodori_instance.last_roi_screenshot_b64
        return jsonify({
            "full": full_b64,
            "roi": roi_b64
        })
    return jsonify({"error": "任务未运行或未进入识别阶段", "full": None, "roi": None})


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


if __name__ == "__main__":
    print("WebUI 已启动，请在浏览器中打开 http://127.0.0.1:5000")
    app.run(host="127.0.0.1", port=5000, debug=False)

