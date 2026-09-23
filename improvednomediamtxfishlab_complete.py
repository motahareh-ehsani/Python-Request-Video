"""
FishLab Complete System - Snapshot Version
==========================================
Simple, fast, works everywhere!

1. Grabs JPEG snapshots directly from cameras (no FFmpeg, no MediaMTX!)
2. Serves them through Flask — works locally AND remotely via Cloudflare
3. Records 10-min videos with Barrier sync (your original logic)
4. Sends reports to Google Sheets
5. Serves OBS-style dashboard at http://localhost:5000
6. Starts Cloudflare Tunnel automatically

Requirements:
    pip install flask requests opencv-python-headless numpy waitress
    cloudflared.exe must be at C:\\cloudflared\\cloudflared.exe

Run (as Administrator):
    python fishlab_complete.py

Open: http://localhost:5000
"""

import cv2
import os, sys, time, datetime, threading, subprocess, signal
import requests
import numpy as np
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from flask import Flask, Response, render_template_string, jsonify, send_file, request as freq, abort

# ─────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────

CAMERA_IPS = [
    f"192.168.20.{i}"
    for i in list(range(101, 121)) + list(range(121, 141)) + list(range(141, 161))
    if i not in {101,133,139,137,112,115,149,154,131,153, 157, 110,119,116,136,141,106,132,117,126,125,111,109,159, 143, 144, 145, 146, 147, 148, 108, 107, 103, 104, 105, 113, 114, 118, 120, 122, 123, 124, 128, 129, 134, 150, 151, 152, 156, 158}
]

EXPERIMENT_START   = datetime.datetime(2026, 3, 10, 6, 0, 0)
VIDEO_DURATION     = 600
FPS                = 5
FRAME_INTERVAL     = 1.0 / FPS
TOTAL_FRAMES       = VIDEO_DURATION * FPS
ROOT_FOLDER        = "C:/Users/Nothospan Vision/Desktop/video10min"
REPORTS_ROOT       = os.path.join(ROOT_FOLDER, "reports")
DASHBOARD_PORT     = 5000
CLOUDFLARED_EXE    = r"C:\cloudflared\cloudflared.exe"
GOOGLE_SCRIPT_URL  = "https://script.google.com/macros/s/AKfycbxZML36LrSBNY2hJq5iqmGRnJI-DPf0Cy-rS7HFNovs-WaZHEDjQQHUXVYitEzpO-Jd/exec"

# Snapshot refresh rate for dashboard (seconds)
SNAPSHOT_INTERVAL  = 0.2   # 2 fps on dashboard — light on network

os.makedirs(ROOT_FOLDER,  exist_ok=True)
os.makedirs(REPORTS_ROOT, exist_ok=True)

# ─────────────────────────────────────────────────────────────
# SHUTDOWN
# ─────────────────────────────────────────────────────────────

_all_processes = []

def _shutdown(sig=None, frame=None):
    print("\n[Shutdown] Stopping all processes...")
    for p in _all_processes:
        try: p.terminate()
        except: pass
    sys.exit(0)

signal.signal(signal.SIGINT,  _shutdown)
signal.signal(signal.SIGTERM, _shutdown)

# ─────────────────────────────────────────────────────────────
# SNAPSHOT STORE
# One latest JPEG per camera, updated continuously
# ─────────────────────────────────────────────────────────────

snapshot_store = {}
snapshot_lock  = threading.Lock()
camera_status  = {ip: "starting" for ip in CAMERA_IPS}
status_lock    = threading.Lock()

def _make_placeholder(text, color=(20, 20, 20)):
    img = np.full((240, 320, 3), color, dtype=np.uint8)
    cv2.putText(img, text, (10, 120), cv2.FONT_HERSHEY_SIMPLEX,
                0.55, (120, 120, 120), 1, cv2.LINE_AA)
    _, buf = cv2.imencode('.jpg', img)
    return buf.tobytes()

DARK_FRAME  = _make_placeholder("DARK PERIOD", (10, 10, 20))
ERROR_FRAME = _make_placeholder("NO SIGNAL",   (20, 8, 8))

# ─────────────────────────────────────────────────────────────
# CLOUDFLARE TUNNEL
# ─────────────────────────────────────────────────────────────

