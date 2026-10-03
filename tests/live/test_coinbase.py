from __future__ import annotations

import copy
import inspect
import json
from importlib import metadata
from pathlib import Path

import pytest
import requests

from ewbot.live.coinbase import API, CoinbaseExchange, fmt_decimal, map_error, parse_order
from ewbot.live.exchange import ExchangeError, Position, RateLimited, Unavailable

FIXTURES = json.loads((Path(__file__).parent / "fixtures" / "coinbase.json").read_text(encoding="utf-8"))
FUT = "BIP-20DEC30-CDE"


def http_error(status: int, text: str = "", headers: dict | None = None) -> requests.exceptions.HTTPError:
    resp = requests.Response()
    resp.status_code = status
    resp._content = text.encode()
    resp.headers.update(headers or {})
    return requests.exceptions.HTTPError(f"{status}", response=resp)


class RecordedClient:
    """Stands in for coinbase.rest.RESTClient: answers get/post from recorded JSON by (method, path).
    An answer may be a fixture name, a list (consumed in order; exceptions are raised) or a callable."""

    def __init__(self, routes: dict) -> None:
        self.routes = routes
        self.calls: list[tuple] = []

    def _answer(self, key, arg):
        ans = self.routes[key]
        if isinstance(ans, list):
            ans = ans.pop(0)
        if callable(ans) and not isinstance(ans, BaseException):
            ans = ans(arg)
        if isinstance(ans, BaseException):
            raise ans
        return copy.deepcopy(FIXTURES[ans] if isinstance(ans, str) else ans)

    def get(self, url_path, params=None, public=False, **kwargs):
        self.calls.append(("GET", url_path, params, public))
        return self._answer(("GET", url_path), params)

    def post(self, url_path, params=None, data=None, **kwargs):
        self.calls.append(("POST", url_path, data))
        return self._answer(("POST", url_path), data)


def make(routes: dict, **kw) -> tuple[CoinbaseExchange, RecordedClient, list[float]]:
    client = RecordedClient({(m, API + p): v for (m, p), v in routes.items()})
    slept: list[float] = []
    ex = CoinbaseExchange(client, FUT, "BTC-USD", sleep=slept.append, clock=lambda: 1_790_000_000.0, **kw)
    return ex, client, slept


def test_product_spec_and_session_from_the_recorded_product():
    ex, client, _ = make({("GET", f"/products/{FUT}"): "product_bip"})
    spec = ex.product()
    assert (spec.product_id, spec.contract_size, spec.price_increment, spec.base_increment) == (FUT, 0.01, 5.0, 1.0)
    assert spec.overnight_margin == (0.21025, 0.256625) and spec.intraday_margin == (0.100079, 0.0999811)
    s = ex.session()
    assert s.state == "OPEN" and s.is_open
    assert (s.maintenance_start, s.maintenance_end) == (1792836000, 1792868400)  # 2026-10-24 10:00-19:00 UTC


def test_quotes_come_from_the_public_order_book():
    ex, client, _ = make({("GET", "/market/product_book"): lambda p: "book_bip" if p["product_id"] == FUT else "book_btc"})
    q, s = ex.quote(FUT), ex.quote("BTC-USD")
    assert (q.bid, q.ask, q.mid) == (84655.0, 84680.0, 84667.5)
    assert s.mid == pytest.approx(84671.265)
    assert client.calls[0] == ("GET", API + "/market/product_book", {"product_id": FUT, "limit": 1}, True)


def test_position_picks_the_bip_product():
    ex, _, _ = make({("GET", "/cfm/positions"): ["positions", "positions_flat"]})
    assert ex.position() == Position(-1, 3, 85100.0)
    assert ex.position() is None


def test_balance_is_cash_plus_unrealized_pnl_and_permissions():
    ex, _, _ = make({("GET", "/cfm/balance_summary"): "balance_summary", ("GET", "/key_permissions"): "key_permissions"})
    b = ex.balance()
    assert b.equity == pytest.approx(5013.05) and b.available_margin == pytest.approx(4361.25)
    k = ex.key_permissions()
    assert (k.can_view, k.can_trade, k.can_transfer, k.portfolio_type) == (True, True, False, "DEFAULT")


