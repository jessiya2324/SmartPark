from asgiref.sync import async_to_sync, sync_to_async
from channels.testing import WebsocketCommunicator
from django.test import SimpleTestCase

from bookings.services import broadcast_availability
from smartpark.asgi import application


class AvailabilityConsumerTests(SimpleTestCase):
    def test_websocket_handshake_and_availability_broadcast(self):
        async_to_sync(self._connect_and_receive_availability_update)()

    async def _connect_and_receive_availability_update(self):
        communicator = WebsocketCommunicator(application, "/ws/availability/")
        connected, _ = await communicator.connect()
        self.assertTrue(connected)

        await sync_to_async(broadcast_availability)(42)
        payload = {"slot_id": 42, "updated": True}
        self.assertEqual(await communicator.receive_json_from(), payload)
        await communicator.disconnect()
