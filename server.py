import os
import sys
import uuid
import pathlib
import threading
import subprocess
import shutil
import time
import json
import queue
from flask import Flask, request, jsonify, send_file, render_template
import frangere

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['OUTPUT_FOLDER'] = 'outputs'
HISTORY_FILE = os.path.join(app.config['OUTPUT_FOLDER'], 'history.json')

os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs(app.config['OUTPUT_FOLDER'], exist_ok=True)

# Paths to tools inside Docker
FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"
AVMENC = "/app/avm/build/avmenc"
AV2_MUX = "/app/av2-tools/build/apps/av2_mux/av2_mux"

tasks = {}
task_queue = queue.Queue()

def fragment_mp4(src_path, out_path):
    mp4 = frangere.ParsedMP4.Parse(pathlib.Path(src_path).read_bytes())
    fragments = frangere.FragmentMetadata.build_fragments(mp4)
    concat = b''.join(b for b, _ in fragments)
    init_bytes = frangere.InitSegment.build(mp4, b'av2E')
    pathlib.Path(out_path).write_bytes(init_bytes + concat)

def ensure_fragmented(tid, tinfo):
    if tinfo.get('status') != 'done':
        return False
    output_mp4 = tinfo.get('output_file') or os.path.join(app.config['OUTPUT_FOLDER'], f"{tid}.mp4")
    output_fmp4 = tinfo.get('fragmented_file') or os.path.join(app.config['OUTPUT_FOLDER'], f"{tid}_frag.mp4")
    changed = (tinfo.get('output_file') != output_mp4) or (tinfo.get('fragmented_file') != output_fmp4)
    tinfo['output_file'] = output_mp4
    tinfo['fragmented_file'] = output_fmp4
    if os.path.exists(output_mp4) and not os.path.exists(output_fmp4):
        fragment_mp4(output_mp4, output_fmp4)
        tinfo.setdefault('log', []).append("Fragmented existing MP4.")
        return True
    return changed

def load_history():
    global tasks
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r") as f:
                tasks = json.load(f)
        except Exception:
            tasks = {}
            return

        dirty = False
        for tid, tinfo in tasks.items():
            if tinfo['status'] in ['queued', 'encoding']:
                tinfo['status'] = 'error'
                tinfo['log'].append("Task aborted due to server restart.")
                dirty = True
            elif tinfo['status'] == 'done':
                try:
                    if ensure_fragmented(tid, tinfo):
                        dirty = True
                except Exception as e:
                    tinfo.setdefault('log', []).append(f"Fragmentation failed: {e}")
                    dirty = True
        if dirty:
            save_history()

def save_history():
    with open(HISTORY_FILE, "w") as f:
        json.dump(tasks, f, indent=2)

load_history()

