# main.py
from my_garden import MyGarden, __version__
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
    try:
        OtaManager(__version__).rollback_if_pending()
    except Exception as e:
        print(f"OTA rollback check failed: {e}")

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
