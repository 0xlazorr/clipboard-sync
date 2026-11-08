#!/usr/bin/env python3
"""
Secure Clipboard Sync Daemon (Linux)
Features:
- Explicit Finite State Machines (SyncEngineFSM, ClipboardItemState)
- Authenticated symmetric payload encryption (HMAC-SHA256 + CTR keystream)
- UDP LAN auto-discovery with authenticated beacons (resolves dynamic IP issues)
- Resilient multi-backend clipboard interface (Wayland, CLI X11, native libX11 ctypes fallback)
- Privacy-first sanitized logging and memory zeroization
"""

import ctypes
from ctypes import Structure, c_int, c_uint, c_ulong, c_void_p, c_char_p, POINTER, byref, c_long, c_ubyte
import json
import os
import secrets
import shutil
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from typing import Optional, Tuple

from protocol import (
    SyncEngineState,
    SyncEngineFSM,
    ClipboardItemState,
    derive_master_key,
    pack_packet,
    unpack_packet,
    sanitize_hash,
    PKT_TEXT,
    PKT_IMAGE,
    PKT_DISCOVERY,
    PKT_DISCOVERY_ACK,
    MAX_PAYLOAD_SIZE,
)

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")


def load_or_create_config() -> dict:
    default_config = {
        "secret_key": secrets.token_hex(32),
        "peer_ip": "192.168.1.100",
        "port": 9999,
        "discovery_port": 9998,
        "enable_discovery": True,
        "poll_interval": 1.0,
    }

    if not os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "w") as f:
            json.dump(default_config, f, indent=4)
        print(f"[+] Generated new configuration with pre-shared key at {CONFIG_PATH}")
        return default_config

    try:
        with open(CONFIG_PATH, "r") as f:
            config = json.load(f)
        changed = False
        if "secret_key" not in config or not config["secret_key"]:
            config["secret_key"] = secrets.token_hex(32)
            changed = True
        if "discovery_port" not in config:
            config["discovery_port"] = 9998
            changed = True
        if "enable_discovery" not in config:
            config["enable_discovery"] = True
            changed = True
        if changed:
            with open(CONFIG_PATH, "w") as f:
                json.dump(config, f, indent=4)
        return config
    except Exception as e:
        print(f"[-] Error reading {CONFIG_PATH}: {e}. Using fallback defaults.")
        return default_config


# =====================================================================
# Clipboard Backends: Wayland, CLI (xclip/xsel), Native ctypes (libX11)
# =====================================================================

class BaseClipboardBackend:
    def get_clipboard(self) -> Tuple[Optional[str], Optional[bytes]]:
        raise NotImplementedError

    def set_clipboard(self, clip_type: str, data: bytes) -> bool:
        raise NotImplementedError

    def close(self):
        pass


