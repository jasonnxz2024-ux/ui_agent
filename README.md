# UAgent：React / Figma Make → LVGL / AiBuilder

作者：**jason gao**

UAgent 是用于嵌入式 HMI 开发的本地源码转换工作台：读取 React / Figma Make 工程，提取组件、资源和事件语义，通过适配器生成 LVGL C 代码及 AiBuilder / UIBuilder 工程，并记录转换结果与缺失项。

当前目标是验证“源码 → 语义模型 → 控件适配 → 可审计的 custom.c / custom.h”转换链路。项目仍在开发中，生成文件不代表所有视觉效果和业务行为都已完整还原。

## 主要能力与边界

- 从应用入口分析组件关系，组合屏幕对象树。
- 转换通用控件，以及图表、表格、Dropdown / Select、Carousel、长页面滚动等结构；具体结果取决于源码写法和适配覆盖范围。
- 复制源码中的实际图片资源，输出资源引用和审计信息。
- 可采集浏览器布局，辅助还原尺寸、位置和部分交互状态。
- 可选视频证据与模型辅助分析；基础静态分析不需要模型账号。
- 使用审计状态和警告说明 `native`、`partial`、`fallback` 等转换结果，不把未支持的行为当作完成。

项目不会执行完整的 React 运行时移植，也不会将整个页面压成一张图片代替控件转换。业务事件保留来源和审计信息，最终设备逻辑与回调仍需开发者接入。缺少可靠布局证据时，结果可能使用默认布局并标记为部分支持。

## 环境要求

推荐在 Windows 上运行当前桌面工作流。

| 项目 | 用途 |
| --- | --- |
| Python 3.11 或更高版本 | 后端、转换器和桌面启动器 |
| Node.js 与 npm | 构建工作台前端、运行 React 输入工程 |
| Git | 获取源码与版本管理 |
| Chrome 或 Edge | 可选的真实浏览器布局采集 |
| AiBuilder / UIBuilder | 打开生成工程，提供模拟器运行资源 |
| C/C++ 编译工具链与 CMake | 可选的模拟器构建与验收，按 UIBuilder 环境配置 |
| FFmpeg（含 ffprobe） | 可选的视频关键帧提取，需加入 PATH |

Python 依赖在 `pyproject.toml` 中声明；前端依赖在 `frontend/package.json` 中声明。仓库不附带虚拟环境、Node 依赖、UIBuilder 安装包或本地设计原稿。

当前生成器从 `D:\UIBuilder` 或 `C:\UIBuilder` 查找 UIBuilder 运行资源，要求其中存在 `tool/simulator`。模拟器运行资源按 UIBuilder 2.2 / LVGL 9.1.0 的目录结构复制；其他版本需要自行验证兼容性。安装在其他位置时，当前版本需调整 `generator/project.py` 中的 `_uibuilder_root()`。

## 安装与启动

下面命令在 **PowerShell** 中执行：

```powershell
git clone https://github.com/jasonnxz2024-ux/ui_agent.git
cd ui_agent
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
cd frontend
npm.cmd ci
npm.cmd run build
cd ..
.\.venv\Scripts\python.exe launcher.py
```

安装完成后也可双击根目录的 `run-uagent.cmd`。脚本使用自身所在目录，可以将仓库放在其他路径。私有仓库需要所有者先授予访问权限。

如果没有 Python Launcher（`py`），可用满足版本要求的 `python` 替换 `py -3`。不必激活虚拟环境，以上命令直接使用其解释器。`npm.cmd` 可避免 PowerShell 对 `npm.ps1` 的执行策略限制。

桌面启动器在 `127.0.0.1` 的空闲端口启动本地服务，并打开工作台窗口。首次使用前必须完成前端构建，否则后端不会挂载首页。

### 浏览器方式启动

如果桌面窗口无法打开，可在项目根目录直接启动服务：

```powershell
.\.venv\Scripts\python.exe -m uvicorn server.app:app --host 127.0.0.1 --port 8765
```

浏览器打开 `http://127.0.0.1:8765`。接口文档位于 `http://127.0.0.1:8765/docs`；健康检查为 `http://127.0.0.1:8765/api/health`。关闭终端中的服务可按 Ctrl+C。

## 如何转换一个源码工程

1. 准备 React / Figma Make **源码目录**，保留入口文件、组件及图片等资源。仅有 `.fig`、截图或视频不能替代源码输入。
2. 对需要浏览器布局采集的输入工程，先按该工程自身说明安装依赖并确认能运行。浏览器采集可能在输入目录准备运行依赖和证据文件，建议使用输入工程的副本。
3. 启动 UAgent，在工作台中选择本机源码目录，执行前置检测及源码分析。
4. 检查组件、资源与警告；需要更可靠布局时执行浏览器采集，可选补充视频证据。
5. 选择输出目录并生成工程，建议使用新目录保存结果。
6. 查看审计及验收报告，再用 AiBuilder / UIBuilder 打开生成的 `.aicpro` 工程。结合目标设备、字体、资源和业务逻辑继续集成与验证。

分析任务保存在服务进程内存中，重启服务后需要重新分析，已有磁盘输出仍保留。生成文件与验收通过是两回事：验收失败时，应根据错误和警告处理缺失环境或未支持项。

## 生成文件在哪里

未指定输出目录时，默认写入：

```text
workspaces/<task-id>/
├── analysis.json
└── aibuilder/
    ├── <工程名>.aicpro
    ├── ui_builder/
    │   └── custom/
    │       ├── custom.c
    │       ├── custom.h
    │       ├── screen-tree.json
    │       └── uagent-audit.json
    └── resources/
        └── image/
```

