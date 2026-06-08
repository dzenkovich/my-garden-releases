import os
import gc
import microcontroller

# adafruit_hashlib gives us SHA-256 on builds that lack a native `hashlib`.
import adafruit_hashlib as hashlib

# wifi / adafruit_connection_manager / adafruit_requests are imported lazily in
# _session() so this module stays importable for the boot-time watchdog even if
# a bad update damaged the lib/ bundle -- rollback only touches the filesystem.


# Where we persist what version is currently installed and whether the last
# install has been confirmed to boot cleanly. Lives on the (code-writable) FS.
VERSION_FILE = "/version.json"

# Suffixes for the staged download and the kept-back previous copy.
STAGE_SUFFIX = ".new"
BACKUP_SUFFIX = ".bak"

# Stream downloads in small chunks -- ESP32 RAM is tight and mbedTLS already
# holds a large contiguous buffer during the TLS session (see mqtt_manager.py).
CHUNK = 512
SHA_BLOCK = 64  # SHA-256 block size, for the hand-rolled HMAC below.


def _hmac_sha256(key, message):
    """HMAC-SHA256 implemented over adafruit_hashlib (no `hmac` module needed).

    `key` and `message` are bytes; returns the lowercase hex digest string.
    """
    if len(key) > SHA_BLOCK:
        key = hashlib.new("sha256", key).digest()
    key = key + b"\x00" * (SHA_BLOCK - len(key))
    o_pad = bytes(b ^ 0x5C for b in key)
    i_pad = bytes(b ^ 0x36 for b in key)
    inner = hashlib.new("sha256", i_pad + message).digest()
    return hashlib.new("sha256", o_pad + inner).hexdigest()


def _const_time_eq(a, b):
    """Constant-time-ish string compare so a bad HMAC can't be timing-probed."""
    if len(a) != len(b):
        return False
    result = 0
    for x, y in zip(a, b):
        result |= ord(x) ^ ord(y)
    return result == 0


class OtaError(Exception):
    pass


