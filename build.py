import json
import os
import shutil
import site
import sys
import zipfile
import argparse
import requests
from io import BytesIO
from zipfile import ZipFile
import platform
import re
import subprocess

import PyInstaller.__main__

parser = argparse.ArgumentParser()
# 保留 version 参数作为手动覆盖的备选项（防备某些没有 git 环境的机器）
parser.add_argument(
    "--version", type=str, help="Override the version manually (optional)", default=None
)
args = parser.parse_args()

# ==================== 1. 自动获取版本号 (从 Git Commit) ====================
if args.version:
    VERSION = args.version
else:
    VERSION = "unknown"
    try:
        # 获取最新的一次 commit message
        commit_msg = subprocess.check_output(['git', 'log', '-1', '--pretty=%B'], text=True).strip()

        # 使用正则匹配类似 "autodoriUI v0.1" 或 "autodoriUI v1.0.2" 的格式，提取 'v' 后面的内容
        match = re.search(r'autodoriUI\s+(v[0-9\.]+)', commit_msg, re.IGNORECASE)
        if match:
            VERSION = match.group(1)
            print(f"✅ Successfully extracted version from Git: {VERSION}")
        else:
            print(
                f"⚠️ Warning: Could not find 'autodoriUI vX.X' format in commit message: '{commit_msg}'. Using 'unknown'.")
    except Exception as e:
        print(f"⚠️ Warning: Failed to get git commit (Is this a git repository?). Error: {e}")

# ==================== 2. 自动获取操作系统和架构 ====================
# 获取操作系统名 (windows, linux, darwin)
os_name = platform.system().lower()
if os_name == 'darwin':
    os_name = 'macos'  # 将 darwin 转换为大家更习惯的 macos

# 获取架构名 (amd64, x86_64, arm64 等)
arch_name = platform.machine().lower()
if arch_name in ['amd64', 'x86_64']:
    arch_name = 'x64'  # 统一 x86_64 和 amd64 的叫法

# ==================== 3. 生成最终的文件名 ====================
ZIP_FILENAME = f"autodoriUI_{VERSION}_{os_name.capitalize()}_{arch_name.capitalize()}.zip"
print(f"📦 Target Build Archive: {ZIP_FILENAME}")


# 获取当前工作目录
current_dir = os.getcwd()

# 获取 site-packages 目录列表
site_packages_paths = site.getsitepackages()

# 查找包含 maa/bin 的路径
maa_bin_path = None
for path in site_packages_paths:
    potential_path = os.path.join(path, "maa", "bin")
    if os.path.exists(potential_path):
        maa_bin_path = potential_path
        break

if maa_bin_path is None:
    raise FileNotFoundError("Path containing maa/bin not found")

# 构建 --add-data 参数
add_data_param = f"{maa_bin_path}{os.pathsep}maa/bin"

# 查找包含 MaaAgentBinary 的路径
maa_bin_path2 = None
for path in site_packages_paths:
    potential_path = os.path.join(path, "MaaAgentBinary")
    if os.path.exists(potential_path):
        maa_bin_path2 = potential_path
        break

if maa_bin_path2 is None:
    raise FileNotFoundError("Path containing MaaAgentBinary not found")

# 构建 --add-data 参数
add_data_param2 = f"{maa_bin_path2}{os.pathsep}MaaAgentBinary"


# 下载 minitouch.zip 并解压
def download_and_extract_minitouch():
    # GitHub 最新 release 页面
    url = "https://github.com/EvATive7/minitouch/releases/latest/download/minitouch.zip"

    # 下载文件
    print("Downloading minitouch.zip...")
    response = requests.get(url)
    if response.status_code == 200:
        with ZipFile(BytesIO(response.content)) as zip_ref:
            # 解压到临时目录
            temp_dir = os.path.join(current_dir, "minitouch_temp")
            if os.path.exists(temp_dir):
                shutil.rmtree(temp_dir)  # 清理旧的临时文件夹
            zip_ref.extractall(temp_dir)
            print("minitouch.zip downloaded and extracted.")
            return temp_dir
    else:
        raise Exception(f"Failed to download minitouch.zip: {response.status_code}")


