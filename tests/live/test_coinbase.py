from __future__ import annotations

import copy
import inspect
import json
from importlib import metadata
from pathlib import Path

import pytest
import requests

from ewbot.live.coinbase import (
    API,
    RETRY_AFTER_MAX_S,
    RETRY_SLEEP_MAX_S,
    CoinbaseExchange,
    fmt_decimal,
    map_error,
    parse_order,
    parse_place,
    retry_after_s,
)
from ewbot.live.exchange import OPEN_STATUSES, ExchangeError, Position, RateLimited, Unavailable

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
    # contract_expiry as recorded: 2089-12-30T16:00:00Z (the id says 20 Dec 2030; the preflight checks both)
    assert spec.expiry == 3786796800
    for odd in (None, "", "soon", 20301220):  # missing or not a timestamp: None (the preflight refuses it)
        reply = copy.deepcopy(FIXTURES["product_bip"])
        reply["future_product_details"]["contract_expiry"] = odd
        assert make({("GET", f"/products/{FUT}"): reply})[0].product().expiry is None
    s = ex.session()
    assert s.state == "OPEN" and s.is_open
    assert (s.maintenance_start, s.maintenance_end) == (1792836000, 1792868400)  # 2026-10-24 10:00-19:00 UTC


def test_quotes_come_from_the_public_order_book():
    ex, client, _ = make({("GET", "/market/product_book"): lambda p: "book_bip" if p["product_id"] == FUT else "book_btc"})
    q, s = ex.quote(FUT), ex.quote("BTC-USD")
    assert (q.bid, q.ask, q.mid) == (84655.0, 84680.0, 84667.5)
    assert s.mid == pytest.approx(84671.265)
    assert client.calls[0] == ("GET", API + "/market/product_book", {"product_id": FUT, "limit": 1}, True)


@pytest.mark.parametrize(("bid", "ask"), [("", "84680"), ("0", "84680"), ("-5", "84680"), ("NaN", "84680"),
                                          ("84655", "inf")])
def test_an_unusable_top_of_book_is_unavailable_not_a_price(bid, ask):
    book = {"pricebook": {"bids": [{"price": bid, "size": "1"}], "asks": [{"price": ask, "size": "1"}]}}
    ex, _, _ = make({("GET", "/market/product_book"): book})
    with pytest.raises(Unavailable):  # a bid read as 0 would make a mid of half the price (and a basis ratio of 0.5)
        ex.quote(FUT)


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
        ("ewb-b-1790942400-tp2-1", "BRACKET", 2, 82000.0, 85900.0, "CANCEL_QUEUED"),  # may still fill
    ]
    assert all(o.is_open for o in orders)
    assert orders[0].side == 1 and orders[0].created_ts == 1791028841
    assert client.calls[0][2]["product_ids"] == [FUT]
    assert client.calls[0][2]["order_status"] == ["OPEN", "PENDING", "QUEUED", "EDIT_QUEUED", "CANCEL_QUEUED"]
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
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    assert metadata.version("coinbase-advanced-py") == "1.8.4"
    init = inspect.signature(rest.RESTClient.__init__).parameters
    assert {"key_file", "timeout", "rate_limit_headers"} <= set(init)
    assert {"url_path", "params", "public"} <= set(inspect.signature(rest.RESTClient.get).parameters)
    assert {"url_path", "data"} <= set(inspect.signature(rest.RESTClient.post).parameters)

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


# ---------------------------------------------------------------------------- Fix round 1
SECRET = "Bearer eyJhbGciOiJFUzI1NiJ9.secret"


