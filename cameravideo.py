import cv2
import os
import datetime
import requests
import numpy as np
import time
from concurrent.futures import ThreadPoolExecutor

camera_ips = ["192.168.8.107", "192.168.8.109", "192.168.8.110", "192.168.8.111", "192.168.8.112", "192.168.8.114", "192.168.8.115", "192.168.8.116", "192.168.8.119", "192.168.8.120", "192.168.8.147"]  # Generates IPs automatically

# Define the video duration (in seconds) and FPS
VIDEO_DURATION = 600  # 10 minutes (600 seconds)
FPS = 1  # Frames per second
FRAME_INTERVAL = 1 / FPS  # Time between frames

# Define the root folder for saving videos
root_folder = "E:/10min"
os.makedirs(root_folder, exist_ok=True)

# Function to record video from a single camera
def record_video(ip):
    capture_url = f"http://{ip}:81/capture"

    # Create camera-specific folders
    camera_folder = os.path.join(root_folder, ip)
    os.makedirs(camera_folder, exist_ok=True)

    date_str = datetime.datetime.now().strftime("%Y-%m-%d")
    date_folder = os.path.join(camera_folder, date_str)
    os.makedirs(date_folder, exist_ok=True)

    # Generate video filename
    start_time = datetime.datetime.now()
    end_time = start_time + datetime.timedelta(seconds=VIDEO_DURATION)
    start_time_str = start_time.strftime("%H-%M-%S")
    end_time_str = end_time.strftime("%H-%M-%S")
    video_filename = f"{start_time_str}-{end_time_str}_{ip}.avi"
    video_path = os.path.join(date_folder, video_filename)

    # Define the video codec and create VideoWriter object
    fourcc = cv2.VideoWriter_fourcc(*'XVID')
    frame_width = 320
    frame_height = 240
    out = cv2.VideoWriter(video_path, fourcc, FPS, (frame_width, frame_height))

    print(f"🎥 Recording from {ip} at {FPS} FPS for 10 minutes...")

    start_recording_time = time.time()

    while (time.time() - start_recording_time) < VIDEO_DURATION:
        frame_start_time = time.time()  # Mark frame capture start time

        try:
            response = requests.get(capture_url, timeout=10)
            if response.status_code == 200:
                img_array = np.array(bytearray(response.content), dtype=np.uint8)
                frame = cv2.imdecode(img_array, -1)

                if frame is not None:
                    frame_resized = cv2.resize(frame, (frame_width, frame_height))
                    out.write(frame_resized)
                else:
                    print(f"⚠️ Error: Couldn't decode frame from {ip}")
            else:
                print(f"⚠️ Error: Failed to fetch frame from {ip}, Status Code: {response.status_code}")

        except requests.exceptions.Timeout:
            print(f"⚠️ Timeout while fetching frame from {ip}, skipping frame...")
        except Exception as e:
            print(f"⚠️ Error: Exception while fetching frame from {ip}: {e}")
            break  

        # Ensure correct frame interval (controls FPS)
        time_elapsed = time.time() - frame_start_time
        sleep_time = max(0, FRAME_INTERVAL - time_elapsed)
        time.sleep(sleep_time)

    # Release the writer
    out.release()
    print(f"✅ Video saved: {video_path}")

# Run multiple camera recordings in parallel using threading
while True:
    with ThreadPoolExecutor(max_workers=len(camera_ips)) as executor:
        executor.map(record_video, camera_ips)