def start_cloudflare():
    if not os.path.exists(CLOUDFLARED_EXE):
        print(f"[Cloudflare] WARNING: not found at {CLOUDFLARED_EXE}")
        return
    print("[Cloudflare] Starting tunnel...")

    def _run():
        cmd = [CLOUDFLARED_EXE, "tunnel", "--protocol", "http2",
               "--url", f"http://localhost:{DASHBOARD_PORT}"]
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True)
        _all_processes.append(p)
        for line in p.stdout:
            line = line.strip()
            if "trycloudflare.com" in line:
                print("\n" + "=" * 58)
                print("  🌍 REMOTE ACCESS URL:")
                for word in line.split():
                    if "trycloudflare.com" in word:
                        print(f"  {word}")
                        break
                print("  Open this on your phone or home PC!")
                print("=" * 58 + "\n")
            elif "ERR" in line and "context canceled" not in line:
                print(f"[Cloudflare] {line}")

    threading.Thread(target=_run, daemon=True).start()

# ─────────────────────────────────────────────────────────────
# LIGHT CYCLE HELPERS
# ─────────────────────────────────────────────────────────────

def get_camera_number(ip):
    return int(ip.split(".")[-1])

def get_cycle_hours(ip):
    n = get_camera_number(ip)
    if 101 <= n <= 120: return 6
    if 121 <= n <= 140: return 12
    return 18

