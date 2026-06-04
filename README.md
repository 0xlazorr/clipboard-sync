# Local Bi-Directional Clipboard Sync (Linux ↔ Android)

I built this lightweight, local-network clipboard sync utility to automatically sync text and images between my Linux host (X11 or Wayland) and my Android phone running Termux.

This project offers two implementations for the Linux host:
*   **Python Daemon ([linux_sync.py](linux_sync.py)):** Simple, requires no compilation, and runs out of the box. Ideal for quick setup.
*   **Rust Daemon ([src/main.rs](src/main.rs)):** High-performance, compiled native binary. Extremely memory-efficient (~2MB RAM) with zero runtime dependencies. Ideal for developers.

---

## Features
*   **Automatic Text Sync:** Bi-directional clipboard syncing (Linux to Android, Android to Linux).
*   **Linux-to-Android Image Sync:** Copies images on Linux and saves them directly to your Android device's `/sdcard/Pictures/Clipboard_Sync/` folder, spawning a notification to open them.
*   **Minimal Dependencies:** Standard library implementations with standard TCP sockets and SHA-256 data hashing to prevent feedback loops.
*   **Systemd Integration:** Out-of-the-box systemd user service templates for background execution.

---

## 1. Network Protocol Spec (Under the Hood)
The scripts communicate over TCP using a simple framed binary protocol:

```
+--------------------+------------------------+-------------------------------+
| Type (1 Byte, raw) | Length (4 Bytes, BE)   | Payload (Length Bytes)        |
+--------------------+------------------------+-------------------------------+
```

*   **Type Byte:**
    *   `0`: Text payload (UTF-8 encoded string).
    *   `1`: PNG image payload (raw binary bytes).
*   **Length Field:** 32-bit big-endian unsigned integer representing the size of the payload.
*   **Connection Model:** Peer-to-peer stateless model. The client opens a connection to the peer's TCP server, sends the framed packet, and terminates the socket immediately. This is self-healing, handles network drops, and saves battery by not maintaining active persistent connections.

---

## 2. Linux Setup

### Prerequisites
Install the required command-line clipboard manager for your windowing system:

```bash
# For Ubuntu/Debian/Linux Mint:
# If you are on Wayland:
sudo apt install wl-clipboard

# If you are on X11:
sudo apt install xclip
```

### Configuration
1. Locate your Android phone's local IP address (e.g. `192.168.1.100` via Wi-Fi Settings).
2. Edit [config.json](config.json):
   ```json
   {
       "peer_ip": "<ANDROID_PHONE_IP>",
       "port": 9999,
       "poll_interval": 1.0
   }
   ```

### Running the Daemon
You can run it manually in a terminal to inspect logs:
```bash
python3 ~/Documents/clipboard-sync/linux_sync.py
```

### Running as a Systemd Service (Background Daemon)
To configure it to run automatically on startup in the background:
1. Create the user systemd folder if it doesn't exist:
   ```bash
   mkdir -p ~/.config/systemd/user/
   ```
2. Copy the systemd service file:
   ```bash
   cp ~/Documents/clipboard-sync/clipboard-sync.service ~/.config/systemd/user/clipboard-sync.service
   ```
3. Reload systemd and enable the service:
   ```bash
   systemctl --user daemon-reload
   systemctl --user enable --now clipboard-sync.service
   ```
4. Verify it is running:
   ```bash
   systemctl --user status clipboard-sync.service
   ```

---

## 3. Android Setup (Termux)

To run this without writing a heavy Java app, I use Termux to access the Android API.

### Steps:
1.  Install **Termux** and the **Termux:API** companion app from F-Droid (do not use the outdated Google Play Store version).
2.  Open Termux and run the following command to update packages and install dependencies:
    ```bash
    pkg update && pkg install python termux-api
    ```
3.  Grant Termux storage permissions (necessary to write synchronized images to your phone storage):
    ```bash
    termux-setup-storage
    ```
