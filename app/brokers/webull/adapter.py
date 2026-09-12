import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
from typing import Literal
from uuid import NAMESPACE_URL, uuid5
from pydantic import SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from app.brokers.errors import BrokerAuthenticationError, OrderRejected, UnsafeEnvironmentError
from app.brokers.models import AccountState, BrokerEvent, BrokerFill, BrokerOrderHistoryPage, BrokerOrderIntent, BrokerOrderView, OrderState

TH_TEST_API_HOST = "th-api.uat.webullbroker.com"
TH_TEST_EVENTS_HOST = "th-events-api.uat.webullbroker.com"


class WebullTestConfig(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="WEBULL_TEST_", extra="ignore", frozen=True)
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
    def nonempty(cls, value):
        if not value.get_secret_value().strip() or value.get_secret_value().startswith("replace-"):
            raise BrokerAuthenticationError("Webull Thailand TEST credentials are required")
        return value

    @property
    def account_ref(self):
        return sha256(self.account_id.get_secret_value().encode()).hexdigest()

    def diagnostics(self):
        return {"broker": "webull", "region": "th", "environment": "test", "endpoint": self.endpoint,
                "events_endpoint": self.events_endpoint, "app_key": "********", "app_secret": "********",
                "account_id": "********", "account_ref": self.account_ref}

    @classmethod
    def from_env(cls, env_file=".env"):
        try:
            return cls(_env_file=env_file)
        except ValidationError as exc:
            raise BrokerAuthenticationError("Valid Webull Thailand TEST credentials/configuration are required") from exc


def map_webull_order_status(value):
    value = str(value or "").strip().upper().replace(" ", "_")
    return {"SUBMITTED": OrderState.ACKNOWLEDGED, "PENDING": OrderState.SUBMITTED,
            "PENDING_SUBMIT": OrderState.SUBMITTING, "PARTIAL_FILLED": OrderState.PARTIALLY_FILLED,
            "PARTIALLY_FILLED": OrderState.PARTIALLY_FILLED, "FILLED": OrderState.FILLED,
            "CANCEL_PENDING": OrderState.CANCEL_PENDING, "PENDING_CANCEL": OrderState.CANCEL_PENDING,
            "CANCELLED": OrderState.CANCELLED, "CANCELED": OrderState.CANCELLED,
            "FAILED": OrderState.REJECTED, "REJECTED": OrderState.REJECTED,
            "EXPIRED": OrderState.EXPIRED}.get(value, OrderState.UNKNOWN)