def should_record(ip, now=None):
    try:
        now = now or datetime.datetime.now()
        if now < EXPERIMENT_START:
            return True
        elapsed   = (now - EXPERIMENT_START).total_seconds()
        block_idx = int(elapsed // (get_cycle_hours(ip) * 3600))
        return (block_idx % 2) == 0
    except:
        return True

def get_group(ip):
    n = get_camera_number(ip)
    if 101 <= n <= 120: return "A"
    if 121 <= n <= 140: return "B"
    return "C"

def get_active_cameras(now=None):
    now = now or datetime.datetime.now()
    return [ip for ip in CAMERA_IPS if should_record(ip, now)]

# ─────────────────────────────────────────────────────────────
# RECORDING (your original Barrier logic)
# ─────────────────────────────────────────────────────────────

def write_batch_report(report_path, date_str, start_str, end_str, results):
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(f"Date: {date_str}\nTime range: {start_str}-{end_str}\n\n")
        f.write(f"{'Camera IP':<20}{'Black frames':<25}{'Good frames':<15}{'Total':<15}\n")
        f.write("-" * 75 + "\n")
        for item in sorted(results, key=lambda x: x["ip"]):
            f.write(f"{item['ip']:<20}{item['black_frames']:<25}"
                    f"{item['good_frames']:<15}{item['total_frames']:<15}\n")

def send_to_google_sheet(results, batch_start):
    time_label   = batch_start.strftime("%H:%M")
    cameras_data = {item["ip"].split(".")[-1]: item["black_frames"] for item in results}
    try:
        r = requests.post(GOOGLE_SCRIPT_URL,
                          json={"time": time_label, "cameras": cameras_data},
                          timeout=10)
        print(f"[Sheets] Updated: {r.text[:80]}")
    except Exception as e:
        print(f"[Sheets] Failed: {e}")

def record_video(ip, batch_start, barrier):
    capture_url = f"http://{ip}:81/capture"

    # Questo controllo è normalmente già fatto da recording_loop(),
    # ma lo manteniamo per sicurezza.
    if not should_record(ip, batch_start):
        with snapshot_lock:
            snapshot_store[ip] = DARK_FRAME

        with status_lock:
            camera_status[ip] = "dark"

        barrier.wait()

        return {
            "ip": ip,
            "good_frames": 0,
            "black_frames": 0,
            "total_frames": TOTAL_FRAMES,
            "status": "skipped"
        }

    cam_folder = os.path.join(
        ROOT_FOLDER,
        ip,
        batch_start.strftime("%Y-%m-%d")
    )
    os.makedirs(cam_folder, exist_ok=True)

    start_str = batch_start.strftime("%H-%M-%S")
    end_str = (
        batch_start + datetime.timedelta(seconds=VIDEO_DURATION)
    ).strftime("%H-%M-%S")

    avi_path = os.path.join(
        cam_folder,
        f"{start_str}-{end_str}_{ip}.avi"
    )

    fourcc = cv2.VideoWriter_fourcc(*"XVID")
    out = cv2.VideoWriter(
        avi_path,
        fourcc,
        FPS,
        (320, 240)
    )

    black = np.zeros((240, 320, 3), dtype=np.uint8)
    session = requests.Session()

    print(f"{ip} ready, waiting for all cameras...")
    barrier.wait()

    rec_start = time.monotonic()
    frames_ok = 0
    frames_bad = 0

    def write_bad_frame():
        """
        Scrive un frame nero nell'AVI e segnala l'errore
        anche sulla dashboard.
        """
        nonlocal frames_bad

        out.write(black)
        frames_bad += 1

        with snapshot_lock:
            snapshot_store[ip] = ERROR_FRAME

        with status_lock:
            camera_status[ip] = "error"

    try:
        for idx in range(TOTAL_FRAMES):

            # Mantiene il ritmo di acquisizione impostato da FPS.
            target_time = rec_start + idx * FRAME_INTERVAL
            wait = target_time - time.monotonic()

            if wait > 0:
                time.sleep(wait)

            try:
                # Questa è l'unica richiesta fatta alla ESP per questo frame.
                resp = session.get(
                    capture_url,
                    timeout=0.3
                )

                if resp.status_code != 200:
                    write_bad_frame()
                    continue

                # Il JPEG originale ricevuto dalla ESP.
                jpeg_bytes = resp.content

                # Decodifica necessaria per scrivere il frame nell'AVI.
                arr = np.frombuffer(
                    jpeg_bytes,
                    dtype=np.uint8
                )

                frame = cv2.imdecode(
                    arr,
                    cv2.IMREAD_COLOR
                )

                if frame is None:
                    write_bad_frame()
                    continue

                # Frame per il video AVI.
                frame = cv2.resize(
                    frame,
                    (320, 240)
                )

                out.write(frame)
                frames_ok += 1

                # Lo stesso JPEG viene reso disponibile alla dashboard.
                # Non viene fatta nessuna seconda richiesta alla ESP.
                with snapshot_lock:
                    snapshot_store[ip] = jpeg_bytes

                with status_lock:
                    camera_status[ip] = "ok"

            except requests.exceptions.Timeout:
                write_bad_frame()

            except Exception as e:
                print(f"[Rec] {ip} error: {e}")
                write_bad_frame()

    finally:
        out.release()
        session.close()

    print(
        f"[Rec] {ip}: "
        f"good={frames_ok} "
        f"black={frames_bad}"
    )

    return {
        "ip": ip,
        "good_frames": frames_ok,
        "black_frames": frames_bad,
        "total_frames": TOTAL_FRAMES,
        "video_path": avi_path,
        "status": "recorded"
    }

def recording_loop():
    while True:
        batch_start    = datetime.datetime.now()
        active_cameras = get_active_cameras(batch_start)
        print(f"\n[Rec] {batch_start.strftime('%H:%M:%S')} — "
              f"{len(active_cameras)}/{len(CAMERA_IPS)} cameras active")
        if active_cameras:
            barrier = Barrier(len(active_cameras))
            with ThreadPoolExecutor(max_workers=len(active_cameras)) as ex:
                futures = [ex.submit(record_video, ip, batch_start, barrier)
                           for ip in active_cameras]
                results = [f.result() for f in futures]
            date_str  = batch_start.strftime("%Y-%m-%d")
            start_str = batch_start.strftime("%H-%M-%S")
            end_str   = (batch_start + datetime.timedelta(
                          seconds=VIDEO_DURATION)).strftime("%H-%M-%S")
            rpt_dir   = os.path.join(REPORTS_ROOT, date_str)
            os.makedirs(rpt_dir, exist_ok=True)
            write_batch_report(
                os.path.join(rpt_dir, f"{start_str}-{end_str}.txt"),
                date_str, start_str, end_str, results)
            send_to_google_sheet(results, batch_start)
        else:
            time.sleep(VIDEO_DURATION)

# ─────────────────────────────────────────────────────────────
# FLASK DASHBOARD
# ─────────────────────────────────────────────────────────────

app = Flask(__name__)
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0

HTML = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>FishLab Monitor</title>
<link href="https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=Barlow:wght@300;400;600&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#0b0e12;--panel:#12171f;--panel2:#181e28;
  --border:#1e2736;--border2:#263040;
  --green:#2ecc8a;--blue:#3a9eff;--red:#ff4f4f;--yellow:#f5c518;
  --text:#adc0cc;--dim:#3d5060;--white:#d8e8f0;
  --mono:'Share Tech Mono',monospace;
  --sans:'Barlow',sans-serif;
}
body{background:var(--bg);color:var(--text);font-family:var(--sans);
  min-height:100vh;display:flex;flex-direction:column;overflow-x:hidden}
