"""Тесты планировщика: раскладка по слотам и plan_rule (без сети)."""

import asyncio
from datetime import datetime, timedelta

import pytest
import pytz

import scheduler

MOSCOW_TZ = pytz.timezone("Europe/Moscow")


def test_parse_slots():
    assert scheduler.parse_slots("9,15,20") == [9, 15, 20]
    assert scheduler.parse_slots("20,9,9,15") == [9, 15, 20]  # сорт + дедуп
    assert scheduler.parse_slots("25, -1, abc, 12") == [12]  # мусор и вне 0..23 отброшены
    assert scheduler.parse_slots("") == []


def test_publish_window_within_today():
    now = MOSCOW_TZ.localize(datetime(2026, 7, 23, 10, 0))
    start, end = scheduler._publish_window(now)
    assert start.date() == now.date() and end.date() == now.date()
    assert start.hour == 10 and start.minute == 2  # окно уже началось -> от текущего момента
    assert end.hour == scheduler.PUBLISH_WINDOW_END_HOUR


def test_publish_window_before_start_uses_window_start():
    now = MOSCOW_TZ.localize(datetime(2026, 7, 23, 3, 0))
    start, end = scheduler._publish_window(now)
    assert start.hour == scheduler.PUBLISH_WINDOW_START_HOUR
    assert start.date() == now.date()


def test_publish_window_rolls_to_tomorrow():
    now = MOSCOW_TZ.localize(datetime(2026, 7, 23, 22, 0))
    start, end = scheduler._publish_window(now)
    # окно сегодня уже кончилось -> целиком завтрашнее
    assert start.date() == now.date() + timedelta(days=1)
    assert start.hour == scheduler.PUBLISH_WINDOW_START_HOUR
    assert end.hour == scheduler.PUBLISH_WINDOW_END_HOUR


def test_compute_publish_times_spread_evenly_in_window():
    now = MOSCOW_TZ.localize(datetime(2026, 7, 23, 0, 0))
    times = scheduler.compute_publish_times(4, 0, 4, now)
    assert len(times) == 4
    # монотонно возрастают и не выходят за пределы окна
    start, end = scheduler._publish_window(now)
    assert all(start <= t < end for t in times)
    assert times == sorted(times)
    # ни одна пара не кучкуется в одной узкой точке — раскиданы по четвертям окна
    span = (end - start).total_seconds()
    step = span / 4
    for i, t in enumerate(times):
        offset = (t - start).total_seconds()
        assert i * step <= offset < (i + 1) * step


def test_compute_publish_times_continues_from_already():
    """Повторный прогон (already>0) продолжает график, а не начинает заново."""
    now = MOSCOW_TZ.localize(datetime(2026, 7, 23, 0, 0))
    first = scheduler.compute_publish_times(4, 0, 2, now)
    second = scheduler.compute_publish_times(4, 2, 2, now)
    assert max(first) < min(second)


def test_compute_publish_times_empty():
    now = MOSCOW_TZ.localize(datetime(2026, 7, 23, 0, 0))
    assert scheduler.compute_publish_times(0, 0, 3, now) == []
    assert scheduler.compute_publish_times(4, 0, 0, now) == []


@pytest.fixture
def vk_clock(monkeypatch):
    now = 0

    async def sleep(seconds):
        nonlocal now
        now += seconds

    monkeypatch.setattr(scheduler.asyncio, "sleep", sleep)
    monkeypatch.setattr(scheduler, "monotonic", lambda: now)
    monkeypatch.setattr(scheduler, "_vk_account_semaphores", {})
    monkeypatch.setattr(scheduler, "_last_vk_post", {})
    monkeypatch.setattr(scheduler, "_vk_flood_until", {})
    monkeypatch.setattr(scheduler, "VK_MIN_POST_INTERVAL_SECONDS", 90)
    monkeypatch.setattr(scheduler, "VK_FLOOD_RETRY_DELAY", 120)
    monkeypatch.setattr(scheduler, "VK_FLOOD_RETRIES", 2)
    monkeypatch.setattr(scheduler, "VK_PUBLISH_RETRIES", 3)
    monkeypatch.setattr(scheduler, "VK_RETRY_BASE_DELAY", 3)
    monkeypatch.setattr(scheduler.random, "uniform", lambda *args: 0)


