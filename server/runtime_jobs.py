"""Local desktop jobs for the existing runtime conversion command.

Each conversion runs in its own process because the compiler temporarily changes
environment variables and capture hooks. No model or cloud service is involved.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field


class ConversionRequest(BaseModel):
    source_dir: str
    width: int = Field(default=1024, ge=1, le=8192)
    height: int = Field(default=600, ge=1, le=8192)
    dependencies: str = ''
    list_capacity: int = Field(default=64, ge=1, le=1024)


def command_for(source: Path, output: Path, request: ConversionRequest) -> list[str]:
    if getattr(sys, 'frozen', False):
        command = [sys.executable, '--convert']
    else:
        command = [sys.executable, '-m', 'tools.runtime_convert']
    command.extend([str(source), str(output), '--width', str(request.width),
                    '--height', str(request.height), '--list-capacity', str(request.list_capacity),
                    '--sample-ms', '0'])
    if request.dependencies.strip():
        command.extend(['--dependencies', str(Path(request.dependencies.strip()).resolve())])
    return command


def report_result(report: dict, output: Path, exit_code: int) -> dict:
    blockers = list(dict.fromkeys(str(item) for item in report.get('blockers', [])))
    exe = Path(report['exe']).resolve() if report.get('exe') else None
    exe_valid = bool(exe and output.resolve() in exe.parents and exe.suffix.lower() == '.exe' and exe.is_file())
    accepted = exit_code == 0 and report.get('status') == 'sample-passed' and report.get('accepted') is True and not blockers and exe_valid
    if accepted:
        status, message = 'passed', '当前尺寸验证通过，可以打开 SDL 示例。'
    elif report.get('status') == 'blocked' or blockers and report.get('status') != 'failed':
        status, message = 'blocked', '当前工程包含尚不支持的能力，未通过转换。生成草稿不能作为完成结果。'
    else:
        status, message = 'failed', '转换运行失败，请查看下面的原因和运行日志。'
    if not accepted and not blockers:
        blockers = ['未获得完整的转换与验证结果，请查看运行日志。']
    return {'status': status, 'message': message, 'blockers': blockers,
            'exe': str(exe) if accepted else None,
            'ssim': report.get('initial_frame_ssim'), 'limitations': report.get('limitations', []),
            'validation_scope': report.get('validation_scope'), 'state_rules': report.get('state_rules')}


class ConversionJobs:
    def __init__(self, output_base: Path, process_factory=subprocess.Popen):
        self.output_base = output_base.resolve()
        self.process_factory = process_factory
        self.jobs: dict[str, dict] = {}
        self.lock = threading.Lock()

    def start(self, request: ConversionRequest) -> dict:
        source = Path(request.source_dir.strip()).resolve()
        if not source.is_dir() or not (source / 'package.json').is_file():
            raise ValueError('请选择包含 package.json 的 React / Figma Make 工程目录。')
        if source == self.output_base or source in self.output_base.parents:
            raise ValueError('输出目录不能位于源码目录内，请更换源码目录。')
        dependencies = Path(request.dependencies.strip()).resolve() if request.dependencies.strip() else source / 'node_modules'
        if not (dependencies / 'vite/bin/vite.js').is_file() or not (dependencies / 'typescript/lib/typescript.js').is_file():
            raise ValueError('缺少工程依赖。请在“可选设置”指定已有、兼容的 node_modules 目录（需要 Vite 和 TypeScript）。')
        if not shutil.which('node'):
            raise ValueError('未找到本机 Node.js，请配置 Node.js 后重新打开程序。')
        cmake = os.environ.get('UAGENT_CMAKE', 'cmake')
        if not shutil.which(cmake) and not Path(cmake).is_file():
            raise ValueError('未找到本机 CMake，无法编译 SDL 示例。')
        job_id = uuid4().hex
        output = self.output_base / (time.strftime('%Y%m%d-%H%M%S') + '-' + source.name[:40] + '-' + job_id[:8])
        command = command_for(source, output, request)
        with self.lock:
            if any(job['status'] in {'queued', 'running'} for job in self.jobs.values()):
                raise RuntimeError('已有转换正在运行，请等待完成。')
            # mkdir without exist_ok protects a previous result even on collision.
            output.mkdir(parents=True, exist_ok=False)
            log_root = self.output_base / '.runtime-jobs'
            log_root.mkdir(exist_ok=True)
            job = {'id': job_id, 'status': 'queued', 'message': '正在准备工程副本…',
                   'source': str(source), 'output': str(output), 'width': request.width,
                   'height': request.height, 'started_at': time.time(), 'blockers': [], 'exe': None,
                   'log_path': str(log_root / (job_id + '.log')), 'report_path': str(output / 'conversion-report.json')}
            self.jobs[job_id] = job
        threading.Thread(target=self._run, args=(job_id, command), daemon=True,
                         name='uagent-convert-' + job_id[:8]).start()
        return self.get(job_id)

    def _run(self, job_id: str, command: list[str]) -> None:
        with self.lock:
            job = self.jobs[job_id]
            job['status'] = 'running'
        output = Path(job['output'])
        try:
            env = os.environ.copy()
            env['PYTHONUNBUFFERED'] = '1'
            env['UAGENT_CONVERSION_LOG'] = job['log_path']
            if getattr(sys, 'frozen', False):
                env['PYINSTALLER_RESET_ENVIRONMENT'] = '1'
            # GUI test/service flags must not alter the conversion child.
            for name in ('UAGENT_HEADLESS', 'UAGENT_STARTUP_LOG'):
                env.pop(name, None)
            with Path(job['log_path']).open('wb') as log:
                process = self.process_factory(command, cwd=str(Path(__file__).resolve().parents[1]),
                                               env=env, stdout=log, stderr=subprocess.STDOUT,
                                               creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
                try:
                    exit_code = process.wait(timeout=1800)
                except subprocess.TimeoutExpired:
                    if os.name == 'nt':
                        subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                       creationflags=subprocess.CREATE_NO_WINDOW)
                    else:
                        process.kill()
                    process.wait()
                    raise ValueError('转换超过 30 分钟，已终止本次任务。请查看运行日志。')
            report_path = output / 'conversion-report.json'
            if not report_path.is_file():
                raise ValueError(f'转换器退出但未生成报告（退出码 {exit_code}），请查看运行日志。')
            if report_path.stat().st_size > 10_000_000:
                raise ValueError('转换报告过大，未载入界面；请查看输出目录中的完整报告。')
            report = json.loads(report_path.read_text(encoding='utf-8'))
            if not isinstance(report, dict):
                raise ValueError('转换报告格式错误。')
            result = report_result(report, output, exit_code)
        except Exception as exc:
            result = {'status': 'failed', 'message': '转换运行失败。', 'blockers': [str(exc)], 'exe': None}
        # The compiler requires a new empty output directory. Keep live logs
        # beside it until conversion finishes, then include them in the result.
        try:
            shutil.copy2(job['log_path'], output / 'conversion.log')
            result['log_path'] = str(output / 'conversion.log')
        except OSError:
            pass
        with self.lock:
            job.update(result, finished_at=time.time())

    def get(self, job_id: str) -> dict:
        with self.lock:
            if job_id not in self.jobs:
                raise KeyError(job_id)
            job = dict(self.jobs[job_id])
        log_path = Path(job['log_path'])
        tail = ''
        if log_path.is_file():
            with log_path.open('rb') as stream:
                stream.seek(max(0, log_path.stat().st_size - 16_384))
                tail = stream.read().decode('utf-8', errors='replace')
        job['log_tail'] = tail
        job['elapsed_seconds'] = round(job.get('finished_at', time.time()) - job['started_at'])
        if job['status'] == 'running':
            for token, message in [('Capturing browser evidence', '正在采集浏览器布局…'),
                                   ('Compiling source contract', '正在解析源码并检查支持能力…'),
                                   ('Building SDL executable', '正在编译并验证 SDL 示例…')]:
                if token in tail:
                    job['message'] = message
        return job


router = APIRouter(prefix='/api/runtime-convert')
jobs = ConversionJobs(Path(os.environ.get('UAGENT_OUTPUT_ROOT', r'D:\uiagent_oct')))


@router.get('/config')
def config():
    return {'output_root': str(jobs.output_base), 'mode': 'local', 'width': 1024, 'height': 600}


@router.post('/jobs', status_code=202)
def start_conversion(request: ConversionRequest):
    try:
        return jobs.start(request)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get('/jobs/{job_id}')
def get_conversion(job_id: str):
    try:
        return jobs.get(job_id)
    except KeyError as exc:
        raise HTTPException(404, '转换任务不存在，请重新选择工程。') from exc


@router.post('/jobs/{job_id}/open-output')
def open_output(job_id: str):
    job = get_conversion(job_id)
    if os.name != 'nt':
        raise HTTPException(503, '此操作需要 Windows 桌面。')
    try:
        os.startfile(job['output'])
    except OSError as exc:
        raise HTTPException(503, f'无法打开输出文件夹：{exc}') from exc
    return {'opened': True}


@router.post('/jobs/{job_id}/preview')
def launch_preview(job_id: str):
    job = get_conversion(job_id)
    if job['status'] != 'passed' or not job.get('exe'):
        raise HTTPException(409, '未通过转换，不能运行生成草稿。')
    executable = Path(job['exe']).resolve()
    if Path(job['output']).resolve() not in executable.parents or not executable.is_file():
        raise HTTPException(400, '生成的 SDL EXE 不存在或路径无效。')
    try:
        subprocess.Popen([str(executable)], cwd=str(executable.parent),
                         creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    except OSError as exc:
        raise HTTPException(503, f'无法启动 SDL 示例：{exc}') from exc
    return {'opened': True}
