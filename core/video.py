"""Local video evidence extraction; model use remains optional."""
from dataclasses import dataclass, asdict
from pathlib import Path
import json
import shutil
import subprocess


@dataclass
class VideoEvidence:
    path: str
    duration: float
    width: int
    height: int
    fps: float
    keyframes: list[str]


def _exe(name: str, preferred: str) -> str:
    found = Path(preferred)
    if found.is_file():
        return str(found)
    resolved = shutil.which(name)
    if not resolved:
        raise RuntimeError(f"未找到 {name}")
    return resolved


def analyze_video(path: Path, output_dir: Path, interval: float = 1.0) -> VideoEvidence:
    path, output_dir = path.resolve(), output_dir.resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    output_dir.mkdir(parents=True, exist_ok=True)
    probe = _exe("ffprobe", r"C:\ffmpeg\bin\ffprobe.exe")
    ffmpeg = _exe("ffmpeg", r"C:\ffmpeg\bin\ffmpeg.exe")
    raw = subprocess.check_output([
        probe, "-v", "error", "-of", "json", "-show_entries",
        "format=duration:stream=width,height,r_frame_rate", str(path)
    ], text=True, encoding="utf-8", errors="replace")
    info = json.loads(raw)
    video = next((s for s in info.get("streams", []) if s.get("width")), {})
    num, den = (video.get("r_frame_rate") or "0/1").split("/", 1)
    fps = float(num) / max(float(den), 1.0)
    pattern = output_dir / "frame_%05d.jpg"
    subprocess.run([ffmpeg, "-y", "-i", str(path), "-vf", f"fps=1/{max(interval, .1)}",
                    "-q:v", "3", str(pattern)], check=True, capture_output=True)
    frames = sorted(str(p) for p in output_dir.glob("frame_*.jpg"))
    return VideoEvidence(str(path), float(info.get("format", {}).get("duration", 0)),
                         int(video.get("width", 0)), int(video.get("height", 0)), fps, frames)


def write_evidence(evidence: VideoEvidence, target: Path) -> Path:
    target.write_text(json.dumps(asdict(evidence), ensure_ascii=False, indent=2), encoding="utf-8")
    return target