def test_flood_retries_then_throttles_next_post(vk_clock, monkeypatch):
    calls = []

    def upload(token, group, *args):
        calls.append((group, scheduler.monotonic()))
        if len(calls) == 1:
            raise scheduler.VKError(9, "Flood control", stage="VK wall.post")

    monkeypatch.setattr(scheduler, "upload_to_vk", upload)

    async def publish():
        await scheduler._publish_to_vk(1, "token", 10, "video", "", "")
        await scheduler._publish_to_vk(2, "other", 20, "video", "", "")
        await scheduler._publish_to_vk(1, "token", 11, "video", "", "")

    asyncio.run(publish())
    assert calls == [(10, 0), (10, 120), (20, 120), (11, 210)]


def test_flood_attempt_limit_leaves_account_cooldown(vk_clock, monkeypatch):
    calls = []

    def upload(token, group, *args):
        calls.append((group, scheduler.monotonic()))
        if group == 10:
            raise scheduler.VKError(9, "Flood control")

    monkeypatch.setattr(scheduler, "upload_to_vk", upload)

    async def publish():
        with pytest.raises(scheduler.VKError, match="Flood control"):
            await scheduler._publish_to_vk(1, "token", 10, "video", "", "")
        await scheduler._publish_to_vk(1, "token", 11, "video", "", "")

    asyncio.run(publish())
    assert calls == [(10, 0), (10, 120), (11, 240)]


def test_other_vk_errors_keep_short_exponential_backoff(vk_clock, monkeypatch):
    calls = []

    def upload(*args):
        calls.append(scheduler.monotonic())
        raise scheduler.VKError(6, "Too many requests per second")

    monkeypatch.setattr(scheduler, "upload_to_vk", upload)
    with pytest.raises(scheduler.VKError, match="Too many requests"):
        asyncio.run(scheduler._publish_to_vk(1, "token", 10, "video", "", ""))
    assert calls == [0, 3, 9]


def test_flood_error_with_no_retry_still_sets_cooldown(vk_clock, monkeypatch):
    calls = []

    def upload(token, group, *args):
        calls.append((group, scheduler.monotonic()))
        if group == 10:
            raise scheduler.VKError(9, "Flood control", no_retry=True)

    monkeypatch.setattr(scheduler, "upload_to_vk", upload)

    async def publish():
        with pytest.raises(scheduler.VKError, match="Flood control"):
            await scheduler._publish_to_vk(1, "token", 10, "video", "", "")
        await scheduler._publish_to_vk(1, "token", 11, "video", "", "")

    asyncio.run(publish())
    assert calls == [(10, 0), (11, 120)]


def test_concurrent_posts_share_semaphore_only_with_same_account(vk_clock, monkeypatch):
    monkeypatch.setattr(scheduler, "VK_PUBLISH_CONCURRENCY", 3)
    calls = []

    async def publish_concurrently():
        loop = asyncio.get_running_loop()
        first_upload_entered = asyncio.Event()
        release_first_upload = asyncio.Event()
        second_job_started = asyncio.Event()

        def upload(token, group, *args):
            calls.append((group, scheduler.monotonic()))

        async def run_in_executor(executor, callback):
            callback()
            if calls[-1][0] == 10:
                first_upload_entered.set()
                await release_first_upload.wait()

        async def publish_second():
            second_job_started.set()
            await scheduler._publish_to_vk(1, "token", 11, "video", "", "")

        monkeypatch.setattr(scheduler, "upload_to_vk", upload)
        monkeypatch.setattr(loop, "run_in_executor", run_in_executor)
        first = asyncio.create_task(scheduler._publish_to_vk(1, "token", 10, "video", "", ""))
        await first_upload_entered.wait()
        second = asyncio.create_task(publish_second())
        await second_job_started.wait()
        try:
            await asyncio.wait_for(scheduler._publish_to_vk(2, "other", 20, "video", "", ""), 1)
            assert calls == [(10, 0), (20, 0)]
        finally:
            release_first_upload.set()
            await asyncio.gather(first, second)

    asyncio.run(publish_concurrently())
    assert calls == [(10, 0), (20, 0), (11, 90)]
