import os
import tempfile
import time
import unittest
from unittest.mock import patch

import main


class LineSpecTests(unittest.TestCase):
    def test_horizontal_and_trendline_prices(self):
        horizontal = main.LineSpec(kind="horizontal", price=100)
        self.assertEqual(horizontal.price_at(123), 100)

        trendline = main.LineSpec(kind="trendline", ts1=1000, price1=100, ts2=2000, price2=120)
        self.assertEqual(trendline.price_at(1500), 110)

    def test_invalid_line_shapes_are_rejected(self):
        with self.assertRaises(ValueError):
            main.LineSpec(kind="horizontal")
        with self.assertRaises(ValueError):
            main.LineSpec(kind="trendline", ts1=1000, price1=100, ts2=1000, price2=110)

    def test_long_short_trigger_matrix(self):
        long = main.FuturesStrategyRequest(
            direction="LONG",
            notional_usdt=100,
            entry_line={"kind": "horizontal", "price": 100},
            stop_line={"kind": "horizontal", "price": 90},
        )
        short = long.model_copy(update={"direction": "SHORT", "stop_line": main.LineSpec(kind="horizontal", price=110)})
        self.assertTrue(main.should_enter(long, 101, 100))
        self.assertFalse(main.should_enter(long, 99, 100))
        self.assertTrue(main.should_stop(long, 89, 90))
        self.assertTrue(main.should_enter(short, 99, 100))
        self.assertTrue(main.should_stop(short, 111, 110))


class StrategyRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"STRATEGY_DB_PATH": os.path.join(self.tempdir.name, "state.db")})
        self.env.start()
        with main.FUTURES_STRATEGIES_LOCK:
            main.FUTURES_STRATEGIES.clear()

    def tearDown(self):
        self.env.stop()
        self.tempdir.cleanup()

    def run_simulation(self, payload, prices):
        now = int(time.time() * 1000)
        state = main.FuturesStrategyState(
            strategy_id="test-strategy",
            payload=payload,
            status="armed",
            created_ts=now,
            updated_ts=now,
        )
        with main.FUTURES_STRATEGIES_LOCK:
            main.FUTURES_STRATEGIES[state.strategy_id] = state

        ticks = [(price, now + index * 1000, "live") for index, price in enumerate(prices)]
        with patch.object(main, "strategy_price_stream", return_value=iter(ticks)):
            main.run_futures_strategy(state.strategy_id)
        return state

    def test_long_enters_immediately_and_closes_on_horizontal_stop(self):
        payload = main.FuturesStrategyRequest(
            direction="LONG",
            notional_usdt=100,
            entry_line={"kind": "horizontal", "price": 100},
            stop_line={"kind": "horizontal", "price": 90},
        )
        state = self.run_simulation(payload, [95, 101, 89])
        self.assertEqual(state.status, "completed")
        self.assertEqual(state.entry_price, 101)
        self.assertEqual(state.exit_price, 89)
        self.assertTrue(state.entry_order["simulated"])
        self.assertTrue(state.exit_order["simulated"])

    def test_short_supports_trendline_entry_and_stop(self):
        now = int(time.time() * 1000)
        payload = main.FuturesStrategyRequest(
            direction="SHORT",
            notional_usdt=250,
            entry_line={"kind": "trendline", "ts1": now - 10000, "price1": 100, "ts2": now + 10000, "price2": 100},
            stop_line={"kind": "trendline", "ts1": now - 10000, "price1": 110, "ts2": now + 10000, "price2": 110},
        )
        state = self.run_simulation(payload, [105, 99, 111])
        self.assertEqual(state.status, "completed")
        self.assertEqual(state.entry_price, 99)
        self.assertEqual(state.exit_price, 111)

    def test_cancel_open_position_can_leave_position_open(self):
        payload = main.FuturesStrategyRequest(
            direction="LONG",
            notional_usdt=100,
            entry_line={"kind": "horizontal", "price": 100},
            stop_line={"kind": "horizontal", "price": 90},
        )
        now = int(time.time() * 1000)
        state = main.FuturesStrategyState(
            strategy_id="open-position",
            payload=payload,
            status="position_open",
            created_ts=now,
            updated_ts=now,
            current_price=105,
            filled_qty=1,
        )
        state.close_on_cancel = False
        state.cancel_event.set()
        with main.FUTURES_STRATEGIES_LOCK:
            main.FUTURES_STRATEGIES[state.strategy_id] = state
        main.run_futures_strategy(state.strategy_id)
        self.assertEqual(state.status, "position_left_open")

    def test_completed_strategy_is_restored_for_recent_history(self):
        payload = main.FuturesStrategyRequest(
            direction="LONG",
            notional_usdt=100,
            entry_line={"kind": "horizontal", "price": 100},
            stop_line={"kind": "horizontal", "price": 90},
        )
        now = int(time.time() * 1000)
        state = main.FuturesStrategyState(
            strategy_id="completed-history",
            payload=payload,
            status="completed",
            created_ts=now,
            updated_ts=now,
            entry_price=101,
            exit_price=89,
        )
        main.persist_strategy(state)
        with main.FUTURES_STRATEGIES_LOCK:
            main.FUTURES_STRATEGIES.clear()
        with patch.object(main, "start_strategy_thread") as start:
            main.restore_persisted_strategies()
        self.assertIn(state.strategy_id, main.FUTURES_STRATEGIES)
        self.assertEqual(main.FUTURES_STRATEGIES[state.strategy_id].exit_price, 89)
        start.assert_not_called()

    def test_live_close_caps_quantity_to_actual_position(self):
        payload = main.FuturesStrategyRequest(
            direction="LONG",
            notional_usdt=100,
            mode="live",
            entry_line={"kind": "horizontal", "price": 100},
            stop_line={"kind": "horizontal", "price": 90},
        )
        now = int(time.time() * 1000)
        state = main.FuturesStrategyState(
            strategy_id="live-close",
            payload=payload,
            status="position_open",
            created_ts=now,
            updated_ts=now,
            filled_qty=2,
        )
        calls = []

        def signed(path, *, method="GET", params=None):
            calls.append((path, method, params))
            if path.endswith("positionRisk"):
                return [{"symbol": "BTCUSDT", "positionAmt": "1.25"}]
            return {"orderId": 10, "executedQty": "1.25", "avgPrice": "99"}

        with patch.object(main, "futures_signed_json", side_effect=signed):
            result = main.submit_futures_market_order(state, closing=True, reference_price=99)
        self.assertEqual(result["orderId"], 10)
        order_params = next(item[2] for item in calls if item[0].endswith("/order") and item[1] == "POST")
        self.assertEqual(order_params["quantity"], "1.25")
        self.assertEqual(order_params["reduceOnly"], "true")


