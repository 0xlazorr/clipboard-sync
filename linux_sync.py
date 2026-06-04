#!/usr/bin/env python3
import os
import sys
import time
import json
import socket
import struct
import hashlib
import subprocess
import threading

# Configuration loader
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

def load_config():
    default_config = {
        "peer_ip": "192.168.1.100",  # Android IP (default placeholder)
        "port": 9999,
        "poll_interval": 1.0
    }
    if not os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "w") as f:
            json.dump(default_config, f, indent=4)
        return default_config
    try:
        with open(CONFIG_PATH, "r") as f:
            return json.load(f)
    except Exception as e:
        print(f"[-] Error reading config.json: {e}. Using defaults.")
        return default_config

# Helper to verify clipboard utilities
def check_is_wayland():
    return os.environ.get("WAYLAND_DISPLAY") is not None or "wayland" in os.environ.get("XDG_SESSION_TYPE", "").lower()

def check_dependencies():
    is_wayland = check_is_wayland()
    if is_wayland:
        if not subprocess.run(["which", "wl-copy"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
            print("[-] Wayland detected but 'wl-clipboard' is not installed. Please install it.")
            sys.exit(1)
    else:
        if not subprocess.run(["which", "xclip"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
            print("[-] X11 detected but 'xclip' is not installed. Please install it.")
            sys.exit(1)
    return is_wayland

# Thread-safe Clipboard State Manager
class ClipboardState:
    def __init__(self):
        self.last_hash = None
        self.lock = threading.Lock()

    def get_hash(self, data):
        if isinstance(data, str):
            return hashlib.sha256(data.encode('utf-8')).hexdigest()
        return hashlib.sha256(data).hexdigest()

    def update_received(self, data_hash):
        with self.lock:
            self.last_hash = data_hash

    def check_and_update_sent(self, data_hash):
        with self.lock:
            if self.last_hash == data_hash:
                return False
            self.last_hash = data_hash
            return True

state_manager = ClipboardState()

# Read/Write interfaces to OS Clipboard
def get_linux_clipboard(is_wayland):
    if is_wayland:
        try:
            res = subprocess.run(["wl-paste", "--list-types"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            types = res.stdout.splitlines()
        except Exception:
            return None, None
        
        if "image/png" in types:
            try:
                img_res = subprocess.run(["wl-paste", "-t", "image/png"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                if img_res.returncode == 0 and len(img_res.stdout) > 0:
                    return "image", img_res.stdout
            except Exception:
                pass
        
        # Fallback to text
        try:
            text_res = subprocess.run(["wl-paste", "-t", "text/plain"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if text_res.returncode != 0:
                text_res = subprocess.run(["wl-paste"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            return "text", text_res.stdout.decode('utf-8', errors='ignore')
        except Exception:
            return None, None
    else:
        # X11
        try:
            res = subprocess.run(["xclip", "-selection", "clipboard", "-o", "-t", "TARGETS"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            if res.returncode != 0:
                return None, None
            types = res.stdout.splitlines()
        except Exception:
            return None, None
            
        if "image/png" in types:
            try:
                img_res = subprocess.run(["xclip", "-selection", "clipboard", "-t", "image/png", "-o"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                if img_res.returncode == 0 and len(img_res.stdout) > 0:
                    return "image", img_res.stdout
            except Exception:
                pass
                
        # Fallback to text
        try:
            target = "UTF8_STRING" if "UTF8_STRING" in types else "STRING"
            text_res = subprocess.run(["xclip", "-selection", "clipboard", "-t", target, "-o"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            return "text", text_res.stdout.decode('utf-8', errors='ignore')
        except Exception:
            return None, None

def set_linux_clipboard(is_wayland, clip_type, data):
    if is_wayland:
        if clip_type == "text":
            proc = subprocess.Popen(["wl-copy", "-t", "text/plain"], stdin=subprocess.PIPE)
            proc.communicate(input=data.encode('utf-8'))
        elif clip_type == "image":
            proc = subprocess.Popen(["wl-copy", "-t", "image/png"], stdin=subprocess.PIPE)
            proc.communicate(input=data)
    else:
        if clip_type == "text":
            proc = subprocess.Popen(["xclip", "-selection", "clipboard", "-t", "UTF8_STRING", "-i"], stdin=subprocess.PIPE)
            proc.communicate(input=data.encode('utf-8'))
        elif clip_type == "image":
            proc = subprocess.Popen(["xclip", "-selection", "clipboard", "-t", "image/png", "-i"], stdin=subprocess.PIPE)
            proc.communicate(input=data)

# Socket Server Thread (Receiving updates from Android)
def run_server(host, port, is_wayland):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((host, port))
        sock.listen(5)
        print(f"[+] TCP Server listening on {host}:{port} for Android updates...")
    except Exception as e:
        print(f"[-] Server failed to bind: {e}")
        return

    while True:
        try:
            conn, addr = sock.accept()
            threading.Thread(target=handle_incoming_connection, args=(conn, addr, is_wayland), daemon=True).start()
        except Exception:
            break

def handle_incoming_connection(conn, addr, is_wayland):
    try:
        conn.settimeout(5.0)
        header = conn.recv(5)
        if not header or len(header) < 5:
            return
        packet_type, length = struct.unpack("!BI", header)
        payload = b""
        while len(payload) < length:
            chunk = conn.recv(min(length - len(payload), 4096))
            if not chunk:
                break
            payload += chunk
        
        if len(payload) == length:
            data_hash = state_manager.get_hash(payload)
            state_manager.update_received(data_hash)
            
            if packet_type == 0:
                text = payload.decode('utf-8', errors='ignore')
                print(f"[+] Received text from {addr[0]}: {text[:30]}...")
                set_linux_clipboard(is_wayland, "text", text)
            elif packet_type == 1:
                print(f"[+] Received image from {addr[0]} ({len(payload)} bytes)...")
                set_linux_clipboard(is_wayland, "image", payload)
        else:
            print(f"[-] Failed to receive entire payload from {addr[0]}")
    except Exception as e:
        print(f"[-] Error handling peer connection: {e}")
    finally:
        conn.close()

# Client Sender (Sending updates to Android)
def send_to_android(peer_ip, port, clip_type, data):
    packet_type = 0 if clip_type == "text" else 1
    payload = data.encode('utf-8') if clip_type == "text" else data
    length = len(payload)
    header = struct.pack("!BI", packet_type, length)
    
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(3.0)
            s.connect((peer_ip, port))
            s.sendall(header + payload)
            return True
    except Exception:
        # Silent failure when phone is offline/out of reach to avoid spamming terminal
        return False

# Monitor Loop
def run_monitor(peer_ip, port, poll_interval, is_wayland):
    # Initialize with current clipboard to avoid startup duplication
    init_type, init_data = get_linux_clipboard(is_wayland)
    if init_data:
        state_manager.last_hash = state_manager.get_hash(init_data)
        print(f"[+] Monitor initialized with existing clipboard selection ({init_type})")

    while True:
        try:
            clip_type, data = get_linux_clipboard(is_wayland)
            if data:
                data_hash = state_manager.get_hash(data)
                # If hash is new, send it
                if state_manager.check_and_update_sent(data_hash):
                    print(f"[*] Change detected ({clip_type}). Syncing to Android ({peer_ip})...")
                    success = send_to_android(peer_ip, port, clip_type, data)
                    if success:
                        print("[+] Sync complete.")
                    else:
                        print("[-] Sync failed (Android device unreachable or server down).")
        except Exception as e:
            print(f"[-] Error in monitor loop: {e}")
        time.sleep(poll_interval)

if __name__ == "__main__":
    is_wayland = check_dependencies()
    config = load_config()
    
    # Start receiver server in background thread
    server_thread = threading.Thread(
        target=run_server,
        args=("0.0.0.0", config["port"], is_wayland),
        daemon=True
    )
    server_thread.start()
    
    # Run sender loop in main thread
    run_monitor(
        peer_ip=config["peer_ip"],
        port=config["port"],
        poll_interval=config["poll_interval"],
        is_wayland=is_wayland
    )