@pytest.mark.parametrize("exc", [
    requests.exceptions.ChunkedEncodingError(SECRET),
    requests.exceptions.ContentDecodingError(SECRET),
    requests.exceptions.JSONDecodeError("Expecting value", SECRET, 0),
    requests.exceptions.InvalidHeader(f"Invalid return character or leading space in header: Authorization: {SECRET}"),
    requests.exceptions.ReadTimeout(SECRET),
    requests.exceptions.SSLError(SECRET),
    requests.exceptions.HTTPError(SECRET),  # no response attached
    requests.exceptions.RequestException(SECRET),
])
def test_ambiguous_post_send_failures_are_unavailable_and_sent_once(exc):
    """The order may exist: any requests failure is ambiguous (Unavailable -> lookup by client_order_id),
    never a final rejection; the message is the type name only (an exception text may carry the JWT)."""
    ex, client, _ = make({("POST", "/orders"): [exc, "create_ok"]})
    with pytest.raises(Unavailable) as err:
        ex.place_market("ewb-e-1-L", 1, 1)
    assert str(err.value) == type(exc).__name__ and "secret" not in str(err.value)
    assert len(client.calls) == 1


def test_unknown_exceptions_keep_only_their_type_name():
    err = map_error(ValueError(SECRET))
    assert type(err) is ExchangeError and not err.retryable and str(err) == "ValueError"


def test_failure_reason_prefers_new_order_failure_reason():
    ex, _, _ = make({("POST", "/orders"): ["create_failed_new_reason", "create_duplicate_new_reason"]})
    r = ex.place_bracket("b", -1, 1, 86000.0, 83500.0)
    assert not r.ok and r.error.startswith("INVALID_BRACKET_PRICES") and not r.duplicate
    d = ex.place_market("a", 1, 1)  # error and failure_reason say UNKNOWN_*; new_order_failure_reason decides
    assert not d.ok and d.duplicate and d.error.startswith("DUPLICATE_CLIENT_ORDER_ID")
    assert parse_place({"success": False, "error_response": {"preview_failure_reason": "DUPLICATE_CLIENT_ORDER_ID"}},
                       "c").duplicate
    assert parse_place({"success": False, "failure_reason": "UNKNOWN_FAILURE_REASON"}, "c").error == "UNKNOWN_FAILURE_REASON"
    assert not parse_place(FIXTURES["create_insufficient"], "c").duplicate


@pytest.mark.parametrize("body", ["http400_duplicate_order_body", "http400_duplicate_gateway_body"])
def test_an_http_4xx_naming_the_duplicate_id_takes_the_duplicate_path(body):
    text = json.dumps(FIXTURES[body])
    ex, client, _ = make({("POST", "/orders"): [http_error(400, text)],
                          ("POST", "/orders/close_position"): [http_error(409, text)]})
    for r in (ex.place_market("ewb-e-1-L", 1, 1), ex.close_position("ewb-x-1-1")):
        assert not r.ok and r.duplicate and r.order_id is None
    assert len(client.calls) == 2
    ex2, _, _ = make({("POST", "/orders"): [http_error(400, json.dumps(FIXTURES["create_insufficient"]))]})
    with pytest.raises(ExchangeError) as err:
        ex2.place_market("a", 1, 1)
    assert not err.value.retryable


def test_position_sides_are_mapped_explicitly():
    ex, _, _ = make({("GET", "/cfm/positions"): ["positions_bip_long", {"positions": [
        {"product_id": FUT, "side": "SHORT", "number_of_contracts": "-3", "avg_entry_price": "85100"}]},
        {"positions": [{"product_id": FUT, "side": "UNKNOWN", "number_of_contracts": "2", "avg_entry_price": "1"}]},
        {"positions": [{"product_id": FUT, "side": "FUTURES_POSITION_SIDE_UNKNOWN", "number_of_contracts": "2"}]},
        {"positions": [{"product_id": FUT, "side": "FUTURES_POSITION_SIDE_LONG", "number_of_contracts": "2"}]},
        {"positions": [{"product_id": FUT, "side": "FUTURES_POSITION_SIDE_SHORT", "number_of_contracts": "1",
                        "avg_entry_price": "85100"}]}]})
    assert ex.position() == Position(1, 2, 84000.0)
    assert ex.position() == Position(-1, 3, 85100.0)  # abs() of a signed count
    for _ in range(2):  # UNKNOWN, with or without the enum's prefix
        with pytest.raises(ExchangeError) as err:
            ex.position()
        assert not err.value.retryable
    # the enum's full name (FUTURES_POSITION_SIDE_LONG) reads as LONG; no average entry reads 0.0
    assert ex.position() == Position(1, 2, 0.0)
    assert ex.position() == Position(-1, 1, 85100.0)