此处列出核心文件；布局证据、验收报告和模拟器相关文件随生成流程及本机环境产生。

- `custom.c` / `custom.h`：生成的 LVGL 控件与屏幕入口，包含 `uagent_build_screen()`。
- `screen-tree.json`：屏幕、组件层级及适配状态。
- `uagent-audit.json`：源码来源、控件、事件、资源和转换警告。
- `resources/image/`：从输入工程复制或生成的实际图片资源。
- 验收报告：用于判断生成工程是否满足当前检查要求；工作台会显示验收状态及错误。

工作台选择的输出目录按工程根目录处理，C 文件位于其 `ui_builder/custom/` 下。`UAGENT_DATA_ROOT` 可指定默认工作区与设置文件的存储根目录。

## 模型辅助分析（可选）

先完成基础静态分析，再根据需要启用模型功能。默认桌面工作流通过 **OpenHmi Station** 配置服务地址、访问令牌和授权模型，在工作台中测试连接后保存配置。Station 是独立服务，本仓库不提供其部署。

模型服务必须支持对应的模型发现及调用接口；视频或视觉验证还需要可接受图片输入的模型。模型处理可能将选中的源码摘要或视觉证据发送到已配置服务。

本机配置文件 `uagent.settings.json` 已被 Git 忽略。令牌通过工作台输入或 `OPENHMI_STATION_TOKEN` 环境变量提供；通过工作台输入的令牌保存在当前进程中，重启后需重新提供。

供本地开发者使用的厂商直连模式可通过以下命令开启：

```powershell
$env:UAGENT_INTERNAL_BYOK = "1"
.\.venv\Scripts\python.exe launcher.py
```

随后在工作台选择内部 BYOK 模式并配置服务商、模型及 API Key。默认模式会限制厂商直连；仅复制旧设置示例中的 API 地址不足以启用模型功能。

## 命令行与 API

仅生成自定义源码及相关文件，可从项目根目录执行：

```powershell
.\.venv\Scripts\python.exe -m tools.generate_source "D:\samples\react-app" "D:\output\demo\ui_builder\custom"
```

该工具采用扫描器直接生成路径，不包含桌面完整分析与运行环境准备流程，且设置 `include_runtime=False`。如需完整工作流，请使用工作台。

本地 API 主要入口如下，参数格式见运行中的 `/docs`：

| 接口 | 用途 |
| --- | --- |
| `POST /api/preflight` | 前置检测；传入 `source_dir` |
| `POST /api/analyze` | 分析源码；返回 `task_id` |
| `POST /api/layout` | 浏览器布局采集；传入 `source_dir` |
| `POST /api/generate` | 生成工程；传入 `task_id`，可选 `output_dir` |
| `POST /api/video` | 上传可选视频证据 |
| `POST /api/validate/visual` | 在模型和模拟器环境就绪后执行视觉验证 |

## 开发与测试

修改 Python 源码后，editable install 无需重复安装。修改前端后需重新执行 `npm.cmd run build`，并重启应用加载新资源。

运行核心回归测试：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_generic_controls.py tests/test_pipeline.py tests/test_widget_adapters.py
```

完整测试可使用虚拟环境解释器执行 `-m pytest`；部分测试依赖本机样例、浏览器或模拟器环境，克隆后可能需要补充这些条件。

| 目录 | 职责 |
| --- | --- |
| `core/` | 扫描、语义模型、分析管线、证据与验收 |
| `adapters/` | 控件语义适配和能力分类 |
| `generator/` | LVGL C 代码、屏幕模型和工程输出 |
| `server/` | FastAPI 本地接口 |
| `frontend/` | React 工作台 |
| `tools/` | 源码生成、布局采集和交叉验证工具 |
| `tests/` | 回归测试 |
| `docs/` | 架构及工程参考说明 |

## 常见问题

**启动后没有首页或返回 404**：先在 `frontend` 中运行 `npm.cmd ci` 与 `npm.cmd run build`，再重启应用。

**双击脚本提示环境缺失**：在仓库根目录创建 `.venv` 并安装 Python 依赖；脚本不会自动安装环境。

**桌面窗口打不开**：先确认 Windows 的 WebView2 运行环境可用；也可使用上述 uvicorn 浏览器启动方式。启动日志可能写入 `uagent-launcher.log`。

**布局来源是 `static-fallback`**：检查输入 React 工程能否运行，以及 Node 和浏览器路径。可通过 `UAGENT_NODE` 指定 Node 可执行文件，通过 `CHROME_PATH` 指定 Chrome / Edge 可执行文件。

**生成了 C 文件，但模拟器或验收失败**：完整工程需要额外的 UIBuilder 运行资源和编译环境，这些不在 Python 依赖中。先查看验收错误，不能仅凭文件存在认定转换完成。

**模型连接失败**：核对部署模式、服务地址、令牌、授权模型及图片输入能力；基础静态转换可先不启用模型功能。

**重启后提示任务不存在**：重新分析源码以获取新 `task_id`，再执行生成。

## 共享与许可

仓库默认排除本机设置、密钥、依赖目录、生成产物、历史记录及本地设计输入。分享转换结果时可单独提供生成工程和审计报告。

当前未指定开源许可证。如需允许他人公开再分发或用于商业用途，请由仓库所有者确定许可条款。协作者运行本项目还需自行准备 UIBuilder、模型服务及输入素材的使用权限。
