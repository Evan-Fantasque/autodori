import logging
import queue
import threading
import tkinter as tk
from tkinter import scrolledtext
from tkinter import ttk

# 导入您的后端脚本作为一个模块
import autodori_ui


# --- 关键部分：设置日志重定向 ---
class QueueHandler(logging.Handler):
    """自定义日志处理器，将日志消息放入一个线程安全的队列中。"""

    def __init__(self, log_queue):
        super().__init__()
        self.log_queue = log_queue

    def emit(self, record):
        self.log_queue.put(self.format(record))


# 2. GUI应用主类
class AutodoriGUI:
    def __init__(self, master):
        self.master = master
        master.title("autodori UI")
        master.geometry("650x550")

        self.bot_thread = None

        # --- 为UI控件创建Tkinter变量 ---
        self.mode_var = tk.StringVar(value='full_auto')
        self.difficulty_var = tk.StringVar(value='hard')
        self.isfull_var = tk.BooleanVar(value=False)
        self.human_var = tk.BooleanVar(value=False)
        self.max_not_fc_var = tk.IntVar(value=1)

        # --- 绑定状态追踪 ---
        self.isfull_var.trace_add('write', self._update_warnings)
        self.human_var.trace_add('write', self._update_warnings)

        # --- 日志队列 ---
        self.log_queue = queue.Queue()

        # --- 修改：不再立即创建控件，而是先显示欢迎界面 ---
        self._create_disclaimer_view()

    def _create_disclaimer_view(self):
        """创建并显示欢迎/风险提示界面。"""
        # 设置主窗口的最小尺寸，防止用户缩得过小
        self.master.minsize(650, 550)

        self.disclaimer_frame = ttk.Frame(self.master, padding="15")
        self.disclaimer_frame.pack(fill=tk.BOTH, expand=True)

        # 标题
        title_label = ttk.Label(self.disclaimer_frame, text="使用须知", font=("", 16, "bold"))
        title_label.pack(pady=(10, 20))

        # --- 文案内容 ---
        info_text = (
            "在开始前，请仔细阅读以下使用说明与风险提示："
        )
        # 步骤1：将标签保存为实例变量，并移除固定的 wraplength
        self.info_label = ttk.Label(self.disclaimer_frame, text=info_text, justify=tk.LEFT, font=("", 11))
        self.info_label.pack(fill=tk.X, pady=5)

        usage_frame = ttk.LabelFrame(self.disclaimer_frame, text="使用方法", padding="10")
        usage_frame.pack(fill=tk.X, pady=10)
        usage_text = (
            "模拟器请设置为1920x1080分辨率。\n"
            "歌曲难度请手动设置为与游戏中一致。\n"
            "单曲模式：启动任务前，请手动进入自由、挑战、组曲模式下的「选择乐队」界面。\n"
            "全自动模式：启动任务前，请手动进入自由模式下的「选择乐曲」界面。"
        )
        self.usage_label = ttk.Label(usage_frame, text=usage_text, justify=tk.LEFT)
        self.usage_label.pack(fill=tk.X)

        warning_frame = ttk.LabelFrame(self.disclaimer_frame, text="风险提示", padding="10")
        warning_frame.pack(fill=tk.X, pady=10)
        warning_text = (
            "本程序仅于自由、挑战、组曲模式下进行开发与测试，不可用于协力模式。\n"
            "程序运行期间，建议您时刻关注模拟器界面。若出现任何异常情况，请立即手动停止任务。\n"
            "使用本程序可能违反游戏的用户协议，您将自行承担一切潜在风险。开发者对由此产生的任何后果概不负责。"
        )
        self.warning_label = ttk.Label(warning_frame, text=warning_text, justify=tk.LEFT, foreground="red")
        self.warning_label.pack(fill=tk.X)

        # --- 同意与继续 ---
        self.agree_var = tk.BooleanVar()
        agree_check = ttk.Checkbutton(self.disclaimer_frame, text="我已阅读并理解以上条款，同意自行承担所有风险。",
                                      variable=self.agree_var, command=self._toggle_proceed_button_state)
        agree_check.pack(pady=15)

        self.proceed_button = ttk.Button(self.disclaimer_frame, text="进入控制面板", state=tk.DISABLED, command=self._show_main_app)
        self.proceed_button.pack(fill=tk.X, padx=100, ipady=5)

        # 步骤3：将事件处理函数绑定到容器的 <Configure> 事件
        self.disclaimer_frame.bind("<Configure>", self._on_disclaimer_resize)

    def _on_disclaimer_resize(self, event):
        """步骤2：创建事件处理函数，在窗口/框架尺寸变化时调用。"""
        # event.width 提供了容器当前的宽度
        # 减去一些像素作为边距(padding)，防止文本紧贴边缘
        new_wraplength = event.width - 40

        # 更新所有需要换行的标签
        self.info_label.config(wraplength=new_wraplength)

        # 对于在LabelFrame内部的标签，其可用宽度要更小一些
        self.usage_label.config(wraplength=new_wraplength - 20)
        self.warning_label.config(wraplength=new_wraplength - 20)

    def _toggle_proceed_button_state(self):
        """根据复选框状态切换“继续”按钮的可用性。"""
        if self.agree_var.get():
            self.proceed_button.config(state=tk.NORMAL)
        else:
            self.proceed_button.config(state=tk.DISABLED)

    def _show_main_app(self):
        """销毁欢迎界面并构建主应用程序界面。"""
        self.disclaimer_frame.destroy()
        self._create_main_widgets()

        # --- 在主界面创建后，再启动日志系统 ---
        queue_handler = QueueHandler(self.log_queue)
        formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
        queue_handler.setFormatter(formatter)
        logging.getLogger().addHandler(queue_handler)
        logging.getLogger().setLevel(logging.INFO)
        self.master.after(100, self._process_log_queue)

    def _create_main_widgets(self):
        """创建主控制面板的所有控件。"""
        # --- 修改：将 paned_window 保存为实例变量，并减少外边距 ---
        self.paned_window = ttk.PanedWindow(self.master, orient=tk.VERTICAL)
        self.paned_window.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)  # 外边距从10减为5

        # --- 修改：减少 top_frame 的内边距 ---
        top_frame = ttk.Frame(self.paned_window, padding="5")  # 内边距从10减为5
        self.paned_window.add(top_frame, weight=0)

        style = ttk.Style()
        style.configure("Warning.TLabel", foreground="red")

        # --- 修改：减少 config_frame 的垂直边距 ---
        config_frame = ttk.LabelFrame(top_frame, text="配置")
        config_frame.pack(fill=tk.X, expand=True, side=tk.TOP, pady=(0, 5))  # 下方边距从10减为5

        # --- 修改：减少 mode_frame 的边距 ---
        mode_frame = ttk.Frame(config_frame)
        mode_frame.pack(fill=tk.X, padx=2, pady=2)  # 边距从5减为2
        ttk.Label(mode_frame, text="模式:").pack(side=tk.LEFT)
        # --- 修改：减少 Radiobutton 之间的距离 ---
        ttk.Radiobutton(mode_frame, text="单曲模式", variable=self.mode_var, value='single').pack(side=tk.LEFT,
                                                                                                  padx=5)  # 边距从10减为5
        ttk.Radiobutton(mode_frame, text="全自动模式", variable=self.mode_var, value='full_auto').pack(side=tk.LEFT,
                                                                                                       padx=5)  # 边距从10减为5

        # --- 修改：减少 warnings_frame 的左边距 ---
        warnings_frame = ttk.Frame(mode_frame)
        warnings_frame.pack(side=tk.RIGHT, padx=(10, 0))  # 边距从20减为10

        # --- 修改：减少 options_frame 的边距 ---
        options_frame = ttk.Frame(config_frame)
        options_frame.pack(fill=tk.X, padx=2, pady=2)  # 边距从5减为2

        ttk.Label(options_frame, text="难度:").pack(side=tk.LEFT)
        ttk.Combobox(options_frame, textvariable=self.difficulty_var,
                     values=['easy', 'normal', 'hard', 'expert', 'special'], width=10).pack(side=tk.LEFT,
                                                                                            padx=3)  # 边距从5减为3

        ttk.Label(options_frame, text="未FC跳过阈值：").pack(side=tk.LEFT, padx=(5, 0))  # 调整边距
        ttk.Spinbox(options_frame, from_=1, to=99, textvariable=self.max_not_fc_var, width=5).pack(side=tk.LEFT, padx=3)

        # --- 修改：减少复选框区域的边距 ---
        full_song_frame = ttk.Frame(options_frame)
        full_song_frame.pack(side=tk.LEFT, padx=5)  # 边距从10减为5
        ttk.Checkbutton(full_song_frame, text="支持FULL曲", variable=self.isfull_var).pack(side=tk.LEFT)
        self.full_song_warning_label = ttk.Label(warnings_frame, text="警告：成功率极低，请勿对没有FULL谱的歌曲使用",
                                                 style="Warning.TLabel")

        human_delay_frame = ttk.Frame(options_frame)
        human_delay_frame.pack(side=tk.LEFT, padx=5)  # 边距从10减为5
        ttk.Checkbutton(human_delay_frame, text="随机化按键", variable=self.human_var).pack(side=tk.LEFT)
        self.human_delay_warning_label = ttk.Label(warnings_frame, text="警告：可能导致不能FC", style="Warning.TLabel")

        self.bg_color = style.lookup("TFrame", "background")

        self.full_song_warning_label.pack(anchor='w')
        self.human_delay_warning_label.pack(anchor='w')

        self._update_warnings()

        control_frame = ttk.Frame(top_frame)
        control_frame.pack(fill=tk.X, expand=True, side=tk.TOP, pady=(5, 0))
        # --- 修改：减少按钮之间的距离 ---
        self.start_button = ttk.Button(control_frame, text="启动任务", command=self.start_bot)
        self.start_button.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=2)  # 边距从5减为2
        self.stop_button = ttk.Button(control_frame, text="停止任务", command=self.stop_bot, state=tk.DISABLED)
        self.stop_button.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=2)  # 边距从5减为2

        log_frame = ttk.Frame(self.paned_window, padding="0")
        self.paned_window.add(log_frame, weight=1)
        self.log_display = scrolledtext.ScrolledText(log_frame, state='disabled', wrap=tk.WORD, bg="#2b2b2b",
                                                     fg="white")
        self.log_display.pack(fill=tk.BOTH, expand=True)

        # --- 新增：绑定事件来禁用 sash 拖动 ---
        self.paned_window.bind("<ButtonPress-1>", self._prevent_resize)
        self.paned_window.bind("<B1-Motion>", self._prevent_resize)

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

    def start_bot(self):
        self.start_button.config(state=tk.DISABLED)
        self.stop_button.config(state=tk.NORMAL)
        config_data = {
            "mode": self.mode_var.get(),
            "difficulty": self.difficulty_var.get(),
            "is_full_song": self.isfull_var.get(),
            "human_delay": self.human_var.get(),
            "max_not_fc_count": self.max_not_fc_var.get()
        }
        self.bot_thread = threading.Thread(target=self._run_bot_task, args=(config_data,), daemon=True)
        self.bot_thread.start()

    def stop_bot(self):
        if hasattr(autodori_ui, 'maatasker') and autodori_ui.maatasker and autodori_ui.maatasker.running:
            logging.info("正在尝试停止任务...")
            autodori_ui.maatasker.post_stop()
        else:
            logging.warning("任务未在运行或尚未初始化，无需停止。")

    def _run_bot_task(self, config_data):
        try:
            logging.info("正在初始化后端模块...")
            autodori_ui.init()
            logging.info("初始化完成，开始执行自动任务...")

            mode = config_data.get("mode")
            if mode == 'full_auto':
                autodori_ui.run_full_auto_mode(config_data)
            else:
                autodori_ui.run_simplified_autodori(config_data)

            logging.info("任务执行完毕。")
        except Exception as e:
            logging.error(f"后端任务执行出错: {e}", exc_info=True)
        finally:
            self.start_button.config(state=tk.NORMAL)
            self.stop_button.config(state=tk.DISABLED)

    def _prevent_resize(self, event):
        """拦截并阻止PanedWindow的尺寸调整事件。"""
        return "break"

    def _update_warnings(self, *args):
        """
        通过改变文字颜色来显示或隐藏警告，而不是从布局中移除它们。
        这可以保持布局的稳定性。
        """
        # 检查 '支持FULL曲' 的状态
        if self.isfull_var.get():
            # 勾选时，设置为红色
            self.full_song_warning_label.config(foreground='red')
        else:
            # 未勾选时，设置为背景色（隐形）
            self.full_song_warning_label.config(foreground=self.bg_color)

        # 检查 '随机化' 的状态
        if self.human_var.get():
            # 勾选时，设置为红色
            self.human_delay_warning_label.config(foreground='red')
        else:
            # 未勾选时，设置为背景色（隐形）
            self.human_delay_warning_label.config(foreground=self.bg_color)


# --- 程序入口 ---
if __name__ == "__main__":
    root = tk.Tk()
    app = AutodoriGUI(root)
    root.mainloop()