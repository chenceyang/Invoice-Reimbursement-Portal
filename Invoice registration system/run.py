import os
import sys


# PaddleOCR 安装在项目专用环境中。即使终端误用了上级目录的 Python，
# 也自动切换到正确解释器，避免悄悄退回耗时较长的 Windows OCR。
project_python = os.path.join(os.path.dirname(__file__), ".venv_paddle", "Scripts", "python.exe")
if os.path.isfile(project_python) and os.path.normcase(sys.executable) != os.path.normcase(project_python):
    os.execv(project_python, [project_python, os.path.abspath(__file__), *sys.argv[1:]])

from app import create_app

app = create_app()

if __name__ == "__main__":
    # 禁用重载器，避免创建两套进程并重复加载 Paddle 模型。
    app.run(debug=False, use_reloader=False, threaded=True)
