"""Недоступные ролики временно исключаются из плана и заменяются в рамках квоты."""

import asyncio
from datetime import datetime

import pytest

import db
import downloader
import scheduler

from conftest import FakeJobQueue
from test_plan_rule import _make_rule_with_source, _FixedDatetime

BLOCKED = ("ERROR: [TikTok] 7520139243537141010: "
           "Your IP address is blocked from accessing this post")


@pytest.mark.parametrize("message, expected", [
    ("[TikTok] 1: This video has been removed", "This video has been removed"),
    ("[TikTok] 1: You do not have permission to view this post. Log into an account",
     "You do not have permission to view this post"),
    ("Unable to download webpage: Read timed out", None),       # сеть — не навсегда
    ("unable to download video data: HTTP Error 403: Forbidden", None),
    ("Видео слишком длинное — 5:00. Максимум 3:00.", None),     # лимит могут поднять
])
def test_unavailable_reason(message, expected):
    assert downloader.unavailable_reason(RuntimeError(message)) == expected


def test_tiktok_ip_block_is_final_only_after_proxy(monkeypatch):
    monkeypatch.setattr(downloader, "DOWNLOAD_PROXY", None)
    assert downloader.unavailable_reason(RuntimeError(BLOCKED)) is None   # прокси мог бы помочь
    monkeypatch.setattr(downloader, "DOWNLOAD_PROXY", "socks5h://xray:1080")
    assert downloader.unavailable_reason(RuntimeError(BLOCKED))


def test_plan_rule_skips_unavailable_videos(user_group_source, tmp_path):
    rid = _make_rule_with_source(user_group_source, str(tmp_path), n_videos=3, videos_per_day=2)
    db.mark_unavailable("v1", "удалён", ttl_seconds=100)
    scheduler.plan_rule(FakeJobQueue(), db.get_rule(rid))
    assert {p["tt_video_id"] for p in db.get_scheduled_posts()} == {"v2", "v3"}


def test_unavailable_is_shared_between_rules_and_idempotent():
    db.mark_unavailable("v9", "удалён", ttl_seconds=100)
    db.mark_unavailable("v9", "скрыт", ttl_seconds=100)
    assert db.get_unavailable_ids() == {"v9"}


def test_unavailable_video_stays_excluded_when_repost_is_enabled(
    user_group_source, tmp_path, monkeypatch,
):
    rid = _make_rule_with_source(user_group_source, str(tmp_path), n_videos=2, videos_per_day=1)
    db.update_rule(rid, allow_repost=1)
    yesterday = scheduler.datetime.now(scheduler.MOSCOW_TZ).timestamp() - 86400
    monkeypatch.setattr(db.time, "time", lambda: yesterday)
    db.mark_unavailable("v1", "удалён", ttl_seconds=100)
    db.mark_published(rid, "v2")

    assert scheduler.plan_rule(FakeJobQueue(), db.get_rule(rid)) == 1
    assert db.get_scheduled_ids(rid) == {"v2"}


def test_manual_test_post_skips_unavailable_video(user_group_source, tmp_path):
    rid = _make_rule_with_source(user_group_source, str(tmp_path), n_videos=2)
    db.mark_unavailable("v1", "удалён", ttl_seconds=100)

    ok, _ = scheduler.schedule_test_now(FakeJobQueue(), rid)

    assert ok
    assert db.get_scheduled_ids(rid) == {"v2"}


# ─── Замена недоступного ролика следующим ─────────────────────────────────────

class _Bot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append(text)


class _Ctx:
    def __init__(self, post_id):
        self.job = type("J", (), {"data": {"scheduled_post_id": post_id}})()
        self.job_queue = FakeJobQueue()
        self.bot = _Bot()


def test_replacement_takes_next_video(user_group_source, tmp_path):
    rid = _make_rule_with_source(user_group_source, str(tmp_path), n_videos=3)
    db.mark_unavailable("v1", "удалён", ttl_seconds=100)
    jq = FakeJobQueue()
    assert scheduler.schedule_replacement(jq, rid) == "https://tiktok/v2"
    assert [p["tt_video_id"] for p in db.get_scheduled_posts()] == ["v2"]
    assert len(jq.jobs) == 1


