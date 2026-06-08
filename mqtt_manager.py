import os
import gc
import wifi
import adafruit_connection_manager
import adafruit_minimqtt.adafruit_minimqtt as MQTT


class MqttManager:
    def __init__(self, pool, device_id):
        self.device_id = device_id

        # Use adafruit_connection_manager to build the socket pool and a
        # correctly-configured SSL context for this board's radio. The context
        # it returns already trusts CircuitPython's bundled CA certificates,
        # which include the Let's Encrypt root (ISRG Root X1) that HiveMQ Cloud
        # serverless uses. Building the context by hand and trying to set
        # CERT_NONE / check_hostname does NOT work on the native (mbedTLS) ssl
        # port -- those attributes are read-only and silently fail.
        self.pool = adafruit_connection_manager.get_radio_socketpool(wifi.radio)
        self.ssl_context = adafruit_connection_manager.get_radio_ssl_context(wifi.radio)
        self.broker = os.getenv("MQTT_BROKER")
        self.port = int(os.getenv("MQTT_PORT", 8883))

        self.mqtt_client = MQTT.MQTT(
            broker=self.broker,
            port=self.port,
            username=os.getenv("MQTT_USERNAME"),
            password=os.getenv("MQTT_PASSWORD"),
            client_id=self.device_id,
            socket_pool=self.pool,
            ssl_context=self.ssl_context,
            is_ssl=True,
            # socket_timeout governs per-socket reads. MiniMQTT requires that
            # loop(timeout=...) be >= socket_timeout, so keep this small or the
            # main loop blocks. The TLS handshake on this board is fast, so 1s
            # is plenty.
            socket_timeout=1,
            connect_retries=5,
            keep_alive=60,
        )

    def diagnose(self):
        """Raw TLS connect to the broker so the *real* error is visible.

        MiniMQTT hides the underlying exception behind the generic
        'Repeated connect failures'. This reproduces the socket + TLS
        handshake by hand and prints what actually goes wrong (DNS,
        certificate, timeout, MemoryError, ...).
        """
        gc.collect()
        print("DIAG: free mem before =", gc.mem_free())
        print("DIAG: resolving", self.broker, "...")
        try:
            addr = self.pool.getaddrinfo(self.broker, self.port)[0][-1]
            print("DIAG: resolved ->", addr)
        except Exception as e:
            print("DIAG: DNS FAILED ->", repr(e))
            return
        sock = self.pool.socket(self.pool.AF_INET, self.pool.SOCK_STREAM)
        sock.settimeout(10)
        try:
            print("DIAG: wrapping socket in TLS...")
            wrapped = self.ssl_context.wrap_socket(sock, server_hostname=self.broker)
            print("DIAG: connecting TLS to", (self.broker, self.port))
            wrapped.connect((self.broker, self.port))
            print("DIAG: RAW TLS CONNECT OK  (free mem =", gc.mem_free(), ")")
            # TLS works; now find out whether the BROKER accepts the actual
            # MQTT CONNECT (credentials / client-id / protocol) on this socket.
            self._mqtt_connect_probe(wrapped)
            wrapped.close()
        except Exception as e:
            print("DIAG: RAW TLS CONNECT FAILED ->", repr(e))
            try:
                sock.close()
            except Exception:
                pass

    def _mqtt_connect_probe(self, s):
        """Hand-build and send an MQTT 3.1.1 CONNECT over the proven-good TLS
        socket, then read the CONNACK return code. This isolates a broker-side
        rejection (bad creds, rejected client-id, protocol) from MiniMQTT's own
        socket handling -- the raw TLS test never sends an MQTT packet."""
        user = (os.getenv("MQTT_USERNAME") or "").encode()
        pw = (os.getenv("MQTT_PASSWORD") or "").encode()
        cid = self.device_id.encode()

        def field(b):
            return bytes((len(b) >> 8, len(b) & 0xFF)) + b

        # protocol "MQTT", level 4 (3.1.1), flags=user+pass+clean (0xC2), keepalive 60
        var_header = b"\x00\x04MQTT\x04\xc2\x00\x3c"
        remaining = var_header + field(cid) + field(user) + field(pw)

        # remaining length as MQTT variable byte integer
        rl = bytearray()
        n = len(remaining)
        while True:
            byte = n & 0x7F
            n >>= 7
            rl.append(byte | 0x80 if n else byte)
            if not n:
                break

        packet = b"\x10" + bytes(rl) + remaining
        try:
            s.send(packet)
            buf = bytearray(4)
            got = s.recv_into(buf)
            print("DIAG MQTT: CONNACK bytes =", bytes(buf[:got]))
            codes = {
                0: "ACCEPTED", 1: "bad protocol version", 2: "client-id rejected",
                3: "server unavailable", 4: "bad username/password", 5: "not authorized",
            }
            if got >= 4 and buf[0] == 0x20:
                print("DIAG MQTT: return code =", buf[3], codes.get(buf[3], "unknown"))
            else:
                print("DIAG MQTT: no/short CONNACK -> broker dropped the connection")
        except Exception as e:
            print("DIAG MQTT: probe failed ->", repr(e))

    def connect(self):
        print("Connecting to MQTT broker...")
        # Free RAM before the TLS handshake -- mbedTLS needs a large contiguous
        # buffer and the handshake fails with MemoryError when fragmented.
        gc.collect()
        try:
            self.mqtt_client.connect()
            print("MQTT connected.")
            return True
        except Exception as e:
            print(f"Failed to connect to MQTT: {e}")
            # Surface the real underlying cause that MiniMQTT swallowed.
            self.diagnose()
            return False

    def loop(self):
        # Must be >= socket_timeout (see __init__). Keep it short so the main
        # loop stays responsive to sensors and BLE.
        self.mqtt_client.loop(timeout=1)

    def is_connected(self):
        return self.mqtt_client.is_connected()

    def reconnect(self):
        self.mqtt_client.reconnect()

    def subscribe(self, topic, qos=1):
        self.mqtt_client.subscribe(topic.format(self.device_id), qos)

    def publish(self, topic, message, retain=False):
        print(f"Published to {topic.format(self.device_id)} value {message}")
        self.mqtt_client.publish(topic.format(self.device_id), message, retain=retain)
