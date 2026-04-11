#! /usr/bin/env python3
# -*- coding: utf-8 -*-

import indigo
import socket
import threading
import time
import logging


def _safe_int(val, default):
    try:
        return int(val)
    except Exception:
        return default


class AnthemConnection:
    def __init__(self, plugin, device):
        self.plugin = plugin
        self.device = device

        self.host = device.pluginProps.get("itachHost", "")
        self.port = _safe_int(device.pluginProps.get("itachPort", 4999), 4999)
        self.poll_seconds = _safe_int(device.pluginProps.get("pollSeconds", 30), 30)
        self.terminator = device.pluginProps.get("commandTerminator", "CR")  # CR/LF/CRLF/NONE

        # Capabilities (by parent device type) so we can gate video/HDMI queries cleanly
        self.caps = plugin._caps_for_parent(device)

        # Debounced refresh for video-setting queries (per current source)
        self._video_lock = threading.Lock()
        self._video_timer = None

        self._sock = None
        self._sock_lock = threading.Lock()
        self._stop = threading.Event()
        self._rx_buffer = b""

        self._last_poll = 0.0
        self._last_slow_poll = 0.0
        self.slow_poll_seconds = max(60, self.poll_seconds * 6)

        # Debounced refresh per zone (1/2/3)
        self._refresh_lock = threading.Lock()
        self._refresh_timers = {}  # zone -> Timer

    def start(self):
        t = threading.Thread(target=self.run, name=f"AnthemConn-{self.device.id}", daemon=True)
        t.start()

    def stop(self):
        self._stop.set()
        self._close_socket()
        with self._refresh_lock:
            for t in self._refresh_timers.values():
                try:
                    t.cancel()
                except Exception:
                    pass
            self._refresh_timers = {}

        with self._video_lock:
            if self._video_timer:
                try:
                    self._video_timer.cancel()
                except Exception:
                    pass
                self._video_timer = None

    def run(self):
        self.plugin.logger.info(f"[{self.device.name}] starting IP2SL connection loop to {self.host}:{self.port}")
        while not self._stop.is_set():
            if not self._sock:
                self._connect()

            if self._sock:
                try:
                    data = self._sock.recv(4096)
                    if data:
                        self._on_bytes(data)
                    else:
                        self.plugin.logger.warning(f"[{self.device.name}] socket closed by remote")
                        self._close_socket()
                        continue
                except socket.timeout:
                    pass
                except Exception as e:
                    self.plugin.logger.warning(f"[{self.device.name}] recv error: {e}")
                    self._close_socket()
                    continue

                now = time.time()
                if (now - self._last_poll) >= self.poll_seconds:
                    self._last_poll = now
                    self.poll_fast_status()

                if (now - self._last_slow_poll) >= self.slow_poll_seconds:
                    self._last_slow_poll = now
                    self.poll_slow_status()

            time.sleep(0.05)

    def _connect(self):
        if not self.host:
            self._set_conn_state_all("no host configured")
            time.sleep(2)
            return

        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(3.0)
            s.connect((self.host, self.port))
            s.settimeout(0.5)
            with self._sock_lock:
                self._sock = s

            self._set_conn_state_all("connected")
            self.plugin.logger.info(f"[{self.device.name}] connected to IP2SL {self.host}:{self.port}")

            # Initial refresh: quick live state first, then slower config/state blocks
            self._last_poll = time.time()
            self._last_slow_poll = time.time()
            self.poll_fast_status()
            self._schedule_poll(self.poll_main_basic, delay=0.35)
            self._schedule_poll(self.poll_slow_status, delay=1.2)

        except Exception as e:
            self._set_conn_state_all(f"connect failed: {e}")
            self.plugin.logger.warning(f"[{self.device.name}] connect failed: {e}")
            self._close_socket()
            time.sleep(2)

    def _close_socket(self):
        with self._sock_lock:
            if self._sock:
                try:
                    self._sock.close()
                except Exception:
                    pass
            self._sock = None
        self._set_conn_state_all("disconnected")

    def _set_conn_state_all(self, val):
        try:
            self.device.updateStateOnServer("connectionState", val)
        except Exception:
            pass

        for child in self.plugin._iter_child_zones(self.device.id):
            try:
                child.updateStateOnServer("connectionState", val)
            except Exception:
                pass

    def _format_cmd(self, cmd_ascii: str) -> bytes:
        term = b""
        if self.terminator == "CR":
            term = b"\r"
        elif self.terminator == "LF":
            term = b"\n"
        elif self.terminator == "CRLF":
            term = b"\r\n"
        return cmd_ascii.encode("ascii") + term

    def send_ascii(self, cmd_ascii: str, zone: int = 1):
        payload = self._format_cmd(cmd_ascii)

        with self._sock_lock:
            s = self._sock

        if not s:
            self.plugin.logger.warning(f"[{self.device.name}] send skipped (not connected): {cmd_ascii}")
            return

        try:
            s.sendall(payload)
            # Wire logging is controlled by the plugin's Logging preference (Normal/Debug).
            self.plugin._log_wire_tx(self.device.name, cmd_ascii)
        except Exception as e:
            self.plugin.logger.warning(f"[{self.device.name}] send error: {e}")
            self._close_socket()
            return

        # Schedule a compact query refresh shortly after commands (debounced)
        self.queue_zone_refresh(zone)

    def queue_zone_refresh(self, zone: int, delay: float = 0.25):
        """Queue a compact status query (P{zone}?) after a short delay to coalesce bursts (e.g., volume ramp)."""
        if zone not in (1, 2, 3):
            zone = 1

        with self._refresh_lock:
            t = self._refresh_timers.get(zone)
            if t:
                try:
                    t.cancel()
                except Exception:
                    pass
            t = threading.Timer(delay, self._refresh_zone_now, args=(zone,))
            t.daemon = True
            self._refresh_timers[zone] = t
            t.start()

    def _refresh_zone_now(self, zone: int):
        try:
            self.poll_zone(zone)
        except Exception:
            pass

    def poll_zone(self, zone: int):
        # Anthem compact zone status query is P{zone}? (e.g., P1?, P2?, P3?)
        self.send_raw_no_refresh(f"P{zone}?")

    def poll_status_all_zones(self):
        # Backward-compatible alias
        self.poll_fast_status()

    def _schedule_poll(self, fn, delay: float = 0.1):
        """Run a poll helper after a short delay without blocking the socket loop."""
        try:
            t = threading.Timer(delay, fn)
            t.daemon = True
            t.start()
            return t
        except Exception:
            return None

    def _send_batch(self, commands, spacing: float = 0.05):
        """Send a batch of polling commands with gentle pacing."""
        delay = 0.0
        for cmd in commands:
            if not cmd:
                continue
            self._schedule_poll(lambda c=cmd: self.send_raw_no_refresh(c), delay=delay)
            delay += max(0.0, spacing)

    def poll_fast_status(self):
        # Only the fast-changing live state. These are the things users actually notice.
        self._send_batch(("P1?", "P2?", "P3?", "P1Q?"), spacing=0.05)

    def poll_slow_status(self):
        # Less-frequently changing settings. Keep this out of the fast poll loop.
        self.poll_main_basic()
        self._schedule_poll(self.poll_main_audio_levels, delay=0.35)
        self._schedule_poll(self.poll_main_tone_balance, delay=0.75)
        self._schedule_poll(self.poll_main_processing, delay=1.25)
        if self.caps.get("has_hdmi_video", False):
            self._schedule_poll(self.poll_main_video, delay=1.8)


    # --- Main Zone (P1) Advanced Poll Groups ---
    def poll_main_basic(self):
        # Slow-moving main-zone metadata that is still useful on control pages.
        self._send_batch(("P1P?", "P1S?", "P4S?"), spacing=0.06)

    def poll_main_audio_levels(self):
        # Current channel trims (returns +0.0 if no signal)
        self._send_batch(("P1VF?", "P1VC?", "P1VR?", "P1VB?", "P1VS?", "P1VL?"), spacing=0.06)

    def poll_main_tone_balance(self):
        self._send_batch(("P1LM?", "P1LF?", "P1LR?", "P1LB?",
                          "P1BM?", "P1BC?", "P1BF?", "P1BR?", "P1BB?",
                          "P1TM?", "P1TC?", "P1TF?", "P1TR?", "P1TB?",
                          "P1TE?"), spacing=0.06)

    def poll_main_processing(self):
        # Decoder / flags / AC3 / dialog norm / DRC
        self._send_batch(("P1D?", "P1DF?", "P1A?", "P1AD?", "P1C?"), spacing=0.06)

        # Effects / THX / tuning params (raw yx responses)
        self._send_batch(("P1E?", "P1EF?", "P1EE?", "P1ES?", "P1ET?", "P1EU?", "P1EX?", "P1EY?", "P1ED?",
                          "P1EMP?", "P1EMC?", "P1EMD?", "P1EMG?",
                          "P1ER?", "P1EN?"), spacing=0.06)


    def poll_main_video(self):
        """Video/scaler polling temporarily disabled.

        The older source-specific "f...?" query block was generating malformed or unsupported
        commands on some processors and cluttering the log with Invalid Command responses.
        Keep the method as a no-op for now so existing call sites remain safe.
        """
        return

    def queue_main_video_refresh(self, delay: float = 0.6):
        if not self.caps.get("has_hdmi_video", False):
            return
        with self._video_lock:
            if self._video_timer:
                try:
                    self._video_timer.cancel()
                except Exception:
                    pass
            self._video_timer = threading.Timer(delay, self.poll_main_video)
            self._video_timer.daemon = True
            self._video_timer.start()

    def poll_main_all(self):
        self.poll_fast_status()
        self._schedule_poll(self.poll_slow_status, delay=0.35)

    def send_raw_no_refresh(self, cmd_ascii: str):
        """Send without scheduling a refresh (used by polling itself)."""
        payload = self._format_cmd(cmd_ascii)
        with self._sock_lock:
            s = self._sock
        if not s:
            return
        try:
            s.sendall(payload)
            self.plugin._log_wire_tx(self.device.name, cmd_ascii)
        except Exception as e:
            self.plugin.logger.warning(f"[{self.device.name}] send error: {e}")
            self._close_socket()

    def _on_bytes(self, data: bytes):
        self._rx_buffer += data

        while b"\n" in self._rx_buffer or b"\r" in self._rx_buffer:
            cr = self._rx_buffer.find(b"\r")
            lf = self._rx_buffer.find(b"\n")
            idxs = [i for i in (cr, lf) if i != -1]
            cut = min(idxs)

            line = self._rx_buffer[:cut]
            rest = self._rx_buffer[cut:]
            while rest.startswith(b"\r") or rest.startswith(b"\n"):
                rest = rest[1:]
            self._rx_buffer = rest

            line_str = line.decode("ascii", errors="ignore").strip()
            if line_str:
                self._handle_line(line_str)

    def _handle_line(self, line: str):
        self.plugin._log_wire_rx(self.device.name, line)

        # store lastRx on parent for debugging
        try:
            self.device.updateStateOnServer("lastRx", line)
        except Exception:
            pass

        # Determine zone from prefix P1 / P2 / P3
        zone = 1
        if line.startswith("P2"):
            zone = 2
        elif line.startswith("P3"):
            zone = 3

        target = self.device if zone == 1 else self.plugin._get_child_zone_device(self.device.id, zone)
        if not target:
            target = self.device

        try:
            target.updateStateOnServer("lastRx", line)
        except Exception:
            pass

        # --- Video/Scaler Responses (D2 / AVM50 family) ---
        if line.startswith("F") and self.caps.get("has_hdmi_video", False):
            # Responses are typically: F<prefix><source><value>
            # Example: Fa1y, Fc1yyy, FS1y, etc.
            pfx_map = {
                "Fa": ("videoScaleMode", int),
                "Fe": ("videoExtractSize", int),
                "Ff": ("videoExtractHPos", int),
                "Fg": ("videoExtractVPos", int),
                "Ft": ("videoThroughHSize", int),
                "Fu": ("videoThroughVSize", int),
                "Fv": ("videoThroughHPos", int),
                "Fw": ("videoThroughVPos", int),
                "FS": ("videoInputColorSpace", int),
                "FR": ("videoRgbMode", int),
                "Fc": ("videoContrast", int),
                "Fb": ("videoBrightness", int),
                "Fs": ("videoSaturation", int),
                "Fh": ("videoHue", int),
                "FF": ("videoFilmMode", int),
                "Fd": ("videoDetailLevel", int),
                "FD": ("videoDetailNoiseThreshold", int),
                "Fn": ("videoNoiseReduction", int),
                "Fm": ("videoMotionThreshold", int),
                "FB": ("videoChromaBugCorrection", int),
                "FC": ("videoSVideoChromaLevel", int),
                "Fl": ("videoSVideoLumaLevel", int),
                "FV": ("videoAdcGain", int),
                "FT": ("videoAdcOffset", int),
                "FP": ("videoSamplingPhase", int),
                "FW": ("videoCropWindowMode", int),
                "FA": ("videoCropEdgesEnabled", int),
                "FE": ("videoCropEdgePixels", int),
                "FO": ("videoCropEdgeMode", int),
                "Fo": ("videoInputWindowWidth", int),
                "Fp": ("videoInputWindowHeight", int),
                "Fq": ("videoInputWindowHPos", int),
                "Fr": ("videoInputWindowVPos", int),
                "FG": ("videoGammaMode", int),
                "Fi": ("videoFrameLockMode", int),
                "FX": ("videoGammaExponential", int),
            }

            # Try longer prefixes first (2 chars), then 1 char pairs already included.
            matched = False
            for pfx in sorted(pfx_map.keys(), key=len, reverse=True):
                if line.startswith(pfx) and len(line) > len(pfx) + 1:
                    src_code = line[len(pfx)]
                    raw_val = line[len(pfx) + 1:].strip()
                    self.plugin._safe_update_state(self.device, "videoSettingsSource", src_code)
                    state_id, cast = pfx_map[pfx]
                    try:
                        val = cast(raw_val)
                    except Exception:
                        val = raw_val
                    self.plugin._safe_update_state(self.device, state_id, val)
                    matched = True
                    break
            if matched:
                return

        # Power lines sometimes come as P1P0/P1P1 etc.
        if len(line) >= 4 and line[2] == "P":
            val = line[3:]
            self._update_power(target, val)
            return

        # --- Main Zone (P1) Advanced Responses ---
        # Numeric responses (signed floats), e.g. P1VM-35.0, P1VF+0.0, P1LM-1.0
        try:
            numeric_map = {
                "P1VM": "volumeDb",
                "P1VF": "trimFrontDb",
                "P1VC": "trimCenterDb",
                "P1VR": "trimSurroundDb",
                "P1VB": "trimBackDb",
                "P1VS": "trimSubDb",
                "P1VL": "trimLfeDb",
                "P1LM": "balanceMaster",
                "P1LF": "balanceFront",
                "P1LR": "balanceSurround",
                "P1LB": "balanceBack",
                "P1BM": "bassMaster",
                "P1BC": "bassCenter",
                "P1BF": "bassFront",
                "P1BR": "bassSurround",
                "P1BB": "bassBack",
                "P1TM": "trebleMaster",
                "P1TC": "trebleCenter",
                "P1TF": "trebleFront",
                "P1TR": "trebleSurround",
                "P1TB": "trebleBack",
            }

            for prefix, state_id in numeric_map.items():
                if line.startswith(prefix) and len(line) > len(prefix):
                    # only main zone has these states
                    if zone == 1:
                        val_str = line[len(prefix):].strip()
                        try:
                            val_f = float(val_str)
                            self.plugin._safe_update_state(target, state_id, val_f)
                        except Exception:
                            self.plugin._safe_update_state(target, state_id, val_str)
                    return
        except Exception:
            pass

        # Tone enable: P1TEx
        if line.startswith("P1TE") and zone == 1 and len(line) > 4:
            v = line[4:].strip()
            if v in ("0", "1"):
                self.plugin._safe_update_state(target, "toneEnabled", True if v == "1" else False)
            else:
                self.plugin._safe_update_state(target, "toneEnabled", v)
            return

        # Processing text: P1Q<text...>
        if line.startswith("P1Q") and zone == 1:
            proc_text = line[3:].strip()
            self.plugin._safe_update_state(target, "processingText", proc_text)
            self.plugin._safe_update_state(target, "currentProcessingMode", proc_text)
            return

        # Record zone source: P4Sx
        if line.startswith("P4S") and len(line) >= 4:
            code = line[3:].strip()
            # record states exist on main processor only
            parent = self.device
            self.plugin._safe_update_state(parent, "recordSourceCode", code)
            self.plugin._safe_update_state(parent, "recordSourceName", self.plugin.source_code_to_name(code))
            return

        # Decoder / flags / AC3 / dialog norm / DRC raw
        if zone == 1:
            if line.startswith("P1DF") and len(line) > 4:
                self.plugin._safe_update_state(target, "decoderFlaggedRaw", line[4:].strip())
                return
            if line.startswith("P1AD") and len(line) > 4:
                self.plugin._safe_update_state(target, "ac3DialogNormRaw", line[4:].strip())
                return
            if line.startswith("P1D") and not line.startswith("P1DF") and len(line) > 3:
                self.plugin._safe_update_state(target, "decoderStatusRaw", line[3:].strip())
                return
            if line.startswith("P1A") and not line.startswith("P1AD") and len(line) > 3:
                self.plugin._safe_update_state(target, "ac3StatusRaw", line[3:].strip())
                return
            if line.startswith("P1C") and len(line) > 3:
                self.plugin._safe_update_state(target, "drc", line[3:].strip())
                return

            # Effects/THX/tuning yx responses
            yx_map = {
                "P1E": "fxStereo",
                "P1EF": "fxDd20Flagged",
                "P1EE": "fxDdExFlagged",
                "P1ES": "fxDtsEsMatrixFlagged",
                "P1ET": "thxStereo",
                "P1EU": "thxDd20Flagged",
                "P1EX": "fxDd51",
                "P1EY": "fxSixOh",
                "P1ED": "fxDts51",
                "P1ER": "thxReEqWhenOn",
                "P1EN": "thxReEqWhenOff",
                "P1EMP": "pl2Panorama",
                "P1EMC": "pl2CenterWidth",
                "P1EMD": "pl2Dimension",
                "P1EMG": "neo6CenterGain",
            }
            # Handle the longer prefixes first to avoid P1E catching P1EF etc.
            for prefix in sorted([k for k in yx_map.keys() if yx_map[k]], key=len, reverse=True):
                if line.startswith(prefix) and len(line) > len(prefix):
                    self.plugin._safe_update_state(target, yx_map[prefix], line[len(prefix):].strip())
                    return
                # Compact status line examples (from Anthem docs): P1SxVsyy.yMn...
        # We'll parse source after 'S', volume after 'V', mute after 'M'
        # Accept both "P1S..." and "P1X..zV..." variants by searching for markers.
        try:
            src = self._extract_after_marker(line, "S", stop_markers=("V", "M", "D", "U", "E"))
            vol = self._extract_after_marker(line, "V", stop_markers=("M", "D", "U", "E"))
            mute = self._extract_after_marker(line, "M", stop_markers=("D", "U", "E"))
            dec = self._extract_after_marker(line, "D", stop_markers=("U", "E"))
            eff = self._extract_after_marker(line, "E", stop_markers=("U",))
        except Exception:
            src = vol = mute = dec = eff = None

        if src is not None and src != "":
            old_src = None
            try:
                old_src = target.states.get("sourceCode", None)
            except Exception:
                pass
            target.updateStateOnServer("sourceCode", src)
            target.updateStateOnServer("sourceName", self.plugin.source_code_to_name(src))
            # If main-zone source changed on a video-capable model, refresh video/scaler settings
            if zone == 1 and self.caps.get("has_hdmi_video", False) and old_src != src:
                self.queue_main_video_refresh()

        if vol is not None and vol != "":
            # volume can be like -35.0 or -35
            try:
                target.updateStateOnServer("volumeDb", float(vol))
            except Exception:
                # leave as-is if odd
                pass

        if mute is not None and mute != "":
            # M0/M1
            if mute in ("0", "1"):
                target.updateStateOnServer("mute", True if mute == "1" else False)

        if zone == 1:
            if dec is not None and dec != "":
                self.plugin._safe_update_state(target, "decoderStatusCompact", dec)
            if eff is not None and eff != "":
                self.plugin._safe_update_state(target, "stereoEffectCompact", eff)

    @staticmethod
    def _extract_after_marker(line: str, marker: str, stop_markers=()):
        idx = line.find(marker)
        if idx == -1:
            return None
        start = idx + 1
        end = len(line)
        for sm in stop_markers:
            j = line.find(sm, start)
            if j != -1:
                end = min(end, j)
        return line[start:end]

    @staticmethod
    def _update_power(dev, val: str):
        if val == "1":
            dev.updateStateOnServer("power", True)
            dev.updateStateOnServer("onOffState", True)
        elif val == "0":
            dev.updateStateOnServer("power", False)
            dev.updateStateOnServer("onOffState", False)