def test_open_orders_follow_the_cursor_and_parse_brackets():
    ex, client, _ = make({("GET", "/orders/historical/batch"): ["orders_open_page1", "orders_open_page2"]})
    orders = ex.open_orders()
    assert [(o.client_order_id, o.kind, o.size, o.limit_price, o.stop_price, o.status) for o in orders] == [
        ("ewb-b-1790942400-tp1-1", "BRACKET", 1, 83500.0, 85900.0, "OPEN"),
        ("ewb-b-1790942400-tp2-1", "BRACKET", 2, 82000.0, 85900.0, "OPEN"),  # CANCEL_QUEUED may still fill
    ]
    assert orders[0].side == 1 and orders[0].created_ts == 1791028841
    assert client.calls[0][2]["product_ids"] == [FUT] and client.calls[0][2]["order_status"] == ["OPEN"]
    assert client.calls[1][2]["cursor"] == "c-2"


def test_find_and_get_order():
    ex, client, _ = make({("GET", "/orders/historical/batch"): "orders_history",
                          ("GET", "/orders/historical/22222222-bbbb-4000-8000-000000000001"): "order_get"})
    o = ex.find_order("ewb-e-1790942400-S", since_ts=1_791_028_000)
    assert o is not None and (o.kind, o.filled, o.avg_fill_price, o.status, o.side) == ("MARKET", 3, 85100.0, "FILLED", -1)
    assert client.calls[0][2]["start_date"] == "2026-10-03T11:46:40Z"
    assert ex.find_order("nope", since_ts=0) is None
    got = ex.get_order("22222222-bbbb-4000-8000-000000000001")
    assert (got.filled, got.fee) == (3, 0.45)


def test_market_and_bracket_payloads():
    ex, client, _ = make({("POST", "/orders"): ["create_ok", "create_ok"]})
    r = ex.place_market("ewb-e-1790942400-S", -1, 3)
    assert r.ok and r.order_id == "33333333-cccc-4000-8000-000000000001"
    assert client.calls[0][2] == {"client_order_id": "ewb-e-1790942400-S", "product_id": FUT, "side": "SELL",
                                  "order_configuration": {"market_market_ioc": {"base_size": "3"}}}
    ex.place_bracket("ewb-b-1790942400-tp1-1", 1, 1, 83500.0, 85900.0)
    assert client.calls[1][2]["order_configuration"] == {
        "trigger_bracket_gtc": {"base_size": "1", "limit_price": "83500", "stop_trigger_price": "85900"}}
    with pytest.raises(ValueError):
        ex.place_market("x", 0, 1)


def test_rejections_and_duplicates_are_results_not_exceptions():
    ex, _, _ = make({("POST", "/orders"): ["create_insufficient", "create_duplicate"]})
    r = ex.place_market("a", 1, 1)
    assert not r.ok and r.error.startswith("INSUFFICIENT_FUND") and not r.duplicate
    d = ex.place_market("a", 1, 1)
    assert not d.ok and d.duplicate


def test_close_position_edit_cancel_and_preview():
    ex, client, _ = make({
        ("POST", "/orders/close_position"): "close_ok",
        ("POST", "/orders/edit"): ["edit_ok", http_error(400, "edit not supported")],
        ("POST", "/orders/batch_cancel"): "cancel_ok",
        ("POST", "/orders/preview"): ["preview_ok", "preview_err"],
    })
    assert ex.close_position("ewb-x-1790942400-1", size=2).ok
    assert client.calls[0][2] == {"client_order_id": "ewb-x-1790942400-1", "product_id": FUT, "size": "2"}
    assert ex.edit_bracket("o1", 2, 82000.0, 85000.0) is True
    assert client.calls[1][2] == {"order_id": "o1", "size": "2", "price": "82000", "stop_price": "85000"}
    assert ex.edit_bracket("o1", 2, 82000.0, 85000.0) is False  # unsupported -> the executor replaces
    ex.cancel(["11111111-aaaa-4000-8000-000000000001"])
    ex.cancel([])
    assert [c[1] for c in client.calls].count(API + "/orders/batch_cancel") == 1
    ok = ex.preview_market(1, 1)
    bad = ex.preview_bracket(-1, 1, 83000.0, 86000.0)
    assert ok.ok and not bad.ok and bad.errors == ("PREVIEW_INSUFFICIENT_FUNDS_FOR_FUTURES",)


def test_reads_retry_429_and_5xx_then_give_up():
    ex, client, slept = make({("GET", "/cfm/positions"): [http_error(429, "slow down", {"Retry-After": "2"}),
                                                            http_error(503, "busy"), "positions"]})
    assert ex.position().contracts == 3 and slept == [2.0, 2.0]
    ex2, _, _ = make({("GET", "/cfm/positions"): [requests.exceptions.ConnectionError("x")] * 3})
    with pytest.raises(Unavailable):
        ex2.position()
    ex3, c3, _ = make({("GET", "/cfm/positions"): [http_error(401, "unauthorized"), "positions"]})
    with pytest.raises(ExchangeError, match="HTTP 401"):
        ex3.position()
    assert len(c3.calls) == 1  # a 4xx is final