class WebullTestAdapter:
    """Thin official-SDK adapter; only Thailand TEST hosts are representable."""
    def __init__(self, config, *, trade_client=None, resolver=None):
        self.config, self.client, self.attested, self.resolver = config, trade_client, False, resolver
        self.bindings, self.reverse = {}, {}
        self._boundary()

    def _boundary(self):
        if (self.config.region, self.config.environment, self.config.endpoint, self.config.events_endpoint) != ("th", "test", TH_TEST_API_HOST, TH_TEST_EVENTS_HOST):
            raise UnsafeEnvironmentError("Only Webull Thailand TEST endpoints are permitted")

    def _client(self):
        if self.client is None:
            from webull.core.client import ApiClient
            from webull.trade.trade_client import TradeClient
            api = ApiClient(self.config.app_key.get_secret_value(), self.config.app_secret.get_secret_value(), "th")
            api.add_endpoint("th", TH_TEST_API_HOST)
            api._stream_logger_set = api._file_logger_set = True
            self.client = TradeClient(api)
        return self.client

    def bind_order(self, internal_order_id, client_order_id):
        self.bindings[internal_order_id] = client_order_id
        self.reverse[client_order_id] = internal_order_id

    async def _call(self, fn, *args):
        return await asyncio.to_thread(fn, *args)

    async def attest_account(self):
        self._boundary()
        rows = self._items(await self._call(self._client().account_v2.get_account_list))
        account = self.config.account_id.get_secret_value()
        if account not in {str(x.get("account_id") or x.get("id")) for x in rows}:
            raise BrokerAuthenticationError("Configured TEST account was not returned by Webull")
        self.attested = True

    async def submit_order(self, intent):
        if not self.attested: await self.attest_account()
        self.bind_order(intent.internal_order_id, intent.client_order_id)
        item = {"combo_type": "NORMAL", "client_order_id": intent.client_order_id, "symbol": intent.symbol,
                "instrument_type": "EQUITY", "market": "US", "order_type": intent.order_type,
                "quantity": str(intent.quantity), "side": intent.side, "time_in_force": "DAY",
                "entrust_type": "QTY", "support_trading_session": "CORE"}
        if intent.limit_price is not None: item["limit_price"] = str(intent.limit_price)
        data = self._data(await self._call(self._client().order_v3.place_order, self._account(), [item]))
        return self._event(data, intent.internal_order_id, intent.client_order_id, OrderState.SUBMITTED)

    async def cancel_order(self, internal_order_id):
        if not self.attested: await self.attest_account()
        client_id = self.bindings.get(internal_order_id)
        if client_id is None and self.resolver:
            client_id = self.resolver.client_id(internal_order_id)
        if not client_id: raise OrderRejected("Order is not bound")
        data = self._data(await self._call(self._client().order_v3.cancel_order, self._account(), client_id))
        return self._event(data, internal_order_id, client_id, OrderState.CANCEL_PENDING)

    async def query_order(self, internal_order_id):
        client_id = self.bindings.get(internal_order_id)
        if client_id is None and self.resolver:
            client_id = self.resolver.client_id(internal_order_id)
        return None if not client_id else await self.query_order_by_client_id(client_id)

    async def query_order_by_client_id(self, client_id):
        response = await self._call(self._client().order_v3.get_order_detail, self._account(), client_id)
        rows, data = self._items(response), self._data(response)
        return self._view(rows[0] if rows else data, client_id) if rows or data else None

    async def query_open_orders(self):
        response = await self._call(self._client().order_v3.list_order_open, self._account())
        return [self._view(x, allow_unresolved=True) for x in self._items(response)]

    async def query_order_history(self, *, start_time, end_time, cursor=None, limit=100):
        if start_time.tzinfo is None or end_time.tzinfo is None or start_time >= end_time or not 1 <= limit <= 500:
            raise ValueError("History requires an aware bounded window and limit 1..500")
        fmt = lambda x: x.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "+0000"
        response = await self._call(self._client().order_v3.list_order_history, self._account(), fmt(start_time), fmt(end_time), cursor)
        # Webull owns page sizing; never slice a returned page before following its cursor.
        return BrokerOrderHistoryPage(orders=[self._view(x, allow_unresolved=True) for x in self._items(response)], next_cursor=self._data(response).get("pagination_key"))

    async def query_fills(self):
        response = await self._call(self._client().order_v3.list_order_executions, self._account())
        result = []
        for x in self._items(response):
            client_id = str(x.get("client_order_id", "")); stamp = self._time(x.get("timestamp") or x.get("filled_time"))
            fill = BrokerFill(execution_id=str(x["execution_id"]), quantity=x.get("quantity") or x.get("filled_qty"),
                              price=x.get("price") or x.get("filled_price"), fee=x.get("fee") or x.get("actual_commission") or 0, timestamp=stamp)
            resolved = self.reverse.get(client_id) or (self.resolver.resolve(client_id) if self.resolver else None)
            result.append(BrokerEvent(event_id=f"webull:execution:{fill.execution_id}",
                                      internal_order_id=resolved or "unmapped:" + client_id,
                                      broker_order_id=x.get("order_id"), state=OrderState.PARTIALLY_FILLED, timestamp=stamp, source="webull-rest", fill=fill))
        return result

    async def query_positions(self):
        response = await self._call(self._client().account_v2.get_account_position, self._account())
        return {str(x["symbol"]): Decimal(str(x.get("quantity") or x.get("qty") or 0)) for x in self._items(response)}

    async def query_account_state(self):
        await self.attest_account()
        data = self._data(await self._call(self._client().account_v2.get_account_balance, self._account()))
        cash = data.get("cash_balance") or data.get("cash") or data.get("available_funds") or 0
        return AccountState(account_ref=self.config.account_ref, environment="test", cash=cash,
                            positions=await self.query_positions(), timestamp=datetime.now(timezone.utc))

    async def recover(self): return await self.query_fills()
    def _account(self): return self.config.account_id.get_secret_value()

    def _view(self, x, fallback="", allow_unresolved=False):
        client = str(x.get("client_order_id") or fallback)
        resolved = self.reverse.get(client) or (self.resolver.resolve(client) if self.resolver else None)
        if resolved is None:
            if not allow_unresolved: raise LookupError(f"LOCAL_MISSING client_order_id={client}")
            resolved = "unmapped:" + client
        return BrokerOrderView(internal_order_id=resolved, client_order_id=client,
            broker_order_id=str(x["order_id"]) if x.get("order_id") is not None else None,
            state=map_webull_order_status(x.get("order_status") or x.get("status")), symbol=str(x.get("symbol", "UNKNOWN")),
            side=str(x.get("side", "BUY")).upper(), quantity=x.get("quantity") or x.get("qty") or 0,
            filled_quantity=x.get("filled_quantity") or x.get("filled_qty") or 0)

    def _event(self, x, internal, client, default):
        state = map_webull_order_status(x.get("order_status") or x.get("status"))
        if state == OrderState.UNKNOWN and not (x.get("order_status") or x.get("status")): state = default
        key = str(x.get("request_id") or x.get("order_id") or client) + ":" + state
        return BrokerEvent(event_id=str(uuid5(NAMESPACE_URL, key)), internal_order_id=internal,
            broker_order_id=str(x["order_id"]) if x.get("order_id") is not None else None, state=state,
            timestamp=self._time(x.get("timestamp")), source="webull-rest", reason=x.get("message"))

    @staticmethod
    def _data(response):
        data = response.json() if hasattr(response, "json") else response
        return data if isinstance(data, dict) else {}

    @classmethod
    def _items(cls, response):
        data = response.json() if hasattr(response, "json") else response
        if isinstance(data, list): return data
        if not isinstance(data, dict): return []
        for key in ("data", "orders", "items", "accounts", "positions", "executions"):
            value = data.get(key)
            if isinstance(value, list): return value
            if isinstance(value, dict):
                for nested in ("orders", "items", "accounts", "positions", "executions"):
                    if isinstance(value.get(nested), list): return value[nested]
        return []

    @staticmethod
    def _time(value):
        if not value: return datetime.now(timezone.utc)
        if isinstance(value, (int, float)): return datetime.fromtimestamp(value / 1000 if value > 10_000_000_000 else value, timezone.utc)
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


WebullSandboxConfig = WebullTestConfig
WebullSandboxAdapter = WebullTestAdapter