class RecognitionTests(unittest.TestCase):
    def test_codex_headers_without_cloudflare_access(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(main.get_codex_headers("test-key"), {"Authorization": "Bearer test-key"})

    def test_codex_headers_include_cloudflare_access_service_token(self):
        with patch.dict(
            os.environ,
            {"CF_ACCESS_CLIENT_ID": "client-id.access", "CF_ACCESS_CLIENT_SECRET": "client-secret"},
            clear=True,
        ):
            self.assertEqual(
                main.get_codex_headers("test-key"),
                {
                    "Authorization": "Bearer test-key",
                    "CF-Access-Client-Id": "client-id.access",
                    "CF-Access-Client-Secret": "client-secret",
                },
            )

    def test_codex_headers_reject_partial_cloudflare_access_config(self):
        with patch.dict(os.environ, {"CF_ACCESS_CLIENT_ID": "client-id.access"}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "必须同时配置"):
                main.get_codex_headers("test-key")

    def test_extract_codex_response_json_from_output_message(self):
        response = {
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": '{"ready": true}'}],
                }
            ]
        }
        self.assertEqual(main.extract_codex_response_json(response), {"ready": True})

    def test_horizontal_recognition_maps_to_line_spec_and_geometry(self):
        parsed = {
            "ready": True,
            "confidence": 0.91,
            "line_type": "horizontal",
            "horizontal_price": 98765.5,
            "anchors": [
                {"time_iso": None, "timestamp_ms": None, "price": 98765.5},
                {"time_iso": None, "timestamp_ms": None, "price": 98765.5},
            ],
            "image_geometry": {"x1": 0.1, "y1": 0.4, "x2": 0.9, "y2": 0.4},
            "notes": "blue horizontal line",
        }
        payload = main.LineRecognitionRequest(
            image_data_url="data:image/png;base64,eA==",
            role="entry",
            expected_line_type="auto",
        )
        response = {
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": main.json.dumps(parsed)}],
                }
            ]
        }
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test"}), patch.object(
            main, "decode_image_data_url", return_value=(b"x", "image/png")
        ), patch.object(main, "request_json", return_value=response) as request_mock:
            result = main.recognize_chart_line(payload)
        self.assertTrue(result["ready_for_strategy"])
        self.assertEqual(result["line"]["kind"], "horizontal")
        self.assertEqual(result["line"]["price"], 98765.5)
        self.assertEqual(result["image_geometry"]["x2"], 0.9)
        request_body = request_mock.call_args.kwargs["payload"]
        self.assertEqual(request_body["model"], "gpt-5.3-codex")
        self.assertIn("input", request_body)
        self.assertNotIn("messages", request_body)


if __name__ == "__main__":
    unittest.main()
