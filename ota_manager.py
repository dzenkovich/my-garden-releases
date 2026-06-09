import os
import gc
import microcontroller

# adafruit_hashlib (SHA-256/HMAC), wifi, adafruit_connection_manager and
# adafruit_requests are ALL imported lazily -- inside the functions that need
# them -- so this module stays importable for the boot-time watchdog even if a
# bad/interrupted update damaged the lib/ bundle. rollback_if_pending() only
# touches the filesystem (os / microcontroller) and must never pull in a lib.


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
    import adafruit_hashlib as hashlib  # lazy: keep module importable at boot
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
        """Clear the boot-pending flag once the new code is running cleanly, and
        drop the now-obsolete .bak copies.

        Clearing boot_pending first (then deleting .bak) keeps the order safe: a
        power loss in between leaves stale .bak files behind, but with
        boot_pending already False the watchdog won't act on them. Removing the
        backups stops a future watchdog pass from ever restoring stale code and
        keeps the (small) filesystem tidy."""
        state = self._read_version_file()
        if not state.get("boot_pending"):
            return
        state["boot_pending"] = False
        state["boot_attempts"] = 0
        self._write_version_file(state)
        for path in state.get("files") or []:
            self._safe_remove(path + BACKUP_SUFFIX)
        print("OTA: boot confirmed OK, watchdog cleared")

    def rollback_if_pending(self):
        """Boot watchdog. Call this very early at boot.

        After a commit, `boot_pending` is True and we reboot into the new code.
        The FIRST boot must be allowed to run so the main loop can reach
        `confirm_boot_ok()` and clear the flag -- otherwise a freshly-installed
        (and perfectly good) version would be reverted before it ever ran. We
        only roll back if a boot attempt was already recorded but never
        confirmed (i.e. the new code crashed before the first clean loop). The
        GP2/USB jumper remains the hard fallback."""
        state = self._read_version_file()
        if not state.get("boot_pending"):
            return False

        # First boot after install: record the attempt and let the new code run.
        # confirm_boot_ok() clears boot_pending once a clean loop completes.
        attempts = state.get("boot_attempts", 0)
        if attempts < 1:
            state["boot_attempts"] = attempts + 1
            self._write_version_file(state)
            print("OTA: first boot of new version -> giving it a chance")
            return False

        # Booted into this version before and it never confirmed -> bad code.
        files = state.get("files") or []
        print("OTA: unconfirmed boot -> rolling back")
        restored = 0
        failed = 0
        for path in files:
            bak = path + BACKUP_SUFFIX
            try:
                os.stat(bak)
            except OSError:
                continue  # no backup for this file -> nothing to restore here
            # Guard EACH file independently. The original code let a single
            # os.rename() failure propagate out (and get swallowed by code.py),
            # which deleted the live .py but left only the .bak -- exactly the
            # stranded state that bricks the board. Now one bad file can't abort
            # the others, and we verify the restore actually landed.
            try:
                self._safe_remove(path)
                os.rename(bak, path)
                os.stat(path)  # confirm the restored file is really there
                restored += 1
            except OSError as e:
                print("OTA: rollback failed for", path, repr(e))
                failed += 1

        if failed:
            # A managed file is still missing/un-restored. Do NOT clear
            # boot_pending and do NOT fall through to running the app: the
            # import cache is now inconsistent with disk, and letting the loop
            # reach confirm_boot_ok() would wrongly clear the watchdog flag.
            # Keep retrying on subsequent boots (code.py now runs the watchdog
            # before importing the app), but cap the auto-reset attempts so a
            # permanent FS fault degrades to "wait for GP2/USB" instead of a hot
            # reset loop.
            attempts = state.get("boot_attempts", 1) + 1
            state["boot_attempts"] = attempts
            self._write_version_file(state)
            self._status("failed:rollback_incomplete")
            if attempts <= 5:
                microcontroller.reset()  # no return; retry from a clean boot
            return True  # gave up auto-retry; GP2/USB is the hard fallback

        # Full success (or there was nothing with a .bak to restore).
        state["boot_pending"] = False
        state["boot_attempts"] = 0
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

        # Commit code.py LAST. It's the entry point that runs the boot watchdog,
        # so we keep the window in which it is briefly absent (mid-swap) as
        # small and as late as possible. An interrupted swap of any OTHER file
        # self-heals on the next boot, because the watchdog in code.py now runs
        # before the app modules are imported.
        for path in sorted(paths, key=lambda p: p == "code.py"):
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
        import adafruit_hashlib as hashlib  # lazy: keep module importable at boot
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

        # Write-then-rename so a reset/power-loss mid-write can never leave a
        # truncated /version.json (a half-written file fails json.load on the
        # next boot, which would lose the installed-version + rollback record).
        tmp = VERSION_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f)
            f.flush()
        self._safe_remove(VERSION_FILE)
        os.rename(tmp, VERSION_FILE)

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
