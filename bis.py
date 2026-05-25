"""
BIS Monitor Raw EEG Acquisition (Binary Protocol, Section 5)
- Baud: 57600, 8N1, no flow control
- Little-endian byte order (Section 5)
- Sends SEND_RAW_EEG (msg ID 111), receives M_DATA_RAW (msg ID 50) at 8 packets/sec
"""

import struct
import time
import csv
import argparse
import serial
import serial.tools.list_ports
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
import numpy as np
from datetime import datetime

# ── Layer 1 directives ──────────────────────────────────────────────────────
START_OF_PACKET = 0xABBA   # transmitted LE as bytes 0xBA 0xAB
L1_DATA_PACKET  = 1
L1_ACK_PACKET   = 2
L1_NAK_PACKET   = 3

# ── Command message IDs (External → Monitor, Section 5.3) ───────────────────
CMD_SEND_RAW_EEG        = 111
CMD_STOP_RAW_EEG        = 112
CMD_SEND_PROCESSED_VARS = 115
CMD_SEND_REVISION_INFO  = 1004

# ── Response message IDs (Monitor → External, Section 5.4) ──────────────────
RSP_M_DATA_RAW          = 50    # raw EEG, 8 packets/sec
RSP_M_PROCESSED_VARS    = 52    # processed vars, 1/sec
RSP_SER_REVISION_INFO   = 1102
RSP_SER_ERROR_MSG       = 1101

# ── Routing IDs ─────────────────────────────────────────────────────────────
ROUTING_EEG  = 4
ROUTING_MISC = 6