header{display:flex;align-items:center;justify-content:space-between;
  padding:10px 20px;background:var(--panel);
  border-bottom:2px solid var(--border2);position:sticky;top:0;z-index:100}
.logo{font-family:var(--mono);font-size:1.05rem;letter-spacing:4px;color:var(--green)}
.logo span{color:var(--blue)}
.hright{display:flex;gap:14px;align-items:center}
.badge{font-family:var(--mono);font-size:.68rem;padding:3px 10px;
  border-radius:20px;border:1px solid}
.badge.g{border-color:#1a4a36;color:var(--green);background:#061a10}
.badge.y{border-color:#4a3a10;color:var(--yellow);background:#1a1206}
#clock{font-family:var(--mono);font-size:.78rem;color:var(--dim)}
main{flex:1;padding:12px 14px;display:flex;flex-direction:column;gap:10px}
.gp{background:var(--panel);border:1px solid var(--border2);border-radius:5px;overflow:hidden}
.gh{display:flex;align-items:center;justify-content:space-between;
  padding:7px 14px;background:var(--panel2);
  border-bottom:1px solid var(--border);border-left:3px solid var(--green)}
.gh.B{border-left-color:var(--blue)}.gh.C{border-left-color:var(--yellow)}
.gtitle{font-family:var(--mono);font-size:.76rem;letter-spacing:2px;
  color:var(--white);font-weight:600}
.gmeta{font-family:var(--mono);font-size:.65rem;color:var(--dim)}
.cgrid{display:grid;grid-template-columns:repeat(10,1fr);gap:3px;padding:6px}
.ct{border-radius:3px;overflow:hidden;border:1px solid var(--border);
  background:#080b0e;cursor:pointer;position:relative;
  transition:border-color .15s,transform .12s}
.ct:hover{border-color:var(--green);transform:scale(1.06);z-index:10}
.ct.ok{border-color:#0d2e1e}.ct.dark{border-color:#151030;opacity:.5}
.ct.error{border-color:#2e0d0d}.ct.starting{border-color:#1a1a2e;opacity:.6}
.ct img{width:100%;aspect-ratio:4/3;display:block;object-fit:cover;background:#000}
.clabel{font-family:var(--mono);font-size:.55rem;
  padding:2px 4px;background:#060a0d;
  display:flex;justify-content:space-between;align-items:center}
.clabel .ip{color:var(--dim)}
.st{font-size:.52rem;padding:1px 4px;border-radius:2px}
.st.ok{color:var(--green)}.st.dark{color:#7060b0}
.st.error{color:var(--red)}.st.starting{color:var(--blue)}
.rp{background:var(--panel);border:1px solid var(--border2);border-radius:5px;overflow:hidden}
.rh{display:flex;align-items:center;justify-content:space-between;
  padding:7px 14px;background:var(--panel2);border-bottom:1px solid var(--border)}
.rh-title{font-family:var(--mono);font-size:.76rem;letter-spacing:2px;color:var(--white)}
.rbtn{font-family:var(--mono);font-size:.62rem;padding:3px 10px;border-radius:3px;
  background:#06182a;border:1px solid #1a3a5a;color:var(--blue);cursor:pointer}
.rbtn:hover{background:#0a2040}
.rlist{padding:6px 14px;max-height:150px;overflow-y:auto}
.rrow{display:flex;justify-content:space-between;align-items:center;
  padding:4px 0;border-bottom:1px solid var(--border);font-size:.7rem}
.rrow:last-child{border:none}
.rname{font-family:var(--mono);font-size:.65rem;color:var(--text)}
.rdl{font-family:var(--mono);font-size:.6rem;padding:2px 8px;border-radius:2px;
  background:#06182a;border:1px solid #1a3a5a;color:var(--blue);cursor:pointer}
.rdl:hover{background:#0a2040}
.novid{color:var(--dim);font-size:.72rem;padding:8px 0}
footer{display:flex;gap:20px;padding:6px 20px;
  background:var(--panel);border-top:1px solid var(--border);
  font-family:var(--mono);font-size:.68rem;color:var(--dim)}
footer b{color:var(--blue)}
#modal{display:none;position:fixed;inset:0;background:#000000ee;z-index:500;
  flex-direction:column;align-items:center;justify-content:center;gap:10px}
#modal.open{display:flex}
#modal img{width:min(88vw,960px);aspect-ratio:4/3;object-fit:cover;
  border:1px solid var(--green);border-radius:4px;background:#000}
#mtitle{font-family:var(--mono);color:var(--green);font-size:.9rem}
#mclose{position:absolute;top:16px;right:22px;
  font-size:2rem;color:var(--dim);cursor:pointer;line-height:1}
#mclose:hover{color:var(--red)}
</style>
</head>
<body>
<header>
  <div class="logo">FISH<span>LAB</span> // MONITOR</div>
  <div class="hright">
    <div class="badge g" id="b-ok">LIVE: 0</div>
    <div class="badge y" id="b-dark">DARK: 0</div>
    <div id="clock"></div>
  </div>
</header>
<main>
  {% for gname, gips in groups.items() %}
  <div class="gp">
    <div class="gh {{ gname }}">
      <span class="gtitle">GROUP {{ gname }} &nbsp;·&nbsp; CAM {{ gips[0].split('.')[-1] }}–{{ gips[-1].split('.')[-1] }}</span>
      <span class="gmeta">{{ gips|length }} cameras</span>
    </div>
    <div class="cgrid">
      {% for ip in gips %}
      {% set key = ip.replace('.','_') %}
      {% set num = ip.split('.')[-1] %}
      <div class="ct starting" id="t-{{key}}" onclick="openModal('{{ip}}')">
        <img id="img-{{key}}" src="/snap/{{ip}}" alt="{{ip}}" loading="lazy">
        <div class="clabel">
          <span class="ip">.{{num}}</span>
          <span class="st starting" id="s-{{key}}">···</span>
        </div>
      </div>
      {% endfor %}
    </div>
  </div>
  {% endfor %}
  <div class="rp">
    <div class="rh">
      <span class="rh-title">SAVED RECORDINGS</span>
      <button class="rbtn" onclick="loadVideos()">REFRESH</button>
    </div>
    <div class="rlist" id="rlist"><div class="novid">Loading...</div></div>
  </div>
</main>
<footer>
  <span>LIVE <b id="f-ok">0</b></span>
  <span>DARK <b id="f-dark">0</b></span>
  <span>TOTAL <b>{{ total_all }}</b></span>
</footer>
<div id="modal">
  <div id="mclose" onclick="closeModal()">×</div>
  <div id="mtitle">--</div>
  <img id="mimg" src="" alt="fullscreen">
</div>
<script>
const cameras  = {{ camera_ips|tojson }};
const totalAll = {{ total_all }};
let activeModal = null;

function updateClock(){
  document.getElementById('clock').textContent = new Date().toLocaleTimeString();
}
setInterval(updateClock,200); updateClock();

// Refresh all snapshots every second
function refreshSnapshots(){
  const t = Date.now();
  cameras.forEach(ip=>{
    const key = ip.replace(/\./g,'_');
    const img = document.getElementById('img-'+key);
    if(img && ip !== activeModal){
      img.src = '/snap/'+ip+'?t='+t;
    }
  });
  if(activeModal){
    document.getElementById('mimg').src = '/snap/'+activeModal+'?t='+t;
  }
}
setInterval(refreshSnapshots, 1000);

// Update status every 5 seconds
function updateStatus(){
  fetch('/status').then(r=>r.json()).then(data=>{
    let ok=0, dark=0;
    cameras.forEach(ip=>{
      const key = ip.replace(/\./g,'_');
      const st  = data[ip]||'starting';
      const tile= document.getElementById('t-'+key);
      const stEl= document.getElementById('s-'+key);
      if(!tile||!stEl) return;
      tile.className = 'ct '+st;
      stEl.className = 'st '+st;
      stEl.textContent = st==='ok'?'LIVE':st==='dark'?'DARK':st==='error'?'ERR':'···';
      if(st==='ok') ok++; else if(st==='dark') dark++;
    });
    document.getElementById('f-ok').textContent   = ok;
    document.getElementById('f-dark').textContent  = totalAll - ok;
    document.getElementById('b-ok').textContent   = 'LIVE: '+ok;
    document.getElementById('b-dark').textContent = 'DARK: '+(totalAll-ok);
  });
}
setInterval(updateStatus, 5000); updateStatus();

function openModal(ip){
  activeModal = ip;
  document.getElementById('mtitle').textContent = ip;
  document.getElementById('mimg').src = '/snap/'+ip+'?t='+Date.now();
  document.getElementById('modal').classList.add('open');
}
function closeModal(){
  activeModal = null;
  document.getElementById('modal').classList.remove('open');
}
document.addEventListener('keydown',e=>{ if(e.key==='Escape') closeModal(); });

function loadVideos(){
  fetch('/videos').then(r=>r.json()).then(data=>{
    const el = document.getElementById('rlist');
    if(!data.length){
      el.innerHTML='<div class="novid">No recordings yet.</div>';
      return;
    }
    el.innerHTML = data.slice(0,100).map(v=>
      `<div class="rrow">
        <span class="rname">${v.name}</span>
        <button class="rdl" onclick="location='/download?path=${encodeURIComponent(v.path)}'">DOWNLOAD</button>
      </div>`
    ).join('');
  });
}
loadVideos();
setInterval(loadVideos, 60000);
</script>
</body>
</html>
"""

@app.route('/')
def index():
    now    = datetime.datetime.now()
    active = get_active_cameras(now)
    groups = {"A":[],"B":[],"C":[]}
    for ip in active:
        groups[get_group(ip)].append(ip)
    groups = {k:v for k,v in groups.items() if v}
    return render_template_string(HTML,
        groups=groups,
        camera_ips=active,
        total=len(active),
        total_all=len(CAMERA_IPS))

@app.route('/snap/<ip>')
def snapshot(ip):
    """Return latest JPEG snapshot for a camera."""
    with snapshot_lock:
        data = snapshot_store.get(ip, ERROR_FRAME)
    return Response(data, mimetype='image/jpeg',
                    headers={'Cache-Control': 'no-cache'})

@app.route('/status')
def status():
    with status_lock:
        return jsonify(dict(camera_status))

@app.route('/videos')
def videos():
    result = []
    for root, dirs, files in os.walk(ROOT_FOLDER):
        for f in sorted(files, reverse=True):
            if f.endswith('.avi'):
                result.append({"name": f, "path": os.path.join(root, f)})
                if len(result) >= 200: break
    return jsonify(result)

@app.route('/download')
def download():
    path = freq.args.get('path','')
    if not path or not os.path.exists(path):
        abort(404)
    return send_file(path, as_attachment=True)

# ─────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────

if __name__ == '__main__':
    print("=" * 58)
    print("  FishLab Complete System — Snapshot Version")
    print("=" * 58)

    # Do not start the old random Quick Tunnel.
    # Cloudflare runs separately as a Windows service.
    # start_cloudflare()

    for ip in CAMERA_IPS:
        snapshot_store[ip] = ERROR_FRAME
        camera_status[ip] = "starting"

    print("[Snapshots] Dashboard will reuse recording frames")

    t_rec = threading.Thread(
        target=recording_loop,
        daemon=True
    )
    t_rec.start()

    print("[Recorder] Background recording started")

    from waitress import serve

    serve(
        app,
        host="0.0.0.0",
        port=DASHBOARD_PORT,
        threads=16
    )
    