def test_order_placement_is_sent_once():
    ex, client, _ = make({("POST", "/orders"): [http_error(502, "bad gateway"), "create_ok"]})
    with pytest.raises(Unavailable):
        ex.place_market("ewb-e-1-L", 1, 1)
    assert len(client.calls) == 1  # ambiguous: the executor looks the order up before resending


def test_rate_limit_headers_pace_the_next_request():
    resp = dict(FIXTURES["positions"], **{"x-ratelimit-remaining": "1", "x-ratelimit-reset": "3"})
    ex, _, slept = make({("GET", "/cfm/positions"): [resp, "positions"]})
    ex.position()
    ex.position()
    assert slept == [3.0]


def test_helpers():
    assert fmt_decimal(84665.0) == "84665" and fmt_decimal(0.5) == "0.5" and fmt_decimal(85000) == "85000"
    assert isinstance(map_error(http_error(429)), RateLimited)
    assert isinstance(map_error(requests.exceptions.Timeout()), Unavailable)
    o = parse_order({"order_id": "x", "side": "SELL", "status": "OPEN", "order_configuration": {}})
    assert (o.kind, o.size, o.client_order_id) == ("OTHER", 0, "")


def test_the_pinned_sdk_matches_the_calls_this_adapter_makes(monkeypatch):
    """Runs only with the `live` dependency group installed (uv run --group live pytest)."""
    rest = pytest.importorskip("coinbase.rest")
    ws = pytest.importorskip("coinbase.websocket")
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    assert metadata.version("coinbase-advanced-py") == "1.8.4"
    init = inspect.signature(rest.RESTClient.__init__).parameters
    assert {"key_file", "timeout", "rate_limit_headers"} <= set(init)
    assert {"url_path", "params", "public"} <= set(inspect.signature(rest.RESTClient.get).parameters)
    assert {"url_path", "data"} <= set(inspect.signature(rest.RESTClient.post).parameters)
    for name in ("open", "subscribe", "close"):
        assert callable(getattr(ws.WSClient, name))

    pem = ec.generate_private_key(ec.SECP256R1()).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()
    ).decode()
    client = rest.RESTClient(api_key="organizations/x/apiKeys/y", api_secret=pem, rate_limit_headers=True)
    seen: list[tuple] = []

    def fake_request(method, url, params=None, json=None, headers=None, timeout=None):
        seen.append((method, url, params, json, headers))
        r = requests.Response()
        r.status_code = 200
        r._content = b'{"positions": []}'
        r.headers.update({"x-ratelimit-remaining": "29", "x-ratelimit-reset": "1"})
        return r

    monkeypatch.setattr(client.session, "request", fake_request)
    assert CoinbaseExchange(client, FUT).position() is None
    method, url, _, _, headers = seen[0]
    assert (method, url) == ("GET", "https://api.coinbase.com/api/v3/brokerage/cfm/positions")
    assert headers["Authorization"].startswith("Bearer ") and len(headers["Authorization"]) > 100  # SDK-signed JWT


@pytest.mark.parametrize("size", [0, 0.5, 1.5, 2.9999, -1, float("nan"), float("inf")])
def test_non_integer_or_sub_one_sizes_are_rejected_not_truncated(size):
    """Controller amendment: str(int(size)) would silently send fewer contracts; the adapter refuses like
    SimExchange (INVALID_SIZE_PRECISION) and never reaches the network."""
    ex, client, _ = make({})
    for r in (ex.place_market("a", 1, size), ex.place_bracket("b", 1, size, 83500.0, 85900.0),
              ex.close_position("c", size=size)):
        assert not r.ok and r.order_id is None and r.error == "INVALID_SIZE_PRECISION"
    assert ex.edit_bracket("o1", size, 82000.0, 85000.0) is False
    for p in (ex.preview_market(1, size), ex.preview_bracket(-1, size, 83000.0, 86000.0)):
        assert not p.ok and p.errors == ("PREVIEW_INVALID_SIZE_PRECISION",)
    assert client.calls == []


def test_whole_number_float_sizes_are_still_sent():
    ex, client, _ = make({("POST", "/orders"): "create_ok", ("POST", "/orders/close_position"): "close_ok"})
    assert ex.place_market("a", 1, 3.0).ok
    assert ex.close_position("c", size=2.0).ok
    assert client.calls[0][2]["order_configuration"] == {"market_market_ioc": {"base_size": "3"}}
    assert client.calls[1][2]["size"] == "2"
