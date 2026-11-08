# Secure Bi-Directional Clipboard Sync (Python Edition)

A high-performance, privacy-focused, zero-dependency local clipboard synchronization utility connecting Linux hosts (X11 / Wayland) and Android phones running Termux.

---

## Key Enhancements & Security Architecture

* **End-to-End Authenticated Encryption (AEAD):** All clipboard payloads are protected using HMAC-SHA256 authenticated framing and CTR-mode keystream payload encryption derived from a 256-bit pre-shared key (PBKDF2-HMAC-SHA256). Eavesdroppers on your local Wi-Fi cannot read or inject clipboard contents.
* **Dynamic LAN Auto-Discovery (UDP):** Automatically discovers peer IP addresses on your local Wi-Fi / hotspot using authenticated UDP beacons. You no longer need to manually look up or configure dynamic DHCP IPs.
* **Explicit Finite State Machines (FSM):** Daemon lifecycles and clipboard state transitions are governed by strict typed FSMs (`SyncEngineFSM` and `ClipboardItemState`), preventing race conditions, loops, and desynchronization.
* **Multi-Backend Resiliency (Zero External Dependencies on X11):** 
  - Direct native `libX11` ctypes backend: reads and writes the X11 clipboard without needing `xclip` or `xsel` installed!
  - `wl-clipboard` support for Wayland.
  - `xclip` support if installed.
* **Privacy by Design:** Zero plaintext clipboard leakage in terminal output or system logs (all logging is sanitized using truncated SHA-256 fingerprints). In-memory sensitive buffers are zeroized upon transmission.

---

## 1. Quick Start (Linux Host)

### Configuration
On initial execution, `linux_sync.py` automatically generates a secure 256-bit secret key in `config.json` if one is not present:

```bash
cd ~/repos/clipboard-sync
python3 linux_sync.py
```

`config.json` schema:
```json
{
    "secret_key": "<64_HEX_CHAR_SECRET_KEY>",
    "peer_ip": "192.168.1.100",
    "port": 9999,
    "discovery_port": 9998,
    "enable_discovery": true,
    "poll_interval": 1.0
}
```

*When `enable_discovery` is set to `true`, `peer_ip` is automatically updated via authenticated UDP beacons whenever the Android phone joins the network.*

### Running as a Background Systemd Service
1. Copy the unit file:
   ```bash
   mkdir -p ~/.config/systemd/user/
   cp clipboard-sync.service ~/.config/systemd/user/
   ```
2. Enable and start:
   ```bash
   systemctl --user daemon-reload
   systemctl --user enable --now clipboard-sync.service
   ```
3. Check status:
   ```bash
   systemctl --user status clipboard-sync.service
   ```

---

## 2. Android Setup (Termux Companion)

1. Install **Termux** and the **Termux:API** app from F-Droid.
2. In Termux, install Python and API tools:
   ```bash
   pkg update && pkg install python termux-api
   termux-setup-storage
   ```
3. Copy `protocol.py`, `android_sync.py`, and `config.json` to Termux.
4. **Crucial:** In Termux `config.json`, set `"secret_key"` to the exact same value generated on your Linux host.
5. Run the daemon:
   ```bash
   python android_sync.py
   ```
6. In Termux notification bar, click **Acquire Wake Lock** and allow unrestricted background battery usage in Android settings.

---

## 3. Protocol Specification

* **Header:** `b"CS02"` (4B magic) + `pkt_type` (1B) + `timestamp` (8B big-endian) + `payload_length` (4B) + `nonce` (16B random) + `HMAC-SHA256` (32B tag)
* **Replay Protection:** Packets with timestamps older or newer than 60 seconds are discarded immediately.
* **Integrity & Secrecy:** Only peers with the matching `secret_key` can generate valid HMAC tags and decrypt payloads.
