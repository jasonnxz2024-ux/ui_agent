import React, { useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';
import './styles.css';

async function api(path, options = {}) {
  const response = await fetch(path, options);
  let data;
  try { data = JSON.parse(await response.text()); }
  catch { throw new Error('本地服务未返回有效结果，请查看运行日志或重新打开程序。'); }
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '请求失败，请检查输入。');
  return data;
}
const post = (path, data) => api(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data || {}) });
const titles = { queued: '准备转换', running: '转换进行中', passed: '当前尺寸验证通过', blocked: '暂时无法转换', failed: '转换运行失败' };
function reasonTitle(reason) {
  if (/duplicate state|duplicate function/i.test(reason)) return '组件作用域或独立实例状态尚未实现';
  if (/Unsupported initial state|record update|bounded arrays|array.*unsupported/i.test(reason)) return '此状态或对象列表暂不支持执行';
  if (/timeout|timer|interval|scheduling/i.test(reason)) return '此定时任务的调度或生命周期暂不支持';
  if (/dynamic source node|source event has no runtime object|view not found|not observed/i.test(reason)) return '部分动态界面或控件尚未完整映射';
  if (/filter primitive|filter|clipPath/i.test(reason)) return '此 SVG 效果暂无可执行的绘制配方';
  if (/File|files|host/i.test(reason)) return '此文件操作或宿主功能暂不支持';
  if (/capacity|overflow/i.test(reason)) return '数据超过配置的容量上限';
  if (/callback|reactive expression/i.test(reason)) return '此交互逻辑尚未实现';
  return '';
}
function Reason({ reason }) {
  const title = reasonTitle(reason);
  return <li>{title && <strong>{title}<br/></strong>}{reason}</li>;
}