class WaylandBackend(BaseClipboardBackend):
    def get_clipboard(self) -> Tuple[Optional[str], Optional[bytes]]:
        try:
            res = subprocess.run(
                ["wl-paste", "--list-types"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=1.5,
            )
            types = res.stdout.splitlines()
        except Exception:
            return None, None

        if "image/png" in types:
            try:
                img_res = subprocess.run(
                    ["wl-paste", "-t", "image/png"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=2.0,
                )
                if img_res.returncode == 0 and len(img_res.stdout) > 0:
                    return "image", img_res.stdout
            except Exception:
                pass

        try:
            text_res = subprocess.run(
                ["wl-paste", "-t", "text/plain"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=1.5,
            )
            if text_res.returncode != 0:
                text_res = subprocess.run(
                    ["wl-paste"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=1.5,
                )
            if text_res.returncode == 0 and text_res.stdout:
                return "text", text_res.stdout
        except Exception:
            pass
        return None, None

    def set_clipboard(self, clip_type: str, data: bytes) -> bool:
        mime = "text/plain" if clip_type == "text" else "image/png"
        try:
            proc = subprocess.Popen(["wl-copy", "-t", mime], stdin=subprocess.PIPE)
            proc.communicate(input=data, timeout=2.0)
            return proc.returncode == 0
        except Exception:
            return False


class X11CliBackend(BaseClipboardBackend):
    def __init__(self, cli_tool: str = "xclip"):
        self.cli_tool = cli_tool

    def get_clipboard(self) -> Tuple[Optional[str], Optional[bytes]]:
        if self.cli_tool == "xclip":
            try:
                res = subprocess.run(
                    ["xclip", "-selection", "clipboard", "-o", "-t", "TARGETS"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=1.5,
                )
                if res.returncode != 0:
                    return None, None
                types = res.stdout.splitlines()
                if "image/png" in types:
                    img_res = subprocess.run(
                        ["xclip", "-selection", "clipboard", "-t", "image/png", "-o"],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        timeout=2.0,
                    )
                    if img_res.returncode == 0 and img_res.stdout:
                        return "image", img_res.stdout

                target = "UTF8_STRING" if "UTF8_STRING" in types else "STRING"
                text_res = subprocess.run(
                    ["xclip", "-selection", "clipboard", "-t", target, "-o"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=1.5,
                )
                if text_res.returncode == 0 and text_res.stdout:
                    return "text", text_res.stdout
            except Exception:
                pass
        return None, None

    def set_clipboard(self, clip_type: str, data: bytes) -> bool:
        if self.cli_tool == "xclip":
            mime = "UTF8_STRING" if clip_type == "text" else "image/png"
            try:
                proc = subprocess.Popen(
                    ["xclip", "-selection", "clipboard", "-t", mime, "-i"],
                    stdin=subprocess.PIPE,
                )
                proc.communicate(input=data, timeout=2.0)
                return proc.returncode == 0
            except Exception:
                return False
        return False


class X11NativeBackend(BaseClipboardBackend):
    """Zero-dependency X11 clipboard backend using libX11 ctypes bindings."""

    class XSelectionRequestEvent(Structure):
        _fields_ = [
            ("type", c_int),
            ("serial", c_ulong),
            ("send_event", c_int),
            ("display", c_void_p),
            ("owner", c_ulong),
            ("requestor", c_ulong),
            ("selection", c_ulong),
            ("target", c_ulong),
            ("property", c_ulong),
            ("time", c_ulong),
        ]

    class XSelectionEvent(Structure):
        _fields_ = [
            ("type", c_int),
            ("serial", c_ulong),
            ("send_event", c_int),
            ("display", c_void_p),
            ("requestor", c_ulong),
            ("selection", c_ulong),
            ("target", c_ulong),
            ("property", c_ulong),
            ("time", c_ulong),
        ]

    def __init__(self):
        self.lib = ctypes.cdll.LoadLibrary("libX11.so.6")
        self._setup_prototypes()
        self.lock = threading.Lock()
        self.current_data: Optional[bytes] = None
        self.stop_event = threading.Event()

        # Dedicated display for serving selections
        self.d_server = self.lib.XOpenDisplay(None)
        if not self.d_server:
            raise RuntimeError("Failed to open X11 display for server backend")
        root = self.lib.XDefaultRootWindow(self.d_server)
        self.w_server = self.lib.XCreateSimpleWindow(self.d_server, root, 0, 0, 1, 1, 0, 0, 0)
        self.clip_atom = self.lib.XInternAtom(self.d_server, b"CLIPBOARD", 0)
        self.utf8_atom = self.lib.XInternAtom(self.d_server, b"UTF8_STRING", 0)
        self.string_atom = self.lib.XInternAtom(self.d_server, b"STRING", 0)
        self.targets_atom = self.lib.XInternAtom(self.d_server, b"TARGETS", 0)

        self.server_thread = threading.Thread(target=self._server_event_loop, daemon=True)
        self.server_thread.start()

    def _setup_prototypes(self):
        self.lib.XOpenDisplay.restype = c_void_p
        self.lib.XCloseDisplay.argtypes = [c_void_p]
        self.lib.XDefaultRootWindow.argtypes = [c_void_p]
        self.lib.XDefaultRootWindow.restype = c_ulong
        self.lib.XInternAtom.argtypes = [c_void_p, c_char_p, c_int]
        self.lib.XInternAtom.restype = c_ulong
        self.lib.XCreateSimpleWindow.argtypes = [
            c_void_p, c_ulong, c_int, c_int, c_uint, c_uint, c_uint, c_ulong, c_ulong
        ]
        self.lib.XCreateSimpleWindow.restype = c_ulong
        self.lib.XDestroyWindow.argtypes = [c_void_p, c_ulong]
        self.lib.XSetSelectionOwner.argtypes = [c_void_p, c_ulong, c_ulong, c_ulong]
        self.lib.XSetSelectionOwner.restype = c_int
        self.lib.XGetSelectionOwner.argtypes = [c_void_p, c_ulong]
        self.lib.XGetSelectionOwner.restype = c_ulong
        self.lib.XConvertSelection.argtypes = [c_void_p, c_ulong, c_ulong, c_ulong, c_ulong, c_ulong]
        self.lib.XChangeProperty.argtypes = [
            c_void_p, c_ulong, c_ulong, c_ulong, c_int, c_int, c_char_p, c_int
        ]
        self.lib.XSendEvent.argtypes = [c_void_p, c_ulong, c_int, c_long, c_void_p]
        self.lib.XFlush.argtypes = [c_void_p]
        self.lib.XPending.argtypes = [c_void_p]
        self.lib.XPending.restype = c_int
        self.lib.XNextEvent.argtypes = [c_void_p, c_void_p]
        self.lib.XGetWindowProperty.argtypes = [
            c_void_p, c_ulong, c_ulong, c_long, c_long, c_int, c_ulong,
            POINTER(c_ulong), POINTER(c_int), POINTER(c_ulong), POINTER(c_ulong),
            POINTER(POINTER(c_ubyte))
        ]
        self.lib.XGetWindowProperty.restype = c_int
        self.lib.XFree.argtypes = [c_void_p]

    def _server_event_loop(self):
        event_buf = (c_ubyte * 192)()
        while not self.stop_event.is_set():
            with self.lock:
                while self.lib.XPending(self.d_server) > 0:
                    self.lib.XNextEvent(self.d_server, event_buf)
                    event_type = c_int.from_buffer(event_buf).value
                    if event_type == 30:  # SelectionRequest
                        req = self.XSelectionRequestEvent.from_buffer_copy(event_buf)
                        reply = self.XSelectionEvent()
                        reply.type = 31  # SelectionNotify
                        reply.serial = 0
                        reply.send_event = 1
                        reply.display = req.display
                        reply.requestor = req.requestor
                        reply.selection = req.selection
                        reply.target = req.target
                        reply.time = req.time

                        data = self.current_data
                        if data is not None and req.property != 0:
                            if req.target == self.targets_atom:
                                targets = (c_ulong * 3)(self.targets_atom, self.utf8_atom, self.string_atom)
                                self.lib.XChangeProperty(
                                    self.d_server, req.requestor, req.property,
                                    4, 32, 0, ctypes.cast(targets, c_char_p), 3
                                )
                                reply.property = req.property
                            elif req.target in (self.utf8_atom, self.string_atom):
                                self.lib.XChangeProperty(
                                    self.d_server, req.requestor, req.property,
                                    req.target, 8, 0, data, len(data)
                                )
                                reply.property = req.property
                            else:
                                reply.property = 0
                        else:
                            reply.property = 0

                        self.lib.XSendEvent(self.d_server, req.requestor, 0, 0, byref(reply))
                        self.lib.XFlush(self.d_server)
            time.sleep(0.02)

    def get_clipboard(self) -> Tuple[Optional[str], Optional[bytes]]:
        # Dedicated client display connection to prevent thread collision
        d = self.lib.XOpenDisplay(None)
        if not d:
            return None, None
        try:
            root = self.lib.XDefaultRootWindow(d)
            w = self.lib.XCreateSimpleWindow(d, root, 0, 0, 1, 1, 0, 0, 0)
            clip_atom = self.lib.XInternAtom(d, b"CLIPBOARD", 0)
            utf8_atom = self.lib.XInternAtom(d, b"UTF8_STRING", 0)
            prop_atom = self.lib.XInternAtom(d, b"XSEL_DATA", 0)

            self.lib.XConvertSelection(d, clip_atom, utf8_atom, prop_atom, w, 0)
            self.lib.XFlush(d)

            event_buf = (c_ubyte * 192)()
            start = time.time()
            found = False
            while time.time() - start < 0.8:
                if self.lib.XPending(d) > 0:
                    self.lib.XNextEvent(d, event_buf)
                    if c_int.from_buffer(event_buf).value == 31:  # SelectionNotify
                        found = True
                        break
                time.sleep(0.01)

            if found:
                actual_type = c_ulong()
                actual_format = c_int()
                nitems = c_ulong()
                bytes_after = c_ulong()
                prop = POINTER(c_ubyte)()
                status = self.lib.XGetWindowProperty(
                    d, w, prop_atom, 0, 1024 * 1024, 0, 0,
                    byref(actual_type), byref(actual_format),
                    byref(nitems), byref(bytes_after), byref(prop)
                )
                if status == 0 and prop:
                    val = ctypes.string_at(prop, nitems.value)
                    self.lib.XFree(prop)
                    self.lib.XDestroyWindow(d, w)
                    return "text", val

            self.lib.XDestroyWindow(d, w)
            return None, None
        finally:
            self.lib.XCloseDisplay(d)

    def set_clipboard(self, clip_type: str, data: bytes) -> bool:
        if clip_type != "text":
            return False
        with self.lock:
            self.current_data = data
            self.lib.XSetSelectionOwner(self.d_server, self.clip_atom, self.w_server, 0)
            self.lib.XFlush(self.d_server)
        return True

    def close(self):
        self.stop_event.set()
        if self.d_server:
            with self.lock:
                self.lib.XDestroyWindow(self.d_server, self.w_server)
                self.lib.XCloseDisplay(self.d_server)
                self.d_server = None


def select_clipboard_backend() -> BaseClipboardBackend:
    is_wayland = os.environ.get("WAYLAND_DISPLAY") is not None or "wayland" in os.environ.get("XDG_SESSION_TYPE", "").lower()

    if is_wayland and shutil.which("wl-copy") and shutil.which("wl-paste"):
        print("[+] Backend: Wayland CLI (wl-clipboard)")
        return WaylandBackend()

    if shutil.which("xclip"):
        print("[+] Backend: X11 CLI (xclip)")
        return X11CliBackend("xclip")

    # Native ctypes libX11 fallback
    try:
        backend = X11NativeBackend()
        print("[+] Backend: Native X11 ctypes (libX11.so.6) - zero external dependencies")
        return backend
    except Exception as e:
        print(f"[-] Native X11 fallback initialization failed: {e}")

    # Fallback to Wayland backend if in Wayland even without verification
    if is_wayland:
        print("[*] Fallback: Attempting Wayland CLI backend")
        return WaylandBackend()

    print("[*] Fallback: Attempting X11 CLI backend")
    return X11CliBackend("xclip")


# =====================================================================
# State Tracking and Synchronization Daemon
# =====================================================================

class SecureClipboardDaemon:
    def __init__(self, config: dict):
        self.config = config
        self.key = derive_master_key(config["secret_key"])
        self.fsm = SyncEngineFSM()
        self.backend = select_clipboard_backend()

        self.peer_ip = config.get("peer_ip", "192.168.1.100")
        self.port = int(config.get("port", 9999))
        self.discovery_port = int(config.get("discovery_port", 9998))
        self.enable_discovery = bool(config.get("enable_discovery", True))
        self.poll_interval = float(config.get("poll_interval", 1.0))

        self.last_hash: Optional[str] = None
        self.state_lock = threading.Lock()
        self.running = threading.Event()
        self.running.set()

    def update_known_peer(self, new_ip: str, source: str = "discovery"):
        with self.state_lock:
            if self.peer_ip != new_ip:
                print(f"[+] Dynamic peer discovered via {source}: {new_ip} (was {self.peer_ip})")
                self.peer_ip = new_ip
                if self.fsm.current_state in (SyncEngineState.DISCOVERING, SyncEngineState.ERROR):
                    self.fsm.transition_to(SyncEngineState.READY, f"Discovered peer {new_ip}")

    def run_discovery_listener(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        try:
            sock.bind(("0.0.0.0", self.discovery_port))
            sock.settimeout(2.0)
            print(f"[+] Secure UDP Discovery listener active on port {self.discovery_port}")
        except Exception as e:
            print(f"[-] Failed to bind discovery port {self.discovery_port}: {e}")
            return

        while self.running.is_set():
            try:
                data, addr = sock.recvfrom(4096)
                pkt_type, payload, err = unpack_packet(self.key, data)
                if err:
                    continue  # Silently ignore untrusted or forged UDP packets

                if pkt_type in (PKT_DISCOVERY, PKT_DISCOVERY_ACK):
                    peer_addr = addr[0]
                    self.update_known_peer(peer_addr, source="UDP beacon")

                    # Reply with ACK if this was an incoming discovery beacon
                    if pkt_type == PKT_DISCOVERY:
                        ack_payload = json.dumps({"role": "linux", "status": "ack"}).encode("utf-8")
                        ack_pkt = pack_packet(self.key, PKT_DISCOVERY_ACK, ack_payload)
                        sock.sendto(ack_pkt, (peer_addr, self.discovery_port))
            except socket.timeout:
                continue
            except Exception:
                pass
        sock.close()

    def run_discovery_beacon(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        beacon_data = json.dumps({"role": "linux", "port": self.port}).encode("utf-8")
        packet = pack_packet(self.key, PKT_DISCOVERY, beacon_data)

        while self.running.is_set():
            try:
                # Broadcast on standard LAN broadcast
                sock.sendto(packet, ("255.255.255.255", self.discovery_port))
                # Also ping configured peer IP as unicast fallback
                if self.peer_ip:
                    sock.sendto(packet, (self.peer_ip, self.discovery_port))
            except Exception:
                pass
            time.sleep(5.0 if self.fsm.current_state == SyncEngineState.DISCOVERING else 20.0)
        sock.close()

    def run_tcp_server(self):
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            server.bind(("0.0.0.0", self.port))
            server.listen(5)
            server.settimeout(2.0)
            print(f"[+] Encrypted TCP server listening on port {self.port}...")
        except Exception as e:
            print(f"[-] Server bind failed on port {self.port}: {e}")
            self.fsm.transition_to(SyncEngineState.ERROR, str(e))
            return

        while self.running.is_set():
            try:
                conn, addr = server.accept()
                threading.Thread(target=self._handle_client, args=(conn, addr), daemon=True).start()
            except socket.timeout:
                continue
            except Exception:
                break
        server.close()

    def _recv_all(self, conn: socket.socket, length: int) -> Optional[bytes]:
        buf = bytearray()
        while len(buf) < length:
            chunk = conn.recv(length - len(buf))
            if not chunk:
                return None
            buf.extend(chunk)
        return bytes(buf)

    def _handle_client(self, conn: socket.socket, addr):
        try:
            conn.settimeout(5.0)
            # Read 65-byte authenticated packet header
            header = self._recv_all(conn, 65)
            if not header or len(header) < 65:
                return

            length = struct.unpack("!I", header[13:17])[0]
            if length > MAX_PAYLOAD_SIZE:
                print(f"[-] Rejected oversized payload from {addr[0]} ({length} bytes)")
                return

            payload_raw = self._recv_all(conn, length)
            if payload_raw is None or len(payload_raw) != length:
                return

            full_packet = header + payload_raw
            pkt_type, decrypted, err = unpack_packet(self.key, full_packet)
            if err:
                print(f"[-] Authentication/decryption error from {addr[0]}: {err}")
                return

            digest = sanitize_hash(decrypted)
            with self.state_lock:
                self.last_hash = digest

            # Apply to local Linux clipboard
            if pkt_type == PKT_TEXT:
                text_content = decrypted.decode("utf-8", errors="replace")
                print(f"[+] Received text payload from {addr[0]} (length: {len(decrypted)}B, hash: {digest})")
                self.backend.set_clipboard("text", text_content.encode("utf-8"))
            elif pkt_type == PKT_IMAGE:
                print(f"[+] Received image payload from {addr[0]} (length: {len(decrypted)}B, hash: {digest})")
                self.backend.set_clipboard("image", decrypted)

            # Update peer IP automatically from verified incoming connection
            self.update_known_peer(addr[0], source="authenticated TCP sync")
        except Exception as e:
            print(f"[-] Error handling incoming sync from {addr[0]}: {e}")
        finally:
            conn.close()

    def send_to_peer(self, clip_type: str, data: bytes) -> bool:
        pkt_type = PKT_TEXT if clip_type == "text" else PKT_IMAGE
        packet = pack_packet(self.key, pkt_type, data)
        peer = self.peer_ip

        if not peer:
            return False

        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(3.0)
                s.connect((peer, self.port))
                s.sendall(packet)
                return True
        except Exception:
            return False

    def run_monitor(self):
        # Initial capture to avoid immediate loop-back reflection
        init_type, init_data = self.backend.get_clipboard()
        if init_data:
            self.last_hash = sanitize_hash(init_data)
            print(f"[+] Monitor initialized with existing clipboard selection (hash: {self.last_hash})")

        self.fsm.transition_to(
            SyncEngineState.DISCOVERING if self.enable_discovery else SyncEngineState.READY,
            "Daemon started"
        )

        while self.running.is_set():
            try:
                clip_type, data = self.backend.get_clipboard()
                if data:
                    current_hash = sanitize_hash(data)
                    should_send = False
                    with self.state_lock:
                        if self.last_hash != current_hash:
                            self.last_hash = current_hash
                            should_send = True

                    if should_send:
                        if self.fsm.current_state != SyncEngineState.TRANSFERRING:
                            try:
                                self.fsm.transition_to(SyncEngineState.TRANSFERRING, "Outbound sync")
                            except Exception:
                                pass

                        print(f"[*] Change detected ({clip_type}, hash: {current_hash}). Encrypting and syncing to {self.peer_ip}...")
                        success = self.send_to_peer(clip_type, data)
                        if success:
                            print(f"[+] Sync complete to {self.peer_ip}.")
                            try:
                                self.fsm.transition_to(SyncEngineState.READY, "Sync finished")
                            except Exception:
                                pass
                        else:
                            print(f"[-] Peer {self.peer_ip} unreachable (searching via discovery beacons).")
                            try:
                                self.fsm.transition_to(SyncEngineState.DISCOVERING, "Peer unreachable")
                            except Exception:
                                pass
            except Exception as e:
                print(f"[-] Error in clipboard monitor loop: {e}")
            time.sleep(self.poll_interval)

    def start(self):
        # Register graceful termination handlers
        def shutdown_handler(signum, frame):
            print("\n[*] Stopping Secure Clipboard Daemon gracefully...")
            self.running.clear()
            self.backend.close()
            try:
                self.fsm.transition_to(SyncEngineState.STOPPED, "Termination signal")
            except Exception:
                pass
            sys.exit(0)

        signal.signal(signal.SIGINT, shutdown_handler)
        signal.signal(signal.SIGTERM, shutdown_handler)

        # Start discovery threads
        if self.enable_discovery:
            threading.Thread(target=self.run_discovery_listener, daemon=True).start()
            threading.Thread(target=self.run_discovery_beacon, daemon=True).start()

        # Start server thread
        threading.Thread(target=self.run_tcp_server, daemon=True).start()

        # Run monitor in main thread
        self.run_monitor()


if __name__ == "__main__":
    cfg = load_or_create_config()
    daemon = SecureClipboardDaemon(cfg)
    daemon.start()
