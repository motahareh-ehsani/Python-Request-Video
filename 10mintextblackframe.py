import cv2
import os
import datetime
import requests
import numpy as np
import time
from concurrent.futures import ThreadPoolExecutor

# Cameras
camera_ips = [
    f"192.168.20.{i}"
    for i in list(range(101, 121)) + list(range(121, 141)) + list(range(141, 161))
]

# Recording settings
VIDEO_DURATION = 600   # 600 seconds = 10 minutes
FPS = 5
FRAME_INTERVAL = 1.0 / FPS
TOTAL_FRAMES = VIDEO_DURATION * FPS

# Save folders
root_folder = "E:/video10min"
reports_root = os.path.join(root_folder, "reports")
os.makedirs(root_folder, exist_ok=True)
os.makedirs(reports_root, exist_ok=True)

# Experiment start time
EXPERIMENT_START = datetime.datetime(2026, 3, 10, 6, 0, 0)


def get_camera_number(ip):
    """Extract camera number from IP, e.g. 192.168.20.101 -> 101"""
    return int(ip.split(".")[-1])


def should_record(ip, now=None):
    """
    Normal behavior:
    - record only when light is ON

    Failsafe behavior:
    - if anything goes wrong in time/light calculation, record anyway
    """
    try:
        if now is None:
            now = datetime.datetime.now()

        camera_number = get_camera_number(ip)

        if 101 <= camera_number <= 120:
            cycle_hours = 6
        elif 121 <= camera_number <= 140:
            cycle_hours = 12
        elif 141 <= camera_number <= 160:
            cycle_hours = 18
        else:
            print(f"Warning: unknown camera group for {ip}. Recording anyway as failsafe.")
            return True

        if now < EXPERIMENT_START:
            print(f"Warning: current time is before experiment start for {ip}. Recording anyway as failsafe.")
            return True

        elapsed_seconds = (now - EXPERIMENT_START).total_seconds()
        cycle_seconds = cycle_hours * 3600
        block_index = int(elapsed_seconds // cycle_seconds)

        light_on = (block_index % 2) == 0
        return light_on

    except Exception as e:
        print(f"Warning: time/light check failed for {ip}: {e}. Recording anyway as failsafe.")
        return True


def write_batch_report(report_path, date_str, start_time_str, end_time_str, results):
    """
    Save one text file for one 10-minute recording interval.
    Each row contains one camera and its skipped/black frame count.
    """
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(f"Date: {date_str}\n")
        f.write(f"Time range: {start_time_str}-{end_time_str}\n")
        f.write("\n")
        f.write(f"{'Camera IP':<20}{'Skipped/black frames':<25}{'Good frames':<15}{'Total frames':<15}\n")
        f.write("-" * 75 + "\n")

        for item in sorted(results, key=lambda x: x["ip"]):
            f.write(
                f"{item['ip']:<20}"
                f"{item['black_frames']:<25}"
                f"{item['good_frames']:<15}"
                f"{item['total_frames']:<15}\n"
            )


def record_video(ip, batch_start_time):
    capture_url = f"http://{ip}:81/capture"

    if not should_record(ip, batch_start_time):
        print(f"Skipping {ip}: scheduled dark period at {batch_start_time.strftime('%Y-%m-%d %H:%M:%S')}")
        return {
            "ip": ip,
            "good_frames": 0,
            "black_frames": 0,
            "total_frames": TOTAL_FRAMES,
            "video_path": None,
            "status": "skipped"
        }

    # Video folders organized by camera/date
    camera_folder = os.path.join(root_folder, ip)
    os.makedirs(camera_folder, exist_ok=True)

    date_str = batch_start_time.strftime("%Y-%m-%d")
    date_folder = os.path.join(camera_folder, date_str)
    os.makedirs(date_folder, exist_ok=True)

    # Shared time range for all cameras
    start_time = batch_start_time
    end_time = start_time + datetime.timedelta(seconds=VIDEO_DURATION)

    start_time_str = start_time.strftime("%H-%M-%S")
    end_time_str = end_time.strftime("%H-%M-%S")

    video_filename = f"{start_time_str}-{end_time_str}_{ip}.avi"
    video_path = os.path.join(date_folder, video_filename)

    # Video writer
    fourcc = cv2.VideoWriter_fourcc(*'XVID')
    frame_width = 320
    frame_height = 240
    out = cv2.VideoWriter(video_path, fourcc, FPS, (frame_width, frame_height))

    # Black frame used when capture fails
    black_frame = np.zeros((frame_height, frame_width, 3), dtype=np.uint8)

    print(f"Recording from {ip} at {FPS} FPS for 10 minutes...")
    print(f"Target: exactly {TOTAL_FRAMES} frames")

    recording_start = time.monotonic()
    frames_ok = 0
    frames_black = 0

    session = requests.Session()

    try:
        for frame_idx in range(TOTAL_FRAMES):
            target_time = recording_start + frame_idx * FRAME_INTERVAL

            now_mono = time.monotonic()
            sleep_time = target_time - now_mono
            if sleep_time > 0:
                time.sleep(sleep_time)

            frame_to_write = black_frame

            try:
                response = session.get(capture_url, timeout=0.15)

                if response.status_code == 200:
                    img_array = np.frombuffer(response.content, dtype=np.uint8)
                    frame = cv2.imdecode(img_array, cv2.IMREAD_COLOR)

                    if frame is not None:
                        frame_resized = cv2.resize(frame, (frame_width, frame_height))
                        frame_to_write = frame_resized
                        frames_ok += 1
                    else:
                        print(f"{ip} frame {frame_idx}: decode failed -> black frame")
                        frames_black += 1
                else:
                    print(f"{ip} frame {frame_idx}: HTTP {response.status_code} -> black frame")
                    frames_black += 1

            except requests.exceptions.Timeout:
                print(f"{ip} frame {frame_idx}: timeout -> black frame")
                frames_black += 1

            except Exception as e:
                print(f"{ip} frame {frame_idx}: exception {e} -> black frame")
                frames_black += 1

            out.write(frame_to_write)

    finally:
        out.release()
        session.close()

    print(f"Video saved: {video_path}")
    print(f"{ip}: good frames = {frames_ok}, black frames = {frames_black}, total = {TOTAL_FRAMES}")

    return {
        "ip": ip,
        "good_frames": frames_ok,
        "black_frames": frames_black,
        "total_frames": TOTAL_FRAMES,
        "video_path": video_path,
        "status": "recorded"
    }


def get_active_cameras(now=None):
    """Return only cameras that should record now."""
    if now is None:
        now = datetime.datetime.now()
    return [ip for ip in camera_ips if should_record(ip, now)]


while True:
    batch_start_time = datetime.datetime.now()
    active_cameras = get_active_cameras(batch_start_time)

    print(f"\nCheck time: {batch_start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Cameras allowed to record now: {len(active_cameras)} / {len(camera_ips)}")

    if active_cameras:
        with ThreadPoolExecutor(max_workers=len(active_cameras)) as executor:
            futures = [executor.submit(record_video, ip, batch_start_time) for ip in active_cameras]
            results = [future.result() for future in futures]

        # Save one shared report file for this 10-minute interval
        date_str = batch_start_time.strftime("%Y-%m-%d")
        start_time_str = batch_start_time.strftime("%H-%M-%S")
        end_time_str = (batch_start_time + datetime.timedelta(seconds=VIDEO_DURATION)).strftime("%H-%M-%S")

        reports_date_folder = os.path.join(reports_root, date_str)
        os.makedirs(reports_date_folder, exist_ok=True)

        report_filename = f"{start_time_str}-{end_time_str}.txt"
        report_path = os.path.join(reports_date_folder, report_filename)

        write_batch_report(
            report_path=report_path,
            date_str=date_str,
            start_time_str=start_time_str,
            end_time_str=end_time_str,
            results=results
        )

        print(f"Shared report saved: {report_path}")

    else:
        print("No cameras should record right now. Sleeping for 600 seconds...")
        time.sleep(600)
