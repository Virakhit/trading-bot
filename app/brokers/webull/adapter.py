from hashlib import sha256
from pydantic import SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from app.brokers.errors import BrokerAuthenticationError, UnsafeEnvironmentError, UnsupportedBrokerFeature


SANDBOX_API_HOST = "api.sandbox.webull.com"
SANDBOX_EVENTS_HOST = "events-api.sandbox.webull.com"


class WebullSandboxConfig(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="WEBULL_", extra="ignore", frozen=True)
    app_key: SecretStr
    app_secret: SecretStr
    account_id: SecretStr
    region: str = "us"
    endpoint: str = SANDBOX_API_HOST
    events_endpoint: str = SANDBOX_EVENTS_HOST

    @field_validator("app_key", "app_secret", "account_id")
    @classmethod
    def nonempty_secret(cls, value: SecretStr):
        raw = value.get_secret_value().strip()
        if not raw or raw.startswith("replace-"):
            raise BrokerAuthenticationError("Webull sandbox credentials are required")
        return value

    @field_validator("region")
    @classmethod
    def official_region(cls, value: str):
        if value != "us":
            raise UnsafeEnvironmentError("Only the documented US sandbox region is accepted")
        return value

    @field_validator("endpoint")
    @classmethod
    def sandbox_only(cls, value: str):
        if value != SANDBOX_API_HOST:
            raise UnsafeEnvironmentError("Only the exact Webull sandbox API host is accepted")
        return value

    @field_validator("events_endpoint")
    @classmethod
    def sandbox_events_only(cls, value: str):
        if value != SANDBOX_EVENTS_HOST:
            raise UnsafeEnvironmentError("Only the exact Webull sandbox events host is accepted")
        return value

    @property
    def account_ref(self) -> str:
        return sha256(self.account_id.get_secret_value().encode()).hexdigest()

    def diagnostics(self) -> dict[str, str]:
        return {"region": self.region, "endpoint": self.endpoint, "events_endpoint": self.events_endpoint,
                "app_key": "********", "app_secret": "********", "account_id": "********", "account_ref": self.account_ref}

    @classmethod
    def from_env(cls, env_file=".env"):
        try:
            return cls(_env_file=env_file)
        except ValidationError as exc:
            raise BrokerAuthenticationError("Valid Webull sandbox credentials are required") from exc


class WebullSandboxAdapter:
    """Fail-closed boundary: Phase 2 contains no SDK client or network transport."""
    def __init__(self, config: WebullSandboxConfig):
        self.config = config

    async def submit_order(self, intent):
        raise UnsupportedBrokerFeature("Webull sandbox submission is disabled until an opt-in SDK test proves account isolation")

    async def cancel_order(self, internal_order_id):
        raise UnsupportedBrokerFeature("Webull sandbox cancellation is disabled")

    async def query_order(self, internal_order_id):
        raise UnsupportedBrokerFeature("Webull SDK transport is not installed")

    async def query_open_orders(self): return await self._disabled()
    async def query_fills(self): return await self._disabled()
    async def query_positions(self): return await self._disabled()
    async def query_account_state(self): return await self._disabled()
    async def recover(self): return await self._disabled()

    async def _disabled(self):
        raise UnsupportedBrokerFeature("Webull SDK transport is not installed")
