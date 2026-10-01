# UAgent — Codex 启动约束

本文件是本仓库的默认指令。启动后先遵守这里，再读 `docs/`。`docs/` 只描述产品方向，不覆盖本文件的禁令。

更细的执行细则见 `.cursor/skills/uagent-codex-guard/SKILL.md`。

## 当前里程碑

只做核心转换验证：

`Figma Make / React 源码 → 语义模型 → 适配器 → 可审计 custom.c / custom.h`

不要做：安装包、PyInstaller 打磨、云服务、把页面压成一张 PNG、覆盖 `D:\aiassit`。

对照样例只读：`D:\aiassit\card`、`D:\aiassit\energy`、`D:\aiassit\test`。用户没点名就不要改这些目录。

## 模型路由（硬规则）

Sol / Sol High 很贵。README 里的「Sol 架构、Terra 解析、Luna 打包」是职责说明，**不是**授权本会话用 High 全库施工。

**只有这些才允许按 Sol / High 思考：**

- 改 IR 或流水线阶段（屏幕对象树、布局 pass、AiBuilder 工程导出 schema）
- Terra 已失败两次、需要架构拍板（必须引用那两次失败）
- 用户明确说：只要架构决策 / 只评审不改代码

**其余全部按 Terra 做最小改动：** 扫描器、适配器、C 渲染、测试、过滤 `components/ui`、修编码、资源拷贝。

**Luna 只做文件：** 拷资源、写生成物路径。不要主动打包。

**Sol High 会话里也禁止：**

- 再开 sub-agent。自己一轮做完。
- 用第二轮 High 审核自己的大 diff。
- 阅读 `workspaces/`、`dist/`、`.venv/`、`frontend/node_modules/`、整份 `uagent-audit.json`。
- 「先探索整个仓库再决定」。

若界面已是 Sol High、但任务不在允许清单：先用一句话标明，然后按 Terra 做最小改动，不再额外探索。

界面选了 High，文档不能把模型变便宜，只能少读、少开子代理、少写。

## 上下文预算

每轮：

- 源文件最多动 6 个，除非用户点名更多。
- 不打算改、也不打算引用失败测试的文件，不要读。
- 生成验证最多看一份 `custom.h` 和审计里的 warnings 列表。
- 优先跑 `tests/test_generic_controls.py`、`tests/test_pipeline.py`、`tests/test_widget_adapters.py`。

## 实现规则

1. 扫描器不懂 LVGL。适配器分类。`generator/lvgl.py` 才出 C。
2. 不能假装成功。必须是 `native` / `partial` / `fallback`，并写进审计。
3. 不要因为组件嵌套或第三方 React 库就把整页压成 PNG。
4. 只编译从入口 / `App` 可达的组件。`components/ui/*` 原语不是业务屏幕。
5. 事件先留注释 + 审计，不要编造业务 callback。
6. 没有坐标/flex 就保持默认并标 `partial`。下一优先是屏幕对象树，不是更多互不嵌套的 `uagent_build_*`。
7. 文件必须 UTF-8，禁止再写出乱码中文。
8. 用户没要求就不要加依赖。

## 用户说「继续」时的顺序

1. 把控件收成一棵屏幕树（parent + children）
2. 堵住 `ui/select`、`ui/table`、`ui/dropdown-menu`、`ui/carousel` 泄漏
3. 打通父组件 `data={mockData}` 到图表数值
4. 然后才是布局；打包最后，且必须用户明确要求
