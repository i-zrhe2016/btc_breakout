import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import main


class PreviewMarkupTests(unittest.TestCase):
    def test_preview_bounds_trendline_to_anchor_interval(self):
        markup = (Path(__file__).parent / "preview.html").read_text(encoding="utf-8")
        self.assertIn("趋势线只绘制 P1–P2 锚点区间", markup)
        self.assertIn("segmentStart", markup)
        self.assertIn("segmentEnd", markup)
        self.assertIn("趋势线仅显示锚点区间", markup)

    def test_timeframe_controls_support_auto_detection_and_switching(self):
        for name in ("recognition.html", "preview.html", "strategy.html"):
            markup = (Path(__file__).parent / name).read_text(encoding="utf-8")
            self.assertIn('value="auto"', markup, name)
            self.assertIn("30m", markup, name)
            self.assertIn("1w", markup, name)

    def test_strategy_expand_persists_current_lines_before_navigation(self):
        markup = (Path(__file__).parent / "strategy.html").read_text(encoding="utf-8")
        self.assertIn('id="chartExpand"', markup)
        self.assertIn("function persistWorkspace()", markup)
        self.assertIn('saved.lines.entry = app.lines.entry || null;', markup)
        self.assertIn('$("chartExpand").addEventListener("click", () => persistWorkspace())', markup)


