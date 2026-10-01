import React, { useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import "./styles.css";
import "./fixes.css";

const initialLogs = [
  ["15:42:08", "系统", "工作区已就绪，等待输入源码"],
  ["15:42:11", "扫描器", "支持 React / Figma Make 项目"],
];
function App() {
  const [path, setPath] = useState("D:\\projects\\energy-dashboard");
  const [files, setFiles] = useState([]);
  const [tab, setTab] = useState("resources");
  const [outputDir, setOutputDir] = useState("");
  const [history, setHistory] = useState([]);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [running, setRunning] = useState(false);
  const [progress, setProgress] = useState(0);
  const [logs, setLogs] = useState(initialLogs);
  const logsRef = useRef(initialLogs);
  const [taskId, setTaskId] = useState(null);
  const [resources, setResources] = useState([]);
  const [components, setComponents] = useState([]);
  const [events, setEvents] = useState([]);
  const [generated, setGenerated] = useState(null);
  const [preflight, setPreflight] = useState(null);
  const videoFrames = 0;
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [modelEnabled, setModelEnabled] = useState(false);
  const [agentEnabled, setAgentEnabled] = useState(false);
  const [agentStatus, setAgentStatus] = useState("disabled");
  const [modelName, setModelName] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [stationToken, setStationToken] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [tokenConfigured, setTokenConfigured] = useState(false);
  const [availableModels, setAvailableModels] = useState([]);
  const [connectionStatus, setConnectionStatus] = useState("未测试");
  const [validationRunning, setValidationRunning] = useState(false);
  const [validationResult, setValidationResult] = useState(null);
  const [deployment, setDeployment] = useState("station");
  const [allowInternalByok, setAllowInternalByok] = useState(false);
  const [apiOnly, setApiOnly] = useState(false);
  const [provider, setProvider] = useState("openai");
  const [providerPresets, setProviderPresets] = useState({});
  const addLog = (who, msg) =>
    setLogs((l) => [
      ...l,
      [new Date().toLocaleTimeString("zh-CN", { hour12: false }), who, msg],
    ]);
  useEffect(() => {
    logsRef.current = logs;
  }, [logs]);
  const readResponse = async (response) => {
    const text = await response.text();
    let data;
    try {
      data = text ? JSON.parse(text) : {};
    } catch {
      throw new Error(
        text.replace(/^Internal Server Error\s*/i, "").trim() ||
          `HTTP ${response.status}`,
      );
    }
    if (!response.ok) {
      const detail = data.detail || data.message || `HTTP ${response.status}`;
      throw new Error(
        typeof detail === "string"
          ? detail
          : detail.message || JSON.stringify(detail),
      );
    }
    return data;
  };
  useEffect(() => {
    const historyButton = [...document.querySelectorAll("button")].find((b) =>
      b.textContent.includes("项目历史"),
    );
    if (historyButton) historyButton.onclick = loadHistory;
    const action = document.querySelector(".action-row");
    if (action && !document.getElementById("output-picker")) {
      const wrap = document.createElement("div");
      wrap.id = "output-picker";
      wrap.className = "output-picker";
      wrap.innerHTML =
        '<input placeholder="输出目录（可选）"/><button>选择输出目录</button>';
      action.prepend(wrap);
      wrap.querySelector("input").oninput = (e) => setOutputDir(e.target.value);
      wrap.querySelector("button").onclick = chooseOutput;
    }
    const logHead = document.querySelector(".logs-head");
    if (logHead && !document.getElementById("copy-logs")) {
      const b = document.createElement("button");
      b.id = "copy-logs";
      b.textContent = "复制日志";
      b.onclick = copyLogs;
      logHead.appendChild(b);
    }
    let modal = document.getElementById("history-modal");
    if (historyOpen && !modal) {
      modal = document.createElement("div");
      modal.id = "history-modal";
      modal.className = "modal-backdrop";
      modal.innerHTML =
        '<div class="history-modal"><h2>历史项目</h2><div class="history-items"></div><button>关闭</button></div>';
      modal.querySelector("button").onclick = () => setHistoryOpen(false);
      document.body.appendChild(modal);
    }
    if (modal && !historyOpen) {
      modal.remove();
    }
    if (modal && historyOpen) {
      const list = modal.querySelector(".history-items");
      list.innerHTML = "";
      history.forEach((item) => {
        const b = document.createElement("button");
        b.className = "history-item";
        b.textContent = item.project || item.path;
        b.title = item.path;
        b.onclick = () => {
          setOutputDir(item.path);
          setHistoryOpen(false);
          addLog("历史", "已选择 " + item.path);
        };
        list.appendChild(b);
      });
    }
  }, [progress, historyOpen, history]);
  useEffect(() => {
    fetch("/api/settings")
      .then((r) => r.json())
      .then((data) => {
        const presets = data.provider_presets || {};
        const directOnly = Boolean(data.api_only);
        const selected = data.provider || "openai";
        const preset = presets[selected] || {};
        setModelEnabled(Boolean(data.enabled));
        setAgentEnabled(Boolean(data.agent_enabled));
        setAgentStatus(data.agent_status || "disabled");
        setModelName(data.model || preset.model || "");
        setBaseUrl(data.base_url || preset.base_url || "");
        setDeployment(
          directOnly ? "internal_byok" : data.deployment || "station",
        );
        setProvider(selected);
        setProviderPresets(presets);
        setAllowInternalByok(Boolean(data.allow_internal_byok));
        setApiOnly(directOnly);
        setTokenConfigured(
          Boolean(data.station_token_configured || data.api_key_configured),
        );
      })
      .catch(() => {});
  }, []);
  const credentialPayload = () =>
    deployment === "internal_byok"
      ? { api_key: apiKey || null }
      : { station_token: stationToken || null };
  const saveSettings = async () => {
    try {
      if (!modelName.trim()) throw new Error("请填写模型名称");
      const data = await readResponse(
        await fetch("/api/settings", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            enabled: modelEnabled,
            agent_enabled: modelEnabled,
            provider: deployment === "internal_byok" ? provider : "station",
            deployment,
            model: modelName,
            base_url: baseUrl,
            ...credentialPayload(),
          }),
        }),
      );
      setTokenConfigured(
        Boolean(data.station_token_configured || data.api_key_configured),
      );
      setAgentStatus(modelEnabled ? "enabled" : "disabled");
      setStationToken("");
      setApiKey("");
      setSettingsOpen(false);
      addLog(
        "设置",
        `${modelEnabled ? "AI 视觉增强验证已启用" : "AI 视觉增强验证已关闭"}；模型 ${modelName}`,
      );
    } catch (error) {
      addLog("错误", error.message);
    }
  };
  const loadModels = async () => {
    setConnectionStatus("正在读取可用模型…");
    try {
      const data = await readResponse(
        await fetch("/api/settings/models", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            base_url: baseUrl,
            provider: deployment === "internal_byok" ? provider : "station",
            deployment,
            ...credentialPayload(),
          }),
        }),
      );
      setAvailableModels(data.models || []);
      if (!modelName && data.models?.length) setModelName(data.models[0]);
      const advisory = data.model_discovery === "advisory";
      setConnectionStatus(
        `已读取 ${data.models?.length || 0} 个模型${advisory ? "（可手动填写模型名）" : ""}`,
      );
      addLog(
        "模型",
        `${deployment === "internal_byok" ? provider : "OpenHmi Station"} 返回 ${data.models?.length || 0} 个模型${advisory ? "；列表仅作候选建议" : ""}`,
      );
    } catch (error) {
      setAvailableModels([]);
      setConnectionStatus("模型列表读取失败；仍可手动填写模型名测试");
      addLog("错误", error.message);
    }
  };
  const testConnection = async () => {
    if (!modelName.trim()) {
      addLog("错误", "请填写模型名称");
      return;
    }
    setConnectionStatus("测试中…");
    try {
      const data = await readResponse(
        await fetch("/api/settings/test", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            provider: deployment === "internal_byok" ? provider : "station",
            deployment,
            model: modelName,
            base_url: baseUrl,
            require_vision: true,
            ...credentialPayload(),
          }),
        }),
      );
      setConnectionStatus(`已连接：${data.model} · 图像输入通过`);
      addLog("模型", "连接、模型权限与图像输入测试通过");
    } catch (error) {
      setConnectionStatus(`连接失败：${error.message}`);
      addLog("错误", "模型连接测试失败：" + error.message);
    }
  };
  const changeDeployment = (value) => {
    setDeployment(value);
    setAvailableModels([]);
    setModelName("");
    setConnectionStatus("未测试");
    if (value === "station") {
      setBaseUrl("");
    } else {
      const preset = providerPresets[provider];
      setBaseUrl(preset?.base_url || "");
    }
  };
  const changeProvider = (value) => {
    setProvider(value);
    const preset = providerPresets[value] || {};
    setBaseUrl(preset.base_url || "");
    setModelName("");
    setAvailableModels([]);
    setConnectionStatus("未测试");
  };
  const chooseSource = async () => {
    try {
      const data = await readResponse(await fetch("/api/dialog/source"));
      if (data.path) {
        setPath(data.path);
        setFiles([data.path]);
        addLog("输入", "已选择源码目录：" + data.path);
      }
    } catch (error) {
      addLog("错误", error.message);
    }
  };
  const chooseOutput = async () => {
    try {
      const data = await readResponse(await fetch("/api/dialog/output"));
      if (data.path) {
        setOutputDir(data.path);
        addLog("输出", "已选择工程目录：" + data.path);
      }
    } catch (error) {
      addLog("错误", error.message);
    }
  };
  const loadHistory = async () => {
    try {
      const data = await readResponse(await fetch("/api/history"));
      setHistory(data.items || []);
      setHistoryOpen(true);
    } catch (error) {
      addLog("错误", error.message);
    }
  };
  const copyLogs = async () => {
    const value = logsRef.current.map((l) => l.join(" | ")).join("\n");
    try {
      await navigator.clipboard.writeText(value);
      addLog("系统", "日志已复制到剪贴板");
    } catch {
      addLog("警告", "剪贴板不可用，请手动选择日志文本");
    }
  };
  const runPreflight = async () => {
    const data = await readResponse(
      await fetch("/api/preflight", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ source_dir: path }),
      }),
    );
    setPreflight(data);
    setAgentStatus(data.agent?.status || "disabled");
    addLog(
      "前置检测",
      data.status === "ready"
        ? `通过：${data.components?.reachable || 0} 个可达组件，${data.scroll_components || 0} 个滚动区域`
        : `阻断：${(data.blockers || []).join("；")}`,
    );
    return data;
  };
  const parse = async () => {
    if (running) return;
    setRunning(true);
    setGenerated(null);
    setProgress(8);
    addLog("前置检测", "正在检查项目可生成性…");
    try {
      const check = await runPreflight();
      if (check.status !== "ready")
        throw new Error((check.blockers || ["前置检测未通过"]).join("；"));
      setProgress(18);
      addLog("解析器", "开始建立组件树…");
      const data = await readResponse(
        await fetch("/api/analyze", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ source_dir: path }),
        }),
      );
      const summary = data.summary || {};
      setPreflight(data.preflight || check);
      setTaskId(data.task_id);
      setResources(
        (summary.resources || []).map((r, i) => ({
          icon: r.kind === "svg" ? "◉" : "▧",
          name: (r.path || r.id).split(/[\\/]/).pop(),
          meta: (r.kind || "asset").toUpperCase(),
          tone: ["blue", "violet", "amber"][i % 3],
        })),
      );
      setComponents(
        (summary.components || []).map((c) => ({
          name: (c.id || "component").split(":").pop(),
          kind: c.kind || "unknown",
          count: "1",
        })),
      );
      setEvents(summary.events || []);
      setProgress(100);
      addLog(
        "解析器",
        `解析完成：${(summary.components || []).length} 组件，${(summary.events || []).length} 事件，${(summary.resources || []).length} 资源`,
      );
    } catch (error) {
      addLog("错误", error.message);
      setProgress(0);
    } finally {
      setRunning(false);
    }
  };
  const generate = async () => {
    if (!taskId) {
      addLog("错误", "请先完成源码解析");
      return;
    }
    addLog("生成器", "正在生成 LVGL / UIBuilder 工程…");
    try {
      const data = await readResponse(
        await fetch("/api/generate", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            task_id: taskId,
            output_dir: outputDir || null,
          }),
        }),
      );
      setGenerated(data);
      addLog("生成器", `✓ 输出完成：${data.units || 0} 个生成单元`);
      if (data.output?.project) addLog("输出", data.output.project);
      (data.warnings || []).forEach((w) => addLog("警告", w));
    } catch (error) {
      addLog("错误", error.message);
    }
  };
  const runVisualValidation = async () => {
    if (validationRunning) return;
    if (!outputDir) {
      addLog("错误", "视觉增强验证前请先选择工程输出目录");
      return;
    }
    setValidationRunning(true);
    addLog("验证", "启动持久 Browser、Windows SDL 与最多 5 轮 90% 门禁验证…");
    try {
      const data = await readResponse(
        await fetch("/api/validate/visual", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            source_dir: path,
            output_dir: outputDir,
            max_rounds: 5,
          }),
        }),
      );
      const gate = data.validation?.deterministic_gate || {};
      setGenerated(data);
      setValidationResult(data);
      addLog(
        "验证",
        `${data.export_ready ? "✓ 已通过" : "未通过"}：${Math.round((gate.confidence || 0) * 100)}% / 90%，${(data.rounds || []).length} 轮`,
      );
      (data.validation?.errors || [])
        .slice(0, 8)
        .forEach((e) => addLog("阻断", e));
    } catch (error) {
      addLog("错误", error.message);
    } finally {
      setValidationRunning(false);
    }
  };
  const status = running
    ? "解析中"
    : progress === 100
      ? "解析完成"
      : "等待输入";
  const projectName =
    path.split(/[\\/]/).filter(Boolean).pop() || "React Project";
  const semanticKinds = [...new Set(components.map((item) => item.kind))].slice(
    0,
    6,
  );
  return (
    <div className="app">
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-mark">✦</span>
          <span>uagent</span>
          <small>DESIGN → EMBEDDED</small>
        </div>
        <nav>
          <div className="nav-label">WORKSPACE</div>
          <button className="nav-item active">
            ⌘ <span>转换工作台</span>
          </button>
          <button className="nav-item">
            ◌ <span>项目历史</span>
          </button>
          <button className="nav-item" onClick={() => setSettingsOpen(true)}>
            ⚙ <span>设置</span>
          </button>
        </nav>
        <div className="sidebar-bottom">
          <div className="engine">
            <span className="online" /> Local Engine <b>v0.1</b>
          </div>
          <div className="user">
            <span>JG</span>
            <div>
              Jason Gao<small>Developer</small>
            </div>
            <i>•••</i>
          </div>
        </div>
      </aside>
      <main>
        <header>
          <div>
            <div className="eyebrow">CONVERSION WORKSPACE · 0.4.0</div>
            <h1>
              设计转换工作台{" "}
              <span className="status-pill">
                <span className={running ? "pulse" : "online"} />
                {status}
              </span>
            </h1>
          </div>
          <div className="header-actions">
            <button className="icon-btn">⌕</button>
            <button className="icon-btn">?</button>
            <button className="avatar">JG</button>
          </div>
        </header>
        <section className="input-row">
          <div className="input-card source-card">
            <div className="card-head">
              <div>
                <span className="section-kicker">01 · SOURCE CODE</span>
                <h2>输入源码目录</h2>
              </div>
              <span className="card-icon">⌁</span>
            </div>
            <div className="path-input">
              <span>⌂</span>
              <input
                value={path}
                onChange={(e) => setPath(e.target.value)}
                aria-label="源码目录"
              />
              <button onClick={chooseSource}>选择目录</button>
            </div>
            <div className="hint">
              支持 React / Vite / Figma Make&nbsp; · &nbsp;
              {files.length ? "已选择本机目录" : "也可直接粘贴绝对路径"}
            </div>
          </div>
        </section>
        {preflight && (
          <section className={"preflight " + preflight.status}>
            <div>
              <span className="section-kicker">
                PREFLIGHT · {preflight.status.toUpperCase()}
              </span>
              <strong>
                {preflight.status === "ready"
                  ? "项目可以进入转换"
                  : "项目暂不能转换"}
              </strong>
            </div>
            <dl>
              <div>
                <dt>解析策略</dt>
                <dd>{preflight.parser}</dd>
              </div>
              <div>
                <dt>包管理器</dt>
                <dd>{preflight.package_manager}</dd>
              </div>
              <div>
                <dt>可达组件</dt>
                <dd>{preflight.components?.reachable || 0}</dd>
              </div>
              <div>
                <dt>滚动区域</dt>
                <dd>{preflight.scroll_components || 0}</dd>
              </div>
              <div>
                <dt>资源 / 事件</dt>
                <dd>
                  {preflight.resources || 0} / {preflight.events || 0}
                </dd>
              </div>
              <div>
                <dt>导航确认</dt>
                <dd>
                  {preflight.navigation?.confirmed || 0} /{" "}
                  {preflight.navigation?.detected || 0}
                </dd>
              </div>
            </dl>
            {preflight.source_browser_plan && (
              <div className="capability-counts">
                {Object.entries(preflight.source_browser_plan).map(
                  ([key, value]) => (
                    <span key={key}>
                      {key} {value.source}/{value.browser ?? "—"}/{value.plan}
                    </span>
                  ),
                )}
              </div>
            )}
            <p>
              Agent planning：{preflight.agent_planning?.status || "enabled"} ·{" "}
              {preflight.agent_planning?.mode || "runtime-first-local"} ·{" "}
              {preflight.agent_planning?.evidence_mode || "source-fallback"}
            </p>
            <p>
              Agent 增强：{preflight.agent?.status || agentStatus} ·
              仅处理未决/低置信度证据
            </p>
            {preflight.navigation?.items?.map((item) => (
              <p key={item.id}>
                导航 {item.trigger} → {item.target_component || item.target} ·{" "}
                {Math.round(item.confidence * 100)}%{" "}
                {item.status === "confirmed" ? "已确认" : "待验证"}
              </p>
            ))}
            {[...(preflight.blockers || []), ...(preflight.warnings || [])].map(
              (item, i) => (
                <p key={"message-" + i}>{item}</p>
              ),
            )}
          </section>
        )}
        <section className="action-row">
          <button
            className="preflight-btn"
            onClick={() =>
              runPreflight().catch((error) => addLog("错误", error.message))
            }
            disabled={running}
          >
            前置检测
          </button>
          <div className="parse-progress">
            <div className="progress-top">
              <span>
                <span className="dot" /> {running ? "正在解析项目" : "解析项目"}
              </span>
              <b>{progress}%</b>
            </div>
            <div className="bar">
              <i style={{ width: progress + "%" }} />
            </div>
          </div>
          <button className="primary" onClick={parse} disabled={running}>
            {running ? "解析中…" : "开始解析"} <span>↗</span>
          </button>
        </section>
        <section className="workspace">
          <div className="panel">
            <div className="panel-tabs">
              <button
                className={tab === "resources" ? "selected" : ""}
                onClick={() => setTab("resources")}
              >
                资源 <b>{resources.length}</b>
              </button>
              <button
                className={tab === "components" ? "selected" : ""}
                onClick={() => setTab("components")}
              >
                组件 <b>{components.length}</b>
              </button>
              <button
                className={tab === "logic" ? "selected" : ""}
                onClick={() => setTab("logic")}
              >
                交互逻辑 <b>{events.length}</b>
              </button>
            </div>
            <div className="panel-body">
              {tab === "resources" && (
                <>
                  <div className="panel-title">
                    <div>
                      <h3>资源清单</h3>
                      <p>从源码中发现的视觉资产</p>
                    </div>
                    <button className="filter">全部⌄</button>
                  </div>
                  <div className="resource-grid">
                    {resources.length ? (
                      resources.map((r) => (
                        <div className="resource" key={r.name}>
                          <div className={"thumb " + r.tone}>{r.icon}</div>
                          <div>
                            <strong>{r.name}</strong>
                            <small>{r.meta}</small>
                          </div>
                          <span>⋮</span>
                        </div>
                      ))
                    ) : (
                      <div className="empty">解析后显示资源</div>
                    )}
                  </div>
                </>
              )}
              {tab === "components" && (
                <>
                  <div className="panel-title">
                    <div>
                      <h3>组件树</h3>
                      <p>已识别并映射到目标平台</p>
                    </div>
                    <button className="filter">按类型⌄</button>
                  </div>
                  <div className="component-list">
                    {components.length ? (
                      components.map((c) => (
                        <div className="component" key={c.name}>
                          <span className="comp-symbol">◇</span>
                          <div>
                            <strong>{c.name}</strong>
                            <small>{c.kind}</small>
                          </div>
                          <b>{c.count}</b>
                        </div>
                      ))
                    ) : (
                      <div className="empty">解析后显示组件</div>
                    )}
                  </div>
                </>
              )}
              {tab === "logic" && (
                <>
                  <div className="panel-title">
                    <div>
                      <h3>交互逻辑</h3>
                      <p>从源码事件与视频回溯推断 · {videoFrames} 帧证据</p>
                    </div>
                  </div>
                  <div className="logic-list">
                    {events.length ? (
                      events.map((event, i) => (
                        <div key={event.id || i}>
                          ↔ <span>{event.id || event.kind || "事件"}</span>
                          <small>
                            {event.kind || event.handler || "交互事件"}
                          </small>
                        </div>
                      ))
                    ) : (
                      <div className="empty">解析后显示交互事件</div>
                    )}
                  </div>
                </>
              )}
            </div>
          </div>
          <div className="preview">
            <div className="preview-head">
              <div>
                <span className="section-kicker">SEMANTIC PREVIEW</span>
                <h3>转换覆盖概览</h3>
              </div>
              <div className="device-toggle">
                <button>Source</button>
                <button className="on">LVGL</button>
              </div>
            </div>
            <div className="device semantic-device">
              <div className="device-top">
                <span>{projectName.toUpperCase()}</span>
                <span className={progress === 100 ? "ready-dot" : ""}>
                  ● {status}
                </span>
              </div>
              <div className="semantic-content">
                <div className="semantic-hero">
                  <span>✦</span>
                  <h4>
                    {progress === 100 ? "中间模型已建立" : "等待解析源码"}
                  </h4>
                  <p>
                    {progress === 100
                      ? "每项识别结果均可在左侧核对，不以模拟图片冒充真实预览。"
                      : "选择 React / Figma Make 目录后开始扫描。"}
                  </p>
                </div>
                <div className="coverage-grid">
                  <div>
                    <strong>{components.length}</strong>
                    <small>组件</small>
                  </div>
                  <div>
                    <strong>{events.length}</strong>
                    <small>事件</small>
                  </div>
                  <div>
                    <strong>{resources.length}</strong>
                    <small>资源</small>
                  </div>
                  <div>
                    <strong>{videoFrames}</strong>
                    <small>视频帧</small>
                  </div>
                </div>
                <div className="kind-cloud">
                  {semanticKinds.length ? (
                    semanticKinds.map((kind) => <span key={kind}>{kind}</span>)
                  ) : (
                    <span>尚无适配数据</span>
                  )}
                </div>
                {generated && (
                  <div className="generated-note">
                    ✓ 已生成 {generated.units || 0} 个 LVGL 单元
                  </div>
                )}
              </div>
            </div>
          </div>
        </section>
        <section className="bottom-row">
          <div className="logs">
            <div className="logs-head">
              <div>
                <span className="section-kicker">ACTIVITY LOG</span>
                <h3>运行日志</h3>
              </div>
              <button onClick={() => setLogs([])}>清空</button>
            </div>
            <div className="log-body">
              {logs.length ? (
                logs.map((l, i) => (
                  <div key={i}>
                    <time>{l[0]}</time>
                    <b>{l[1]}</b>
                    <span>{l[2]}</span>
                  </div>
                ))
              ) : (
                <div className="empty">暂无日志</div>
              )}
            </div>
          </div>
          <button
            className="generate"
            onClick={runVisualValidation}
            disabled={validationRunning}
          >
            <span>◉</span>
            <div>
              <strong>{validationRunning ? "验证中…" : "视觉增强验证"}</strong>
              <small>Browser ↔ SDL · 90% 门禁</small>
            </div>
            <b>↗</b>
          </button>
          <button className="generate" onClick={generate}>
            <span>✦</span>
            <div>
              <strong>生成工程</strong>
              <small>输出 LVGL / UIBuilder 项目</small>
            </div>
            <b>↗</b>
          </button>
        </section>
      </main>
      {settingsOpen && (
        <div
          className="modal-backdrop"
          onMouseDown={(e) =>
            e.target === e.currentTarget && setSettingsOpen(false)
          }
        >
          <div className="settings-modal">
            <div className="settings-title">
              <div>
                <span className="section-kicker">
                  {deployment === "internal_byok"
                    ? "INTERNAL BYOK"
                    : "OPENHMI STATION"}
                </span>
                <h2>AI 视觉增强验证</h2>
              </div>
              <button onClick={() => setSettingsOpen(false)}>×</button>
            </div>
            <p>
              {deployment === "internal_byok"
                ? "仅用于内部测试：厂商 API Key 只保存在本次进程内存，禁止向客户分发此构建。"
                : "正式模式：客户端不接收厂商 API Key，密钥仅配置在 OpenHmi Station 服务端。"}
            </p>
            {allowInternalByok && !apiOnly && (
              <label>
                运行模式
                <select
                  value={deployment}
                  onChange={(e) => changeDeployment(e.target.value)}
                >
                  <option value="station">OpenHmi Station（正式）</option>
                  <option value="internal_byok">内部 API Key 测试</option>
                </select>
              </label>
            )}
            <label className="toggle-row">
              <span>
                <strong>启用 AI 视觉增强验证</strong>
                <small>
                  {tokenConfigured ? "当前凭据已配置" : "需要先配置凭据"}
                </small>
              </span>
              <input
                type="checkbox"
                checked={modelEnabled}
                onChange={(e) => {
                  setModelEnabled(e.target.checked);
                  setAgentEnabled(e.target.checked);
                }}
              />
              <i />
            </label>
            {deployment === "internal_byok" && (
              <label>
                服务商
                <select
                  value={provider}
                  onChange={(e) => changeProvider(e.target.value)}
                >
                  <option value="openai">OpenAI</option>
                  <option value="deepseek">DeepSeek</option>
                  <option value="xai">xAI / Grok</option>
                  <option value="zhipu">智谱 AI / GLM</option>
                  <option value="custom">自定义兼容服务</option>
                </select>
              </label>
            )}
            <label>
              {deployment === "internal_byok"
                ? "Base URL"
                : "OpenHmi Station URL"}
              <input
                value={baseUrl}
                onChange={(e) => {
                  setBaseUrl(e.target.value);
                  setAvailableModels([]);
                  setConnectionStatus("未测试");
                }}
                placeholder={
                  deployment === "internal_byok"
                    ? "https://api.example.com/v1"
                    : "https://station.example.com/v1"
                }
              />
            </label>
            {deployment === "internal_byok" ? (
              <label>
                厂商 API Key（仅本次进程内存）
                <input
                  type="password"
                  value={apiKey}
                  onChange={(e) => setApiKey(e.target.value)}
                  placeholder={
                    tokenConfigured ? "已配置；留空则保持" : "粘贴内部测试 Key"
                  }
                />
              </label>
            ) : (
              <label>
                Station 访问令牌（短期、可撤销）
                <input
                  type="password"
                  value={stationToken}
                  onChange={(e) => setStationToken(e.target.value)}
                  placeholder={
                    tokenConfigured
                      ? "已配置；留空则保持"
                      : "粘贴 Station 会话令牌"
                  }
                />
              </label>
            )}
            <label>
              模型名称
              <input
                value={modelName}
                onChange={(e) => setModelName(e.target.value)}
                list="model-candidates"
                placeholder="例如 glm-4v-flash"
              />
              <datalist id="model-candidates">
                {availableModels.map((id) => <option key={id} value={id} />)}
              </datalist>
              <small>刷新结果仅供参考；内部 API Key 测试可手动填写模型名，再以“测试连接与识图”确认。</small>
            </label>
            <p>{connectionStatus}</p>
            <div className="settings-actions">
              <button onClick={loadModels}>刷新可用模型</button>
              <button onClick={testConnection}>测试连接与识图</button>
              <button onClick={() => setSettingsOpen(false)}>取消</button>
              <button className="save" onClick={saveSettings}>
                保存设置
              </button>
            </div>
          </div>
        </div>
      )}
      {validationResult && (
        <div
          className="modal-backdrop"
          onMouseDown={(e) =>
            e.target === e.currentTarget && setValidationResult(null)
          }
        >
          <div className="validation-modal">
            <div className="settings-title">
              <div>
                <span className="section-kicker">BROWSER ↔ WINDOWS SDL</span>
                <h2>视觉增强验证结果</h2>
              </div>
              <button onClick={() => setValidationResult(null)}>×</button>
            </div>
            <div
              className={
                validationResult.export_ready
                  ? "validation-pass"
                  : "validation-fail"
              }
            >
              {validationResult.export_ready
                ? "已通过 90% 确定性门禁"
                : "未通过，工程不可交付"}
            </div>
            <div className="validation-rounds">
              {(validationResult.rounds || []).map((round) => (
                <article className="validation-round" key={round.round}>
                  <header>
                    <strong>
                      第 {round.round} 轮 ·{" "}
                      {Math.round((round.confidence || 0) * 100)}%
                    </strong>
                    <span>
                      {round.passed
                        ? "通过"
                        : `本轮后应用 ${round.repair?.applied || 0} 项修复`}
                    </span>
                  </header>
                  {round.comparison ? (
                    <div className="comparison-grid">
                      <figure>
                        <img
                          src={round.comparison.browser_image}
                          alt={`第 ${round.round} 轮 Browser`}
                        />
                        <figcaption>Browser evidence</figcaption>
                      </figure>
                      <figure>
                        <img
                          src={round.comparison.simulator_image}
                          alt={`第 ${round.round} 轮 Windows SDL`}
                        />
                        <figcaption>
                          Windows SDL · {round.round === 1 ? "初始输出" : "上一轮修复后"}
                        </figcaption>
                      </figure>
                    </div>
                  ) : (
                    <p className="comparison-missing">
                      本轮未生成截图：
                      {round.errors?.[0] || "验证在截图前停止"}
                    </p>
                  )}
                  {round.stop_reason && (
                    <p className="comparison-note">
                      停止原因：{round.stop_reason}
                    </p>
                  )}
                </article>
              ))}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
createRoot(document.getElementById("root")).render(<App />);