# 将 minitouch 文件夹移动到 dist/assets/minitouch_EvATive7 目录
def move_minitouch_to_assets(temp_dir):
    minitouch_src_dir = os.path.join(temp_dir, "minitouch")
    minitouch_dest_dir = os.path.join(
        current_dir, "dist", "assets", "minitouch_EvATive7"
    )

    if os.path.exists(minitouch_dest_dir):
        shutil.rmtree(minitouch_dest_dir)  # 如果目标目录已存在，先删除

    shutil.copytree(minitouch_src_dir, minitouch_dest_dir)

    if os.path.exists(temp_dir):
        shutil.rmtree(temp_dir)
    print(f"minitouch files have been copied to {minitouch_dest_dir}")


# 复制 assets 文件夹到 dist 目录
dist_dir = os.path.join(current_dir, "dist")
assets_source_path = os.path.join(current_dir, "assets")
assets_dest_path = os.path.join(dist_dir, "assets")
syc_bat_source_path = os.path.join(current_dir, "syc.bat")
syc_bat_dest_path = os.path.join(dist_dir, "syc.bat")
metedata_file_path = os.path.join(assets_dest_path, "build_metadata.json")

if not os.path.exists(assets_source_path):
    raise FileNotFoundError("assets folder not found")

# 如果目标路径存在，先删除它
if os.path.exists(dist_dir):
    shutil.rmtree(dist_dir)

# 运行 PyInstaller 打包命令
command = [
    "src/gui.py",
    "--onefile",
    "--name=autodoriUI.exe",
    f"--add-data={add_data_param}",
    f"--add-data={add_data_param2}",
    "--noconsole"
    # "--clean",
]
if sys.platform == "win32":
    command.append(
        f'--add-binary={os.path.join(current_dir, "assets", "misc", "windows", "dll", "msvcp140.dll")}{os.pathsep}.'
    )
    command.append(
        f'--add-binary={os.path.join(current_dir, "assets", "misc", "windows", "dll", "vcruntime140.dll")}{os.pathsep}.'
    )

print(" ".join(command))
PyInstaller.__main__.run(command)


# 使用 shutil 复制整个文件夹
shutil.copytree(
    assets_source_path,
    assets_dest_path,
    ignore=lambda dirname, _: (
        ["misc", "MaaCommonAssets"] if os.path.basename(dirname) else []
    ),
)
# 删除OCR模型
ocr_model_path = os.path.join(assets_dest_path, "resource", "model", "ocr")
if os.path.exists(ocr_model_path):
    shutil.rmtree(ocr_model_path)
# 复制OCR模型
shutil.copytree(
    os.path.join(current_dir, "assets", "MaaCommonAssets", "OCR", "ppocr_v3", "zh_cn"),
    ocr_model_path,
    ignore=lambda *_: ["README.md"],
    dirs_exist_ok=True,
)
shutil.copytree(
    os.path.join(current_dir, "assets", "MaaCommonAssets", "OCR", "ppocr_v5", "zh_cn"),
    os.path.join(ocr_model_path, "ppocr_v5", "zh_cn"),
    ignore=lambda dirname, _: (
        ["misc", "MaaCommonAssets"] if os.path.basename(dirname) else []
    ),
    dirs_exist_ok=True,
)
temp_dir = download_and_extract_minitouch()
move_minitouch_to_assets(temp_dir)
json.dump(
    {"version": args.version},
    open(metedata_file_path, "w", encoding="utf-8"),
    ensure_ascii=False,
)

# # 复制 syc.bat 文件
# if os.path.exists(syc_bat_source_path):
#     shutil.copy(syc_bat_source_path, syc_bat_dest_path)
# else:
#     raise FileNotFoundError("syc.bat file not found")

# 压缩 dist 文件夹为 zip 文件，并保存在 dist 目录中
zip_filepath = os.path.join(dist_dir, ZIP_FILENAME)

with zipfile.ZipFile(zip_filepath, "w", zipfile.ZIP_DEFLATED) as zipf:
    for root, dirs, files in os.walk(dist_dir):
        for file in files:
            # 获取文件的绝对路径并相对路径
            file_path = os.path.join(root, file)
            # 跳过刚生成的压缩包
            if file == ZIP_FILENAME:
                continue
            arcname = os.path.relpath(file_path, dist_dir)
            zipf.write(file_path, arcname)

# 删除 dist 文件夹中的所有文件和文件夹，保留压缩包
for root, dirs, files in os.walk(dist_dir):
    for file in files:
        file_path = os.path.join(root, file)
        # 不删除生成的压缩包
        if file != ZIP_FILENAME:
            os.remove(file_path)
    for dir in dirs:
        shutil.rmtree(os.path.join(root, dir), ignore_errors=True)


print(f"Packaging and compression completed: {zip_filepath}")