class Plugin(indigo.PluginBase):
    def __init__(self, pluginId, pluginDisplayName, pluginVersion, pluginPrefs):
        super().__init__(pluginId, pluginDisplayName, pluginVersion, pluginPrefs)
        self.logger.info("Plugin runtime version: %s, display name: %s", pluginVersion, pluginDisplayName)
        self._conns = {}

        # Parent processor device types (child zones are managed under these)
        # NOTE: We keep the original "anthemProcessor" id as the D2/AVM50 (HDMI/video-capable) family
        # so existing installs don't break when adding the D1-specific device type.
        self._parent_type_ids = {"anthemProcessor", "anthemProcessorD1"}

        # Capability flags by parent device type id (future-proofing for HDMI/video actions)
        self._caps_by_type = {
            "anthemProcessor": {"has_hdmi_video": True},      # D2 / AVM50 family
            "anthemProcessorD1": {"has_hdmi_video": False},   # Statement D1 family
        }

    def _is_parent_processor(self, device) -> bool:
        return device.deviceTypeId in self._parent_type_ids

    def _caps_for_parent(self, parent_device) -> dict:
        return dict(self._caps_by_type.get(parent_device.deviceTypeId, {"has_hdmi_video": False}))

    def startup(self):
        self._apply_log_level(self.pluginPrefs.get("logLevel", "info"))
        self.logger.info("Anthem RS232 Plugin starting")

    def closedPrefsConfigUi(self, valuesDict, userCancelled):
        """Apply logging preference changes immediately when the user clicks Save."""
        if userCancelled:
            return
        # Save preferences
        try:
            self.pluginPrefs.update(valuesDict)
        except Exception:
            pass
        self._apply_log_level(valuesDict.get("logLevel", "info"))

    def _apply_log_level(self, lvl_value):
        """Set both logger and handler levels so DEBUG traffic shows in Indigo's Event Log."""
        lvl = (lvl_value or "info").lower()
        target = logging.DEBUG if lvl == "debug" else logging.INFO

        # Plugin logger
        self.logger.setLevel(target)

        # Attached handlers
        for h in getattr(self.logger, "handlers", []) or []:
            try:
                h.setLevel(target)
            except Exception:
                pass

        # Root logger handlers (Indigo may route messages through root depending on host)
        root = logging.getLogger()
        for h in getattr(root, "handlers", []) or []:
            try:
                h.setLevel(target)
            except Exception:
                pass

        self.logger.info("Logging level set to %s", "Debug" if target == logging.DEBUG else "Normal")

    # ----- Wire (TX/RX) logging -----
    # We want Normal logging to be quiet in the Event Log, while Debug shows full RS-232 traffic.
    def _log_wire_tx(self, dev_name: str, cmd_ascii: str):
        if self.logger.isEnabledFor(logging.DEBUG):
            self.logger.debug(f"[{dev_name}] TX: {cmd_ascii}")

    def _log_wire_rx(self, dev_name: str, line: str):
        if self.logger.isEnabledFor(logging.DEBUG):
            self.logger.debug(f"[{dev_name}] RX: {line}")

    def shutdown(self):
        for devId, conn in list(self._conns.items()):
            conn.stop()
        self._conns = {}

    def deviceStartComm(self, device):
        # Start comm only for parent processor devices
        if not self._is_parent_processor(device):
            return

        device.stateListOrDisplayStateIdChanged()

        # Create/maintain children if enabled
        if device.pluginProps.get("enableChildZones", True):
            self._ensure_child_zone(device, 2)
            self._ensure_child_zone(device, 3)

        conn = AnthemConnection(self, device)
        self._conns[device.id] = conn
        conn.start()

    def deviceStopComm(self, device):
        conn = self._conns.pop(device.id, None)
        if conn:
            conn.stop()

    # ----- Child device management -----
    def _ensure_child_zone(self, parent_dev, zone: int):
        name = f"{parent_dev.name} - Zone {zone}"
        # find existing
        for dev in indigo.devices.iter("self"):
            if dev.deviceTypeId == "anthemZone":
                if dev.pluginProps.get("parentId", "") == str(parent_dev.id) and dev.pluginProps.get("zoneNumber", "") == str(zone):
                    return dev

        props = indigo.Dict()
        props["parentId"] = str(parent_dev.id)
        props["zoneNumber"] = str(zone)
        # Child belongs to plugin, device type anthemZone
        new_dev = indigo.device.create(protocol=indigo.kProtocol.Plugin,
                                       address=f"{parent_dev.id}:{zone}",
                                       name=name,
                                       description="Anthem Zone (auto)",
                                       pluginId=self.pluginId,
                                       deviceTypeId="anthemZone",
                                       props=props)
        return new_dev

    def _get_child_zone_device(self, parent_id: int, zone: int):
        for dev in indigo.devices.iter("self"):
            if dev.deviceTypeId == "anthemZone":
                if dev.pluginProps.get("parentId", "") == str(parent_id) and dev.pluginProps.get("zoneNumber", "") == str(zone):
                    return dev
        return None

    def _iter_child_zones(self, parent_id: int):
        for dev in indigo.devices.iter("self"):
            if dev.deviceTypeId == "anthemZone" and dev.pluginProps.get("parentId", "") == str(parent_id):
                yield dev

    # ----- Indigo Standard device actions -----
    def _request_status_for_device(self, device):
        parent, zone = self._resolve_parent_and_zone(device)
        conn = self._conns.get(parent.id)
        if not conn:
            self.logger.warning(f"[{device.name}] status request skipped (not connected)")
            return

        if zone == 1 and device.deviceTypeId != "anthemZone":
            conn.poll_main_all()
            self.logger.info(f"[{device.name}] status request sent (processor + zones)")
        else:
            conn.poll_zone(zone)
            self.logger.info(f"[{device.name}] status request sent (zone {zone})")

    def actionControlDevice(self, action, device):
        # Allow standard On/Off/Toggle/RequestStatus for both parent and zone devices.
        act = getattr(action, "deviceAction", None)
        if act == indigo.kDeviceAction.TurnOn:
            self.power_on(None, device)
        elif act == indigo.kDeviceAction.TurnOff:
            self.power_off(None, device)
        elif act == indigo.kDeviceAction.Toggle:
            is_on = bool(device.states.get("power", False))
            if is_on:
                self.power_off(None, device)
            else:
                self.power_on(None, device)
        elif act == getattr(indigo.kDeviceAction, "RequestStatus", None):
            self._request_status_for_device(device)
        else:
            # Some Indigo device UIs can route the built-in status button differently depending on class.
            # Fall back to a name check so the button still works across device variants.
            try:
                if str(act).lower().endswith("requeststatus"):
                    self._request_status_for_device(device)
            except Exception:
                pass

    def actionControlUniversal(self, action, device):
        # Belt-and-suspenders support for the built-in "Send Status Request" button.
        try:
            mode = getattr(action, "deviceAction", None) or getattr(action, "actionMode", None)
            if mode == getattr(indigo.kUniversalAction, "RequestStatus", None) or str(mode).lower().endswith("requeststatus"):
                self._request_status_for_device(device)
                return
        except Exception:
            pass

    # ----- Plugin actions -----
    def power_on(self, pluginAction, device):
        self._send_for_device(device, "P{z}P1")

    def power_off(self, pluginAction, device):
        self._send_for_device(device, "P{z}P0")

    def mute_on(self, pluginAction, device):
        self._send_for_device(device, "P{z}M1")

    def mute_off(self, pluginAction, device):
        self._send_for_device(device, "P{z}M0")

    def volume_set(self, pluginAction, device):
        v = pluginAction.props.get("volumeDb", "-35.0") if pluginAction else "-35.0"
        self._send_for_device(device, "P{z}VM" + str(v))

    # ----- Explicit Zone 2 / Zone 3 helpers (for Action Groups / Control Pages) -----

    def _fmt_db_compact(self, v: float, max_decimals: int = 2) -> str:
        """Format a dB float similar to Anthem responses: strip trailing zeros and plus sign."""
        s = f"{v:.{max_decimals}f}"
        if '.' in s:
            s = s.rstrip('0').rstrip('.')
        if s.startswith('+'):
            s = s[1:]
        return s

    def _send_for_explicit_zone(self, device, target_zone: int, cmd: str):
        """Send a command to an explicit zone regardless of which Indigo device invoked the action."""
        parent, _ = self._resolve_parent_and_zone(device)
        conn = self._conns.get(parent.id)
        if not conn:
            self.logger.warning(f"[{device.name}] no connection object available")
            return
        conn.send_ascii(cmd, zone=target_zone)


    def _volume_step_zone(self, device, target_zone: int, direction: int, step_db_raw: str):
        parent, _ = self._resolve_parent_and_zone(device)
        conn = self._conns.get(parent.id)
        if not conn:
            self.logger.warning(f"[{device.name}] no connection object available")
            return

        # Get current volume from the zone child device (preferred for Z2/Z3).
        zdev = self._get_child_zone_device(parent.id, target_zone)
        cur = None
        try:
            if zdev is not None:
                cur = zdev.states.get("volumeDb", None)
        except Exception:
            cur = None

        # Z1 volume lives on the parent device (no child for zone 1).
        if cur is None and target_zone == 1:
            try:
                cur = parent.states.get("volumeDb", None)
            except Exception:
                cur = None

        if cur is None:
            # Kick a refresh for that zone and ask the user to try again.
            conn.send_raw_no_refresh(f"P{target_zone}?")
            self.logger.error(f"[{parent.name}] Z{target_zone} Volume Up/Down: current volume unknown (no state yet). Sent refresh; try again.")
            return

        try:
            step = float(str(step_db_raw).strip())
        except Exception:
            self.logger.error(f"[{parent.name}] Z{target_zone} Volume Up/Down: invalid step {step_db_raw!r}")
            return

        new_v = float(cur) + (direction * step)
        cmd = f"P{target_zone}VM{self._fmt_db_compact(new_v)}"
        conn.send_ascii(cmd, zone=target_zone)
        # refresh compact status for that zone
        conn.send_raw_no_refresh(f"P{target_zone}?")

    def volume_step_up(self, pluginAction, device):
        raw = pluginAction.props.get("stepDb", "1.0") if pluginAction else "1.0"
        self._volume_step_zone(device, 1, +1, raw)

    def volume_step_down(self, pluginAction, device):
        raw = pluginAction.props.get("stepDb", "1.0") if pluginAction else "1.0"
        self._volume_step_zone(device, 1, -1, raw)

    # Z2
    def power_on_z2(self, pluginAction, device):
        self._send_for_explicit_zone(device, 2, "P2P1")

    def power_off_z2(self, pluginAction, device):
        self._send_for_explicit_zone(device, 2, "P2P0")

    def mute_on_z2(self, pluginAction, device):
        self._send_for_explicit_zone(device, 2, "P2M1")

    def mute_off_z2(self, pluginAction, device):
        self._send_for_explicit_zone(device, 2, "P2M0")

    def volume_set_z2(self, pluginAction, device):
        raw = pluginAction.props.get("volumeDb", "-35.0") if pluginAction else "-35.0"
        try:
            v = float(str(raw).strip())
        except Exception:
            self.logger.error(f"[Anthem] Z2 Set Volume: invalid dB value {raw!r}")
            return

        # Match Anthem style: compact numeric formatting (typically 0.5 dB steps shown as one decimal)
        # We allow up to 2 decimals but strip trailing zeros.
        val = self._fmt_db_compact(v, max_decimals=2)
        # Zone 2 volume set uses P2V (no 'M')
        self._send_for_explicit_zone(device, 2, f"P2V{val}")

    def volume_step_up_z2(self, pluginAction, device):
        raw = pluginAction.props.get("stepDb", "") if pluginAction else ""
        step = str(raw).strip()
        # One-step up: P2VU (no value). Optional amount: P2VUx (e.g. 2) / P2VU2.5
        if not step:
            self._send_for_explicit_zone(device, 2, "P2VU")
            return
        try:
            v = float(step)
        except Exception:
            self.logger.error(f"[Anthem] Z2 Volume Up: invalid step {raw!r}")
            return
        self._send_for_explicit_zone(device, 2, f"P2VU{self._fmt_db_compact(v, max_decimals=2)}")


    def volume_step_down_z2(self, pluginAction, device):
        raw = pluginAction.props.get("stepDb", "") if pluginAction else ""
        step = str(raw).strip()
        # One-step down: P2VD (no value). Optional amount: P2VDx (e.g. 2) / P2VD2.5
        if not step:
            self._send_for_explicit_zone(device, 2, "P2VD")
            return
        try:
            v = float(step)
        except Exception:
            self.logger.error(f"[Anthem] Z2 Volume Down: invalid step {raw!r}")
            return
        self._send_for_explicit_zone(device, 2, f"P2VD{self._fmt_db_compact(v, max_decimals=2)}")


    def source_set_z2(self, pluginAction, device):
        src = pluginAction.props.get("sourceCode", "5") if pluginAction else "5"
        self._send_for_explicit_zone(device, 2, "P2S" + str(src))

    # Z3
    def power_on_z3(self, pluginAction, device):
        self._send_for_explicit_zone(device, 3, "P3P1")

    def power_off_z3(self, pluginAction, device):
        self._send_for_explicit_zone(device, 3, "P3P0")

    def mute_on_z3(self, pluginAction, device):
        self._send_for_explicit_zone(device, 3, "P3M1")

    def mute_off_z3(self, pluginAction, device):
        self._send_for_explicit_zone(device, 3, "P3M0")

    def volume_set_z3(self, pluginAction, device):
        raw = pluginAction.props.get("volumeDb", "-35.0") if pluginAction else "-35.0"
        try:
            v = float(str(raw).strip())
        except Exception:
            self.logger.error(f"[Anthem] Z3 Set Volume: invalid dB value {raw!r}")
            return

        val = self._fmt_db_compact(v, max_decimals=2)
        # Zone 3 volume set uses P3V (no 'M')
        self._send_for_explicit_zone(device, 3, f"P3V{val}")

    def volume_step_up_z3(self, pluginAction, device):
        raw = pluginAction.props.get("stepDb", "") if pluginAction else ""
        step = str(raw).strip()
        if not step:
            self._send_for_explicit_zone(device, 3, "P3VU")
            return
        try:
            v = float(step)
        except Exception:
            self.logger.error(f"[Anthem] Z3 Volume Up: invalid step {raw!r}")
            return
        self._send_for_explicit_zone(device, 3, f"P3VU{self._fmt_db_compact(v, max_decimals=2)}")


    def volume_step_down_z3(self, pluginAction, device):
        raw = pluginAction.props.get("stepDb", "") if pluginAction else ""
        step = str(raw).strip()
        if not step:
            self._send_for_explicit_zone(device, 3, "P3VD")
            return
        try:
            v = float(step)
        except Exception:
            self.logger.error(f"[Anthem] Z3 Volume Down: invalid step {raw!r}")
            return
        self._send_for_explicit_zone(device, 3, f"P3VD{self._fmt_db_compact(v, max_decimals=2)}")


    def source_set_z3(self, pluginAction, device):
        src = pluginAction.props.get("sourceCode", "5") if pluginAction else "5"
        self._send_for_explicit_zone(device, 3, "P3S" + str(src))

    # ----- Main Zone (P1) Channel Trim SET actions -----
    def _coerce_db(self, raw, *, min_db: float, max_db: float, step: float = 0.5):
        """Parse/validate a dB value.

        Accepts strings like -1.5, +2.0, 0, etc.
        Enforces range and step (default 0.5 dB).
        Returns a float.
        """
        try:
            v = float(str(raw).strip())
        except Exception:
            raise ValueError(f"Invalid dB value: {raw!r}")

        if v < min_db - 1e-9 or v > max_db + 1e-9:
            raise ValueError(f"Value {v:.1f} dB out of range ({min_db:.1f} to {max_db:.1f})")

        # step check: (v - min) must land on step grid; use rounding tolerance
        steps = round((v - min_db) / step)
        snapped = min_db + steps * step
        if abs(snapped - v) > 1e-6:
            raise ValueError(f"Value {v:.2f} dB must be in {step:.1f} dB steps")

        # normalize to nearest step
        return snapped

    def _send_main_trim(self, device, cmd_prefix: str, raw_db, *, min_db: float, max_db: float):
        parent, zone = self._resolve_parent_and_zone(device)
        if zone != 1:
            raise ValueError("This action applies to Main Zone (Zone 1) only")

        conn = self._conns.get(parent.id)
        if not conn:
            return

        v = self._coerce_db(raw_db, min_db=min_db, max_db=max_db, step=0.5)
        cmd = f"{cmd_prefix}{v:+.1f}"
        conn.send_ascii(cmd, zone=1)
        # refresh the trim value shortly after
        conn.send_raw_no_refresh(cmd_prefix + "?")

    def set_main_trim_front(self, pluginAction, device):
        raw = pluginAction.props.get("db", "0.0") if pluginAction else "0.0"
        try:
            self._send_main_trim(device, "P1VF", raw, min_db=-10.0, max_db=+10.0)
        except Exception as e:
            self.logger.error(f"Set Main Front Trim failed: {e}")

    def set_main_trim_center(self, pluginAction, device):
        raw = pluginAction.props.get("db", "0.0") if pluginAction else "0.0"
        try:
            self._send_main_trim(device, "P1VC", raw, min_db=-10.0, max_db=+10.0)
        except Exception as e:
            self.logger.error(f"Set Main Center Trim failed: {e}")

    def set_main_trim_surround(self, pluginAction, device):
        raw = pluginAction.props.get("db", "0.0") if pluginAction else "0.0"
        try:
            self._send_main_trim(device, "P1VR", raw, min_db=-10.0, max_db=+10.0)
        except Exception as e:
            self.logger.error(f"Set Main Surround Trim failed: {e}")

    def set_main_trim_back(self, pluginAction, device):
        raw = pluginAction.props.get("db", "0.0") if pluginAction else "0.0"
        try:
            self._send_main_trim(device, "P1VB", raw, min_db=-10.0, max_db=+10.0)
        except Exception as e:
            self.logger.error(f"Set Main Back Trim failed: {e}")

    def set_main_trim_sub(self, pluginAction, device):
        raw = pluginAction.props.get("db", "0.0") if pluginAction else "0.0"
        try:
            self._send_main_trim(device, "P1VS", raw, min_db=-30.0, max_db=+20.0)
        except Exception as e:
            self.logger.error(f"Set Main Sub Trim failed: {e}")

    def set_main_trim_lfe(self, pluginAction, device):
        raw = pluginAction.props.get("db", "0.0") if pluginAction else "0.0"
        try:
            self._send_main_trim(device, "P1VL", raw, min_db=-10.0, max_db=+0.0)
        except Exception as e:
            self.logger.error(f"Set Main LFE Trim failed: {e}")



    # ----- Main Zone (P1) Channel Trim STEP actions -----
    def _is_no_signal_trims(self, parent_dev) -> bool:
        """Best-effort detection of 'no signal' condition for channel trim queries.

        Anthem returns +0.0 when there is no signal. Unfortunately, 0.0 can also be a valid value.
        We treat it as 'no signal' only if *all* channel trims report ~0.0.
        """
        keys = ("trimFrontDb", "trimCenterDb", "trimSurroundDb", "trimBackDb", "trimSubDb", "trimLfeDb")
        vals = []
        for k in keys:
            try:
                v = parent_dev.states.get(k, None)
            except Exception:
                v = None
            if v is None:
                return False  # don't block if we can't tell
            try:
                vals.append(float(v))
            except Exception:
                return False
        return all(abs(v) < 0.001 for v in vals)

    def _coerce_step_db(self, raw_step):
        try:
            step = float(str(raw_step).strip())
        except Exception:
            raise ValueError(f"Invalid step size: {raw_step!r}")
        if step <= 0:
            raise ValueError("Step size must be > 0")
        # must be multiple of 0.5
        steps = round(step / 0.5)
        snapped = steps * 0.5
        if abs(snapped - step) > 1e-6:
            raise ValueError("Step size must be in 0.5 dB increments")
        return snapped

    def _step_main_trim(self, device, *, state_id: str, cmd_prefix: str, direction: int,
                        step_db_raw, min_db: float, max_db: float):
        parent, zone = self._resolve_parent_and_zone(device)
        if zone != 1:
            raise ValueError("This action applies to Main Zone (Zone 1) only")

        conn = self._conns.get(parent.id)
        if not conn:
            return

        # Refuse to step if it *looks* like there is no signal (all trims report 0.0)
        if self._is_no_signal_trims(parent):
            raise ValueError("No signal detected (all trim queries returned 0.0) — step aborted")

        cur_raw = parent.states.get(state_id, None)
        if cur_raw is None:
            raise ValueError(f"Current value not available for state '{state_id}'")
        try:
            cur = float(cur_raw)
        except Exception:
            raise ValueError(f"Current value for '{state_id}' is not numeric: {cur_raw!r}")

        step = self._coerce_step_db(step_db_raw)
        new_val = cur + (step * (1 if direction >= 0 else -1))

        # Clamp + snap to 0.5 dB grid in-range
        new_val = self._coerce_db(new_val, min_db=min_db, max_db=max_db, step=0.5)

        cmd = f"{cmd_prefix}{new_val:+.1f}"
        conn.send_ascii(cmd, zone=1)
        conn.send_raw_no_refresh(cmd_prefix + "?")

    def step_main_trim_front_up(self, pluginAction, device):
        step = pluginAction.props.get("stepDb", "0.5") if pluginAction else "0.5"
        try:
            self._step_main_trim(device, state_id="trimFrontDb", cmd_prefix="P1VF", direction=+1,
                                step_db_raw=step, min_db=-10.0, max_db=+10.0)
        except Exception as e:
            self.logger.error(f"Step Main Front Trim Up failed: {e}")

    def step_main_trim_front_down(self, pluginAction, device):
        step = pluginAction.props.get("stepDb", "0.5") if pluginAction else "0.5"
        try:
            self._step_main_trim(device, state_id="trimFrontDb", cmd_prefix="P1VF", direction=-1,
                                step_db_raw=step, min_db=-10.0, max_db=+10.0)
        except Exception as e:
            self.logger.error(f"Step Main Front Trim Down failed: {e}")

    def step_main_trim_center_up(self, pluginAction, device):
        step = pluginAction.props.get("stepDb", "0.5") if pluginAction else "0.5"
        try:
            self._step_main_trim(device, state_id="trimCenterDb", cmd_prefix="P1VC", direction=+1,
                                step_db_raw=step, min_db=-10.0, max_db=+10.0)
        except Exception as e:
            self.logger.error(f"Step Main Center Trim Up failed: {e}")

    def step_main_trim_center_down(self, pluginAction, device):
        step = pluginAction.props.get("stepDb", "0.5") if pluginAction else "0.5"
        try:
            self._step_main_trim(device, state_id="trimCenterDb", cmd_prefix="P1VC", direction=-1,
                                step_db_raw=step, min_db=-10.0, max_db=+10.0)
        except Exception as e:
            self.logger.error(f"Step Main Center Trim Down failed: {e}")

    def step_main_trim_surround_up(self, pluginAction, device):
        step = pluginAction.props.get("stepDb", "0.5") if pluginAction else "0.5"
        try:
            self._step_main_trim(device, state_id="trimSurroundDb", cmd_prefix="P1VR", direction=+1,
                                step_db_raw=step, min_db=-10.0, max_db=+10.0)
        except Exception as e:
            self.logger.error(f"Step Main Surround Trim Up failed: {e}")

    def step_main_trim_surround_down(self, pluginAction, device):
        step = pluginAction.props.get("stepDb", "0.5") if pluginAction else "0.5"
        try:
            self._step_main_trim(device, state_id="trimSurroundDb", cmd_prefix="P1VR", direction=-1,
                                step_db_raw=step, min_db=-10.0, max_db=+10.0)
        except Exception as e:
            self.logger.error(f"Step Main Surround Trim Down failed: {e}")

    def step_main_trim_back_up(self, pluginAction, device):
        step = pluginAction.props.get("stepDb", "0.5") if pluginAction else "0.5"
        try:
            self._step_main_trim(device, state_id="trimBackDb", cmd_prefix="P1VB", direction=+1,
                                step_db_raw=step, min_db=-10.0, max_db=+10.0)
        except Exception as e:
            self.logger.error(f"Step Main Back Trim Up failed: {e}")

    def step_main_trim_back_down(self, pluginAction, device):
        step = pluginAction.props.get("stepDb", "0.5") if pluginAction else "0.5"
        try:
            self._step_main_trim(device, state_id="trimBackDb", cmd_prefix="P1VB", direction=-1,
                                step_db_raw=step, min_db=-10.0, max_db=+10.0)
        except Exception as e:
            self.logger.error(f"Step Main Back Trim Down failed: {e}")

    def step_main_trim_sub_up(self, pluginAction, device):
        step = pluginAction.props.get("stepDb", "0.5") if pluginAction else "0.5"
        try:
            self._step_main_trim(device, state_id="trimSubDb", cmd_prefix="P1VS", direction=+1,
                                step_db_raw=step, min_db=-30.0, max_db=+20.0)
        except Exception as e:
            self.logger.error(f"Step Main Sub Trim Up failed: {e}")

    def step_main_trim_sub_down(self, pluginAction, device):
        step = pluginAction.props.get("stepDb", "0.5") if pluginAction else "0.5"
        try:
            self._step_main_trim(device, state_id="trimSubDb", cmd_prefix="P1VS", direction=-1,
                                step_db_raw=step, min_db=-30.0, max_db=+20.0)
        except Exception as e:
            self.logger.error(f"Step Main Sub Trim Down failed: {e}")

    def step_main_trim_lfe_up(self, pluginAction, device):
        step = pluginAction.props.get("stepDb", "0.5") if pluginAction else "0.5"
        try:
            self._step_main_trim(device, state_id="trimLfeDb", cmd_prefix="P1VL", direction=+1,
                                step_db_raw=step, min_db=-10.0, max_db=+0.0)
        except Exception as e:
            self.logger.error(f"Step Main LFE Trim Up failed: {e}")

    def step_main_trim_lfe_down(self, pluginAction, device):
        step = pluginAction.props.get("stepDb", "0.5") if pluginAction else "0.5"
        try:
            self._step_main_trim(device, state_id="trimLfeDb", cmd_prefix="P1VL", direction=-1,
                                step_db_raw=step, min_db=-10.0, max_db=+0.0)
        except Exception as e:
            self.logger.error(f"Step Main LFE Trim Down failed: {e}")


    def _z1_send_source_mode(self, device, prefix: str, source_code: str, mode_code: str):
        parent, _ = self._resolve_parent_and_zone(device)
        conn = self._conns.get(parent.id)
        if not conn:
            return
        conn.send_ascii(f"{prefix}{source_code}{mode_code}", zone=1)
        conn.send_raw_no_refresh("P1Q?")

    def _z1_send_source_value(self, device, prefix: str, source_code: str, value_code: str):
        parent, _ = self._resolve_parent_and_zone(device)
        conn = self._conns.get(parent.id)
        if not conn:
            return
        conn.send_ascii(f"{prefix}{source_code}{value_code}", zone=1)
        conn.send_raw_no_refresh("P1Q?")

    def z1_stereo_input_effect(self, pluginAction, device):
        self._z1_send_source_mode(device, "P1E", pluginAction.props.get("sourceCode", "9"), pluginAction.props.get("modeCode", "1"))

    def z1_dd20_flagged_effect(self, pluginAction, device):
        self._z1_send_source_mode(device, "P1EF", pluginAction.props.get("sourceCode", "9"), pluginAction.props.get("modeCode", "1"))

    def z1_dd_ex_effect(self, pluginAction, device):
        self._z1_send_source_mode(device, "P1EE", pluginAction.props.get("sourceCode", "9"), pluginAction.props.get("modeCode", "1"))

    def z1_dts_es_matrix_effect(self, pluginAction, device):
        self._z1_send_source_mode(device, "P1ES", pluginAction.props.get("sourceCode", "9"), pluginAction.props.get("modeCode", "1"))

    def z1_dd20_thx_mode(self, pluginAction, device):
        self._z1_send_source_mode(device, "P1EU", pluginAction.props.get("sourceCode", "9"), pluginAction.props.get("modeCode", "0"))

    def z1_stereo_thx_mode(self, pluginAction, device):
        self._z1_send_source_mode(device, "P1ET", pluginAction.props.get("sourceCode", "9"), pluginAction.props.get("modeCode", "0"))

    def z1_dd51_effect(self, pluginAction, device):
        self._z1_send_source_mode(device, "P1EX", pluginAction.props.get("sourceCode", "9"), pluginAction.props.get("modeCode", "0"))

    def z1_input60_effect(self, pluginAction, device):
        self._z1_send_source_mode(device, "P1EY", pluginAction.props.get("sourceCode", "2"), pluginAction.props.get("modeCode", "0"))

    def z1_dts51_effect(self, pluginAction, device):
        self._z1_send_source_mode(device, "P1ED", pluginAction.props.get("sourceCode", "9"), pluginAction.props.get("modeCode", "0"))

    def z1_thx_button_step(self, pluginAction, device):
        self._send_for_explicit_zone(device, 1, "P1EB" + str(pluginAction.props.get("dirCode", "1")))

    def z1_mode_button_step(self, pluginAction, device):
        self._send_for_explicit_zone(device, 1, "P1EC" + str(pluginAction.props.get("dirCode", "1")))

    def z1_music_panorama(self, pluginAction, device):
        self._z1_send_source_value(device, "P1EMP", pluginAction.props.get("sourceCode", "9"), pluginAction.props.get("valueCode", "0"))

    def z1_music_center_width(self, pluginAction, device):
        self._z1_send_source_value(device, "P1EMC", pluginAction.props.get("sourceCode", "9"), pluginAction.props.get("valueCode", "3"))

    def z1_music_dimension(self, pluginAction, device):
        self._z1_send_source_value(device, "P1EMD", pluginAction.props.get("sourceCode", "9"), pluginAction.props.get("valueCode", "3"))

    def z1_neo6_center_gain(self, pluginAction, device):
        self._z1_send_source_value(device, "P1EMG", pluginAction.props.get("sourceCode", "9"), pluginAction.props.get("valueCode", "2"))

    def z1_thx_reeq_on(self, pluginAction, device):
        self._z1_send_source_value(device, "P1ER", pluginAction.props.get("sourceCode", "9"), pluginAction.props.get("valueCode", "0"))

    def z1_thx_reeq_off(self, pluginAction, device):
        self._z1_send_source_value(device, "P1EN", pluginAction.props.get("sourceCode", "9"), pluginAction.props.get("valueCode", "0"))

    def z1_dynamic_range_compression(self, pluginAction, device):
        self._send_for_explicit_zone(device, 1, "P1C" + str(pluginAction.props.get("valueCode", "0")))

    def source_set(self, pluginAction, device):
        src = pluginAction.props.get("sourceCode", "5") if pluginAction else "5"
        self._send_for_device(device, "P{z}S" + str(src))

    def refresh_status(self, pluginAction, device):
        parent, zone = self._resolve_parent_and_zone(device)
        conn = self._conns.get(parent.id)
        if conn:
            conn.poll_zone(zone)


    # ----- Main Zone Advanced Refresh Actions -----
    def refresh_main_basic(self, pluginAction, device):
        parent, _ = self._resolve_parent_and_zone(device)
        conn = self._conns.get(parent.id)
        if conn:
            conn.poll_main_basic()

    def refresh_main_audio_levels(self, pluginAction, device):
        parent, _ = self._resolve_parent_and_zone(device)
        conn = self._conns.get(parent.id)
        if conn:
            conn.poll_main_audio_levels()

    def refresh_main_tone_balance(self, pluginAction, device):
        parent, _ = self._resolve_parent_and_zone(device)
        conn = self._conns.get(parent.id)
        if conn:
            conn.poll_main_tone_balance()

    def refresh_main_processing(self, pluginAction, device):
        parent, _ = self._resolve_parent_and_zone(device)
        conn = self._conns.get(parent.id)
        if conn:
            conn.poll_main_processing()

    def refresh_main_all(self, pluginAction, device):
        parent, _ = self._resolve_parent_and_zone(device)
        conn = self._conns.get(parent.id)
        if conn:
            conn.poll_main_all()

    # ----- Record Zone Actions -----
    def set_record_source(self, pluginAction, device):
        parent, _ = self._resolve_parent_and_zone(device)
        conn = self._conns.get(parent.id)
        if not conn:
            return
        src = pluginAction.props.get("src", "")
        if src:
            conn.send_ascii("P4S" + str(src), zone=1)
            # quick refresh of record source
            conn.send_raw_no_refresh("P4S?")

    def set_record_simulcast(self, pluginAction, device):
        parent, _ = self._resolve_parent_and_zone(device)
        conn = self._conns.get(parent.id)
        if not conn:
            return
        video = pluginAction.props.get("video", "")
        audio = pluginAction.props.get("audio", "")
        if video and audio:
            conn.send_ascii("P4X" + str(video) + str(audio), zone=1)
            conn.send_raw_no_refresh("P4S?")

    # ----- Helpers -----
    def _safe_update_state(self, device, state_id, value):
        try:
            device.updateStateOnServer(state_id, value)
        except Exception:
            pass

    def _resolve_parent_and_zone(self, device):
        if self._is_parent_processor(device):
            return device, 1
        if device.deviceTypeId == "anthemZone":
            parent_id = int(device.pluginProps.get("parentId", "0"))
            zone = int(device.pluginProps.get("zoneNumber", "1"))
            parent = indigo.devices.get(parent_id)
            return parent, zone
        return device, 1

    def _send_for_device(self, device, cmd_template: str):
        parent, zone = self._resolve_parent_and_zone(device)
        conn = self._conns.get(parent.id)
        if not conn:
            self.logger.warning(f"[{device.name}] no connection object available")
            return
        cmd = cmd_template.format(z=zone)
        conn.send_ascii(cmd, zone=zone)

    def source_code_to_name(self, code: str) -> str:
        mapping = {
            "0": "CD",
            "1": "2-Ch BAL",
            "2": "6-Ch S/E",
            "3": "Tape",
            "4": "FM/AM",
            "5": "DVD1",
            "6": "TV1",
            "7": "SAT1",
            "8": "VCR",
            "9": "AUX",
            "c": "Current",
            "d": "DVD2",
            "e": "DVD3",
            "f": "DVD4",
            "g": "TV2",
            "h": "TV3",
            "i": "TV4",
            "j": "SAT2",
        }
        return mapping.get(code, f"Unknown({code})")

#testwrite
