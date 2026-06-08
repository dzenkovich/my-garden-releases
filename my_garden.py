import time
import os
import json
import rtc
import wifi
import socketpool
import adafruit_ntp
import adafruit_ble
import binascii
import microcontroller
from adafruit_ble.advertising.standard import ProvideServicesAdvertisement

from ble_services import ProvisioningService, DeviceInformationService
from hardware_manager import HardwareManager
from mqtt_manager import MqttManager

# Running application-code version. Bump this on every release; the OTA manifest
# compares against it to decide whether an update is available.
__version__ = "1.0.0"


class MyGarden:
    # --- MQTT Topics ---
    BASE_TOPIC = "greenhouse/{}"
    TEMP_TOPIC = BASE_TOPIC + "/sensors/temperature"
    HUMID_TOPIC = BASE_TOPIC + "/sensors/humidity"
    LUM_TOPIC = BASE_TOPIC + "/sensors/luminosity"
    LED_CONTROL_TOPIC = BASE_TOPIC + "/control/led_brightness"
    MODE_CONTROL_TOPIC = BASE_TOPIC + "/control/light_mode"
    HISTORY_SYNC_TOPIC = BASE_TOPIC + "/control/sync_history"
    HISTORY_DATA_TOPIC = BASE_TOPIC + "/data/history"
    LIGHT_CONFIG_TOPIC = BASE_TOPIC + "/control/light_config"
    OTA_CHECK_TOPIC = BASE_TOPIC + "/control/ota_check"
    OTA_APPLY_TOPIC = BASE_TOPIC + "/control/ota_apply"
    OTA_STATUS_TOPIC = BASE_TOPIC + "/data/ota_status"
    VERSION_TOPIC = BASE_TOPIC + "/data/version"

    # --- Constants ---
    LOG_FILE = "/history.log"
    WIFI_CONFIG_FILE = "/wifi.json"
    DEVICE_ID_FILE = "/device_id.txt"
    LIGHT_CONFIG_FILE = "/light_config.json"
    MAX_LOG_SIZE = 50 * 1024
    PUBLISH_INTERVAL = 3

    # --- Auto-lighting constants ---
    FADE_MINUTES = 60          # length of the sunrise / sunset ramp
    DAY_CENTER_MIN = 15 * 60   # photoperiod centers on 15:00 local (windowsill-friendly)
    CONTROL_GAIN = 0.1         # proportional gain of the lux closed loop
    LUX_DEADBAND = 8.0         # ignore errors smaller than this to avoid flicker
    EMA_ALPHA = 0.3            # smoothing factor for the averaged lux reading

    def __init__(self):
        self.device_id = self._get_or_create_device_id()
        print(f"My unique device ID is: {self.device_id}")

        self.hw_manager = HardwareManager(int(os.getenv("MAX_DUTY_CYCLE", 52428)))
        self.hw_manager.flash_led(1)
        self.hw_manager.init_sensors()
        self._last_sensor_init = 0

        self.ble = adafruit_ble.BLERadio()
        self.prov_service = ProvisioningService()
        self.info_service = DeviceInformationService()
        self.info_service.device_id = self.device_id.encode('utf-8')
        
        self.mqtt_manager = None
        self.ota_manager = None
        self.wifi_connected = False
        self.last_published_temp = None
        self.last_published_humid = None
        self.last_published_lum = None

        # Auto-lighting configuration. "auto" runs the photoperiod schedule and
        # closed-loop lux controller; "manual" applies manual_brightness directly.
        # Overlaid from /light_config.json so it survives reboots offline.
        self.light_config = {
            "mode": "auto",
            "target_lux": 500,
            "photoperiod_hours": 12.0,
            "tz_offset_min": 0,
            "manual_brightness": 0,
        }
        self._load_light_config()
        self.lux_avg = None  # smoothed luminosity feeding the closed loop

        self.rtc = rtc.RTC()

    def _get_or_create_device_id(self):
        try:
            with open(self.DEVICE_ID_FILE, "r") as f:
                return f.read().strip()
        except OSError:
            mac_address = wifi.radio.mac_address
            device_id = binascii.hexlify(mac_address).decode('utf-8')
            try:
                with open(self.DEVICE_ID_FILE, "w") as f:
                    f.write(device_id)
            except Exception as e:
                print(f"Failed to save device ID: {e}")
            return device_id

    def _load_light_config(self):
        try:
            with open(self.LIGHT_CONFIG_FILE, "r") as f:
                stored = json.load(f)
            # Only overlay keys we recognise so a stale file can't inject junk.
            for key in self.light_config:
                if key in stored:
                    self.light_config[key] = stored[key]
            print(f"Loaded light config: {self.light_config}")
        except (OSError, ValueError):
            print("No saved light config; using defaults.")

    def _save_light_config(self):
        # Called only on config/mode/brightness changes (never in the loop) to
        # limit flash wear.
        try:
            with open(self.LIGHT_CONFIG_FILE, "w") as f:
                json.dump(self.light_config, f)
        except Exception as e:
            print(f"Failed to save light config: {e}")

    def connect_wifi(self):
        try:
            with open(self.WIFI_CONFIG_FILE, "r") as f:
                config = json.load(f)
            wifi.radio.connect(config["ssid"], config["password"])
            self.wifi_connected = True
            self.hw_manager.flash_led(3)
            
            pool = socketpool.SocketPool(wifi.radio)
            ntp = adafruit_ntp.NTP(pool, tz_offset=0)
            self.rtc.datetime = ntp.datetime
            
            return pool
        except Exception as e:
            print(f"Could not connect with saved credentials: {e}")
            self.wifi_connected = False
            return None

    def start_discovery_and_provisioning_server(self):
        if not self.ble.advertising:
            advertisement = ProvideServicesAdvertisement(self.info_service, self.prov_service)
            self.ble.name = f"MyGarden-{self.device_id[-4:]}"
            self.ble.start_advertising(advertisement)
            print("BLE discovery and provisioning server started.")

    def _handle_provisioning_connection(self):
        print("Starting WiFi provisioning process...")
        self.prov_service.status = b"Ready to provision"

        received_ssid = None
        received_pass = None

        provision_start_time = time.monotonic()
        while self.ble.connected and time.monotonic() - provision_start_time < 120:
            print(f"Received self.prov_service.ssid: {self.prov_service.ssid}")
            print(f"Received self.prov_service.password: {self.prov_service.password}")
            ssid_bytes = self.prov_service.ssid.strip(b'\x00')
            if received_ssid is None and ssid_bytes:
                received_ssid = ssid_bytes.decode('utf-8')
                print(f"Received SSID: {received_ssid}")
                self.prov_service.status = b"Got SSID"

            password_bytes = self.prov_service.password.strip(b'\x00')
            if received_pass is None and password_bytes:
                received_pass = password_bytes.decode('utf-8')
                print("Received password.")
                self.prov_service.status = b"Got Password"

            if received_ssid is not None and received_pass is not None:
                self.prov_service.status = b"Credentials received"
                print("Saving credentials...")

                config = {"ssid": received_ssid, "password": received_pass}
                try:
                    with open(self.WIFI_CONFIG_FILE, "w") as f:
                        json.dump(config, f)
                    print("Credentials saved successfully.")
                    self.prov_service.status = b"Credentials saved"
                    time.sleep(2)
                    print("Rebooting to apply settings...")
                    microcontroller.reset()
                except Exception as e:
                    print(f"Failed to save credentials: {e}")
                    self.prov_service.status = b"Save failed"
                    received_ssid = None
                    received_pass = None
            time.sleep(0.1)

        print("Finished provisioning attempt.")

    def start_ble_server(self):
        if not self.ble.advertising:
            advertisement = ProvideServicesAdvertisement(self.info_service, self.prov_service)
            self.ble.name = f"MyGarden-{self.device_id[-4:]}"
            self.ble.start_advertising(advertisement)
            print("BLE server started.")

    def run(self):
        last_log_time = 0
        last_update_time = 0
        
        self.start_ble_server()

        # Restore persisted manual brightness so a reboot in manual mode keeps
        # the lamp where the user left it. Auto mode self-corrects in the loop.
        if self.light_config["mode"] == "manual":
            self.hw_manager.set_led_brightness_percent(self.light_config["manual_brightness"])

        sensor_retry_interval = 60

        boot_confirmed = False
        last_ota_check = time.monotonic()
        ota_auto_check = int(os.getenv("OTA_AUTO_CHECK", 0)) == 1
        ota_check_interval = 24 * 60 * 60  # once a day when auto-check is enabled

        while True:
            if self.ble.connected:
                if self.info_service.provision and any(self.info_service.provision):
                    self.info_service.provision = b''
                    self._handle_provisioning_connection()

            current_time = time.monotonic()

            if self.hw_manager.temp_humid_sensor is None or self.hw_manager.lum_sensor is None:
                if current_time - self._last_sensor_init >= sensor_retry_interval:
                    self._last_sensor_init = current_time
                    aht_ok, bh_ok = self.hw_manager.init_sensors()
                    print(f"Sensor re-init: AHT={aht_ok} BH={bh_ok}")
            if (current_time - last_log_time) > self.PUBLISH_INTERVAL:
                self.hw_manager.read_sensor_cache()
                temp = self.hw_manager.get_temperature()
                humid = self.hw_manager.get_humidity()
                luminosity = self.hw_manager.get_luminosity()
                if luminosity is not None:
                    if self.lux_avg is None:
                        self.lux_avg = luminosity
                    else:
                        self.lux_avg = (self.EMA_ALPHA * luminosity
                                        + (1 - self.EMA_ALPHA) * self.lux_avg)

                self.log_sensor_data(temp, humid, luminosity)

                if self.wifi_connected and self.mqtt_manager:
                    try:
                        self.mqtt_manager.loop()
                        if not self.mqtt_manager.is_connected():
                            self.mqtt_manager.reconnect()
                        else:
                            # Publish data if changed
                            is_overdue = (current_time - last_update_time) > self.PUBLISH_INTERVAL * 10
                            if temp is not None and (self.last_published_temp is None or abs(temp - self.last_published_temp) > 0.1 or is_overdue):
                                self.mqtt_manager.publish(self.TEMP_TOPIC, str(temp), retain=True)
                                self.last_published_temp = temp
                            if humid is not None and (self.last_published_humid is None or abs(humid - self.last_published_humid) > 1.0 or is_overdue):
                                self.mqtt_manager.publish(self.HUMID_TOPIC, str(humid), retain=True)
                                self.last_published_humid = humid
                            if luminosity is not None and (self.last_published_lum is None or abs(luminosity - self.last_published_lum) > 5.0 or is_overdue):
                                self.mqtt_manager.publish(self.LUM_TOPIC, str(luminosity), retain=True)
                                self.last_published_lum = luminosity
                                last_update_time = current_time
                            
                    except Exception as e:
                        print(f"MQTT Error in main loop: {e}")

                self.update_auto_light()

                # First full loop completed cleanly: confirm the OTA watchdog so
                # a freshly-installed version isn't rolled back on next boot.
                if not boot_confirmed:
                    boot_confirmed = True
                    try:
                        self._ensure_ota_manager().confirm_boot_ok()
                    except Exception as e:
                        print(f"OTA boot-confirm failed: {e}")

                # Optional periodic check -- reports availability only, never
                # auto-installs (the app drives the actual apply).
                if (ota_auto_check and self.wifi_connected and self.mqtt_manager
                        and (current_time - last_ota_check) > ota_check_interval):
                    last_ota_check = current_time
                    self.handle_ota_check()

                last_log_time = current_time
            time.sleep(0.5)
    
    def on_message(self, client, topic, message):
        print(f"Received message on topic {topic}: {message}")
        # Simplified topic checking
        topic_suffix = topic.split('/')[-1]

        if topic_suffix == "light_config":
            try:
                cfg = json.loads(message)
                for key in ("target_lux", "photoperiod_hours", "tz_offset_min"):
                    if key in cfg:
                        self.light_config[key] = cfg[key]
                self.light_config["mode"] = "auto"
                self._save_light_config()
            except ValueError:
                print("Received invalid light config")
        elif topic_suffix == "light_mode":
            mode = message.lower()
            if mode in ("auto", "manual"):
                self.light_config["mode"] = mode
                self._save_light_config()
        elif topic_suffix == "led_brightness":
            try:
                brightness_val = int(float(message))
                self.light_config["mode"] = "manual"
                self.light_config["manual_brightness"] = brightness_val
                self._save_light_config()
                self.hw_manager.set_led_brightness_percent(brightness_val)
            except ValueError:
                print("Received invalid brightness value")
        elif topic_suffix == "sync_history":
            self.publish_history()
        elif topic_suffix == "ota_check":
            self.handle_ota_check()
        elif topic_suffix == "ota_apply":
            self.handle_ota_apply()

    def _publish_ota_status(self, text):
        # Retained so the app sees the latest OTA state immediately on (re)subscribe.
        if self.mqtt_manager and self.mqtt_manager.is_connected():
            try:
                self.mqtt_manager.publish(self.OTA_STATUS_TOPIC, text, retain=True)
            except Exception as e:
                print(f"Failed to publish OTA status: {e}")

    def _ensure_ota_manager(self):
        if self.ota_manager is None:
            from ota_manager import OtaManager
            self.ota_manager = OtaManager(__version__, status_cb=self._publish_ota_status)
        return self.ota_manager

    def handle_ota_check(self):
        try:
            ota = self._ensure_ota_manager()
            ota.check()
        except Exception as e:
            print(f"OTA check failed: {e}")
            self._publish_ota_status(f"failed:{e}")

    def handle_ota_apply(self):
        try:
            ota = self._ensure_ota_manager()
            # On success this resets the board and does not return.
            ota.update()
        except Exception as e:
            print(f"OTA apply failed: {e}")
            self._publish_ota_status(f"failed:{e}")

    def setup_mqtt(self, pool):
        self.mqtt_manager = MqttManager(pool, self.device_id)
        self.mqtt_manager.mqtt_client.on_message = self.on_message
        if not self.mqtt_manager.connect():
            # Don't subscribe on a dead connection -- that raises
            # MMQTTStateError. The main loop's reconnect() will retry.
            print("MQTT not connected; skipping subscribe. Will retry in loop.")
            return
        # Subscribe to topics
        self.mqtt_manager.subscribe(self.LED_CONTROL_TOPIC)
        self.mqtt_manager.subscribe(self.MODE_CONTROL_TOPIC)
        self.mqtt_manager.subscribe(self.LIGHT_CONFIG_TOPIC)
        self.mqtt_manager.subscribe(self.HISTORY_SYNC_TOPIC)
        self.mqtt_manager.subscribe(self.OTA_CHECK_TOPIC)
        self.mqtt_manager.subscribe(self.OTA_APPLY_TOPIC)
        # Announce the running version (retained) so the app can show it and
        # decide whether an update is available.
        try:
            self.mqtt_manager.publish(self.VERSION_TOPIC, __version__, retain=True)
        except Exception as e:
            print(f"Failed to publish version: {e}")
        # Clear any stale retained OTA status left over from a previous apply +
        # reboot. Without this the app would re-receive the retained "installing"
        # / "rebooting" status on reconnect and stay stuck on the progress
        # spinner. OTA only runs from the main loop (never across a boot), so
        # "idle" is always the correct state at startup.
        self._publish_ota_status("idle")

    @staticmethod
    def compute_light_fraction(local_min, center_min, photoperiod_hours, fade_min):
        """Setpoint multiplier (0..1) for the given local minute-of-day.

        The photoperiod window is centered on ``center_min``. The lamp ramps up
        over ``fade_min`` (sunrise), holds at 1.0, then ramps down (sunset).
        Pure function so the schedule can be unit-tested off-device.
        """
        if photoperiod_hours <= 0:
            return 0.0
        half = photoperiod_hours * 60 / 2
        if photoperiod_hours >= 24:
            return 1.0
        on = center_min - half
        off = center_min + half
        # Minutes since lights-on, wrapped into [0, 1440) so windows that cross
        # midnight still work.
        since_on = (local_min - on) % 1440
        window = off - on
        if since_on >= window:
            return 0.0  # night
        fade = min(fade_min, half)
        if since_on < fade:
            return since_on / fade          # sunrise ramp 0 -> 1
        if since_on > window - fade:
            return (window - since_on) / fade  # sunset ramp 1 -> 0
        return 1.0                          # hold

    def update_auto_light(self):
        if self.light_config["mode"] != "auto":
            return  # manual brightness already applied by on_message

        now = self.rtc.datetime
        utc_min = now.tm_hour * 60 + now.tm_min
        local_min = (utc_min + self.light_config["tz_offset_min"]) % 1440

        frac = self.compute_light_fraction(
            local_min, self.DAY_CENTER_MIN,
            self.light_config["photoperiod_hours"], self.FADE_MINUTES)

        if frac <= 0:
            self.hw_manager.ledpin.duty_cycle = 0
            return

        # Closed loop toward the ramped setpoint. Ramping the *target* (not raw
        # brightness) yields a graceful dawn/dusk while still compensating for
        # ambient light.
        effective_target = self.light_config["target_lux"] * frac
        if self.lux_avg is None:
            return  # no sensor reading yet; leave lamp untouched
        error = effective_target - self.lux_avg
        if abs(error) > self.LUX_DEADBAND:
            new_duty = self.hw_manager.ledpin.duty_cycle + int(error * self.CONTROL_GAIN)
            self.hw_manager.ledpin.duty_cycle = max(0, min(new_duty, self.hw_manager.max_duty_cycle))

    def log_sensor_data(self, temp, humid, lux):
        log_entry = {"ts": time.time(), "t": temp, "h": humid, "l": lux}
        try:
            if os.stat(self.LOG_FILE)[6] > self.MAX_LOG_SIZE:
                os.remove(self.LOG_FILE)
        except OSError:
            pass # File doesn't exist yet
        try:
            with open(self.LOG_FILE, "a") as f:
                f.write(json.dumps(log_entry) + "\n")
        except Exception as e:
            print(f"Error writing to log file: {e}")

    def publish_history(self):
        if self.mqtt_manager and self.mqtt_manager.is_connected():
            print("Syncing history...")
            try:
                with open(self.LOG_FILE, "r") as f:
                    for line in f:
                        self.mqtt_manager.publish(self.HISTORY_DATA_TOPIC, line.strip())
                print("History sync complete.")
            except Exception as e:
                print(f"Error publishing history: {e}")

