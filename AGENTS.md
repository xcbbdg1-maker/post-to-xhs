# 仓库指南

## 项目结构与模块组织
本仓库是一个用于小红书自动化的 Python Skill 包。
- `scripts/` 包含全部可执行逻辑：`publish_pipeline.py`（主流程）、`cdp_publish.py`（CDP 自动化与账号命令）、`chrome_launcher.py`（Chrome 生命周期管理）以及辅助模块（`image_downloader.py`、`account_manager.py`、`run_lock.py`）。
- `config/accounts.json.example` 是账号与 Profile 的配置模板。
- `docs/` 存放集成说明（例如 `docs/claude-code-integration.md`）。
- `images/publish_temp/` 是临时素材目录占位。
- 根目录关键文件：`README.md`、`SKILL.md`、`requirements.txt`。

## 文件修改 /工作流
- 当新增或者修改功能之后，注意同步修改 SKILL.md 以及 README.md
- 新增功能时，建议在 plan.md 中先规划好，再实现，然后优化其中的内容


## 构建、测试与开发命令
- `python -m venv .venv && source .venv/bin/activate`：创建并激活本地虚拟环境。
- `pip install -r requirements.txt`：安装运行依赖。
- `python scripts/chrome_launcher.py`：启动启用 CDP 的 Chrome（有界面）。
- `python scripts/cdp_publish.py check-login`：检查当前登录状态。
- `python scripts/publish_pipeline.py --headless --title "T" --content "C" --image-urls "https://example.com/a.jpg"`：执行端到端预览流程，默认不点击发布。
- `python scripts/publish_pipeline.py --headless --auto-publish --title "T" --content "C" --image-urls "https://example.com/a.jpg"`：用户确认最终内容后执行实际发布。
- `python scripts/chrome_launcher.py --kill`：关闭测试浏览器。

## 代码风格与命名规范
- 遵循 PEP 8，使用 4 空格缩进，并为模块提供清晰 docstring。
- 函数/变量使用 `snake_case`，类使用 `PascalCase`，并显式添加类型注解（如 `list[str]`、`str | None`）。
- CLI 参数优先使用长参数名，`argparse` 说明保持清晰、可读。
- 日志输出沿用 `[module]` 前缀风格，便于排查。
- 时序抖动只用于降低页面操作节奏突变，不得用于绕过平台限制或规避风控规则。

## 测试指南
提交 PR 前先运行纯离线测试：
- `python -B -m unittest discover -s tests -p "test_*.py" -v`
- 测试必须保持离线，不得启动 Chrome、连接小红书或执行发布/互动。
- 如需人工冒烟，默认先在测试账号执行非破坏的预览流程；只有用户确认最终标题、正文和媒体后，实际发布命令才可显式添加 `--auto-publish`。
- 新测试放在 `tests/`，文件命名为 `test_*.py`，并同步维护 Windows CI。


## 安全与配置建议
- 禁止提交真实 Cookie、账号令牌或个人 Chrome Profile 路径。
- 本地账号配置请基于 `config/accounts.json.example` 复制为未跟踪的 `config/accounts.json` 后再使用。