def test_order_sides_are_mapped_explicitly():
    base = {"order_id": "x", "status": "OPEN", "order_configuration": {}}
    assert parse_order({**base, "side": "BUY"}).side == 1 and parse_order({**base, "side": "SELL"}).side == -1
    for side in ("", "UNKNOWN_ORDER_SIDE", None, "buy"):
        with pytest.raises(ExchangeError):
            parse_order({**base, "side": side})


def test_sell_bracket_protects_a_long():
    ex, client, _ = make({("POST", "/orders"): "create_ok"})
    assert ex.place_bracket("ewb-b-1790942400-tp1-1", -1, 2, 86000.0, 83500.0).ok
    assert client.calls[0][2] == {
        "client_order_id": "ewb-b-1790942400-tp1-1", "product_id": FUT, "side": "SELL",
        "order_configuration": {"trigger_bracket_gtc": {"base_size": "2", "limit_price": "86000",
                                                        "stop_trigger_price": "83500"}}}


@pytest.mark.parametrize("status", ["OPEN", "PENDING", "QUEUED", "EDIT_QUEUED", "CANCEL_QUEUED"])
def test_resting_statuses_are_open(status):
    o = parse_order({"order_id": "x", "side": "BUY", "status": status, "order_configuration": {}})
    assert o.status == status and o.is_open and not o.is_terminal
    assert status in OPEN_STATUSES


def test_unknown_order_status_is_neither_open_nor_terminal():
    o = parse_order({"order_id": "x", "side": "BUY", "status": "UNKNOWN_ORDER_STATUS", "order_configuration": {}})
    assert o.status == "UNKNOWN" and not o.is_open and not o.is_terminal


@pytest.mark.parametrize(("value", "expected"), [
    (None, 1.0), ("2", 2.0), ("0.5", 0.5), ("120", RETRY_AFTER_MAX_S), ("-5", 0.0), ("soon", 1.0), ("nan", 1.0),
    ("inf", 1.0), ("Wed, 21 Oct 2015 07:28:00 GMT", 0.0), ("Fri, 31 Dec 2099 23:59:59 GMT", RETRY_AFTER_MAX_S),
])
def test_retry_after_is_parsed_leniently_and_capped(value, expected):
    assert retry_after_s(value) == pytest.approx(expected)
    headers = {} if value is None else {"Retry-After": value}
    err = map_error(http_error(429, "slow down", headers))
    assert isinstance(err, RateLimited) and err.retry_after == pytest.approx(expected)


def test_a_long_retry_after_waits_at_most_the_cap():
    ex, _, slept = make({("GET", "/cfm/positions"): [http_error(429, "", {"Retry-After": "3600"}), "positions"]})
    assert ex.position().contracts == 3 and slept == [RETRY_AFTER_MAX_S]
    resp = dict(FIXTURES["positions"], **{"x-ratelimit-remaining": "lots", "x-ratelimit-reset": "soon"})
    ex2, _, slept2 = make({("GET", "/cfm/positions"): [resp, "positions"]})
    ex2.position()
    ex2.position()
    assert slept2 == []  # unreadable pacing headers are ignored