function App() {
  const [source, setSource] = useState('');
  const [width, setWidth] = useState(1024);
  const [height, setHeight] = useState(600);
  const [dependencies, setDependencies] = useState('');
  const [capacity, setCapacity] = useState(64);
  const [outputRoot, setOutputRoot] = useState('D:\\uiagent_oct');
  const [job, setJob] = useState(null);
  const [error, setError] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [picking, setPicking] = useState(false);
  const running = submitting || job?.status === 'running' || job?.status === 'queued';

  useEffect(() => {
    api('/api/runtime-convert/config').then(data => setOutputRoot(data.output_root)).catch(e => setError(e.message));
  }, []);
  useEffect(() => {
    if (!job || !['queued', 'running'].includes(job.status)) return;
    let canceled = false;
    let timer;
    const poll = async () => {
      try {
        const current = await api(`/api/runtime-convert/jobs/${job.id}`);
        if (canceled) return;
        setError('');
        setJob(current);
        if (['queued', 'running'].includes(current.status)) timer = setTimeout(poll, 1200);
      } catch (e) {
        if (!canceled) {
          setError(`无法读取任务状态：${e.message}。请保持程序打开，稍后会重试。`);
          timer = setTimeout(poll, 3000);
        }
      }
    };
    timer = setTimeout(poll, 400);
    return () => { canceled = true; clearTimeout(timer); };
  }, [job?.id, job?.status]);

  async function pick() {
    setError(''); setPicking(true);
    try { const data = await api('/api/dialog/source'); if (data.path) setSource(data.path); }
    catch (e) { setError(e.message); }
    finally { setPicking(false); }
  }
  async function convert(event) {
    event.preventDefault(); setError(''); setJob(null); setSubmitting(true);
    try {
      const current = await post('/api/runtime-convert/jobs', {
        source_dir: source.trim(), width: Number(width), height: Number(height),
        dependencies: dependencies.trim(), list_capacity: Number(capacity),
      });
      setJob(current);
    } catch (e) { setError(e.message); }
    finally { setSubmitting(false); }
  }
  async function action(name) {
    setError('');
    try { await post(`/api/runtime-convert/jobs/${job.id}/${name}`); }
    catch (e) { setError(e.message); }
  }

  return <main>
    <header><div className="brand-mark">U</div><div><strong>UAgent</strong><span>React / Figma Make → LVGL · Windows SDL</span></div><span className="local-badge">本地转换 · 无 AI 调用</span></header>
    <section className="intro"><p className="eyebrow">从源码到可运行界面</p><h1>选择工程，开始转换。</h1><p>解析 React 源码与浏览器布局，生成 LVGL C 工程，并编译验证 SDL 示例。</p></section>
    {error && <div className="error-banner" style={{marginBottom:20}} role="alert"><strong>操作未完成</strong><p>{error}</p></div>}
    <div className="workspace">
      <form className="panel form-panel" onSubmit={convert}>
        <div className="section-title"><span className="step">1</span><h2>工程与尺寸</h2></div>
        <label htmlFor="source">React 工程目录</label>
        <div className="path-row"><input id="source" value={source} onChange={e => setSource(e.target.value)} placeholder="选择包含 package.json 的工程目录" required disabled={running || picking}/><button type="button" onClick={pick} disabled={running || picking}>{picking ? '选择中…' : '浏览…'}</button></div>
        <p className="hint">请选择源码文件夹。程序会先复制工程，再进行转换。</p>
        <div className="dimensions"><div><label htmlFor="width">宽度</label><div className="number-field"><input id="width" type="number" min="1" max="8192" value={width} onChange={e => setWidth(e.target.value)} disabled={running} required/><span>px</span></div></div><div><label htmlFor="height">高度</label><div className="number-field"><input id="height" type="number" min="1" max="8192" value={height} onChange={e => setHeight(e.target.value)} disabled={running} required/><span>px</span></div></div></div>
        <div className="presets">{[[320, 240], [1024, 600], [1280, 800]].map(([w, h]) => <button type="button" key={w} disabled={running} onClick={() => { setWidth(w); setHeight(h); }} className={Number(width) === w && Number(height) === h ? 'selected' : ''}>{w} × {h}</button>)}</div>
        <div className="destination"><span>输出位置</span><code>{outputRoot}</code><p>每次转换创建独立子目录，保留之前的结果。</p></div>
        <details className="advanced"><summary>可选设置</summary><label htmlFor="dependencies">已有 node_modules 目录</label><input id="dependencies" value={dependencies} disabled={running} onChange={e => setDependencies(e.target.value)} placeholder="留空则使用工程自身的依赖"/><p className="hint">工程需要兼容的 Vite 与 TypeScript。本机还需 Node.js、CMake 及 LVGL/SDL 编译工具链。</p><label htmlFor="capacity">列表容量上限</label><input id="capacity" type="number" min="1" max="1024" value={capacity} disabled={running} onChange={e => setCapacity(e.target.value)}/><p className="hint">默认 64。超过容量会明确报错，不会静默截断。</p></details>
        <button className="primary convert" type="submit" disabled={running || picking}>{running ? '正在转换…' : '开始转换'}<span aria-hidden="true">→</span></button>
        <p className="hint centered">转换过程可能需要数分钟。</p>
      </form>
      <section className={`panel result-panel ${job?.status || ''}`} aria-live="polite">
        <div className="section-title"><span className="step">2</span><h2>转换结果</h2></div>
        {!job ? <div className="empty"><div className="empty-icon" aria-hidden="true">↗</div><h3>等待选择工程</h3><p>转换完成后，在这里查看结果或无法转换的原因。</p></div> : <>
          <div className="status-line"><span className={`status-dot ${running ? 'pulse' : ''}`}/><h3>{titles[job.status]}</h3></div>
          <p className="message">{job.message}</p>
          <div className="meta"><span>{job.width} × {job.height}</span><span>已用时 {job.elapsed_seconds || 0} 秒</span></div>
          {job.status === 'passed' && <div className="success-note">{typeof job.ssim === 'number' && <strong>画面相似度指标 SSIM {(job.ssim * 100).toFixed(1)}%</strong>}<p>通过当前尺寸的初始画面与采样状态验证，不代表所有交互和尺寸均已覆盖。</p><button className="primary" type="button" onClick={() => action('preview')}>打开 SDL 示例</button></div>}
          {!!job.blockers?.length && <div className="blocker-list"><strong>{job.status === 'blocked' ? '阻断原因' : '失败原因'} · {job.blockers.length} 项</strong><ol>{job.blockers.slice(0, 4).map((reason, i) => <Reason key={i} reason={reason}/>)}</ol>{job.blockers.length > 4 && <details><summary>查看其余 {job.blockers.length - 4} 项</summary><ol start="5">{job.blockers.slice(4).map((reason, i) => <Reason key={i} reason={reason}/>)}</ol></details>}</div>}
          {!running && <><button type="button" className="output-button" onClick={() => action('open-output')}>打开输出文件夹</button><p className="file-path"><code>{job.output}</code></p><p className="hint">完整报告：conversion-report.json<br/>运行日志：conversion.log</p></>}
          {job.log_tail && <details className="logs"><summary>运行日志</summary><pre>{job.log_tail}</pre></details>}
        </>}
      </section>
    </div>
    <footer>未知或尚未支持的能力会明确阻断转换。能力不支持时，不会提供可运行的完成结果。</footer>
  </main>;
}
createRoot(document.getElementById('root')).render(<App/>);