class LineSpecTests(unittest.TestCase):
    def test_strategy_defaults_to_30x_and_utc_plus_8(self):
        payload = main.FuturesStrategyRequest(
            direction="LONG",
            notional_usdt=100,
            entry_line={"kind": "horizontal", "price": 100},
        )
        self.assertEqual(payload.leverage, 30)
        self.assertEqual(payload.chart_timezone, "Asia/Shanghai")
        self.assertEqual(main.normalize_chart_timezone("UTC+8"), "Asia/Shanghai")

    def test_kline_preview_falls_back_to_binance_spot_reference(self):
        candle = [1000, "100", "110", "90", "105"]
        with patch.object(
            main,
            "request_json",
            side_effect=[RuntimeError("451"), [], [], [], [candle]],
        ) as request:
            result = main.fetch_futures_klines("BTCUSDT", "1h", 10)
        self.assertEqual(result[0]["close"], 105)
        self.assertEqual(result[0]["source"], "binance_spot_fallback")
        self.assertIn("data-api.binance.vision", request.call_args.args[0])

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

    def test_stop_line_is_optional(self):
        payload = main.FuturesStrategyRequest(
            direction="LONG",
            notional_usdt=100,
            entry_line={"kind": "horizontal", "price": 100},
        )
        entry, stop = main.validate_stop_side(payload, current_price=95, ts_ms=123)
        self.assertEqual(entry, 100)
        self.assertIsNone(stop)


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

    def test_bark_notifies_after_open_and_close(self):
        main.save_bark_settings(
            main.BarkSettingsUpdate(
                endpoint="https://api.day.app/test-key/",
                enabled=True,
                notify_on_open=True,
                notify_on_close=True,
            )
        )
        payload = main.FuturesStrategyRequest(
            direction="LONG",
            notional_usdt=100,
            entry_line={"kind": "horizontal", "price": 100},
            stop_line={"kind": "horizontal", "price": 90},
        )
        with patch.object(main, "send_bark_message") as bark:
            state = self.run_simulation(payload, [101, 89])
        self.assertEqual(state.status, "completed")
        self.assertEqual(bark.call_count, 2)
        self.assertIn("已开仓", bark.call_args_list[0].args[1])
        self.assertIn("已平仓", bark.call_args_list[1].args[1])

    def test_bark_settings_persist(self):
        saved = main.save_bark_settings(
            main.BarkSettingsUpdate(
                endpoint="https://api.day.app/device/",
                enabled=True,
                notify_on_open=False,
                notify_on_close=True,
            )
        )
        loaded = main.get_bark_settings()
        self.assertEqual(loaded, saved)

    def test_simulated_strategy_uses_spot_reference_when_futures_price_is_blocked(self):
        payload = main.FuturesStrategyRequest(
            direction="LONG",
            notional_usdt=100,
            entry_line={"kind": "horizontal", "price": 80000},
        )
        with patch.object(main, "fetch_futures_price", side_effect=RuntimeError("HTTP 451")), patch.object(
            main, "fetch_spot_reference_price", return_value=79000
        ), patch.object(main, "start_strategy_thread"):
            result = main.create_futures_strategy(payload)
        self.assertEqual(result["status"], "armed")
        self.assertEqual(result["current_price"], 79000)

    def test_live_strategy_checks_configuration_before_market_price(self):
        payload = main.FuturesStrategyRequest(
            direction="LONG",
            notional_usdt=100,
            mode="live",
            entry_line={"kind": "horizontal", "price": 80000},
        )
        with patch.object(main, "live_strategy_preflight", side_effect=ValueError("live futures 未启用")), patch.object(
            main, "fetch_futures_price"
        ) as price:
            with self.assertRaises(main.HTTPException) as caught:
                main.create_futures_strategy(payload)
        self.assertEqual(caught.exception.status_code, 400)
        price.assert_not_called()

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

    def test_strategy_without_stop_stays_open_until_cancelled(self):
        payload = main.FuturesStrategyRequest(
            direction="LONG",
            notional_usdt=100,
            entry_line={"kind": "horizontal", "price": 100},
        )
        state = self.run_simulation(payload, [99, 101, 80])
        self.assertEqual(state.status, "position_left_open")
        self.assertEqual(state.entry_price, 101)
        self.assertIsNone(state.exit_price)
        self.assertIsNone(state.stop_line_price)
        self.assertIsNone(main.strategy_public_dict(state)["stop_line"])
        self.assertIn("未设置自动止损", next(event["message"] for event in state.events if event["type"] == "status" and "入场成交" in event["message"]))

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
    def test_local_codex_uses_saved_login_and_structured_output(self):
        captured = {}

        def run(command, **kwargs):
            captured["command"] = command
            captured["env"] = kwargs["env"]
            output_path = command[command.index("--output-last-message") + 1]
            with open(output_path, "w", encoding="utf-8") as output:
                output.write('{"ready": true}')
            return main.subprocess.CompletedProcess(command, 0, stdout='{"ready": true}', stderr="")

        env = {
            "OPENAI_API_KEY": "must-not-leak",
            "CODEX_API_KEY": "must-not-leak",
            "CODEX_MODEL": "gpt-test-codex",
        }
        with patch.dict(os.environ, env, clear=True), patch.object(main.subprocess, "run", side_effect=run):
            result = main.run_local_codex("recognize", b"image", "image/png", {"type": "object"})

        self.assertEqual(result, {"ready": True})
        self.assertNotIn("OPENAI_API_KEY", captured["env"])
        self.assertNotIn("CODEX_API_KEY", captured["env"])
        self.assertIn("--ephemeral", captured["command"])
        self.assertIn("--output-schema", captured["command"])
        self.assertIn("--image", captured["command"])
        self.assertEqual(captured["command"][-2:], ["--model", "gpt-test-codex"])
        self.assertEqual(captured["command"][2], "recognize")

    def test_horizontal_recognition_maps_to_line_spec_and_geometry(self):
        timeframe = {"detected_timeframe": "1h", "confidence": 0.96, "evidence": "顶部显示 1h"}
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
        with patch.object(main, "decode_image_data_url", return_value=(b"x", "image/png")), patch.object(
            main, "run_local_codex", side_effect=[timeframe, parsed]
        ) as codex_mock:
            result = main.recognize_chart_line(payload)
        self.assertTrue(result["ready_for_strategy"])
        self.assertEqual(result["line"]["kind"], "horizontal")
        self.assertEqual(result["line"]["price"], 98765.5)
        self.assertEqual(result["image_geometry"]["x2"], 0.9)
        self.assertEqual(result["detected_timeframe"], "1h")
        self.assertEqual(codex_mock.call_count, 2)
        self.assertEqual(codex_mock.call_args.args[1], b"x")
        self.assertEqual(codex_mock.call_args.args[2], "image/png")
        self.assertEqual(codex_mock.call_args.args[3], main.LINE_RECOGNITION_SCHEMA["schema"])

    def test_auto_timeframe_uses_detected_left_corner_period(self):
        parsed_timeframe = {"detected_timeframe": "4h", "confidence": 0.95, "evidence": "左上角显示 4h"}
        parsed_line = {
            "ready": True,
            "confidence": 0.9,
            "line_type": "horizontal",
            "horizontal_price": 98765.5,
            "anchors": [
                {"time_iso": None, "timestamp_ms": None, "price": 98765.5},
                {"time_iso": None, "timestamp_ms": None, "price": 98765.5},
            ],
            "image_geometry": {"x1": 0.1, "y1": 0.4, "x2": 0.9, "y2": 0.4},
            "notes": "line",
        }
        payload = main.LineRecognitionRequest(
            image_data_url="data:image/png;base64,eA==",
            role="entry",
            timeframe="auto",
        )
        with patch.object(main, "decode_image_data_url", return_value=(b"x", "image/png")), patch.object(
            main, "run_local_codex", side_effect=[parsed_timeframe, parsed_line]
        ):
            result = main.recognize_chart_line(payload)
        self.assertEqual(result["detected_timeframe"], "4h")
        self.assertEqual(result["timeframe"], "4h")

    def test_timeframe_mismatch_rejects_before_line_recognition(self):
        payload = main.LineRecognitionRequest(
            image_data_url="data:image/png;base64,eA==",
            role="entry",
            timeframe="1h",
        )
        detected = {"detected_timeframe": "4h", "confidence": 0.98, "evidence": "顶部显示 4h"}
        with patch.object(main, "decode_image_data_url", return_value=(b"x", "image/png")), patch.object(
            main, "run_local_codex", return_value=detected
        ) as codex_mock:
            with self.assertRaisesRegex(ValueError, "检测为 4h，当前选择为 1h"):
                main.recognize_chart_line(payload)
        codex_mock.assert_called_once()

    def test_low_confidence_timeframe_rejects_before_line_recognition(self):
        payload = main.LineRecognitionRequest(
            image_data_url="data:image/png;base64,eA==",
            role="entry",
            timeframe="1h",
        )
        detected = {"detected_timeframe": "1h", "confidence": 0.4, "evidence": "刻度模糊"}
        with patch.object(main, "decode_image_data_url", return_value=(b"x", "image/png")), patch.object(
            main, "run_local_codex", return_value=detected
        ) as codex_mock:
            with self.assertRaisesRegex(ValueError, "置信度不足"):
                main.recognize_chart_line(payload)
        codex_mock.assert_called_once()


if __name__ == "__main__":
    unittest.main()