def test_no_replacement_for_disabled_rule(user_group_source, tmp_path):
    rid = _make_rule_with_source(user_group_source, str(tmp_path), n_videos=3)
    db.update_rule(rid, enabled=0)
    assert scheduler.schedule_replacement(FakeJobQueue(), rid) is None


def _queue_video(rid, video_id="v1", publish_at=None):
    rule = db.get_rule(rid)
    if publish_at is None:
        publish_at = int(scheduler.datetime.now(scheduler.MOSCOW_TZ).timestamp())
    return db.add_scheduled_post(
        telegram_id=rule["telegram_id"], rule_id=rid, tt_video_id=video_id,
        url=f"https://tiktok/{video_id}", title="t", description=None,
        vk_group_id=rule["vk_group_id"], vk_group_name=rule["group_name"],
        publish_at=publish_at, account_id=rule["account_id"],
    )


def test_dead_video_is_replaced_by_next(user_group_source, tmp_path, monkeypatch):
    rid = _make_rule_with_source(user_group_source, str(tmp_path), n_videos=3)
    post_id = _queue_video(rid)
    monkeypatch.setattr(downloader, "DOWNLOAD_PROXY", "socks5h://xray:1080")

    async def dead(url, token):
        raise RuntimeError(BLOCKED)

    monkeypatch.setattr(scheduler, "_download", dead)
    ctx = _Ctx(post_id)
    asyncio.run(scheduler._publish_job(ctx))

    assert db.get_unavailable_ids() == {"v1"}
    assert [p["tt_video_id"] for p in db.get_scheduled_posts()] == ["v2"]   # v1 убран, v2 встал
    assert len(ctx.job_queue.jobs) == 1
    delay = ctx.job_queue.jobs[0]["when"] - scheduler.datetime.now(scheduler.MOSCOW_TZ)
    assert delay.total_seconds() == scheduler.REPLACEMENT_DELAY_SECONDS
    assert "https://tiktok/v2" in ctx.bot.sent[-1]


@pytest.mark.parametrize("message", ["Read timed out", "HTTP Error 403: Forbidden"])
def test_network_error_is_not_replaced(user_group_source, tmp_path, monkeypatch, message):
    rid = _make_rule_with_source(user_group_source, str(tmp_path), n_videos=3)
    post_id = _queue_video(rid)

    async def flaky(url, token):
        raise RuntimeError(message)

    monkeypatch.setattr(scheduler, "_download", flaky)
    ctx = _Ctx(post_id)
    asyncio.run(scheduler._publish_job(ctx))

    assert not db.get_unavailable_ids()
    assert not db.get_scheduled_posts() and not ctx.job_queue.jobs


@pytest.fixture(autouse=True)
def _reset_streak_and_clock(monkeypatch):
    monkeypatch.setattr(scheduler, "datetime", _FixedDatetime)
    scheduler._dead_streak.clear()
    yield
    scheduler._dead_streak.clear()


def test_unavailable_video_returns_to_plan(user_group_source, tmp_path, monkeypatch):
    rid = _make_rule_with_source(user_group_source, str(tmp_path), n_videos=1)
    monkeypatch.setattr(scheduler, "MAX_REPLACEMENTS_IN_ROW", 0)

    async def dead(url, token):
        raise RuntimeError("This video has been removed")

    monkeypatch.setattr(scheduler, "_download", dead)
    now = int(db.time.time())
    monkeypatch.setattr(db.time, "time", lambda: now)
    asyncio.run(scheduler._publish_job(_Ctx(_queue_video(rid))))
    assert scheduler.plan_rule(FakeJobQueue(), db.get_rule(rid)) == 0
    monkeypatch.setattr(db.time, "time", lambda: now + scheduler.UNAVAILABLE_RETRY_DAYS * 86400)
    assert scheduler.plan_rule(FakeJobQueue(), db.get_rule(rid)) == 1
    assert db.get_scheduled_ids(rid) == {"v1"}