@pytest.mark.parametrize(("route", "reply", "call"), [
    (("GET", f"/products/{FUT}"), {"price_increment": "5"}, lambda ex: ex.product()),
    (("GET", f"/products/{FUT}"), {"fcm_trading_session_details": {"maintenance": {"start_time": "soon"}}},
     lambda ex: ex.session()),
    (("GET", "/market/product_book"), {"pricebook": {"bids": [{"px": "1"}], "asks": [{"px": "2"}]}},
     lambda ex: ex.quote(FUT)),
    (("GET", "/cfm/positions"), {"positions": [{"product_id": FUT, "side": "LONG", "number_of_contracts": "two"}]},
     lambda ex: ex.position()),
    (("GET", "/orders/historical/batch"), {"orders": [{"side": "BUY", "status": "OPEN"}]}, lambda ex: ex.open_orders()),
    (("GET", "/orders/historical/o1"), {"not_order": {}}, lambda ex: ex.get_order("o1")),
    (("GET", "/cfm/balance_summary"), {"balance_summary": {"total_usd_balance": {"value": "n/a"}}},
     lambda ex: ex.balance()),
    (("POST", "/orders/preview"), {"errs": 5}, lambda ex: ex.preview_market(1, 1)),
    (("POST", "/orders/batch_cancel"), {"results": [7]}, lambda ex: ex.cancel(["o1"])),
])
def test_malformed_replies_raise_exchange_errors_only(route, reply, call):
    ex, _, _ = make({route: reply})
    with pytest.raises(ExchangeError) as err:
        call(ex)
    assert not err.value.retryable


def test_a_reply_that_is_not_an_object_or_an_accepted_order_without_id_is_ambiguous():
    ex, client, _ = make({("POST", "/orders"): [["not", "an", "object"], {"success": True, "success_response": {}}]})
    for _ in range(2):
        with pytest.raises(Unavailable):
            ex.place_market("ewb-e-1-L", 1, 1)
    assert len(client.calls) == 2


@pytest.mark.parametrize("bad", [0.0, -5.0, float("nan"), float("inf")])
def test_non_finite_or_non_positive_prices_are_refused_locally(bad):
    ex, client, _ = make({})
    r = ex.place_bracket("b", -1, 1, bad, 83500.0)
    assert not r.ok and r.error == "INVALID_PRICE_PRECISION"
    r = ex.place_bracket("b", -1, 1, 86000.0, bad)
    assert not r.ok and r.error == "INVALID_STOP_PRICE_PRECISION"
    assert ex.edit_bracket("o1", 1, bad, 83500.0) is False and ex.edit_bracket("o1", 1, 86000.0, bad) is False
    assert ex.preview_bracket(-1, 1, bad, 83500.0).errors == ("PREVIEW_INVALID_PRICE_PRECISION",)
    assert ex.preview_bracket(-1, 1, 86000.0, bad).errors == ("PREVIEW_INVALID_STOP_PRICE_PRECISION",)
    assert client.calls == []


def test_key_permissions_fail_closed():
    ex, _, _ = make({("GET", "/key_permissions"): [{"can_view": True, "can_trade": True},
                                                    {"can_view": True, "can_trade": True, "can_transfer": None}, {}]})
    k = ex.key_permissions()
    assert (k.can_view, k.can_trade, k.can_transfer) == (True, True, True)  # can_transfer missing: assume it can
    assert ex.key_permissions().can_transfer is True  # null: the same
    k = ex.key_permissions()
    assert (k.can_view, k.can_trade, k.can_transfer) == (False, False, True)


@pytest.mark.parametrize("summary", [
    {"balance_summary": {"unrealized_pnl": {"value": "13.05", "currency": "USD"}}},
    {"balance_summary": {"total_usd_balance": {"currency": "USD"}, "unrealized_pnl": {"value": "13.05"}}},
    {"balance_summary": {"total_usd_balance": {"value": "", "currency": "USD"}}},
    {"balance_summary": {"total_usd_balance": {"value": "inf", "currency": "USD"}}},
    {},
])
def test_balance_without_a_cash_total_is_an_error(summary):
    ex, _, _ = make({("GET", "/cfm/balance_summary"): summary})
    with pytest.raises(ExchangeError):
        ex.balance()


def _order_reply(order_id: str, status: str) -> dict:
    return {"order": {"order_id": order_id, "side": "SELL", "status": status, "client_order_id": "ewb-b-1-tp1-1",
                      "order_configuration": {"trigger_bracket_gtc": {"base_size": "1", "limit_price": "86000",
                                                                      "stop_trigger_price": "83500"}}}}