4.  Copy [android_sync.py](android_sync.py) and [config.json](config.json) to your Android device (e.g., using `scp`, KDE Connect, or a direct file transfer tool).
5.  On the Android device, edit `config.json` to point `peer_ip` to your Linux machine's local IP address:
    ```json
    {
        "peer_ip": "<LINUX_PC_IP>",
        "port": 9999,
        "poll_interval": 1.5
    }
    ```
6.  Execute the script in Termux:
    ```bash
    python android_sync.py
    ```

### Crucial Android Battery Settings:
Modern Android aggressively kills background tasks. To keep the sync daemon running continuously:
1.  Go to **Settings** → **Apps** → **Termux**.
2.  Tap **Battery** (or Battery Saver) and set it to **Unrestricted** (Disable battery optimization).
3.  Pull down your notification shade when Termux is running, expand the Termux notification, and click **Acquire Wake Lock**. This prevents the CPU from entering deep sleep when the screen turns off.
4.  Ensure **Termux:API** is also excluded from Battery Saver.

---

## 4. Limitations & Edge Cases

*   **Android Clipboard Reads:** Due to Android 10+ sandbox limits, Termux can only read the system clipboard when the Termux window is focused, or when Termux is allowed to draw over other apps (under **Settings** → **Apps** → **Special app access** → **Display over other apps** → Allow **Termux**).
*   **Android Image Writes:** Termux-API does not support injecting raw binary images into the Android clipboard. Instead, images copied on Linux are saved to `/sdcard/Pictures/Clipboard_Sync/` and a notification is triggered. You can tap the notification to immediately view or share the image.

---

## 5. Rust Daemon Upgrade (Linux Host)

If you prefer a highly optimized, compiled native binary for the Linux host instead of running Python, you can compile and use the Rust implementation included in this directory.

### Why Use the Rust Daemon?
1.  **Memory Footprint:** The Rust binary runs using only **~2MB to 3MB of RAM** (compared to Python's ~30MB).
2.  **Zero Overhead:** Compiled native execution with zero startup/interpreter delay.
3.  **Static Execution:** No dependency on python packages or version discrepancies on the host.

### Build and Run instructions:
1.  Navigate to the directory and run Cargo to build in release mode:
    ```bash
    cargo build --release
    ```
2.  The optimized binary will be compiled to `./target/release/clipboard-sync`.
3.  To run it manually:
    ```bash
    ./target/release/clipboard-sync
    ```

### Updating Systemd to Use the Rust Daemon:
If you want systemd to run your compiled Rust binary instead of the Python script:
1. Edit your systemd service file `~/.config/systemd/user/clipboard-sync.service` (or [clipboard-sync.service](clipboard-sync.service) in this folder).
2. Change the `ExecStart` line to target your compiled Rust binary:
   ```ini
   ExecStart=%h/Documents/clipboard-sync/target/release/clipboard-sync
   ```
3. Reload and restart the service:
   ```bash
   systemctl --user daemon-reload
   systemctl --user restart clipboard-sync.service
   ```

---

## 6. Security Guidelines

This utility is designed for trusted local connections and does not implement built-in transport encryption or authorization checks. 

To keep your clipboard contents (which may contain passwords or sensitive keys) safe when uploading or using this setup:

1.  **Do Not Run on Public Wi-Fi:** Avoid exposing the daemon port (`9999`) to untrusted physical subnets. Anyone on the same network could potentially intercept clipboard packets or write data to your clipboard.
2.  **Expose Only via Secure VPNs (Tailscale):** The safest deployment is over an encrypted mesh network like **Tailscale**. Instead of listening on all interfaces (`0.0.0.0`), you can modify the socket listener in the code to bind only to your device's Tailscale interface IP.
3.  **Local SSH Tunneling Forwarding:** For remote access, bind the listeners to localhost (`127.0.0.1`) and map them using an SSH port forward:
    ```bash
    ssh -L 9999:127.0.0.1:9999 user@your-linux-host
    ```
4.  **DoS Mitigation:** To prevent Denial-of-Service attacks, the daemons (both Python and Rust versions) enforce a strict **25 MB payload size limit** to avoid arbitrary memory allocation exhaustion crashes.


