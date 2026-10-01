# React/Figma Make → AiBuilder 适配架构

## 不允许的降级

不能因为组件嵌套、资源数量或使用第三方 React 库，就把整个页面静默压成一张 PNG。

## 资源图

递归遍历本地 import、`figma:asset/...`、`src/assets`、`public`，建立资源 ID；再通过 DOM 采集实际坐标、尺寸和可见状态。

## 语义适配器

- `CarouselAdapter`: items/indices/paginate、drag/swipe、层级和动画；
- `ChartAdapter`: Recharts Line/Bar/Area/Pie → LVGL chart series；
- `TableAdapter`: 表头、单元格、滚动、选择和排序；
- `SelectAdapter`: Radix Select/Dropdown → LVGL dropdown/list；
- `ScrollAdapter`: overflow/滚动内容 → LVGL scroll container。

## LVGL 控件适配器注册表

适配器按控件类别注册，而不是按 demo 注册：

- 基础：`obj`、`label`、`button`、`image`、`line`、`arc`、`bar`、`slider`、`switch`；
- 选择：`checkbox`、`radio/button matrix`、`dropdown`、`roller`、`list`、`tabview`；
- 数据：`chart`、`table`、`calendar`、`keyboard`、`textarea`、`spinbox`；
- 容器：`flex/grid container`、`scroll container`、`menu`、`tileview`、`window`、`msgbox`；
- 高级：`meter`、`scale`、`led`、`spinner`、`imagebutton`、`animimg`。

每个控件适配器都必须声明：可识别的 JSX/状态模式、属性映射、事件映射、资源依赖、动画策略和降级条件。目标不是把每个 HTML 标签硬套成 LVGL 控件，而是从 React 的语义和行为选择最匹配的 LVGL 控件。

每个适配器必须输出 native、partial 或 fallback 状态，不能伪装成已完成。

## 现代工作台

前端采用现代 IDE/Agent 工作台布局：项目资源栏、源码/视频面板、画布预览、模型与生成设置、实时日志和缺口报告。视频解析是独立输入面板，可与源码状态和生成结果对照。

## 穷尽式解析

解析从入口开始递归遍历所有本地模块、组件、资源和事件引用；每个节点必须进入 `native`、`partial` 或 `fallback` 清单。遇到无法转换的语法时停止猜测，但继续扫描其余分支，并记录文件、行号、原始表达式和原因，直到整个依赖图完成。

## 多阶段转换合同

转换器固定为六个阶段，项目特征不得绕过阶段边界：

1. `SourceEvidence`: 从入口组件收集 DOM、SVG、CSS、事件、资源和状态绑定，不包含 LVGL 判断。
2. `BrowserEvidence`: 记录真实画布、计算样式、层级、交互前后状态和截图区域。
3. `SemanticIR`: 形成 Screen、Region、Widget、Navigation 与 VisualEffect，不包含生成代码。
4. `CapabilityPlan`: 每个 Region/Widget 标记 `native`、`compound`、`fallback` 或 `unsupported`，同时记录理由和置信度。
5. `RenderPlan`: 将 compound 展开为多个原生控件，将 fallback 限制为最小 DOM 区域截图。
6. `Acceptance`: 对比源码、浏览器、IR、snapshot 与模拟器的数量、尺寸、效果和交互；不一致时禁止标记完成。

## VisualEffectIR

视觉效果必须是一等语义，不能只作为 className 字符串留在扫描结果中：

- `glow`: 颜色、透明度、扩散半径、关联控件；
- `shadow`: 颜色、偏移、模糊、扩散；
- `blur`: 半径、输入层、裁剪区域；
- `gradient`: 类型、角度、色标；
- `mask` / `clip`: 路径、目标区域；
- `filter`: SVG filter id 与原始 filter primitives。

Arc Glow 优先生成同步的多层 Arc；目标平台不能表达真实模糊时，只把光晕层裁剪成透明 PNG，前景 Arc 和数值仍保持原生动态控件。禁止因为一个效果不支持而把整屏压成 PNG。

## CapabilityPlan

每个输出项必须携带：

- `support`: `native | compound | fallback | unsupported`；
- `adapter`: 使用的适配器；
- `confidence`: 0 到 1；
- `evidence_ids`: 源码与浏览器证据；
- `reason`: 选择该策略的原因；
- `fallback_bounds`: 仅 fallback 时允许存在的最小截图区域。

生成前必须检查 Screen、Gauge、Arc、导航目标、滚动区域和视觉效果数量。任何高置信度源码证据在 RenderPlan 中缺失，都属于 blocker。

## Agent 增强模式

Agent 只接收未知或低置信度证据，并输出受约束 JSON：候选语义、候选现有组件、适配策略、置信度和理由。Agent 不得输出 C 代码、修改源码、创造不存在的页面或跳过 Acceptance。

客户配置包含：

- Base URL；
- 模型名称；
- API Key。

Base URL 与模型名称可保存在用户配置目录；API Key 不得进入源码、生成工程、日志或安装包。桌面版优先使用进程内存，后续可接 Windows Credential Manager。仅当规则无法确定目标且用户启用 Agent 时调用模型。

Agent 建议通过浏览器验证后可导出为版本化规则包。规则包按结构特征匹配，不得使用项目目录名或客户名称作为条件。

## 发布验收

打包前至少验证 `print`、`meter`、`test`、`cluster`：分辨率、Screen 数量、Gauge/Arc 数量、滚动区域、导航和资源路径。发布物不得包含开发者 API Key，并必须能在未安装 Python 的 Windows 环境启动；React 采集运行时缺失时应明确提示并自动修复，而不是返回空页面成功。
