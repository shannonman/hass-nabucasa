"""Manage remote UI connections."""
import logging
import ssl
from typing import Optional

import async_timeout
from homeassistant.util.ssl import server_context_modern
from snitun.utils.aes import generate_aes_keyset
from snitun.utils.aiohttp_client import SniTunClientAioHttp

from . import cloud_api
from .acme import AcmeHandler

_LOGGER = logging.getLogger(__name__)


class RemoteError(Exception):
    """General remote error."""


class RemoteBackendError(RemoteError):
    """Backend problem with nabucasa API."""


class RemoteNotConnected(RemoteError):
    """Raise if a request need connection and we are not ready."""


class RemoteUI:
    """Class to help manage remote connections."""

    def __init__(self, cloud):
        """Initialize cloudhooks."""
        self.cloud = cloud
        self._acme = None
        self._snitun = None
        self._token = None
        self._snitun_server = None

        # Register start/stop
        cloud.iot.register_on_connect(self.load_backend)
        cloud.iot.register_on_disconnect(self.close_backend)

    @property
    def snitun_server(self) -> Optional[str]:
        """Return connected snitun server."""
        return self._snitun_server

    async def _create_context(self) -> ssl.SSLContext:
        """Create SSL context with acme certificate."""
        context = server_context_modern()

        await self.cloud.run_executor(
            context.load_cert_chain,
            self._acme.path_fullchain,
            self._acme.path_private_key,
        )

        return context

    async def load_backend(self) -> None:
        """Load backend details."""
        if self._snitun:
            return

        # Load instance data from backend
        async with async_timeout.timeout(10):
            resp = await cloud_api.async_remote_register(self.cloud)

        if resp.status != 200:
            _LOGGER.error("Can't update remote details from Home Assistant cloud")
            raise RemoteBackendError()
        data = await resp.json()

        # Set instance details for certificate
        self._acme = AcmeHandler(self.cloud, data["domain"], data["email"])

        # Issue a certificate
        if not await self._acme.is_valid_certificate():
            await self._acme.issue_certificate()

        context = await self._create_context()
        self._snitun = SniTunClientAioHttp(
            self.cloud.client.aiohttp_runner,
            context,
            snitun_server=data["server"],
            snitun_port=443,
        )
        self._snitun_server = data["server"]

        await self._snitun.start()

    async def close_backend(self) -> None:
        """Close connections and shutdown backend."""
        if self._snitun:
            await self._snitun.stop()

        self._snitun = None
        self._acme = None

    async def handle_connection_requests(self, caller_ip):
        """Handle connection requests."""
        if not self._snitun or self._token:
            raise RemoteNotConnected()

        # Generate session token
        aes_key, aes_iv = generate_aes_keyset()
        async with async_timeout.timeout(10):
            resp = await cloud_api.async_remote_token(self.cloud, aes_key, aes_iv)

        if resp.status != 200:
            _LOGGER.error("Can't register a snitun token by server")
            raise RemoteBackendError()
        data = await resp.json()

        await self._snitun.connect(data["token"], aes_key, aes_iv)
