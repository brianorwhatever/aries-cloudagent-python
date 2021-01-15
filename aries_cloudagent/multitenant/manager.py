"""Manager for multitenancy."""

import logging
import jwt
from typing import List, Optional, cast

from ..core.profile import (
    Profile,
    ProfileSession,
)
from ..config.wallet import wallet_config
from ..config.injection_context import InjectionContext
from ..wallet.models.wallet_record import WalletRecord
from ..wallet.base import BaseWallet
from ..core.error import BaseError
from ..protocols.routing.v1_0.manager import RouteNotFoundError, RoutingManager
from ..protocols.routing.v1_0.models.route_record import RouteRecord
from ..transport.wire_format import BaseWireFormat
from ..storage.base import BaseStorage
from ..storage.error import StorageNotFoundError

from .error import WalletKeyMissingError


LOGGER = logging.getLogger(__name__)


class MultitenantManagerError(BaseError):
    """Generic multitenant error."""


class MultitenantManager:
    """Class for handling multitenancy."""

    def __init__(self, profile: Profile):
        """Initialize multitenant Manager.

        Args:
            profile: The profile for this manager
        """
        self._profile = profile
        if not profile:
            raise MultitenantManagerError("Missing profile")

        self._instances: dict[str, Profile] = {}

    @property
    def profile(self) -> Profile:
        """
        Accessor for the current profile.

        Returns:
            The profile for this manager

        """
        return self._profile

    async def _wallet_name_exists(
        self, session: ProfileSession, wallet_name: str
    ) -> bool:
        """
        Check whether wallet with specified wallet name already exists.

        Besides checking for wallet records, it will also check if the base wallet

        Args:
            session: The profile session to use
            wallet_name: the wallet name to check for

        Returns:
            bool: Whether the wallet name already exists

        """
        # wallet_name is same as base wallet name
        if session.settings.get("wallet.name") == wallet_name:
            return True

        # subwallet record exists, we assume the wallet actually exists
        wallet_records = await WalletRecord.query(session, {"wallet_name": wallet_name})
        if len(wallet_records) > 0:
            return True

        return False

    async def get_wallet_profile(
        self,
        base_context: InjectionContext,
        wallet_record: WalletRecord,
        extra_settings: dict = {},
        *,
        provision=False,
    ) -> Profile:
        """Get profile for a wallet record.

        Args:
            base_context: Base context to extend from
            wallet_record: Wallet record to get the context for
            extra_settings: Any extra context settings

        Returns:
            Profile: Profile for the wallet record

        """
        wallet_id = wallet_record.wallet_id

        if wallet_id not in self._instances:
            # Extend base context
            context = base_context.copy()

            # MTODO: take over or remove?
            # wallet.local_did

            # Settings to take over (for now)
            # wallet.replace_public_did
            # wallet.storage_type
            # walet.storage_config
            # wallet.storage_creds

            # Settings we don't want to use from base wallet
            reset_settings = {
                "wallet.recreate": False,
                "wallet.seed": None,
                "wallet.rekey": None,
                "wallet.name": None,
                "wallet.type": None,
            }

            dispatch_type = wallet_record.wallet_dispatch_type
            webhook_urls = wallet_record.wallet_webhook_urls
            base_webhook_urls = context.settings.get("admin.webhook_urls")
            if dispatch_type == "both":
                target_urls = list(set(base_webhook_urls) | set(webhook_urls))
                extra_settings["admin.webhook_urls"] = target_urls
            elif dispatch_type == "default":
                extra_settings["admin.webhook_urls"] = webhook_urls

            context.settings = (
                context.settings.extend(reset_settings)
                .extend(wallet_record.settings)
                .extend(extra_settings)
            )

            context.settings = context.settings.extend(wallet_record.settings).extend(
                extra_settings
            )

            # MTODO: add ledger config
            profile, _ = await wallet_config(context, provision=provision)
            self._instances[wallet_id] = profile

        return self._instances[wallet_id]

    async def create_wallet(
        self,
        settings: dict,
        key_management_mode: str,
    ) -> WalletRecord:
        """Create new wallet and wallet record.

        Args:
            settings: The context settings for this wallet
            key_management_mode: The mode to use for key management. Either "unmanaged"
                to not store the wallet key, or "managed" to store the wallet key

        Raises:
            MultitenantManagerError: If the wallet name already exists

        Returns:
            WalletRecord: The newly created wallet record

        """
        wallet_key = settings.get("wallet.key")
        wallet_name = settings.get("wallet.name")

        # base wallet context
        async with self.profile.session() as session:
            # Check if the wallet name already exists to avoid indy wallet errors
            if wallet_name and await self._wallet_name_exists(session, wallet_name):
                raise MultitenantManagerError(
                    f"Wallet with name {wallet_name} already exists"
                )

            # In unmanaged mode we don't want to store the wallet key
            if key_management_mode == WalletRecord.MODE_UNMANAGED:
                del settings["wallet.key"]
            # create and store wallet record
            wallet_record = WalletRecord(
                settings=settings, key_management_mode=key_management_mode
            )

            await wallet_record.save(session)

        # provision wallet
        profile = await self.get_wallet_profile(
            self.profile.context,
            wallet_record,
            {
                "wallet.key": wallet_key,
            },
            provision=True,
        )

        # subwallet context
        async with profile.session() as session:
            wallet = session.inject(BaseWallet)
            public_did_info = await wallet.get_public_did()

            if public_did_info:
                await self.add_wallet_route(
                    wallet_record.wallet_id, public_did_info.verkey, skip_if_exists=True
                )

        return wallet_record

    async def remove_wallet(self, wallet_id: str, wallet_key: str = None):
        """Remove the wallet with specified wallet id.

        Args:
            wallet_id: The wallet id of the wallet record
            wallet_key: The wallet key to open the wallet.
                Only required for "unmanaged" wallets

        Raises:
            WalletKeyMissingError: If the wallet key is missing.
                Only thrown for "unmanaged" wallets

        """
        async with self.profile.session() as session:
            wallet = cast(
                WalletRecord,
                await WalletRecord.retrieve_by_id(session, wallet_id),
            )

            wallet_key = wallet_key or wallet.wallet_key
            if wallet.requires_external_key and not wallet_key:
                raise WalletKeyMissingError("Missing key to open wallet")

            profile = await self.get_wallet_profile(
                self.profile.context,
                wallet,
                {"wallet.key": wallet_key},
            )

            del self._instances[wallet_id]
            await profile.remove()

            # Remove all routing records associated with wallet
            storage = session.inject(BaseStorage)
            await storage.delete_all_records(
                RouteRecord.RECORD_TYPE, {"wallet_id": wallet.wallet_id}
            )

            await wallet.delete_record(session)

    async def add_wallet_route(
        self, wallet_id: str, recipient_key: str, *, skip_if_exists: bool = False
    ):
        """
        Add a wallet route to map incoming messages to specific subwallets.

        Args:
            wallet_id: The wallet id the key corresponds to
            recipient_key: The recipient key belonging to the wallet
        """

        async with self.profile.session() as session:
            LOGGER.info(
                f"Add route record for recipient {recipient_key} to wallet {wallet_id}"
            )
            routing_mgr = RoutingManager(session)

            if skip_if_exists:
                try:
                    await RouteRecord.retrieve_by_recipient_key(session, recipient_key)

                    # If no error is thrown, it means there is already a record
                    return
                except (StorageNotFoundError):
                    pass

            await routing_mgr.create_route_record(
                recipient_key=recipient_key, internal_wallet_id=wallet_id
            )

    def create_auth_token(
        self, wallet_record: WalletRecord, wallet_key: str = None
    ) -> str:
        """Create JWT auth token for specified wallet record.

        Args:
            wallet_record: The wallet record to create the token for
            wallet_key: The wallet key to include in the token.
                Only required for "unmanaged" wallets

        Raises:
            WalletKeyMissingError: If the wallet key is missing.
                Only thrown for "unmanaged" wallets

        Returns:
            str: JWT auth token

        """

        jwt_payload = {"wallet_id": wallet_record.wallet_id}
        jwt_secret = self.profile.settings.get("multitenant.jwt_secret")

        if wallet_record.requires_external_key:
            if not wallet_key:
                raise WalletKeyMissingError()

            jwt_payload["wallet_key"] = wallet_key

        token = jwt.encode(jwt_payload, jwt_secret, algorithm="HS256").decode()

        return token

    async def get_profile_for_token(
        self, context: InjectionContext, token: str
    ) -> Profile:
        """Get the profile associated with a JWT header token.

        Args:
            context: The context to use for profile creation
            token: The token

        Raises:
            WalletKeyMissingError: If the wallet_key is missing for an unmanaged wallet
            InvalidTokenError: If there is an exception while decoding the token

        Returns:
            Profile associated with the token

        """
        jwt_secret = self.profile.context.settings.get("multitenant.jwt_secret")
        extra_settings = {}

        token_body = jwt.decode(token, jwt_secret, algorithms=["HS256"])

        wallet_id = token_body.get("wallet_id")
        wallet_key = token_body.get("wallet_key")

        async with self.profile.session() as session:
            wallet = await WalletRecord.retrieve_by_id(session, wallet_id)

            if wallet.requires_external_key:
                if not wallet_key:
                    raise WalletKeyMissingError()

                extra_settings["wallet.key"] = wallet_key

            profile = await self.get_wallet_profile(context, wallet, extra_settings)

            return profile

    async def _get_wallet_by_key(
        self, session: ProfileSession, recipient_key: str
    ) -> Optional[WalletRecord]:
        """Get the wallet record associated with the recipient key.

        Args:
            session: The profile session to use
            recipient_key: The recipient key
        Returns:
            Wallet record associated with the recipient key
        """
        routing_mgr = RoutingManager(session)

        try:
            routing_record = await routing_mgr.get_recipient(recipient_key)
            wallet = await WalletRecord.retrieve_by_id(
                session, routing_record.wallet_id
            )

            return wallet
        except (RouteNotFoundError):
            pass

    async def get_wallets_by_message(
        self, message_body, wire_format: BaseWireFormat = None
    ) -> List[WalletRecord]:
        """Get the wallet records associated with the message boy.

        Args:
            message_body: The body of the message
            wire_format: Wire format to use for recipient detection

        Returns:
            Wallet records associated with the message body

        """
        async with self.profile.session() as session:
            wire_format = wire_format or session.inject(BaseWireFormat)

            recipient_keys = wire_format.get_recipient_keys(message_body)
            wallets = []

            for key in recipient_keys:
                wallet = await self._get_wallet_by_key(session, key)

                if wallet:
                    wallets.append(wallet)

            return wallets