def get_framerate(input_file):
    try:
        probe = subprocess.run(
            [FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=r_frame_rate", "-of", "default=noprint_wrappers=1:nokey=1", input_file],
            capture_output=True, text=True, check=True
        )
        fps_info = probe.stdout.strip()
        if "/" in fps_info:
            num, den = fps_info.split("/")
            return str(float(num) / float(den))
        return fps_info
    except Exception:
        return "30"

def process_encode_task(task_id, input_path, threads, cpu_used, limit):
    tasks[task_id]['status'] = 'encoding'
    tasks[task_id]['start_time'] = time.time()
    save_history()

    output_mp4 = os.path.join(app.config['OUTPUT_FOLDER'], f"{task_id}.mp4")
    output_fmp4 = os.path.join(app.config['OUTPUT_FOLDER'], f"{task_id}_frag.mp4")
    temp_obu = os.path.join(app.config['OUTPUT_FOLDER'], f"{task_id}.obu")
    
    tasks[task_id]['log'].append("Detecting framerate...")
    fps = get_framerate(input_path)
    tasks[task_id]['log'].append(f"Framerate detected: {fps}")

    tasks[task_id]['log'].append("Starting ffmpeg -> avmenc encoding pipeline...")
    
    ffmpeg_cmd = [
        FFMPEG, "-y", "-i", input_path, 
        "-pix_fmt", "yuv420p", "-strict", "-1",
        "-f", "yuv4mpegpipe", "-"
    ]
    
    avmenc_cmd = [
        AVMENC, "--passes=1", f"--threads={threads}", 
        f"--cpu-used={cpu_used}", "--obu", "-o", temp_obu, "-"
    ]
    if limit:
        avmenc_cmd.insert(1, f"--limit={limit}")
        
    try:
        with subprocess.Popen(ffmpeg_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as ffmpeg:
            with subprocess.Popen(avmenc_cmd, stdin=ffmpeg.stdout, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True) as avmenc:
                for line in avmenc.stdout:
                    line = line.strip()
                    if line:
                        tasks[task_id]['log'].append(line)
                
                avmenc.wait()
                if avmenc.returncode != 0:
                    tasks[task_id]['status'] = 'error'
                    tasks[task_id]['log'].append("Encoding failed.")
                    tasks[task_id]['end_time'] = time.time()
                    save_history()
                    return

        tasks[task_id]['log'].append("Encoding finished. Muxing to MP4...")
        
        mux_cmd = [
            AV2_MUX, temp_obu, "-o", output_mp4, "--fps", fps
        ]
        
        mux = subprocess.run(mux_cmd, capture_output=True, text=True)
        if mux.returncode != 0:
            tasks[task_id]['status'] = 'error'
            tasks[task_id]['log'].append(f"Muxing failed: {mux.stderr}")
            tasks[task_id]['end_time'] = time.time()
            save_history()
            return
            
        tasks[task_id]['log'].append("Muxing complete. Fragmenting MP4...")
        fragment_mp4(output_mp4, output_fmp4)
        tasks[task_id]['log'].append("Fragmentation complete.")
        
        if os.path.exists(temp_obu):
            os.remove(temp_obu)
            
        tasks[task_id]['status'] = 'done'
        tasks[task_id]['output_file'] = output_mp4
        tasks[task_id]['fragmented_file'] = output_fmp4
        
    except Exception as e:
        tasks[task_id]['status'] = 'error'
        tasks[task_id]['log'].append(f"Exception: {str(e)}")
    finally:
        tasks[task_id]['end_time'] = time.time()
        if 'start_time' in tasks[task_id]:
             tasks[task_id]['elapsed_seconds'] = tasks[task_id]['end_time'] - tasks[task_id]['start_time']
        save_history()

def worker():
    while True:
        task = task_queue.get()
        process_encode_task(**task)
        task_queue.task_done()

# Start background queue worker
worker_thread = threading.Thread(target=worker, daemon=True)
worker_thread.start()

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/upload", methods=["POST"])
def upload():
    if 'file' not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
        
    f = request.files['file']
    if f.filename == '':
        return jsonify({"error": "Empty filename"}), 400
        
    threads = request.form.get("threads", 32, type=int)
    cpu_used = request.form.get("cpu_used", 9, type=int)
    limit = request.form.get("limit", '', type=str)
    limit = int(limit) if limit.isdigit() else None

    task_id = str(uuid.uuid4())
    ext = os.path.splitext(f.filename)[1]
    input_path = os.path.join(app.config['UPLOAD_FOLDER'], f"{task_id}{ext}")
    
    f.save(input_path)
    
    tasks[task_id] = {
        'status': 'queued',
        'log': [],
        'filename': f.filename,
        'params': {
            'threads': threads,
            'cpu_used': cpu_used,
            'limit': limit
        },
        'queued_time': time.time()
    }
    save_history()
    
    task_queue.put({
        'task_id': task_id,
        'input_path': input_path,
        'threads': threads,
        'cpu_used': cpu_used,
        'limit': limit
    })
    
    return jsonify({"task_id": task_id})

@app.route("/status/<task_id>")
def status(task_id):
    if task_id not in tasks:
        return jsonify({"error": "Task not found"}), 404
        
    task = tasks[task_id]
    return jsonify({
        "status": task['status'],
        "log": task['log'][-10:] if task['log'] else []
    })

@app.route("/history")
def history():
    return jsonify(tasks)

def resolve_task_file(task_id, fragmented=False):
    if task_id not in tasks:
        return None, None, ("Task not found", 404)
    task = tasks[task_id]
    if task['status'] != 'done':
        return None, None, ("Not available", 400)
    if fragmented:
        try:
            if ensure_fragmented(task_id, task):
                save_history()
        except Exception as e:
            return None, None, (f"Fragmentation failed: {e}", 500)
        file_path = task.get('fragmented_file')
    else:
        file_path = task.get('output_file')
    if not file_path or not os.path.exists(file_path):
        return None, None, ("File not found", 404)
    return task, file_path, None

@app.route("/download/<task_id>")
def download(task_id):
    frag = request.args.get("fragmented") in ("1", "true", "yes")
    task, file_path, err = resolve_task_file(task_id, fragmented=frag)
    if err:
        return err
    prefix = "encoded_frag_" if frag else "encoded_"
    return send_file(file_path, as_attachment=True, download_name=f"{prefix}{task['filename']}.mp4")

@app.route("/download_fragmented/<task_id>")
def download_fragmented(task_id):
    task, file_path, err = resolve_task_file(task_id, fragmented=True)
    if err:
        return err
    return send_file(file_path, as_attachment=True, download_name=f"encoded_frag_{task['filename']}.mp4")

@app.route("/play/<task_id>")
def play(task_id):
    frag = request.args.get("fragmented") in ("1", "true", "yes")
    _, file_path, err = resolve_task_file(task_id, fragmented=frag)
    if err:
        return err
    return send_file(file_path, mimetype="video/mp4", as_attachment=False, conditional=True)

@app.route("/play_fragmented/<task_id>")
def play_fragmented(task_id):
    _, file_path, err = resolve_task_file(task_id, fragmented=True)
    if err:
        return err
    return send_file(file_path, mimetype="video/mp4", as_attachment=False, conditional=True)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