def test_replacement_chain_stops_after_limit(user_group_source, tmp_path, monkeypatch):
    rid = _make_rule_with_source(user_group_source, str(tmp_path), n_videos=6)
    post_id = _queue_video(rid)
    monkeypatch.setattr(downloader, "DOWNLOAD_PROXY", "socks5h://xray:1080")

    async def dead(url, token):
        raise RuntimeError(BLOCKED)

    monkeypatch.setattr(scheduler, "_download", dead)
    sent = []
    for _ in range(scheduler.MAX_REPLACEMENTS_IN_ROW + 1):
        ctx = _Ctx(post_id)
        asyncio.run(scheduler._publish_job(ctx))
        sent += ctx.bot.sent
        queued = db.get_scheduled_posts()
        post_id = queued[0]["id"] if queued else None

    assert post_id is None                                   # цепочка оборвалась
    assert len(sent) == scheduler.MAX_REPLACEMENTS_IN_ROW + 1
    assert "Замены остановил" in sent[-1]
    assert len(db.get_unavailable_ids()) == scheduler.MAX_REPLACEMENTS_IN_ROW + 1


def test_replacement_preserves_dedup_and_fills_one_slot(user_group_source, tmp_path, monkeypatch):
    rid = _make_rule_with_source(user_group_source, str(tmp_path), n_videos=5, videos_per_day=3)
    now = scheduler.datetime.now(scheduler.MOSCOW_TZ).timestamp()
    monkeypatch.setattr(db.time, "time", lambda: now)
    db.mark_published(rid, "v2")
    _queue_video(rid, "v3")
    post_id = _queue_video(rid)

    async def dead(url, token):
        raise RuntimeError("This video has been removed")

    monkeypatch.setattr(scheduler, "_download", dead)
    ctx = _Ctx(post_id)
    asyncio.run(scheduler._publish_job(ctx))
    assert db.get_scheduled_ids(rid) == {"v3", "v4"}
    assert db.get_published_ids(rid) == {"v2"}
    assert scheduler.plan_rule(ctx.job_queue, db.get_rule(rid)) == 0


def test_replacement_does_not_overfill_tomorrow(user_group_source, tmp_path, monkeypatch):
    class LateDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return scheduler.MOSCOW_TZ.localize(datetime(2026, 7, 23, 23, 59, 45))

    monkeypatch.setattr(scheduler, "datetime", LateDatetime)
    rid = _make_rule_with_source(user_group_source, str(tmp_path), videos_per_day=1)
    tomorrow = scheduler.MOSCOW_TZ.localize(datetime(2026, 7, 24, 9))
    _queue_video(rid, "v3", publish_at=int(tomorrow.timestamp()))
    post_id = _queue_video(rid)

    async def dead(url, token):
        raise RuntimeError("This video has been removed")

    monkeypatch.setattr(scheduler, "_download", dead)
    ctx = _Ctx(post_id)
    asyncio.run(scheduler._publish_job(ctx))
    assert not ctx.job_queue.jobs
    assert db.get_scheduled_ids(rid) == {"v3"}
    assert scheduler.plan_rule(ctx.job_queue, db.get_rule(rid)) == 0


def test_success_resets_replacement_streak(user_group_source, tmp_path, monkeypatch):
    rid = _make_rule_with_source(user_group_source, str(tmp_path))
    scheduler._dead_streak[rid] = scheduler.MAX_REPLACEMENTS_IN_ROW + 1

    async def download(url, token):
        return str(tmp_path / "video.mp4"), "Title"

    async def publish(*args):
        pass

    monkeypatch.setattr(scheduler, "_download", download)
    monkeypatch.setattr(scheduler, "_publish_to_vk", publish)
    asyncio.run(scheduler._publish_job(_Ctx(_queue_video(rid))))
    assert rid not in scheduler._dead_streak
    assert db.get_published_ids(rid) == {"v1"}


def test_cancelled_publication_keeps_queue_entry(user_group_source, tmp_path, monkeypatch):
    rid = _make_rule_with_source(user_group_source, str(tmp_path))
    ctx = _Ctx(_queue_video(rid))
    video = tmp_path / "video.mp4"
    video.write_bytes(b"test")

    async def download(url, token):
        return str(video), "Title"

    async def cancel_during_wait():
        publication_started = asyncio.Event()

        async def publish(*args):
            publication_started.set()
            await asyncio.Future()

        monkeypatch.setattr(scheduler, "_publish_to_vk", publish)
        task = asyncio.create_task(scheduler._publish_job(ctx))
        await asyncio.wait_for(publication_started.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    monkeypatch.setattr(scheduler, "_download", download)
    asyncio.run(cancel_during_wait())

    assert db.get_scheduled_ids(rid) == {"v1"}
    assert not db.get_published_ids(rid)
    assert not ctx.bot.sent
    assert not video.exists()