def test_cancel_honours_each_result():
    ex, client, _ = make({("POST", "/orders/batch_cancel"): ["cancel_done_or_refused", "cancel_done_or_refused",
                                                             "cancel_ok"],
                          ("GET", "/orders/historical/o-refused"): _order_reply("o-refused", "OPEN")})
    ex.cancel(["o-ok", "o-queued", "o-filled"])  # cancelled, already queued, already filled: nothing rests
    with pytest.raises(ExchangeError) as err:
        ex.cancel(["o-ok", "o-refused"])  # refused, and read back still OPEN
    assert "o-refused: UNKNOWN_CANCEL_ORDER" in str(err.value) and "o-ok" not in str(err.value)
    assert not err.value.retryable
    with pytest.raises(ExchangeError, match="o-missing: NO_RESULT"):  # no result, and it cannot be read back
        ex.cancel(["11111111-aaaa-4000-8000-000000000001", "o-missing"])
    assert [c[1].rsplit("/", 1)[-1] for c in client.calls] == ["batch_cancel", "batch_cancel", "o-refused",
                                                               "batch_cancel", "o-missing"]


@pytest.mark.parametrize("status", ["CANCELLED", "FILLED", "EXPIRED", "FAILED"])
def test_a_refused_cancel_of_an_order_that_already_ended_is_no_error(status):
    """Final fix: the failure reason for an order that left the book is undocumented (e.g. UNKNOWN_CANCEL_ORDER). An
    order read back terminal rests no more - no error; one read back open, or unreadable, may still rest."""
    ex, client, _ = make({("POST", "/orders/batch_cancel"): ["cancel_done_or_refused"],
                          ("GET", "/orders/historical/o-refused"): _order_reply("o-refused", status)})
    ex.cancel(["o-ok", "o-refused"])
    assert client.calls[-1][:2] == ("GET", API + "/orders/historical/o-refused")


def test_the_pinned_sdk_sends_every_open_status_as_a_repeated_parameter(monkeypatch):
    """List Orders takes order_status as an exploded array; SDK 1.8.4 passes the list to requests unchanged
    (list_orders(order_status: Optional[List[str]])), which repeats the key. Needs the `live` group."""
    rest = pytest.importorskip("coinbase.rest")
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    assert "List" in str(inspect.signature(rest.RESTClient.list_orders).parameters["order_status"].annotation)
    pem = ec.generate_private_key(ec.SECP256R1()).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()
    ).decode()
    client = rest.RESTClient(api_key="organizations/x/apiKeys/y", api_secret=pem)
    urls: list[str] = []

    def fake_request(method, url, params=None, json=None, headers=None, timeout=None):
        urls.append(requests.Request(method, url, params=params).prepare().url)
        r = requests.Response()
        r.status_code = 200
        r._content = b'{"orders": [], "has_next": false, "cursor": ""}'
        return r

    monkeypatch.setattr(client.session, "request", fake_request)
    assert CoinbaseExchange(client, FUT).open_orders() == []
    query = urls[0].split("?", 1)[1]
    assert [p.split("=")[1] for p in query.split("&") if p.startswith("order_status=")] == list(OPEN_STATUSES)


# ---------------------------------------------------------------------------- Final fix (non-finite amounts, replies)
def _order(**fields) -> dict:
    o = {"order_id": "o1", "side": "SELL", "status": "FILLED", "client_order_id": "ewb-e-1-S", "filled_size": "1",
         "order_configuration": {"market_market_ioc": {"base_size": "1"}}}
    return {"order": {**o, **fields}}


