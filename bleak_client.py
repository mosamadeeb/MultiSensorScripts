import asyncio
import logging
import concurrent.futures
from queue import Queue
from typing import Optional
import time

import msgpack

from bleak import BleakClient, BleakScanner, BLEDevice
from bleak.backends.characteristic import BleakGATTCharacteristic

SERVICE_UUID = "4fafc201-1fb5-459e-8fcc-c5c9c331914b"
CHARACTERISTIC_UUID = "beb5483e-36e1-4688-b7f5-ea07361b26a8"

DEVICES = [
    "08:D1:F9:C7:14:DE",  # ESP32 DevkitC v4 1 (Left arm)
    "08:D1:F9:DF:D7:BA",  # ESP32 DevkitC v4 2 (Right arm)
    # "CD:C8:D6:CF:45:50",  # XIAO 1 (Left leg)
    # "D9:4D:33:22:7F:55",  # XIAO 2 (Right leg)
]

DEVICE_NAMES = [
    "LEFT_ARM",
    "RIGHT_ARM",
    "LEFT_LEG",
    "RIGHT_LEG",
]

bleak_clients: list[Optional[BleakClient]] = [None] * len(DEVICES)

notification_queues = [Queue() for _ in DEVICES]

logger = logging.getLogger(__name__)

executor = concurrent.futures.ThreadPoolExecutor(max_workers=5)


def handle_notification(
    index: int, characteristic: BleakGATTCharacteristic, data: bytearray
):
    notification_queues[index].put((time.time(), data))
    # logger.info(f"Notified by {DEVICE_NAMES[index]}: {data.hex()}")


def thread_callback(callback, *args):
    # This function runs the callback in a separate thread
    loop = asyncio.get_event_loop()
    loop.run_in_executor(executor, callback, *args)


def disconnect_client(index: int, client: BleakClient):
    logger.info(f"Disconnected from {DEVICE_NAMES[index]}")
    bleak_clients[index] = None


CONNECTION_TIMEOUT = 8

async def add_client(index: int, device: BLEDevice):
    client = BleakClient(
        device, disconnected_callback=lambda bc: disconnect_client(index, bc)
    )

    try:
        # Connection can bug out sometimes
        async with asyncio.timeout(CONNECTION_TIMEOUT):
            await client.connect()

        logger.info(f"Successfully connected to {DEVICE_NAMES[index]}")

        # Handle notifications in a separate thread for each client
        await client.start_notify(
            CHARACTERISTIC_UUID,
            callback=lambda ch, d: thread_callback(handle_notification, index, ch, d),
        )

        # Add the client to the list
        bleak_clients[index] = client
    except TimeoutError:
        logger.error(f"Connection to {DEVICE_NAMES[index]} timed out")
        await client.disconnect()
        return
    except Exception as e:
        logger.error(f"Error connecting to {DEVICE_NAMES[index]}: {e}")
        await client.disconnect()
        return


# Thresholds in seconds
MAX_NOTIFICATION_INTERVAL = 0.150
DATA_VALIDITY_THRESHOLD = 0.800
SCAN_TIMEOUT = 2
SCAN_CHECK_INTERVAL = 0.2


async def check_and_reconnect():
    indices = [i for i in range(len(bleak_clients)) if bleak_clients[i] is None]
    addresses = [DEVICES[i] for i in indices]

    if len(indices) == 0:
        return
    
    logger.info(f"Scanning for {[DEVICE_NAMES[i] for i in indices]}...")
    
    scanned_devices = dict()
    
    try:
        async with asyncio.timeout(SCAN_TIMEOUT):
            async with BleakScanner() as scanner:
                while True:
                    for bd in scanner.discovered_devices:
                        print((bd.name, bd.address))
                        if bd.address in addresses:
                            scanned_devices[bd.address] = bd

                    await asyncio.sleep(SCAN_CHECK_INTERVAL)
    except TimeoutError:
        pass
        

    for bd in scanned_devices.values():
        await asyncio.sleep(1)  # Sleep for a while before connecting
        logger.info(f"Connecting to {bd.name} at {bd.address}")
        await add_client(indices[addresses.index(bd.address)], bd)
    
    # devices = await BleakScanner.discover(timeout=SCAN_TIMEOUT)
    
    # print('Scanned devices:')
    # print([(bd.name, bd.address) for bd in devices])
    # for bd in devices:
    #     if bd.address in addresses:
    #         await asyncio.sleep(1)  # Sleep for a while before connecting
    #         logger.info(f"Connecting to {bd.name} at {bd.address}")
    #         await add_client(indices[addresses.index(bd.address)], bd)


def combine_data_and_send():
    while True:
        # Get the latest notification from each queue
        latest_notifications = [
            q.queue[-1] if not q.empty() else None for q in notification_queues
        ]

        if any(n is None for n in latest_notifications):
            logger.warning("Skipping combined packet due to empty queues")
            return

        # Calculate the time difference between the latest notifications
        time_diff = max(n[0] for n in latest_notifications) - min(
            n[0] for n in latest_notifications
        )

        # While the time difference is greater than the threshold, use older values from the queue with the latest data
        if time_diff <= MAX_NOTIFICATION_INTERVAL:
            # Combine the data from the notifications
            last_i = 0
            try:
                combined_data = dict()
                for i, n in enumerate(latest_notifications):
                    last_i = i
                    combined_data[DEVICE_NAMES[i]] = msgpack.unpackb(n[1])

                # Send combined data to server Pi
                logger.info(f"Combined data: {combined_data}")

                combined_data_packed = msgpack.packb(combined_data)

                # Remove the notifications from the queues
                for q in notification_queues:
                    q.get()

                return
            except Exception as e:
                logger.error(f"Error unpacking data from {DEVICE_NAMES[last_i]}: {e}")
                return

        # Find the queue with the latest data
        max_queue = notification_queues[
            latest_notifications.index(max(latest_notifications, key=lambda n: n[0]))
        ]

        # If the current time minus the max is larger than a the data validity threshold, break the loop
        if time.time() - max_queue.queue[-1][0] > DATA_VALIDITY_THRESHOLD:
            logger.warning(
                f"Skipping combined packet due to large time difference ({time_diff}s)"
            )
            return

        # Remove the latest notification from the queue with the latest data
        max_queue.get()


async def main():
    while True:
        combine_data_and_send()
        await check_and_reconnect()
        await asyncio.sleep(1)  # Sleep for a while before checking again


if __name__ == "__main__":
    log_level = logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)-15s %(name)-8s %(levelname)s: %(message)s",
    )

    try:
        asyncio.run(main())
    except Exception as e:
        for i, client in enumerate(bleak_clients):
            if client is not None:
                disconnect_client(i, client)

        raise e