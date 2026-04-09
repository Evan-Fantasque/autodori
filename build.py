import os
import shutil
import site
import sys
import zipfile
import argparse
import platform
import re
import subprocess
import PyInstaller.__main__

parser = argparse.ArgumentParser()
parser.add_argument("--version", type=str, help="Override the version manually", default=None)
args = parser.parse_args()

# ==================== 1 & 2. 获取版本号、系统与架构 ====================
VERSION = args.version or "unknown"
if not args.version:
    try:
        commit_msg = subprocess.check_output(['git', 'log', '-1', '--pretty=%B'], text=True).strip()
        match = re.search(r'autodoriUI\s+(v[0-9\.]+)', commit_msg, re.IGNORECASE)
        if match:
            VERSION = match.group(1)
            print(f"Successfully extracted version: {VERSION}")
    except Exception:
        pass

os_name = platform.system().lower()
os_name = 'macos' if os_name == 'darwin' else os_name
arch_name = 'x64' if platform.machine().lower() in ['amd64', 'x86_64'] else platform.machine().lower()

ZIP_FILENAME = f"autodoriUI_{VERSION}_{os_name.capitalize()}_{arch_name.capitalize()}.zip"
print(f"Target Build Archive: {ZIP_FILENAME}")

# ==================== 3. 寻找依赖路径 ====================
current_dir = os.getcwd()
site_packages_paths = site.getsitepackages()

def find_package_path(*subpaths):
    for path in site_packages_paths:
        potential_path = os.path.join(path, *subpaths)
        if os.path.exists(potential_path):
            return potential_path
    raise FileNotFoundError(f"Path containing {os.path.join(*subpaths)} not found")

maa_bin_path = find_package_path("maa", "bin")
maa_bin_path2 = find_package_path("MaaAgentBinary")

# ==================== 4. 运行 PyInstaller ====================
dist_dir = os.path.join(current_dir, "dist")
if os.path.exists(dist_dir):
    shutil.rmtree(dist_dir)

command = [
    "src/gui.py",
    "--onefile",
    "--name=autodoriUI.exe",
    f"--add-data={maa_bin_path}{os.pathsep}maa/bin",
    f"--add-data={maa_bin_path2}{os.pathsep}MaaAgentBinary",
    "--noconsole"
]
if sys.platform == "win32":
    command.append(f'--add-binary={os.path.join(current_dir, "assets", "misc", "windows", "dll", "msvcp140.dll")}{os.pathsep}.')
    command.append(f'--add-binary={os.path.join(current_dir, "assets", "misc", "windows", "dll", "vcruntime140.dll")}{os.pathsep}.')

PyInstaller.__main__.run(command)

# 明确我们要塞进 exe 的轻量级目录
image_src = os.path.join(current_dir, "assets", "resource", "image")
pipeline_src = os.path.join(current_dir, "assets", "resource", "pipeline")

# ==================== 4. 运行 PyInstaller ====================
dist_dir = os.path.join(current_dir, "dist")
if os.path.exists(dist_dir):
    shutil.rmtree(dist_dir)

command = [
    "src/gui.py",
    "--onefile",
    "--name=autodoriUI.exe",
    f"--add-data={maa_bin_path}{os.pathsep}maa/bin",
    f"--add-data={maa_bin_path2}{os.pathsep}MaaAgentBinary",
    # 新增：将 image 和 pipeline 文件夹打包进 exe 的临时目录中对应的路径
    f"--add-data={image_src}{os.pathsep}assets/resource/image",
    f"--add-data={pipeline_src}{os.pathsep}assets/resource/pipeline",
    "--noconsole"
]

if sys.platform == "win32":
    command.append(f'--add-binary={os.path.join(current_dir, "assets", "misc", "windows", "dll", "msvcp140.dll")}{os.pathsep}.')
    command.append(f'--add-binary={os.path.join(current_dir, "assets", "misc", "windows", "dll", "vcruntime140.dll")}{os.pathsep}.')

print("执行打包命令:\n" + " ".join(command))
PyInstaller.__main__.run(command)

# ==================== 5. 打包 Zip ====================
zip_filepath = os.path.join(dist_dir, ZIP_FILENAME)

with zipfile.ZipFile(zip_filepath, "w", zipfile.ZIP_DEFLATED) as zipf:
    # 1. 写入生成的 exe 文件
    for file in os.listdir(dist_dir):
        if file.endswith(".exe") and file != ZIP_FILENAME:
            zipf.write(os.path.join(dist_dir, file), file)

# 清理 dist 目录下除了 zip 以外的多余文件
for file in os.listdir(dist_dir):
    if file != ZIP_FILENAME:
        file_path = os.path.join(dist_dir, file)
        if os.path.isfile(file_path):
            os.remove(file_path)

print(f"Packaging and compression completed: {zip_filepath}")