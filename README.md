# Local Bi-Directional Clipboard Sync (Linux ↔ Android)

I built this lightweight, local-network clipboard sync utility in Python to automatically sync text and images between my Linux host (X11 or Wayland) and my Android phone running Termux.

---

## Features
*   **Automatic Text Sync:** Bi-directional clipboard syncing (Linux to Android, Android to Linux).
*   **Linux-to-Android Image Sync:** Copies images on Linux and saves them directly to your Android device's `/sdcard/Pictures/Clipboard_Sync/` folder, spawning a notification to open them.
*   **Zero External Dependencies:** Built entirely using Python standard libraries (`socket`, `struct`, `threading`) interfacing with OS-native binaries.
*   **Anti-Feedback loop logic:** Utilizes SHA-256 data hashing to ensure content received from a peer isn't echoed back, preventing infinite loops.
*   **Systemd Integration:** Out-of-the-box systemd user service template for hands-free background execution on Linux.

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