@pytest.mark.parametrize("bad", ["NaN", "nan", "inf", "-Infinity", "1e999"])
def test_non_finite_amounts_never_leave_the_adapter(bad):
    """Final fix (P0): Python reads "NaN" / "inf" as floats. An optional amount that is not finite reads None (an
    unread fill price, never a NaN the journal refuses - which left a filled entry without a trade or brackets); a
    required one is an unexpected reply (ExchangeError, never an OverflowError escaping the adapter)."""
    bracket = {"trigger_bracket_gtc": {"base_size": "1", "limit_price": bad, "stop_trigger_price": bad}}
    routes = {
        ("GET", "/orders/historical/o1"): [_order(average_filled_price=bad, total_fees=bad), _order(filled_size=bad),
                                           _order(order_configuration=bracket, status="OPEN", filled_size="0")],
        ("GET", "/cfm/positions"): [
            {"positions": [{"product_id": FUT, "side": "LONG", "number_of_contracts": "2", "avg_entry_price": bad}]},
            {"positions": [{"product_id": FUT, "side": "LONG", "number_of_contracts": bad}]}],
        ("GET", "/cfm/balance_summary"): [
            {"balance_summary": {"total_usd_balance": {"value": "5000"}, "available_margin": {"value": bad}}},
            {"balance_summary": {"total_usd_balance": {"value": "5000"}, "unrealized_pnl": {"value": bad}}},
            {"balance_summary": {"total_usd_balance": {"value": bad}}}],
        ("GET", f"/products/{FUT}"): {**FIXTURES["product_bip"], "price_increment": bad},
    }
    ex, _, _ = make(routes)
    o = ex.get_order("o1")
    assert (o.filled, o.avg_fill_price, o.fee) == (1, None, None)
    assert ex.position() == Position(1, 2, 0.0)  # the size and side are what protection needs
    for read in (lambda: ex.get_order("o1"), ex.position, ex.product):
        with pytest.raises(ExchangeError) as err:
            read()
        assert not err.value.retryable
    o = ex.get_order("o1")
    assert (o.kind, o.limit_price, o.stop_price) == ("BRACKET", None, None)
    b = ex.balance()
    assert b.equity == 5000.0 and b.available_margin is None
    for _ in range(2):
        with pytest.raises(ExchangeError):
            ex.balance()


def test_a_position_without_a_finite_entry_price_stays_readable():
    ex, _, _ = make({("GET", "/cfm/positions"): {"positions": [
        {"product_id": FUT, "side": "SHORT", "number_of_contracts": "3", "avg_entry_price": "NaN"}]}})
    assert ex.position() == Position(-1, 3, 0.0)  # what protection is placed for; the executor never takes 0 as a price


@pytest.mark.parametrize("reply", [{}, {"order_id": "33333333-cccc-4000-8000-000000000001"}, {"success": "true"},
                                   {"success": None, "failure_reason": "UNKNOWN_FAILURE_REASON"}])
def test_an_order_reply_without_a_boolean_success_is_ambiguous_not_a_refusal(reply):
    """Final fix: a 200 reply that is not a create reply says nothing about the order: Unavailable (looked up by its
    client order id), never a refusal (a refused entry is never sent again). Sent once."""
    ex, client, _ = make({("POST", "/orders"): [reply], ("POST", "/orders/close_position"): [reply]})
    with pytest.raises(Unavailable):
        ex.place_market("ewb-e-1-L", 1, 1)
    with pytest.raises(Unavailable):
        ex.close_position("ewb-x-1-1")
    assert len(client.calls) == 2


_LONG = {"product_id": FUT, "side": "LONG", "number_of_contracts": "2", "avg_entry_price": "84000"}


@pytest.mark.parametrize("reply", [
    {},  # no positions key
    {"cfm_positions": [_LONG]},  # the positions under another key
    {"positions": None}, {"positions": {"product_id": FUT}}, {"positions": "none"},  # not a list
    {"positions": [{k: v for k, v in _LONG.items() if k != "product_id"} | {"product": FUT}]},  # product_id renamed
    {"positions": ["BIP"]},  # an entry that is not an object
    {"positions": [{k: v for k, v in _LONG.items() if k != "number_of_contracts"} | {"contracts": "2"}]},
    {"positions": [{**_LONG, "number_of_contracts": ""}]},
    {"positions": [{**_LONG, "number_of_contracts": "2.5"}]},  # not a whole number of contracts
    {"positions": [{k: v for k, v in _LONG.items() if k != "side"}]},
    {"positions": [_LONG, {**_LONG, "side": "SHORT"}]},  # two entries for the product
])
def test_a_position_reply_of_another_shape_is_an_error_never_flat(reply):
    """Final review A-D: a reply without the documented shape never reads as flat (a flat read closes the trade's record,
    cancels its brackets and lets the next entry stack on the real position): `positions` must be a list, every entry
    an object with a product_id, the product's one entry needs number_of_contracts (a whole number) and, unless it
    is 0, its side."""
    ex, _, _ = make({("GET", "/cfm/positions"): reply})
    with pytest.raises(ExchangeError) as err:
        ex.position()
    assert not err.value.retryable


