from hashlib import sha256
from typing import Literal
from pydantic import SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from app.brokers.errors import BrokerAuthenticationError, UnsafeEnvironmentError, UnsupportedBrokerFeature


TH_TEST_API_HOST = "th-api.uat.webullbroker.com"
TH_TEST_EVENTS_HOST = "th-events-api.uat.webullbroker.com"


class WebullTestConfig(BaseSettings):
    """Thailand OpenAPI test-only configuration. Production execution is not representable."""
    model_config = SettingsConfigDict(env_file=".env", env_prefix="WEBULL_", extra="ignore", frozen=True)
    app_key: SecretStr
    app_secret: SecretStr
    account_id: SecretStr
    broker: Literal["webull"] = "webull"
    region: Literal["th"] = "th"
    environment: Literal["test"] = "test"
    endpoint: Literal[TH_TEST_API_HOST] = TH_TEST_API_HOST
    events_endpoint: Literal[TH_TEST_EVENTS_HOST] = TH_TEST_EVENTS_HOST

    @field_validator("app_key", "app_secret", "account_id")
    @classmethod
    def nonempty_secret(cls, value: SecretStr):
        raw = value.get_secret_value().strip()
        if not raw or raw.startswith("replace-"):
            raise BrokerAuthenticationError("Webull Thailand test credentials are required")
        return value

    @property
    def account_ref(self) -> str:
        return sha256(self.account_id.get_secret_value().encode()).hexdigest()

    def diagnostics(self) -> dict[str, str]:
        return {"broker": self.broker, "region": self.region, "environment": self.environment,
                "endpoint": self.endpoint, "events_endpoint": self.events_endpoint,
                "app_key": "********", "app_secret": "********", "account_id": "********",
                "account_ref": self.account_ref}

    @classmethod
    def from_env(cls, env_file=".env"):
        try:
            return cls(_env_file=env_file)
        except ValidationError as exc:
            raise BrokerAuthenticationError("Valid Webull Thailand test credentials/configuration are required") from exc


# Backward-compatible import name; semantics are now the official Thailand Test environment.
WebullSandboxConfig = WebullTestConfig


class WebullSandboxAdapter:
    """Fail-closed Thailand test boundary: Phase 2 contains no SDK client or network transport."""
    def __init__(self, config: WebullTestConfig):
        self.config = config

    async def submit_order(self, intent):
        raise UnsupportedBrokerFeature("Webull Thailand test submission remains disabled until an explicit opt-in SDK test")

    async def cancel_order(self, internal_order_id):
        raise UnsupportedBrokerFeature("Webull Thailand test cancellation is disabled")

    async def query_order(self, internal_order_id):
        raise UnsupportedBrokerFeature("Webull SDK transport is not installed")

    async def query_open_orders(self): return await self._disabled()
    async def query_order_history(self, **kwargs): return await self._disabled()
    async def query_fills(self): return await self._disabled()
    async def query_positions(self): return await self._disabled()
    async def query_account_state(self): return await self._disabled()
    async def recover(self): return await self._disabled()

    async def _disabled(self):
        raise UnsupportedBrokerFeature("Webull SDK transport is not installed")