class BISMonitor:
    def __init__(self, port: str, baudrate: int = 57600):
        self.port     = port
        self.baudrate = baudrate
        self.ser: serial.Serial | None = None
        self._l1_seq  = 0                  # Layer-1 packet sequence counter
        self._l3_seqs: dict[int, int] = {} # Layer-3 per-message-ID counters

    # ── Connection ───────────────────────────────────────────────────────────

    def connect(self) -> None:
        self.ser = serial.Serial(
            port=self.port,
            baudrate=self.baudrate,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=1.0,
        )
        print(f"Connected to {self.port} at {self.baudrate} baud")

    def disconnect(self) -> None:
        if self.ser and self.ser.is_open:
            try:
                self._send(ROUTING_EEG, CMD_STOP_RAW_EEG)
            except Exception:
                pass
            self.ser.close()
            print("Disconnected")

    # ── Packet builder ───────────────────────────────────────────────────────

    def _build_packet(self, routing_id: int, msg_id: int, payload: bytes = b"") -> bytes:
        """
        Construct a full binary protocol packet (Layers 1-3).

        Layer 3  = msg_id (4B) | l3_seq (2B) | length (2B) | payload
        Layer 2  = routing_id (4B) | Layer 3
        Layer 1  = START (2B) | l1_seq (2B) | opt_len (2B) | directive (2B)
                   | Layer 2  | checksum (2B)
        Checksum = sum of bytes of: l1_seq, opt_len, directive, optional_data
        """
        l3_seq = self._l3_seqs.get(msg_id, 0)
        self._l3_seqs[msg_id] = l3_seq + 1

        layer3 = struct.pack("<IHH", msg_id, l3_seq, len(payload)) + payload
        layer2 = struct.pack("<I", routing_id) + layer3

        l1_seq     = self._l1_seq
        self._l1_seq = (self._l1_seq + 1) & 0xFFFF
        opt_len    = len(layer2)
        directive  = L1_DATA_PACKET

        chk_input  = struct.pack("<HHH", l1_seq, opt_len, directive) + layer2
        checksum   = sum(chk_input) & 0xFFFF

        header = struct.pack("<HHHH", START_OF_PACKET, l1_seq, opt_len, directive)
        return header + layer2 + struct.pack("<H", checksum)

    def _send(self, routing_id: int, msg_id: int, payload: bytes = b"") -> None:
        assert self.ser, "Not connected"
        self.ser.write(self._build_packet(routing_id, msg_id, payload))

    # ── Packet reader ────────────────────────────────────────────────────────

    def _read_packet(self) -> dict | None:
        """
        Block until one valid packet arrives.
        Returns a dict with keys: msg_id, routing_id, data
        Returns None on timeout, checksum error, or short read.
        """
        assert self.ser

        # Scan for 0xBA 0xAB (0xABBA little-endian)
        while True:
            b = self.ser.read(1)
            if not b:
                return None
            if b[0] == 0xBA:
                b2 = self.ser.read(1)
                if b2 and b2[0] == 0xAB:
                    break

        hdr = self.ser.read(6)  # l1_seq(2) + opt_len(2) + directive(2)
        if len(hdr) < 6:
            return None
        l1_seq, opt_len, directive = struct.unpack("<HHH", hdr)

        opt_data = self.ser.read(opt_len)
        if len(opt_data) < opt_len:
            return None

        chk_bytes = self.ser.read(2)
        if len(chk_bytes) < 2:
            return None
        recv_checksum = struct.unpack("<H", chk_bytes)[0]

        # Verify checksum
        expected = sum(struct.pack("<HHH", l1_seq, opt_len, directive) + opt_data) & 0xFFFF
        if recv_checksum != expected:
            print(f"[WARN] Checksum mismatch (expected {expected:#06x}, got {recv_checksum:#06x})")
            return None

        if directive in (L1_ACK_PACKET, L1_NAK_PACKET):
            return {"type": "ack" if directive == L1_ACK_PACKET else "nak"}

        if len(opt_data) < 12:  # 4 (routing) + 4 (msg_id) + 2 (seq) + 2 (len)
            return None

        routing_id = struct.unpack("<I", opt_data[:4])[0]
        msg_id, l3_seq, length = struct.unpack("<IHH", opt_data[4:12])
        msg_data = opt_data[12 : 12 + length]

        return {"routing_id": routing_id, "msg_id": msg_id, "l3_seq": l3_seq, "data": msg_data}

    # ── EEG data parser ──────────────────────────────────────────────────────

    @staticmethod
    def _parse_m_data_raw(data: bytes) -> dict | None:
        """
        M_DATA_RAW payload (Section 5.4.1):
          2 bytes  – num_channels (unsigned short: 2 or 4)
          2 bytes  – sample_rate  (unsigned short: 128 or 256)
          remaining – EEG samples, 16-bit signed integers, interleaved by channel
        """
        if len(data) < 4:
            return None
        num_ch, rate = struct.unpack("<HH", data[:4])
        raw = data[4:]
        n   = len(raw) // 2
        samples = struct.unpack(f"<{n}h", raw[:n * 2])

        # De-interleave: [ch1, ch2, ch1, ch2, ...] → {ch1: [...], ch2: [...]}
        channels = {f"ch{i + 1}": [] for i in range(num_ch)}
        for idx, val in enumerate(samples):
            ch_key = f"ch{(idx % num_ch) + 1}"
            channels[ch_key].append(val)

        return {"num_channels": num_ch, "sample_rate": rate, "channels": channels}

    # ── High-level API ───────────────────────────────────────────────────────

    def collect_raw_eeg(
        self,
        duration_sec: float | None = None,
        sample_rate: int = 128,
        output_csv: str | None = None,
        verbose: bool = True,
    ) -> list[dict]:
        """
        Request and receive raw EEG for `duration_sec` seconds, or indefinitely
        if duration_sec is None. Press Ctrl+C to stop.

        Parameters
        ----------
        duration_sec : recording length in seconds, or None for indefinite
        sample_rate  : 128 sps (VISTA supports 128 only; A-2000 also supports 256)
        output_csv   : optional path to save results
        verbose      : print packet summaries to stdout

        Returns
        -------
        List of dicts: [{"timestamp": float, "num_channels": int,
                          "sample_rate": int, "channels": {ch1: [...], ...}}, ...]
        """
        self._send(ROUTING_EEG, CMD_SEND_RAW_EEG, struct.pack("<H", sample_rate))
        if duration_sec is None:
            print(f"Requesting raw EEG at {sample_rate} sps — recording indefinitely (press Ctrl+C to stop) …")
        else:
            print(f"Requesting raw EEG at {sample_rate} sps for {duration_sec}s …")

        records: list[dict] = []
        t0 = time.time()

        try:
            while duration_sec is None or time.time() - t0 < duration_sec:
                pkt = self._read_packet()
                if pkt is None:
                    continue
                if pkt.get("msg_id") == RSP_M_DATA_RAW:
                    eeg = self._parse_m_data_raw(pkt["data"])
                    if eeg is None:
                        continue
                    ts = time.time() - t0
                    record = {"timestamp": round(ts, 4), **eeg}
                    records.append(record)
                    if verbose:
                        preview = {k: v[:3] for k, v in eeg["channels"].items()}
                        print(f"  t={ts:6.2f}s  {eeg['num_channels']}ch @ {eeg['sample_rate']}sps  {preview}")
                elif pkt.get("msg_id") == RSP_SER_ERROR_MSG:
                    print(f"[ERROR from monitor] {pkt['data'].decode('ascii', errors='replace').strip()}")
        except KeyboardInterrupt:
            print("\nInterrupted by user.")
        finally:
            self._send(ROUTING_EEG, CMD_STOP_RAW_EEG)
            print(f"Stopped. Received {len(records)} packets.")

        if output_csv:
            _save_csv(records, output_csv)

        return records


