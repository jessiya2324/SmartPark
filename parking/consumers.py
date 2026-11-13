import logging

from channels.generic.websocket import AsyncJsonWebsocketConsumer

logger = logging.getLogger(__name__)


class AvailabilityConsumer(AsyncJsonWebsocketConsumer):
    async def connect(self):
        self.group_name = "parking_availability"
        try:
            await self.channel_layer.group_add(self.group_name, self.channel_name)
            await self.accept()
        except Exception:
            logger.exception("Availability WebSocket connection failed")
            raise
        logger.info("Availability WebSocket connected")

    async def disconnect(self, close_code):
        try:
            await self.channel_layer.group_discard(self.group_name, self.channel_name)
        except Exception:
            logger.exception("Availability WebSocket disconnection failed")
            raise
        logger.info("Availability WebSocket disconnected (close_code=%s)", close_code)

    async def receive_json(self, content, **kwargs):
        logger.warning("Ignoring client message on server-push-only availability WebSocket")

    async def availability_update(self, event):
        try:
            await self.send_json(event["payload"])
        except Exception:
            logger.exception("Failed to send parking availability update")
            raise
        logger.info("Sent parking availability update to connected client")