class OtaManager:
    """Application-code OTA for CircuitPython.

    Moves the bytes over HTTPS (manifest + files), authenticates the manifest
    with an HMAC shared secret, verifies each file's SHA-256, then swaps files
    atomically-per-file (keeping a `.bak`) and reboots. The GP2 jumper remains
    the guaranteed manual USB-reflash fallback if anything goes wrong.
    """

    def __init__(self, current_version, status_cb=None):
        self.current_version = current_version
        # status_cb(text) is used to surface progress over MQTT; optional.
        self._status_cb = status_cb or (lambda _text: None)

        self.manifest_url = os.getenv("OTA_MANIFEST_URL")
        secret = os.getenv("OTA_HMAC_SECRET") or ""
        self._hmac_key = secret.encode("utf-8")

        # Optional auth for PRIVATE-repo hosting. Empty => no headers, which is
        # exactly what public raw.githubusercontent.com needs. When set (a
        # fine-grained PAT), we hit the GitHub Contents API instead, which
        # streams raw bytes for private repos.
        self._auth_token = os.getenv("OTA_AUTH_TOKEN") or ""

        # The HTTPS session is created lazily (see _session) so the boot-time
        # watchdog -- which only touches the filesystem -- can run before WiFi
        # is connected, without building a radio socket pool.
        self._requests = None

    def _session(self):
        if self._requests is None:
            import wifi
            import adafruit_connection_manager
            import adafruit_requests
            # Reuse the radio's connection-manager pool + SSL context -- the
            # exact setup already proven to work for the MQTT/TLS connection.
            pool = adafruit_connection_manager.get_radio_socketpool(wifi.radio)
            ssl_context = adafruit_connection_manager.get_radio_ssl_context(wifi.radio)
            self._requests = adafruit_requests.Session(pool, ssl_context)
        return self._requests

    def _headers(self):
        # token set -> GitHub Contents API style auth (private repos);
        # empty -> no headers (public raw.githubusercontent.com).
        if self._auth_token:
            return {
                "Authorization": "token " + self._auth_token,
                "Accept": "application/vnd.github.raw",
                "User-Agent": "my-garden-ota",
            }
        return {}

    # --- status helper -----------------------------------------------------

    def _status(self, text):
        print("OTA:", text)
        try:
            self._status_cb(text)
        except Exception as e:
            print("OTA: status_cb failed:", repr(e))

    # --- public API --------------------------------------------------------

    def check(self):
        """Fetch + authenticate the manifest. Returns (remote_version, available).

        Raises OtaError on network / auth failure so callers can report it.
        """
        if not self.manifest_url:
            raise OtaError("no OTA_MANIFEST_URL configured")
        if not self._hmac_key:
            raise OtaError("no OTA_HMAC_SECRET configured")

        self._status("checking")
        manifest = self._fetch_manifest()
        remote = manifest.get("version")
        available = remote is not None and remote != self.current_version
        self._status("up_to_date" if not available else "available:" + str(remote))
        return remote, available

    def update(self):
        """Full update cycle: check -> download/stage -> commit (+reset).

        Returns False (and reports status) if already up to date or on failure;
        on success it does not return -- the device resets into the new code.
        """
        try:
            manifest = self._fetch_manifest()
        except OtaError as e:
            self._status("failed:" + str(e))
            return False

        remote = manifest.get("version")
        if remote is None or remote == self.current_version:
            self._status("up_to_date")
            return False

        files = manifest.get("files") or {}
        base_url = manifest.get("base_url") or ""
        if not files:
            self._status("failed:empty_manifest")
            return False

        try:
            self._download_and_stage(base_url, files)
        except OtaError as e:
            self._status("failed:" + str(e))
            self._cleanup_staged(files)
            return False

        self._status("installing")
        try:
            self._commit(remote, list(files.keys()))
        except OtaError as e:
            self._status("failed:" + str(e))
            self._cleanup_staged(files)
            return False

        self._status("rebooting")
        microcontroller.reset()  # no return

    # --- boot watchdog (optional self-heal; GP2/USB is the hard fallback) ---

    def confirm_boot_ok(self):
        """Clear the boot-pending flag once the new code is running cleanly."""
        state = self._read_version_file()
        if state.get("boot_pending"):
            state["boot_pending"] = False
            self._write_version_file(state)
            print("OTA: boot confirmed OK, watchdog cleared")

    def rollback_if_pending(self):
        """If the last install never confirmed a clean boot, restore the .bak
        copies and reset. Call this very early at boot."""
        state = self._read_version_file()
        if not state.get("boot_pending"):
            return False
        files = state.get("files") or []
        print("OTA: boot-pending detected -> rolling back")
        restored = False
        for path in files:
            bak = path + BACKUP_SUFFIX
            try:
                os.stat(bak)
            except OSError:
                continue
            self._safe_remove(path)
            os.rename(bak, path)
            restored = True
        state["boot_pending"] = False
        state["version"] = state.get("previous_version", self.current_version)
        self._write_version_file(state)
        if restored:
            self._status("failed:rolled_back")
            microcontroller.reset()  # no return
        return True

    # --- internals ---------------------------------------------------------

    def _fetch_manifest(self):
        import json

        gc.collect()
        manifest_bytes = self._get_bytes(self.manifest_url)
        sig = self._get_text(self.manifest_url + ".hmac").strip()
        expected = _hmac_sha256(self._hmac_key, manifest_bytes)
        if not _const_time_eq(sig, expected):
            raise OtaError("bad_signature")
        try:
            return json.loads(manifest_bytes)
        except Exception:
            raise OtaError("bad_manifest_json")

    def _download_and_stage(self, base_url, files):
        total = len(files)
        done = 0
        for path, want_hash in files.items():
            gc.collect()
            url = base_url + path
            stage = path + STAGE_SUFFIX
            self._ensure_parent_dir(stage)
            got_hash = self._stream_to_file(url, stage)
            if got_hash != want_hash:
                raise OtaError("hash_mismatch:" + path)
            done += 1
            self._status("downloading %d%%" % (done * 100 // total))
        self._status("verifying")  # all hashes already matched per-file above

    def _commit(self, remote_version, paths):
        # Record intent BEFORE swapping so a power-loss mid-commit is detectable.
        state = {
            "version": remote_version,
            "previous_version": self.current_version,
            "files": paths,
            "boot_pending": True,
        }
        self._write_version_file(state)

        for path in paths:
            stage = path + STAGE_SUFFIX
            bak = path + BACKUP_SUFFIX
            try:
                os.stat(path)
                self._safe_remove(bak)
                os.rename(path, bak)  # keep the old copy for rollback
            except OSError:
                pass  # new file that didn't exist before
            os.rename(stage, path)

    def _cleanup_staged(self, files):
        for path in files:
            self._safe_remove(path + STAGE_SUFFIX)

    # --- HTTP helpers ------------------------------------------------------

    def _get_bytes(self, url):
        try:
            resp = self._session().get(url, headers=self._headers())
        except Exception as e:
            raise OtaError("http_error:" + repr(e))
        try:
            if resp.status_code != 200:
                raise OtaError("http_status:%d" % resp.status_code)
            return resp.content
        finally:
            resp.close()

    def _get_text(self, url):
        try:
            resp = self._session().get(url, headers=self._headers())
        except Exception as e:
            raise OtaError("http_error:" + repr(e))
        try:
            if resp.status_code != 200:
                raise OtaError("http_status:%d" % resp.status_code)
            return resp.text
        finally:
            resp.close()

    def _stream_to_file(self, url, dest):
        """Download `url` to `dest` in chunks, returning the SHA-256 hex digest."""
        try:
            resp = self._session().get(url, headers=self._headers())
        except Exception as e:
            raise OtaError("http_error:" + repr(e))
        h = hashlib.new("sha256")
        try:
            if resp.status_code != 200:
                raise OtaError("http_status:%d" % resp.status_code)
            with open(dest, "wb") as f:
                for chunk in resp.iter_content(CHUNK):
                    if chunk:
                        f.write(chunk)
                        h.update(chunk)
        finally:
            resp.close()
        return h.hexdigest()

    # --- file helpers ------------------------------------------------------

    def _read_version_file(self):
        import json

        try:
            with open(VERSION_FILE, "r") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _write_version_file(self, state):
        import json

        with open(VERSION_FILE, "w") as f:
            json.dump(state, f)

    def _ensure_parent_dir(self, path):
        if "/" not in path.strip("/"):
            return
        parent = path.rsplit("/", 1)[0]
        if not parent:
            return
        try:
            os.stat(parent)
        except OSError:
            try:
                os.mkdir(parent)
            except OSError:
                pass

    def _safe_remove(self, path):
        try:
            os.remove(path)
        except OSError:
            pass
