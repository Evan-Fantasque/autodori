import logging
import queue
import threading
import tkinter as tk
from tkinter import scrolledtext
from tkinter import ttk

# 导入您的后端脚本作为一个模块
import autodori_ui


# --- 关键部分：设置日志重定向 ---
# 1. 创建一个自定义的日志处理器，它会将日志消息放入一个线程安全的队列中
class QueueHandler(logging.Handler):
    def __init__(self, log_queue):
        super().__init__()
        self.log_queue = log_queue

    def emit(self, record):
        # 将格式化后的日志消息放入队列
        self.log_queue.put(self.format(record))


# 2. GUI应用主类
class AutodoriGUI:
    def __init__(self, master):
        self.master = master
        master.title("Autodori Controller")

        self.bot_thread = None

        # --- 为UI控件创建Tkinter变量 ---
        self.mode_var = tk.StringVar(value='full_auto')
        self.difficulty_var = tk.StringVar(value='hard')
        self.isfull_var = tk.BooleanVar()
        self.human_var = tk.BooleanVar()

        # --- 新增：为警示文本绑定状态追踪 ---
        self.isfull_var.trace_add('write', self._update_warnings)
        self.human_var.trace_add('write', self._update_warnings)

        # --- 创建控件 ---
        self._create_widgets()

        # --- 设置日志队列 ---
        self.log_queue = queue.Queue()
        queue_handler = QueueHandler(self.log_queue)
        formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
        queue_handler.setFormatter(formatter)
        logging.getLogger().addHandler(queue_handler)
        logging.getLogger().setLevel(logging.INFO)

        # --- 启动日志处理循环 ---
        self.master.after(100, self._process_log_queue)

    def _create_widgets(self):
        paned_window = ttk.PanedWindow(self.master, orient=tk.VERTICAL)
        paned_window.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        top_frame = ttk.Frame(paned_window, padding="10")
        paned_window.add(top_frame, weight=0)

        # --- 新增：为警示标签定义醒目样式 ---
        style = ttk.Style()
        style.configure("Warning.TLabel", foreground="red")


        # --- 配置区 ---
        config_frame = ttk.LabelFrame(top_frame, text="配置")
        config_frame.pack(fill=tk.X, expand=True, side=tk.TOP, pady=(0, 10))

        # --- 新增：模式选择 ---
        mode_frame = ttk.Frame(config_frame)
        mode_frame.pack(fill=tk.X, padx=5, pady=5)
        ttk.Label(mode_frame, text="模式:").pack(side=tk.LEFT)
        ttk.Radiobutton(mode_frame, text="单曲模式", variable=self.mode_var, value='single').pack(side=tk.LEFT, padx=10)
        ttk.Radiobutton(mode_frame, text="全自动模式", variable=self.mode_var, value='full_auto').pack(side=tk.LEFT,
                                                                                                   padx=10)

        # --- 新增: 创建一个用于放置警告的容器，并将其放在 mode_frame 的右侧 ---
        warnings_frame = ttk.Frame(mode_frame)
        warnings_frame.pack(side=tk.RIGHT, padx=(20, 0)) # padx 在左侧增加一些间距

        # --- 其他配置 ---
        options_frame = ttk.Frame(config_frame)
        options_frame.pack(fill=tk.X, padx=5, pady=5)

        ttk.Label(options_frame, text="难度:").pack(side=tk.LEFT)
        ttk.Combobox(options_frame, textvariable=self.difficulty_var,
                     values=['easy', 'normal', 'hard', 'expert', 'special'], width=10).pack(side=tk.LEFT, padx=5)

        ttk.Label(options_frame, text="未FC跳过阈值：").pack(side=tk.LEFT, padx=5)
        # Add a variable to hold the value
        self.max_not_fc_var = tk.IntVar(value=1)
        ttk.Spinbox(options_frame, from_=1, to=99, textvariable=self.max_not_fc_var, width=5).pack(side=tk.LEFT, padx=5)

        # --- 修改：为复选框和警示标签创建独立的容器 ---
        # 1. 支持FULL曲
        full_song_frame = ttk.Frame(options_frame)
        full_song_frame.pack(side=tk.LEFT, padx=10)
        ttk.Checkbutton(full_song_frame, text="支持FULL曲", variable=self.isfull_var).pack(side=tk.LEFT)

        # --- 修改: 将警告标签的父组件改为 warnings_frame ---
        self.full_song_warning_label = ttk.Label(warnings_frame, text="警告：成功率极低，绝对不要对没有FULL的歌曲使用", style="Warning.TLabel")

        # 2. 随机化
        human_delay_frame = ttk.Frame(options_frame)
        human_delay_frame.pack(side=tk.LEFT, padx=10)
        ttk.Checkbutton(human_delay_frame, text="随机化按键", variable=self.human_var).pack(side=tk.LEFT)

        # --- 修改: 将警告标签的父组件改为 warnings_frame ---
        self.human_delay_warning_label = ttk.Label(warnings_frame, text="警告：可能导致不能FC", style="Warning.TLabel")

        # 初始时根据默认值决定是否显示
        self._update_warnings()


        # --- 控制区 ---
        control_frame = ttk.Frame(top_frame)
        control_frame.pack(fill=tk.X, expand=True, side=tk.TOP)
        self.start_button = ttk.Button(control_frame, text="启动任务", command=self.start_bot)
        self.start_button.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
        self.stop_button = ttk.Button(control_frame, text="停止任务", command=self.stop_bot, state=tk.DISABLED)
        self.stop_button.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)

        # --- 日志显示区 ---
        log_frame = ttk.Frame(paned_window, padding="0")
        paned_window.add(log_frame, weight=1)
        self.log_display = scrolledtext.ScrolledText(log_frame, state='disabled', wrap=tk.WORD, bg="#2b2b2b",
                                                     fg="white")
        self.log_display.pack(fill=tk.BOTH, expand=True)

    def _process_log_queue(self):
        try:
            while True:
                record = self.log_queue.get(block=False)
                self.log_display.configure(state='normal')
                self.log_display.insert(tk.END, record + '\n')
                self.log_display.see(tk.END)
                self.log_display.configure(state='disabled')
        except queue.Empty:
            pass
        self.master.after(100, self._process_log_queue)

    # In gui.py -> AutodoriGUI -> start_bot
    def start_bot(self):
        self.start_button.config(state=tk.DISABLED)
        self.stop_button.config(state=tk.NORMAL)

        config_data = {
            "mode": self.mode_var.get(),
            "difficulty": self.difficulty_var.get(),
            "is_full_song": self.isfull_var.get(),
            "human_delay": self.human_var.get(),
            # Add the new value here
            "max_not_fc_count": self.max_not_fc_var.get()
        }

        self.bot_thread = threading.Thread(target=self._run_bot_task, args=(config_data,), daemon=True)
        self.bot_thread.start()

    def stop_bot(self):
        if autodori_ui.maatasker.running:
            logging.info(f"尝试停止：{autodori_ui.maatasker.running}")
            autodori_ui.maatasker.post_stop()

    def _run_bot_task(self, config_data):
        """线程的目标函数，封装了后端的初始化和运行"""
        try:
            logging.info("正在初始化后端模块...")
            autodori_ui.init()
            logging.info("初始化完成，开始执行自动任务...")

            # 修改：根据模式调用不同的后端函数
            mode = config_data.get("mode")
            if mode == 'full_auto':
                autodori_ui.run_full_auto_mode(config_data)
            else:  # 'single' 模式
                autodori_ui.run_simplified_autodori(config_data)

            logging.info("任务执行完毕。")
        except Exception as e:
            logging.error(f"后端任务执行出错: {e}", exc_info=True)
        finally:
            self.start_button.config(state=tk.NORMAL)
            self.stop_button.config(state=tk.DISABLED)

    def _update_warnings(self, *args):
        # 检查 "支持FULL曲" 的状态
        if self.isfull_var.get():
            if not self.full_song_warning_label.winfo_exists() or not self.full_song_warning_label.winfo_ismapped():
                # 修改: 使用 anchor='w' 让文本在垂直容器内左对齐
                self.full_song_warning_label.pack(anchor='w')
        else:
            self.full_song_warning_label.pack_forget()

        # 检查 "随机化" 的状态
        if self.human_var.get():
            if not self.human_delay_warning_label.winfo_exists() or not self.human_delay_warning_label.winfo_ismapped():
                # 修改: 使用 anchor='w' 让文本在垂直容器内左对齐
                self.human_delay_warning_label.pack(anchor='w')
        else:
            self.human_delay_warning_label.pack_forget()


# --- 程序入口 ---
if __name__ == "__main__":
    if not autodori_ui.resource_path("assets/resource").exists():
        print("错误：未找到 'assets/resource' 目录。")
        print("请确保 gui.py 与 autodori_ui.py 在同一个目录下，并且项目结构完整。")
    else:
        root = tk.Tk()
        app = AutodoriGUI(root)
        # 手动调用一次，确保初始状态正确
        app._update_warnings()
        root.mainloop()
