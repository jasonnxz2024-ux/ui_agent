# 已验证的 AiBuilder 转换规律

## 当前基线

- Print 固定设备画布为 320x240；必须先识别设备帧，再把浏览器坐标归一化到帧内。
- 浏览器 DOM 只提供页面外框和几何证据，源码数组提供动态语义、图标名、颜色、文字和事件。
- SettingsPage、BasicSettings、MaintenancePage 必须生成复合卡片：卡片、图标、标题、副标题和箭头属于同一父控件。
- 同一语义节点只能生成一次；通用 fallback 不得追加空白卡片覆盖专用卡片。
- AiBuilder 对复杂按钮内部的嵌套文字绘制不稳定；需要使用稳定的扁平子控件和局部坐标。
- SVG 资源文件存在不等于工程引用存在；每个 `<src>` 必须有对应 PNG/SVG 并进入 AiBuilder 资源索引。

## Meter 当前状态

- 已识别为 meter parser，画布 320x240。
- 已从同一 App 的 React state 条件分支识别 Dashboard / Settings 两个 ScreenIR 页面。
- 已识别 `useSensorValues()` 的 1 秒更新证据，但尚未完成浏览器交互采集、Gauge→LVGL Arc 转换和 timer 刷新回调。
- meter 当前生成若显示 `static-fallback`，不能视为完整还原成功。
