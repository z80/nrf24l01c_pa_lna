import time
import json
import threading
import cv2
import numpy as np
import mss
import win32gui
import os


# ============================================================
# 1. Locate Chiaki window
# ============================================================
def find_chiaki_window(title_substring="Chiaki"):
    hwnd = win32gui.FindWindow(None, title_substring)
    if hwnd == 0:
        raise RuntimeError("Chiaki window not found. Make sure it's open.")
    return win32gui.GetWindowRect(hwnd)  # (left, top, right, bottom)


# ============================================================
# 2. Frame capture thread
# ============================================================
class FrameGrabber(threading.Thread):
    def __init__(self, bbox, target_fps=20):
        super().__init__()
        self.bbox = bbox
        self.target_fps = target_fps
        self.sct = mss.mss()
        self.running = True
        self.latest_frame = None
        self.latest_timestamp = None

    def run(self):
        left, top, right, bottom = self.bbox
        w, h = right - left, bottom - top
        interval = 1.0 / self.target_fps

        while self.running:
            t = time.time()
            img = self.sct.grab({
                "left": left,
                "top": top,
                "width": w,
                "height": h
            })
            frame = np.array(img)[:, :, :3]  # BGRA -> BGR

            # Downsample to 256×256 for NitroGen
            frame_small = cv2.resize(frame, (256, 256), interpolation=cv2.INTER_AREA)

            self.latest_frame = frame_small
            self.latest_timestamp = t

            time.sleep(interval)

    def stop(self):
        self.running = False


# ============================================================
# 3. Action logger (full DS4 schema)
# ============================================================
class ActionLogger:
    def __init__(self):
        # Full DS4 action schema (NitroGen-compatible)
        self.current_action = {
            # Sticks (normalized)
            "lx": 0.0, "ly": 0.0,
            "rx": 0.0, "ry": 0.0,

            # Triggers (0–1)
            "l2": 0.0, "r2": 0.0,

            # Face buttons
            "cross": 0,
            "circle": 0,
            "square": 0,
            "triangle": 0,

            # Shoulder buttons
            "l1": 0, "r1": 0,

            # Stick clicks
            "l3": 0, "r3": 0,

            # System buttons
            "share": 0,
            "options": 0,
            "ps": 0,
            "touchpad": 0,

            # D-pad (0–7, 8 = NONE)
            "dpad": 8
        }

    def update(self, **kwargs):
        for k, v in kwargs.items():
            if k in self.current_action:
                self.current_action[k] = v


# ============================================================
# 4. Recorder loop
# ============================================================
def record_session(duration_sec=60, out_dir="dataset"):
    bbox = find_chiaki_window("Chiaki")
    grabber = FrameGrabber(bbox)
    grabber.start()

    actions = []
    action_logger = ActionLogger()

    print("Recording...")

    start = time.time()
    frame_id = 0

    while time.time() - start < duration_sec:
        frame = grabber.latest_frame
        ts = grabber.latest_timestamp

        if frame is None:
            time.sleep(0.01)
            continue

        # Save frame
        frame_path = f"{out_dir}/frames/{frame_id:06d}.jpg"
        cv2.imwrite(frame_path, frame)

        # Save action
        actions.append({
            "frame_id": frame_id,
            "timestamp": ts,
            "action": dict(action_logger.current_action)
        })

        frame_id += 1
        time.sleep(0.01)

    grabber.stop()
    grabber.join()

    # Save metadata
    with open(f"{out_dir}/actions.json", "w") as f:
        json.dump(actions, f, indent=2)

    print("Recording complete.")


# ============================================================
# 5. Example usage
# ============================================================
if __name__ == "__main__":
    os.makedirs("dataset/frames", exist_ok=True)
    record_session(duration_sec=60)