# ── CSV export ────────────────────────────────────────────────────────────────

def _save_csv(records: list[dict], path: str) -> None:
    if not records:
        print("No data to save.")
        return
    ch_keys = list(records[0]["channels"].keys())
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        # Header row
        sample_cols = [f"{ch}_s{i}" for ch in ch_keys
                       for i in range(len(records[0]["channels"][ch]))]
        writer.writerow(["timestamp_s", "num_channels", "sample_rate"] + sample_cols)
        for rec in records:
            row = [rec["timestamp"], rec["num_channels"], rec["sample_rate"]]
            for ch in ch_keys:
                row.extend(rec["channels"][ch])
            writer.writerow(row)
    print(f"Saved {len(records)} records → {path}")


# ── Plot ─────────────────────────────────────────────────────────────────────

def plot_eeg(records: list[dict], save_path: str | None = None) -> None:
    """
    Plot raw EEG channels from collected records.

    Each channel gets its own subplot. Samples within a packet are placed at
    evenly-spaced intervals based on the packet timestamp and sample rate.
    """
    if not records:
        print("No data to plot.")
        return

    num_ch   = records[0]["num_channels"]
    rate     = records[0]["sample_rate"]
    ch_keys  = [f"ch{i + 1}" for i in range(num_ch)]

    # Build continuous time + amplitude arrays per channel
    times   = {k: [] for k in ch_keys}
    signals = {k: [] for k in ch_keys}

    for rec in records:
        t_pkt    = rec["timestamp"]
        n_samp   = len(rec["channels"][ch_keys[0]])
        dt       = 1.0 / rate
        # The packet timestamp marks the *end* of the packet window
        t_start  = t_pkt - (n_samp - 1) * dt
        for i in range(n_samp):
            t = t_start + i * dt
            for k in ch_keys:
                times[k].append(t)
                signals[k].append(rec["channels"][k][i])

    fig = plt.figure(figsize=(14, 3 * num_ch))
    fig.suptitle("BIS Monitor – Raw EEG", fontsize=13, fontweight="bold")
    gs  = gridspec.GridSpec(num_ch, 1, hspace=0.45)

    colors = plt.rcParams["axes.prop_cycle"].by_key()["color"]

    for idx, k in enumerate(ch_keys):
        ax = fig.add_subplot(gs[idx])
        t  = np.array(times[k])
        y  = np.array(signals[k])

        ax.plot(t, y, lw=0.6, color=colors[idx % len(colors)])
        ax.set_ylabel("Amplitude (ADC)", fontsize=9)
        ax.set_title(f"Channel {idx + 1}  ({rate} sps)", fontsize=10)
        ax.set_xlim(t[0], t[-1])
        ax.axhline(0, color="gray", lw=0.4, ls="--")
        ax.grid(True, axis="y", alpha=0.3)
        ax.tick_params(labelsize=8)

        if idx == num_ch - 1:
            ax.set_xlabel("Time (s)", fontsize=9)
        else:
            ax.set_xticklabels([])

    plt.tight_layout()
    if save_path:
        fig.savefig(save_path, dpi=150)
        print(f"Plot saved → {save_path}")
    plt.show()


