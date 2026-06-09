# main.py
#
# IMPORTANT: the application modules (my_garden, and the mqtt/hardware/ble
# modules it imports) are imported INSIDE main(), AFTER the OTA boot watchdog
# has run. If a bad or power-loss-interrupted update left a managed module as a
# .bak with no live .py, a top-level `from my_garden import ...` would raise
# ImportError before we ever reached the watchdog -- stranding the .bak copies
# and bricking the board until a manual USB reflash. ota_manager is dependency-
# light (it pulls in wifi/requests/hashlib only lazily), so it is safe to
# import up front and lets the watchdog self-heal first.
from ota_manager import OtaManager

def main():
    """
    Main entry point for the greenhouse controller.
    This function initializes the garden controller, connects to network services,
    and starts the main processing loop.
    """
    # OTA boot watchdog: if the previous install never confirmed a clean boot,
    # restore the .bak copies and reset before running anything heavy. This is
    # file-only (no WiFi needed); the GP2/USB jumper remains the hard fallback.
    # The version arg is unused by the rollback path (it restores the recorded
    # previous_version), so we pass an empty string and avoid importing the app.
    try:
        OtaManager("").rollback_if_pending()
    except Exception as e:
        print(f"OTA rollback check failed: {e}")

    # Import the app only AFTER the watchdog has had a chance to restore any
    # missing module from its .bak.
    from my_garden import MyGarden

    garden = MyGarden()

    # Establish network connections.
    socket_pool = garden.connect_wifi()

    # If wifi is connected, setup mqtt
    if garden.wifi_connected:
        garden.setup_mqtt(socket_pool)

    # The main loop will handle both sensor reading/logging and MQTT communication
    garden.run()

if __name__ == "__main__":
    main()
