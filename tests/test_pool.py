"""What the pool counts and keeps: each account's limits, and the tokens and requests per account and per
developer, with their estimated API cost and share of the pool's limits."""

import time

import pytest

from hotpocket import store
from hotpocket.pool import Pool, used
from hotpocket.usage import usage_of

from conftest import call, dev, status, use_accounts


async def test_tokens_and_estimated_api_cost_are_counted(gw):
    await call(gw)  # streamed: Claude Opus 5.5, 100 in + 200 cache writes + 1000 cache reads + 50 out
    good = (await status(gw))["accounts"]["good"]
    assert good["tokens"] == {"input_tokens": 100, "output_tokens": 50, "cache_creation_input_tokens": 200,
                              "cache_read_input_tokens": 1000}
    assert good["cost"] == pytest.approx((100 * 4 + 200 * 5 + 1000 * 0.2 + 50 * 20) / 1e6)
    use_accounts("PLAIN")
    await call(gw)  # not streamed: Claude Sonnet 5.5, 1000 in + 100 out
    snap = await status(gw)
    assert snap["accounts"]["plain"]["cost"] == pytest.approx((1000 * 2 + 100 * 10) / 1e6)
    alice = snap["developers"]["alice"]
    assert alice["requests"] == 2 and alice["tokens"]["input_tokens"] == 1100 and alice["cost"] == pytest.approx(0.0026 + 0.003)
    assert time.time() - snap["since"] < 60


async def test_developers_share_the_pool_limits_by_what_their_requests_cost(gw):
    store.save_account("credits", "sk-ant-api03-CREDITS")  # not a seat
    await call(gw)  # alice, on the good account: 42% of its 5-hour limit, 10% of its weekly
    alice = (await status(gw))["developers"]["alice"]
    assert alice["five_hour"] == pytest.approx(0.42 / 2) and alice["seven_day"] == pytest.approx(0.10 / 2)  # 2 seats
    await call(gw, dev(store.new_key("bob")))  # bob's request costs the same, so they split it evenly
    devs = (await status(gw))["developers"]
    assert devs["alice"]["five_hour"] == devs["bob"]["five_hour"] == pytest.approx(0.42 / 2 / 2)


async def test_a_plan_with_only_a_weekly_limit_is_left_out_of_the_5_hour_totals(gw):
    use_accounts("GOOD", "WEEKLY")
    assert (await status(gw))["accounts"]["weekly"]["windows"] == ["five_hour", "seven_day"]  # until heard from
    await call(gw)  # alice, on good: 42% of its 5-hour limit, 10% of its weekly
    await call(gw, dev(store.new_key("bob"), "s2"))  # bob, on weekly (the one with the most room)
    snap = await status(gw)
    assert snap["accounts"]["weekly"]["windows"] == ["seven_day"] and snap["accounts"]["good"]["windows"] == ["five_hour", "seven_day"]
    alice, bob = snap["developers"]["alice"], snap["developers"]["bob"]
    assert alice["five_hour"] == pytest.approx(0.42) and not bob["five_hour"]  # good is the only seat with a 5-hour limit
    assert alice["seven_day"] == bob["seven_day"] == pytest.approx(0.10 / 2)  # both have a weekly one


async def test_accounts_weigh_in_the_pools_limits_by_their_plan(gw):
    use_accounts("GOOD", "WEEKLY")
    store.save_account("good", "", "team_premium")  # 6.25x Pro, with a 5-hour and a weekly limit
    store.save_account("weekly", "", "ultra")  # 10x Pro, weekly only
    await call(gw)  # alice, on good: 42% of its 5-hour limit, 10% of its weekly
    await call(gw, dev(store.new_key("bob"), "s2"))  # bob, on weekly: 10% of its weekly
    snap = await status(gw)
    assert [snap["accounts"][l]["size"] for l in ("good", "weekly")] == [6.25, 10]
    assert snap["accounts"]["good"]["weight"] == pytest.approx({"five_hour": 1, "seven_day": 6.25 / 16.25})  # the pool tiles'
    assert snap["accounts"]["weekly"]["weight"] == pytest.approx({"seven_day": 10 / 16.25})
    alice, bob = snap["developers"]["alice"], snap["developers"]["bob"]
    assert alice["five_hour"] == pytest.approx(0.42)  # good is the only seat with a 5-hour limit
    assert alice["seven_day"] == pytest.approx(0.10 * 6.25 / 16.25) and bob["seven_day"] == pytest.approx(0.10 * 10 / 16.25)


async def test_status_shows_usage_blocks_and_developers_but_no_tokens_and_survives_a_restart(gw):
    await call(gw)
    snap = await status(gw)
    acc = snap["accounts"]
    assert acc["limited"]["blocked_kind"] == "limit" and acc["limited"]["blocked_until"] > time.time() + 7000
    assert acc["good"]["five_hour"] == 0.42 and acc["good"]["requests"] == 1
    assert time.time() - acc["good"]["usage_at"] < 5  # when that usage was heard, for the page's "as of"
    assert {n: d["requests"] for n, d in snap["developers"].items()} == {"alice": 1}
    assert "sk-ant" not in str(snap)
    await gw.close()  # shutting down saves usage
    assert Pool().accounts["limited"]["blocked_until"] > time.time()


async def test_removed_accounts_and_developers_are_forgotten(gw):
    await call(gw)
    use_accounts("LIMITED")
    store.write("DELETE FROM keys")
    gw.pool.tick()  # so a label or name added again starts fresh
    assert set(gw.pool.accounts) == {"limited"} and not gw.pool.devs


def test_a_streams_later_nulls_and_zeros_keep_its_counts():
    start = b'{"message": {"model": "m", "usage": {"input_tokens": 100, "output_tokens": 1}}}'
    delta = b'{"usage": {"input_tokens": null, "cache_read_input_tokens": 0, "output_tokens": 50}}'
    reply = b"event: message_start\ndata: " + start + b"\n\nevent: message_delta\ndata: " + delta
    assert usage_of(reply) == ("m", {"input_tokens": 100, "output_tokens": 50, "cache_read_input_tokens": 0})


def test_a_window_past_its_reset_reads_zero():
    assert used({"five_hour": 0.9, "five_hour_reset": time.time() - 1}, "five_hour", time.time()) == 0
    assert used({}, "five_hour", time.time()) is None
