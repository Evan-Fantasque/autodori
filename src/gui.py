import logging
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from functools import partial
from tkinter import messagebox
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
        self.is_float_mode = False
        self.float_window = None # Toplevel 实例

        # --- 为UI控件创建Tkinter变量 ---
        self.mode_var = tk.StringVar(value='full_auto')
        self.difficulty_var = tk.StringVar(value=autodori_ui.DIFFICULTY)
        self.human_var = tk.BooleanVar(value=autodori_ui.HUMAN_DELAY_ENABLED)
        self.max_continuous_not_fc_var = tk.IntVar(value=autodori_ui.MAX_CONTINUOUS_NOT_FC_COUNT)
        self.max_attempt_var = tk.IntVar(value=autodori_ui.MAX_SONG_ATTEMPTS)

        # --- 绑定状态追踪 ---
        self.human_var.trace_add('write', self._update_warnings)

        # --- 设置日志队列 (用于GUI显示) ---
        self.log_queue = queue.Queue()
        queue_handler = QueueHandler(self.log_queue)
        # 注意：这里我们不再对 queue_handler 设置格式，让根记录器统一处理

        # --- 新增：设置文件日志 (用于输出到文件) ---
        debug_folder = "debug"
        os.makedirs(debug_folder, exist_ok=True)  # 创建debug文件夹

        # 创建带时间戳的日志文件名
        log_filename = f"autodori_{time.strftime('%Y%m%d-%H%M%S')}.log"
        log_filepath = os.path.join(debug_folder, log_filename)

        # 创建文件处理器，指定路径和编码
        file_handler = logging.FileHandler(log_filepath, encoding='utf-8')

        # --- 配置根记录器 (Logger) ---
        # 获取根记录器
        root_logger = logging.getLogger()
        root_logger.setLevel(logging.INFO)  # 设置日志级别

        # 创建一个通用的日志格式
        formatter = logging.Formatter('%(asctime)s - [%(levelname)s] - %(message)s', datefmt='%Y-%m-%d %H:%M:%S')

        # 为两个处理器设置相同的格式
        queue_handler.setFormatter(formatter)
        file_handler.setFormatter(formatter)

        # 将两个处理器都添加到根记录器中
        root_logger.addHandler(queue_handler)
        root_logger.addHandler(file_handler)

        # --- 启动日志处理循环 ---
        self.master.after(100, self._process_log_queue)

        # --- 修改：不再立即创建控件，而是先显示欢迎界面 ---
        self._create_disclaimer_view()

        # --- 新增：用于存储全局变量UI控件的变量 ---
        self.global_vars_entries = {}

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

        self.proceed_button = ttk.Button(self.disclaimer_frame, text="进入控制面板", state=tk.DISABLED,
                                         command=self._show_main_app)
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

    def _create_float_window(self):
        """创建并配置悬浮窗。"""
        self.float_window = tk.Toplevel(self.master)
        self.float_window.title("Autodori Float")

        # 移除窗口装饰 (标题栏和边框)
        self.float_window.overrideredirect(True)

        # 始终保持在最上方
        self.float_window.attributes('-topmost', True)

        # 设置透明度 (可选，但常用)
        # self.float_window.attributes('-alpha', 0.9)

        # 绑定鼠标拖动事件
        self.float_window.bind("<ButtonPress-1>", self._start_drag)
        self.float_window.bind("<B1-Motion>", self._drag_window)

        # 隐藏主窗口 (可选：如果想完全隐藏主UI)
        self.master.withdraw()

        self.float_window.protocol("WM_DELETE_WINDOW", self.exit_float_mode)

        # 标记当前模式
        self.is_float_mode = True

        self._create_float_widgets(self.float_window)

    def _create_float_widgets(self, parent):
        """填充悬浮窗的UI内容 (日志、按钮)，并使其紧凑。"""

        # 1. 控件容器 Frame：减小 padding 和 borderwidth
        # padding=1 边距非常小
        main_frame = ttk.Frame(parent, padding=1, relief=tk.RAISED, borderwidth=1)
        main_frame.pack(fill=tk.BOTH, expand=True)

        # 2. 按钮 Frame：移除垂直间距 pady
        button_frame = ttk.Frame(main_frame)
        button_frame.pack(fill=tk.X)  # 移除 pady=(0, 5)

        # 停止按钮：简化文本
        float_stop_button = ttk.Button(button_frame, text="停止", command=self.stop_bot)
        # padx=1 减小按钮间的水平间距
        float_stop_button.pack(side=tk.LEFT, padx=1, fill=tk.X, expand=True)
        self.float_stop_button = float_stop_button

        # 退出悬浮模式按钮：简化文本
        exit_float_button = ttk.Button(button_frame, text="主界面", command=self.exit_float_mode)
        exit_float_button.pack(side=tk.LEFT, padx=1, fill=tk.X, expand=True)

        # 退出程序按钮：简化文本
        exit_app_button = ttk.Button(button_frame, text="退出", command=self.master.destroy)
        exit_app_button.pack(side=tk.LEFT, padx=1)

        # 3. 日志显示：减小默认尺寸
        # height=5, width=30 减少日志区域的最小尺寸
        LOG_FONT = ("Cascadia Mono", 8)  # 或 ("Arial", 10), ("Courier New", 7) 等

        self.float_log_display = scrolledtext.ScrolledText(main_frame, state='disabled', wrap=tk.WORD,
                                                           bg="#2b2b2b", fg="white",
                                                           height=10, width=40,
                                                           font=LOG_FONT)  # <<<--- 新增 font 参数

        self.float_log_display.pack(fill=tk.BOTH, expand=True)

        # 初始化按钮状态
        self._update_float_button_state()

    def exit_float_mode(self):
        """退出悬浮窗模式，返回主控制面板。"""
        if self.float_window:
            self.float_window.destroy()
            self.float_window = None
            self.is_float_mode = False
            self.master.deiconify()  # 显示主窗口

    # --- 悬浮窗拖动逻辑 ---
    def _start_drag(self, event):
        """记录鼠标点击时的初始位置。"""
        self._x = event.x
        self._y = event.y

    def _drag_window(self, event):
        """计算并移动窗口。"""
        deltax = event.x - self._x
        deltay = event.y - self._y
        x = self.float_window.winfo_x() + deltax
        y = self.float_window.winfo_y() + deltay
        self.float_window.geometry(f"+{x}+{y}")

    def _update_float_button_state(self):
        """同步更新悬浮窗的停止按钮状态。"""
        if self.is_float_mode and hasattr(self, 'float_stop_button'):
            is_running = (self.bot_thread and self.bot_thread.is_alive())
            self.float_stop_button.config(state=tk.NORMAL if is_running else tk.DISABLED)

    def _create_main_widgets(self):
        """创建主控制面板的所有控件。"""
        # --- 新增: 创建Notebook作为选项卡容器 ---
        self.notebook = ttk.Notebook(self.master)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        # --- 创建第一个选项卡：控制面板 ---
        control_panel_frame = ttk.Frame(self.notebook, padding="5")
        self.notebook.add(control_panel_frame, text='控制面板')
        self._create_control_panel_tab(control_panel_frame)

        # --- 创建第二个选项卡：全局变量调试 ---
        globals_frame = ttk.Frame(self.notebook, padding="10")
        self.notebook.add(globals_frame, text='全局变量调试')
        self._create_globals_tab(globals_frame)

    def _create_control_panel_tab(self, parent_frame):
        """填充“控制面板”选项卡的内容"""
        self.paned_window = ttk.PanedWindow(parent_frame, orient=tk.VERTICAL)
        self.paned_window.pack(fill=tk.BOTH, expand=True)

        top_frame = ttk.Frame(self.paned_window, padding="5")
        self.paned_window.add(top_frame, weight=0)

        style = ttk.Style()
        style.configure("Warning.TLabel", foreground="red")

        config_frame = ttk.LabelFrame(top_frame, text="配置")
        config_frame.pack(fill=tk.X, expand=True, side=tk.TOP, pady=(0, 5))

        mode_frame = ttk.Frame(config_frame)
        mode_frame.pack(fill=tk.X, padx=2, pady=2)
        ttk.Label(mode_frame, text="模式:").pack(side=tk.LEFT)
        # --- 修改：为Radiobutton添加command回调 ---
        ttk.Radiobutton(mode_frame, text="单曲模式", variable=self.mode_var, value='single', command=self._on_mode_change).pack(side=tk.LEFT, padx=5)
        ttk.Radiobutton(mode_frame, text="全自动模式", variable=self.mode_var, value='full_auto', command=self._on_mode_change).pack(side=tk.LEFT, padx=5)

        warnings_frame = ttk.Frame(mode_frame)
        warnings_frame.pack(side=tk.RIGHT, padx=(10, 0))

        options_frame = ttk.Frame(config_frame)
        options_frame.pack(fill=tk.X, padx=2, pady=2)

        ttk.Label(options_frame, text="难度:").pack(side=tk.LEFT)
        # --- 修改：将控件保存为实例变量，用于定位 ---
        self.difficulty_combobox = ttk.Combobox(options_frame, textvariable=self.difficulty_var,
                                                values=['easy', 'normal', 'hard', 'expert', 'special'], width=10)
        self.difficulty_combobox.pack(side=tk.LEFT, padx=3)

        # --- 修改：将FC相关控件放入独立的Frame中 ---
        self.fc_options_frame = ttk.Frame(options_frame)
        ttk.Label(self.fc_options_frame, text="未FC跳过阈值：").pack(side=tk.LEFT, padx=(5, 0))
        ttk.Spinbox(self.fc_options_frame, from_=1, to=99, textvariable=self.max_continuous_not_fc_var, width=5).pack(side=tk.LEFT, padx=3)
        ttk.Label(self.fc_options_frame, text="最大尝试次数：").pack(side=tk.LEFT, padx=(5, 0))
        ttk.Spinbox(self.fc_options_frame, from_=1, to=99, textvariable=self.max_attempt_var, width=5).pack(side=tk.LEFT, padx=3)

        self.human_delay_frame = ttk.Frame(options_frame)
        self.human_delay_frame.pack(side=tk.LEFT, padx=5)
        ttk.Checkbutton(self.human_delay_frame, text="随机化按键", variable=self.human_var).pack(side=tk.LEFT)

        # 警告标签
        self.human_delay_warning_label = ttk.Label(warnings_frame, text="警告：可能导致不能FC", style="Warning.TLabel")

        self.bg_color = style.lookup("TFrame", "background")

        self.human_delay_warning_label.pack(anchor='w')

        self._update_warnings()

        control_frame = ttk.Frame(top_frame)
        control_frame.pack(fill=tk.X, expand=True, side=tk.TOP, pady=(5, 0))
        self.start_button = ttk.Button(control_frame, text="启动任务", command=self.start_bot)
        self.start_button.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=2)
        self.stop_button = ttk.Button(control_frame, text="停止任务", command=self.stop_bot, state=tk.DISABLED)
        self.stop_button.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=2)
        self.float_button = ttk.Button(control_frame, text="进入悬浮窗", command=self._create_float_window)
        self.float_button.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=2) # 占据剩余空间

        log_frame = ttk.Frame(self.paned_window, padding="0")
        self.paned_window.add(log_frame, weight=1)
        self.log_display = scrolledtext.ScrolledText(log_frame, state='disabled', wrap=tk.WORD, bg="#2b2b2b",
                                                     fg="white")
        self.log_display.pack(fill=tk.BOTH, expand=True)

        self.paned_window.bind("<ButtonPress-1>", self._prevent_resize)
        self.paned_window.bind("<B1-Motion>", self._prevent_resize)

        # --- 新增：在UI创建完成后，调用一次模式切换函数来设置初始状态 ---
        self._on_mode_change()

    # --- 新增方法：用于根据模式动态显示/隐藏UI控件 ---
    def _on_mode_change(self):
        """当模式（单曲/全自动）切换时，动态显示或隐藏相关配置项。"""
        mode = self.mode_var.get()
        if mode == 'full_auto':
            # 在全自动模式下：显示FC相关设置
            self.fc_options_frame.pack(side=tk.LEFT, after=self.difficulty_combobox)
        elif mode == 'single':
            # 在单曲模式下：隐藏FC相关设置
            self.fc_options_frame.pack_forget()

    def _create_globals_tab(self, parent_frame):
        """填充“全局变量调试”选项卡的内容"""
        # 定义需要暴露的全局变量及其类型 (int, float, dict)
        self.editable_globals = {
            'PHOTOGATE_LATENCY': int,
            'MIN_LIVEBOOST': int,
            'DEFAULT_MOVE_SLICE_SIZE': int,
            'CMD_SLICE_SIZE': int,
            'MAX_CONTINUOUS_FAILED_TIMES': int,
            'STABLE_THRESHOLD': int,
            'CONSECUTIVE_FRAMES_NEEDED': int,
            'FREEZE_SLEEP_TIME': float,
            'CONFIDENCE_THRESHOLD_FAILURE': float,
            'CONFIDENCE_THRESHOLD_PLAY': float,
            'IS_FULL_SONG': bool,
            'IS_HIGH_DIFFICULTY': bool
        }

        # --- 新增：本地化文本映射 ---
        localization_map = {
            'PHOTOGATE_LATENCY': "光电门延迟 (ms)",
            'MIN_LIVEBOOST': "最小 LiveBoost 值",
            'DEFAULT_MOVE_SLICE_SIZE': "滑动音符切片大小",
            'CMD_SLICE_SIZE': "指令分片大小",
            'MAX_CONTINUOUS_FAILED_TIMES': "最大连续失败次数",
            'STABLE_THRESHOLD': "画面静止判定阈值",
            'CONSECUTIVE_FRAMES_NEEDED': "画面静止所需帧数",
            'FREEZE_SLEEP_TIME': "画面静止检测间隔时间 (s)",
            'CONFIDENCE_THRESHOLD_FAILURE': "失败检测置信度",
            'CONFIDENCE_THRESHOLD_PLAY': "歌曲开始检测置信度",
            'IS_FULL_SONG': "FULL乐曲支持",
            'IS_HIGH_DIFFICULTY': "超高难易度支持"
        }

        # 使用 grid 布局
        parent_frame.columnconfigure(1, weight=1)

        # 动态创建 Label 和 Entry
        current_row = 0
        for var_name, var_type in self.editable_globals.items():
            # --- 修改：使用本地化文本 ---
            # 使用 .get() 方法，如果映射中没有找到，则安全地回退到原始变量名
            display_name = localization_map.get(var_name, var_name)
            ttk.Label(parent_frame, text=f"{display_name}:", font=("", 10)).grid(row=current_row, column=0, sticky='w',
                                                                                 padx=5, pady=5)

            if var_type == dict:
                dict_frame = ttk.Frame(parent_frame)
                dict_frame.grid(row=current_row, column=1, sticky='ew', padx=5, pady=2)
                self.global_vars_entries[var_name] = {}

                # 获取字典的当前值
                current_dict = getattr(autodori_ui, var_name)

                col_count = 0
                for key, value in current_dict.items():
                    ttk.Label(dict_frame, text=key).grid(row=0, column=col_count, padx=(0, 2))
                    entry_var = tk.StringVar(value=str(value))
                    entry = ttk.Entry(dict_frame, textvariable=entry_var, width=8)
                    entry.grid(row=0, column=col_count + 1, padx=(0, 10))

                    self.global_vars_entries[var_name][key] = entry_var
                    col_count += 2

            elif var_type == bool:
                # 使用 tk.BooleanVar 来存储复选框的状态 (True/False)
                bool_var = tk.BooleanVar(value=getattr(autodori_ui, var_name))
                checkbutton = ttk.Checkbutton(parent_frame, variable=bool_var)
                checkbutton.grid(row=current_row, column=1, sticky='w', padx=5, pady=2)  # 使用 sticky='w' 左对齐
                self.global_vars_entries[var_name] = bool_var

            else:  # int or float
                entry_var = tk.StringVar(value=str(getattr(autodori_ui, var_name)))
                entry = ttk.Entry(parent_frame, textvariable=entry_var)
                entry.grid(row=current_row, column=1, sticky='ew', padx=5, pady=2)
                self.global_vars_entries[var_name] = entry_var

            current_row += 1

        # 添加分隔符
        ttk.Separator(parent_frame, orient='horizontal').grid(row=current_row, column=0, columnspan=2, sticky='ew',
                                                              pady=15)
        current_row += 1

        # 添加按钮
        button_frame = ttk.Frame(parent_frame)
        button_frame.grid(row=current_row, column=0, columnspan=2, sticky='e')

        apply_button = ttk.Button(button_frame, text="应用修改", command=self._apply_global_settings)
        apply_button.pack(side=tk.RIGHT, padx=5)

    def _apply_global_settings(self):
        """将UI中的值应用到后端的全局变量"""
        try:
            for var_name, controls in self.global_vars_entries.items():
                var_type = self.editable_globals[var_name]

                if var_type == dict:
                    current_dict = getattr(autodori_ui, var_name)
                    for key, entry_var in controls.items():
                        new_val_str = entry_var.get()
                        # 尝试转换为 float，如果失败再转为 int
                        try:
                            current_dict[key] = float(new_val_str)
                        except ValueError:
                            current_dict[key] = int(new_val_str)
                else:  # int or float
                    new_val_str = controls.get()
                    if var_type == int:
                        setattr(autodori_ui, var_name, int(new_val_str))
                    elif var_type == float:
                        setattr(autodori_ui, var_name, float(new_val_str))
                    elif var_type == bool:
                        setattr(autodori_ui, var_name, new_val_str)

            messagebox.showinfo("成功", "全局变量已成功更新！")

        except ValueError as e:
            messagebox.showerror("输入错误", f"修改失败，请输入有效的数值。\n错误: {e}")
        except Exception as e:
            messagebox.showerror("未知错误", f"应用设置时发生错误: {e}")

    def _process_log_queue(self):
        try:
            while True:
                record = self.log_queue.get(block=False)
                # --- 更新主窗口日志 ---
                if hasattr(self, 'log_display'):
                    self.log_display.configure(state='normal')
                    self.log_display.insert(tk.END, record + '\n')
                    self.log_display.see(tk.END)
                    self.log_display.configure(state='disabled')

                # --- 更新悬浮窗日志 ---
                # --- 更新悬浮窗日志 (格式化) ---
                if self.is_float_mode and hasattr(self, 'float_log_display'):

                    # 原始格式: YYYY-MM-DD HH:MM:SS - [LEVEL] - MESSAGE

                    try:
                        # 1. 查找第一个分隔符 ' - [' 的起始位置 (用于时间)
                        time_separator_index = record.find(' - [')

                        # 2. 查找第二个分隔符 '] - ' 的起始位置 (用于消息)
                        msg_separator_index = record.find('] - ')

                        if time_separator_index != -1 and msg_separator_index != -1:

                            # 提取 HH:MM:SS (从索引 11 开始到 time_separator_index 之前)
                            time_part = record[time_separator_index - 8:time_separator_index]  # 提取 HH:MM:SS

                            # 提取 [LEVEL] (从 time_separator_index + 3 开始，到 msg_separator_index 结束)
                            level_part = record[time_separator_index + 3: msg_separator_index + 1]  # 提取 [LEVEL]

                            # 提取 MESSAGE (从 msg_separator_index + 4 开始)
                            message_part = record[msg_separator_index + 4:].strip()  # 提取 MESSAGE 并去除首尾空格

                            # 组合新的紧凑格式: HH:MM:SS [LEVEL]消息
                            # 注意: level_part 已经是 '[LEVEL]' 的形式，后面紧跟 message_part
                            compact_record = f"{time_part}{level_part}{message_part}"
                        else:
                            compact_record = record.split(" - ", 2)[-1]  # 无法解析时，只保留消息

                    except Exception:
                        # 出现异常时（如索引错误），退回到只保留消息
                        compact_record = record.split(" - ", 2)[-1]

                    self.float_log_display.configure(state='normal')
                    self.float_log_display.insert(tk.END, compact_record + '\n')
                    self.float_log_display.see(tk.END)
                    self.float_log_display.configure(state='disabled')
        except queue.Empty:
            pass
        self.master.after(100, self._process_log_queue)

    def start_bot(self):
        self.start_button.config(state=tk.DISABLED)
        self.stop_button.config(state=tk.NORMAL)
        self._update_float_button_state() # <<<--- 新增：调用状态更新
        # --- 新增: 禁用全局变量选项卡 ---
        self.notebook.tab(1, state='disabled')

        config_data = {
            "mode": self.mode_var.get(),
            "difficulty": self.difficulty_var.get(),
            "human_delay": self.human_var.get(),
            "max_not_fc_count": self.max_continuous_not_fc_var.get(),
            "max_attempt_count": self.max_attempt_var.get()
        }
        self.bot_thread = threading.Thread(target=self._run_bot_task, args=(config_data,), daemon=True)
        self.bot_thread.start()

    def stop_bot(self):
        # 优先设置停止信号，让内部循环能够尽快响应
        if hasattr(autodori_ui, 'stop_event'):
            logging.info("Sending stop signal to internal loop.")
            autodori_ui.stop_event.set()  # <-- 这是关键的新增行

        # 然后再通知maatasker停止任务流
        if hasattr(autodori_ui, 'maatasker') and autodori_ui.maatasker and autodori_ui.maatasker.running:
            logging.info("Attempting to stop maatasker.")
            autodori_ui.maatasker.post_stop()
        else:
            pass

    def _run_bot_task(self, config_data):
        try:
            logging.info("Initialising.")
            autodori_ui.init()
            logging.info("Initialisation complete.")

            mode = config_data.get("mode")
            if mode == 'full_auto':
                autodori_ui.run_full_auto_mode(config_data)
            elif mode == 'single':
                autodori_ui.run_single_mode_free(config_data)
            elif mode == 'medley':
                autodori_ui.run_single_mode_free(config_data)

            logging.info("Task completed.")
        except Exception as e:
            logging.error(f"Exception: {e}", exc_info=True)
        finally:
            self.start_button.config(state=tk.NORMAL)
            self.stop_button.config(state=tk.DISABLED)
            # --- 新增: 恢复全局变量选项卡 ---
            self.notebook.tab(1, state='normal')
            self._update_float_button_state()  # <<<--- 新增：调用状态更新

    def _prevent_resize(self, event):
        """拦截并阻止PanedWindow的尺寸调整事件。"""
        return "break"

    def _update_warnings(self, *args):
        """
        通过改变文字颜色来显示或隐藏警告，而不是从布局中移除它们。
        这可以保持布局的稳定性。
        """
        # 检查 '随机化' 的状态
        if self.human_var.get():
            # 勾选时，设置为红色
            self.human_delay_warning_label.config(foreground='red')
        else:
            # 未勾选时，设置为背景色（隐形）
            self.human_delay_warning_label.config(foreground=self.bg_color)


# --- 程序入口 ---
if __name__ == "__main__":
    # 仅在Windows平台上执行此操作
    if sys.platform == 'win32':
        # 创建一个 STARTUPINFO 对象，用于更精细地控制新进程的创建
        startupinfo = subprocess.STARTUPINFO()
        # 设置 dwFlags 标志位，告诉系统我们要自己控制窗口的显示方式
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        # wShowWindow 标志位可以被设置为 SW_HIDE (值为0) 来彻底隐藏窗口
        # 通常上面的 dwFlags 设置已经足够，但为了保险起见可以加上
        # startupinfo.wShowWindow = 0 # 0 means SW_HIDE

        # 使用 functools.partial 创建一个新的 Popen “版本”
        # 这个新版本会自动将我们配置好的 startupinfo 作为默认参数传入
        # 然后用这个新版本覆盖（“猴子补丁”）标准的 subprocess.Popen
        # 这样，程序中所有（包括库里）对 subprocess.Popen 的调用都会自动应用我们的设置，从而隐藏窗口
        subprocess.Popen = partial(subprocess.Popen, startupinfo=startupinfo)
    # --- 新增代码结束 ---
    root = tk.Tk()
    app = AutodoriGUI(root)
    root.mainloop()