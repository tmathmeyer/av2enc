import os
import sys
import uuid
import threading
import subprocess
import shutil
from flask import Flask, request, jsonify, send_file, render_template

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['OUTPUT_FOLDER'] = 'outputs'

os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs(app.config['OUTPUT_FOLDER'], exist_ok=True)

# Paths to tools inside Docker
FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"
AVMENC = "/app/avm/build/avmenc"
AV2_MUX = "/app/av2-tools/build/apps/av2_mux/av2_mux"

tasks = {}

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

def encode_task(task_id, input_path, threads, cpu_used, limit):
    tasks[task_id]['status'] = 'encoding'
    output_mp4 = os.path.join(app.config['OUTPUT_FOLDER'], f"{task_id}.mp4")
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
                    return

        tasks[task_id]['log'].append("Encoding finished. Muxing to MP4...")
        
        mux_cmd = [
            AV2_MUX, temp_obu, "-o", output_mp4, "--fps", fps
        ]
        
        mux = subprocess.run(mux_cmd, capture_output=True, text=True)
        if mux.returncode != 0:
            tasks[task_id]['status'] = 'error'
            tasks[task_id]['log'].append(f"Muxing failed: {mux.stderr}")
            return
            
        tasks[task_id]['log'].append("Muxing complete.")
        
        if os.path.exists(temp_obu):
            os.remove(temp_obu)
            
        tasks[task_id]['status'] = 'done'
        tasks[task_id]['output_file'] = output_mp4
        
    except Exception as e:
        tasks[task_id]['status'] = 'error'
        tasks[task_id]['log'].append(f"Exception: {str(e)}")

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
    limit = request.form.get("limit", None, type=int)

    task_id = str(uuid.uuid4())
    ext = os.path.splitext(f.filename)[1]
    input_path = os.path.join(app.config['UPLOAD_FOLDER'], f"{task_id}{ext}")
    
    f.save(input_path)
    
    tasks[task_id] = {
        'status': 'queued',
        'log': [],
        'filename': f.filename
    }
    
    thread = threading.Thread(target=encode_task, args=(task_id, input_path, threads, cpu_used, limit))
    thread.daemon = True
    thread.start()
    
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

@app.route("/download/<task_id>")
def download(task_id):
    if task_id not in tasks:
        return "Task not found", 404
        
    task = tasks[task_id]
    if task['status'] != 'done':
        return "Not available", 400
        
    return send_file(task['output_file'], as_attachment=True, download_name=f"encoded_{task['filename']}.mp4")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