def test_a_flat_position_reads_none_only_from_the_documented_shape():
    ex, _, _ = make({("GET", "/cfm/positions"): [
        {"positions": []}, {"positions": [{**_LONG, "product_id": "BIT-31OCT26-CDE"}]},
        {"positions": [{"product_id": FUT, "number_of_contracts": "0", "side": "UNKNOWN"}]},
        {"positions": [{"product_id": FUT, "number_of_contracts": "0"}]}]})
    for _ in range(4):  # no entry, only another product's, the product's at 0 contracts (no side needed)
        assert ex.position() is None


def test_a_crossed_book_is_unavailable_not_a_price():
    """Final review A minor 3: a bid above the ask (a stale or one-sided top of book) is no price to map levels or
    size with."""
    book = {"pricebook": {"bids": [{"price": "84700", "size": "1"}], "asks": [{"price": "84680", "size": "1"}]}}
    ex, _, _ = make({("GET", "/market/product_book"): [book, "book_bip"]})
    with pytest.raises(Unavailable, match="crossed"):
        ex.quote(FUT)
    assert ex.quote(FUT).bid == 84655.0  # a locked or normal book reads


def test_a_call_waits_at_most_its_budget_and_a_429_then_fails_fast():
    """Final review A minor 5: a call's waits (pacing and retries) add up to at most RETRY_SLEEP_MAX_S, then its error
    is raised; while the last 429's Retry-After runs, a new call raises at once and sends nothing - so one pass of the
    loop is never held for tens of seconds by a 429 storm."""
    now = [1_790_000_000.0]
    slept: list[float] = []

    def sleep(s: float) -> None:
        slept.append(s)
        now[0] += s

    client = RecordedClient({("GET", API + "/cfm/positions"): [
        http_error(429, "", {"Retry-After": "10"}), http_error(429, "", {"Retry-After": "10"}), "positions"]})
    ex = CoinbaseExchange(client, FUT, "BTC-USD", sleep=sleep, clock=lambda: now[0])
    with pytest.raises(RateLimited):
        ex.position()
    assert slept == [10.0] and sum(slept) <= RETRY_SLEEP_MAX_S and len(client.calls) == 2
    with pytest.raises(RateLimited, match="nothing sent"):
        ex.position()  # the second 429's Retry-After still runs: no request, no wait
    assert len(client.calls) == 2 and slept == [10.0]
    now[0] += 10.0
    assert ex.position().contracts == 3  # it has passed: sent again
    assert len(client.calls) == 3


def test_a_429_on_public_market_data_never_holds_a_private_request():
    """Final review A minor (probe pH): the public order book and the private account and order endpoints are limited
    apart; a 429 on a quote opens the fail-fast window of public requests only - a close sent right after it goes out
    (and a private 429 never holds a quote)."""
    now = [1_790_000_000.0]
    client = RecordedClient({("GET", API + "/market/product_book"): [http_error(429, "", {"Retry-After": "5"})] * 3,
                             ("POST", API + "/orders/close_position"): ["close_ok"],
                             ("GET", API + "/cfm/positions"): [http_error(429, "", {"Retry-After": "5"})] * 3 + ["positions"]})
    ex = CoinbaseExchange(client, FUT, "BTC-USD", sleep=lambda s: None, clock=lambda: now[0], attempts=1)
    with pytest.raises(RateLimited):
        ex.quote(FUT)
    assert ex.close_position("ewb-x-1-1").ok  # sent: the public window does not cover it
    with pytest.raises(RateLimited, match="nothing sent"):
        ex.quote(FUT)
    with pytest.raises(RateLimited):
        ex.position()
    client.routes[("GET", API + "/market/product_book")] = ["book_bip"]
    now[0] += 6
    assert ex.quote(FUT).bid == 84655.0