# ── Port detection ────────────────────────────────────────────────────────────

def find_port() -> str:
    """
    Scan available serial ports and return the best candidate for a BIS monitor.
    If a single port is found, it is selected automatically.
    If multiple ports are found, the user is prompted to choose.
    Raises SystemExit if no ports are detected.
    """
    ports = serial.tools.list_ports.comports()

    if not ports:
        raise SystemExit("No serial ports found. Check the USB-Serial connection.")

    # Prefer ports whose description/hwid mentions common USB-Serial chips
    KEYWORDS = ("usbserial", "usb serial", "ftdi", "ch340", "cp210", "prolific", "bis")
    candidates = [
        p for p in ports
        if any(kw in (p.description + p.hwid).lower() for kw in KEYWORDS)
    ]
    pool = candidates if candidates else ports

    if len(pool) == 1:
        print(f"Puerto detectado automáticamente: {pool[0].device}  ({pool[0].description})")
        return pool[0].device

    print("Puertos seriales disponibles:")
    for i, p in enumerate(pool):
        print(f"  [{i}] {p.device}  —  {p.description}")
    while True:
        try:
            idx = int(input(f"Selecciona un puerto [0-{len(pool)-1}]: "))
            if 0 <= idx < len(pool):
                return pool[idx].device
        except (ValueError, KeyboardInterrupt):
            pass
        print("  Opción no válida, intenta de nuevo.")


# ── CLI entry point ───────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Collect raw EEG from Aspect BIS Monitor (binary protocol)")
    parser.add_argument("--port",     default=None,
                        help="Serial port (e.g. /dev/ttyUSB0 or COM3); auto-detected if omitted")
    parser.add_argument("--duration", type=float, default=None,
                        help="Recording duration in seconds (default: run until Ctrl+C)")
    parser.add_argument("--rate",     type=int,   default=128, choices=[128, 256],
                        help="Sample rate in sps (VISTA: 128 only; A-2000: 128 or 256)")
    parser.add_argument("--output",   default=None,
                        help="Output CSV file (auto-named if omitted)")
    parser.add_argument("--quiet",       action="store_true",
                        help="Suppress per-packet console output")
    parser.add_argument("--plot",        action="store_true",
                        help="Show EEG plot after collection")
    parser.add_argument("--plot-output", default=None, metavar="FILE",
                        help="Save plot to FILE (PNG/PDF) instead of just displaying")
    args = parser.parse_args()

    if args.output is None:
        args.output = f"bis_raw_eeg_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"

    port = args.port or find_port()
    monitor = BISMonitor(port=port)
    monitor.connect()
    try:
        records = monitor.collect_raw_eeg(
            duration_sec=args.duration,
            sample_rate=args.rate,
            output_csv=args.output,
            verbose=not args.quiet,
        )
    finally:
        monitor.disconnect()

    if args.plot or args.plot_output:
        plot_eeg(records, save_path=args.plot_output)


if __name__ == "__main__":
    main()
