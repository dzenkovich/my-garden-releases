from adafruit_ble.services import Service
from adafruit_ble.characteristics import Characteristic
from adafruit_ble.uuid import VendorUUID


# --- Define the custom BLE service for device information ---
class DeviceInformationService(Service):
    """
    A custom BLE service to advertise the device's ID and trigger provisioning.
    """
    uuid = VendorUUID("A49A20B0-45A0-4475-B058-2268755A316D")

    device_id = Characteristic(
        uuid=VendorUUID("A49A20B1-45A0-4475-B058-2268755A316D"),
        properties=Characteristic.READ,
        max_length=12,
    )
    provision = Characteristic(
        uuid=VendorUUID("A49A20B2-45A0-4475-B058-2268755A316D"),
        properties=Characteristic.WRITE,
        max_length=1
    )


# --- Define the custom BLE service for provisioning ---
class ProvisioningService(Service):
    """
    A custom BLE service to handle WiFi credential provisioning.
    """
    uuid = VendorUUID("ADA47540-3E67-4054-874E-34442114639B")

    # Define the characteristics that belong to this service.
    # These are class-level objects that act as descriptors.
    ssid = Characteristic(
        uuid=VendorUUID("ADA47541-3E67-4054-874E-34442114639B"),
        properties=Characteristic.WRITE,
        max_length=32,
        initial_value=None,
    )
    password = Characteristic(
        uuid=VendorUUID("ADA47542-3E67-4054-874E-34442114639B"),
        properties=Characteristic.WRITE,
        max_length=64,
        initial_value=None,
    )
    status = Characteristic(
        uuid=VendorUUID("ADA47543-3E67-4054-874E-34442114639B"),
        properties=Characteristic.READ | Characteristic.NOTIFY,
        max_length=20,
    )
