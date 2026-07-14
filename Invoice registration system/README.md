# 发票登记系统

Flask + Vue 3 + MySQL 的发票登记、审核和核销系统。Vue 运行文件已放在项目中，前端不需要单独执行 `npm install` 或 `npm run dev`。

## 环境要求

- Windows 10/11
- Python 3.10 或兼容版本
- MySQL 8.0+

## 首次安装

1. 创建虚拟环境并安装依赖：

   ```powershell
   py -m venv .venv_paddle
   .\.venv_paddle\Scripts\python.exe -m pip install -r requirements.txt
   ```

2. 复制环境变量示例并填写数据库密码：

   ```powershell
   Copy-Item .env.example .env
   ```

3. 新数据库请在 MySQL 中执行 [database/schema.sql](database/schema.sql)。脚本不会写入员工、发票或密码数据。

   已有数据库升级到当前结构时执行：

   ```powershell
   .\.venv_paddle\Scripts\python.exe .\database\upgrade.py
   ```

   升级程序会记录版本并可重复运行，不会清空已有业务数据。

4. 在 `users` 表中创建员工或管理员账号。`password` 必须保存 Werkzeug 生成的密码哈希，不能保存明文密码。

## 启动

```powershell
.\.venv_paddle\Scripts\python.exe .\run.py
```

打开 <http://127.0.0.1:5000>。OCR 模型会在后台预热，首次识别所需时间可能稍长。

## 项目结构

- `app/`：Flask 后端、Vue 页面及静态文件
- `database/schema.sql`：与当前代码匹配的完整数据库结构
- `database/upgrade.py`：已有数据库的幂等升级程序
- `requirements.txt`：当前 Python 直接依赖及已验证版本
- `.env.example`：数据库与 OCR 配置模板
