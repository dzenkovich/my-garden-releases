import board
import busio
import pwmio
import adafruit_ahtx0
import adafruit_bh1750
import time

class HardwareManager:
    def __init__(self, max_duty_cycle):
        self.led = pwmio.PWMOut(board.LED, frequency=5000, duty_cycle=0)
        self.ledpin = pwmio.PWMOut(board.GP10, frequency=25000, duty_cycle=0)
        self.max_duty_cycle = max_duty_cycle
        self.i2cAHT = None
        self.i2cBH = None
        self.temp_humid_sensor = None
        self.lum_sensor = None
        self._cached_temp = None
        self._cached_humid = None

    def init_sensors(self):
        aht_ok = self.temp_humid_sensor is not None
        bh_ok = self.lum_sensor is not None

        if not aht_ok:
            if self.i2cAHT is not None:
                try:
                    self.i2cAHT.deinit()
                except Exception:
                    pass
                self.i2cAHT = None
            try:
                self.i2cAHT = busio.I2C(scl=board.GP18, sda=board.GP19)
                self.temp_humid_sensor = adafruit_ahtx0.AHTx0(self.i2cAHT)
                aht_ok = True
            except Exception as e:
                print("AHTx0 init failed:", e)
                self.temp_humid_sensor = None

        if not bh_ok:
            if self.i2cBH is not None:
                try:
                    self.i2cBH.deinit()
                except Exception:
                    pass
                self.i2cBH = None
            try:
                self.i2cBH = busio.I2C(scl=board.GP20, sda=board.GP21)
                self.lum_sensor = adafruit_bh1750.BH1750(self.i2cBH)
                bh_ok = True
            except Exception as e:
                print("BH1750 init failed:", e)
                self.lum_sensor = None

        return aht_ok, bh_ok

    def flash_led(self, count, duration=0.2):
        for _ in range(count):
            self.led.duty_cycle = 32768
            time.sleep(duration / 2)
            self.led.duty_cycle = 0
            time.sleep(duration / 2)

    def set_led_brightness_percent(self, percent):
        if percent > 0:
            scaled_brightness = percent / 100.0
            self.ledpin.duty_cycle = int(self.max_duty_cycle * scaled_brightness)
        else:
            self.ledpin.duty_cycle = 0

    def read_sensor_cache(self):
        """Read temperature and humidity in a single AHT20 conversion and cache
        them. The AHT20 measures both together, so this avoids two back-to-back
        I2C conversions and keeps temp/humidity from the same measurement."""
        if self.temp_humid_sensor is None:
            return
        try:
            self._cached_temp = self.temp_humid_sensor.temperature
            self._cached_humid = self.temp_humid_sensor.relative_humidity
        except Exception:
            self.temp_humid_sensor = None
            self._cached_temp = None
            self._cached_humid = None

    def get_temperature(self):
        return self._cached_temp

    def get_humidity(self):
        return self._cached_humid

    def get_luminosity(self):
        if self.lum_sensor is None:
            return None
        try:
            return self.lum_sensor.lux
        except Exception:
            self.lum_sensor = None
            return None
