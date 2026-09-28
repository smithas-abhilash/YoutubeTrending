"""Analyse a local video file: shot cuts, pacing and keyframes.

Use this only with video files you are allowed to use (your own videos, or
files the owner shared with you). The app never downloads videos from YouTube.
Needs `opencv-python-headless`.
"""

from __future__ import annotations

from pathlib import Path
from statistics import mean, median

SAMPLE_FPS = 4          # frames per second to inspect
CUT_THRESHOLD = 0.45    # Bhattacharyya distance between colour histograms that counts as a cut
MIN_SHOT_S = 0.4        # ignore "cuts" closer together than this (flashes, fast zooms)


def _hist(cv2, frame):
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    h = cv2.calcHist([hsv], [0, 1], None, [50, 60], [0, 180, 0, 256])
    cv2.normalize(h, h, 0, 1, cv2.NORM_MINMAX)
    return h


def _label(t: float) -> str:
    m, s = divmod(int(t), 60)
    return f"{m:02d}m{s:02d}s"


def analyze_video_file(path: str | Path, out_dir: str | Path, max_frames: int = 16) -> dict:
    try:
        import cv2
    except ImportError as e:
        raise RuntimeError("Install opencv-python-headless to analyse video files.") from e

    path, out_dir = Path(path), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        raise RuntimeError(f"Could not open video: {path}")
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    duration = total / fps if total else 0.0
    step = max(1, round(fps / SAMPLE_FPS))

    cuts: list[float] = []
    prev_hist = None
    idx = 0
    while True:
        ok = cap.grab()
        if not ok:
            break
        if idx % step == 0:
            ok, frame = cap.retrieve()
            if not ok:
                break
            t = idx / fps
            h = _hist(cv2, frame)
            if prev_hist is not None:
                dist = cv2.compareHist(prev_hist, h, cv2.HISTCMP_BHATTACHARYYA)
                if dist > CUT_THRESHOLD and (not cuts or t - cuts[-1] >= MIN_SHOT_S):
                    cuts.append(t)
            prev_hist = h
        idx += 1
    if not duration:
        duration = idx / fps

    # Shots = spans between cuts.
    bounds = [0.0] + cuts + [duration]
    shots = [{"start": round(a, 2), "end": round(b, 2), "length": round(b - a, 2)}
             for a, b in zip(bounds, bounds[1:]) if b > a]
    lengths = [s["length"] for s in shots]
    hook = [s["length"] for s in shots if s["start"] < 30]
    body = [s["length"] for s in shots if s["start"] >= 30]

    # Keyframes: the opening (hook), the first frame after each cut, the ending.
    # If there are too many, keep an even spread but always keep the first 10 s.
    candidates = [0.5] + [c + 0.2 for c in cuts] + [max(duration - 1.0, 0.0)]
    early = [t for t in candidates if t < 10]
    later = [t for t in candidates if t >= 10]
    room = max(max_frames - len(early[:6]), 1)
    if len(later) > room:
        later = [later[round(i * (len(later) - 1) / (room - 1))] for i in range(room)] if room > 1 else later[-1:]
    wanted = sorted(set(early[:6] + later))

    frames = []
    for t in wanted:
        cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
        ok, frame = cap.read()
        if not ok:
            continue
        h, w = frame.shape[:2]
        if w > 768:
            frame = cv2.resize(frame, (768, int(h * 768 / w)))
        p = out_dir / f"frame_{_label(t)}.jpg"
        cv2.imwrite(str(p), frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
        frames.append({"time_s": round(t, 1), "path": str(p)})
    cap.release()

    minutes = duration / 60 if duration else 0
    return {
        "file": path.name,
        "duration_s": round(duration, 1),
        "shots": len(shots),
        "cuts_per_minute": round(len(cuts) / minutes, 1) if minutes else None,
        "avg_shot_s": round(mean(lengths), 2) if lengths else None,
        "median_shot_s": round(median(lengths), 2) if lengths else None,
        "hook_avg_shot_s": round(mean(hook), 2) if hook else None,
        "body_avg_shot_s": round(mean(body), 2) if body else None,
        "shot_timeline": shots[:150],
        "frames": frames,
    }
