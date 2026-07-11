"""Tests for CronService._arm_timer guarding against re-arm while executing."""

import asyncio
import contextlib

from vikingbot.cron.service import CronService


async def test_arm_timer_does_not_cancel_running_tick(tmp_path):
    """While a tick is executing jobs, _arm_timer must not cancel/replace the timer."""
    service = CronService(store_path=tmp_path / "cron.json")
    service._running = True

    async def _long_sleep():
        await asyncio.sleep(3600)

    original_task = asyncio.create_task(_long_sleep())
    service._timer_task = original_task
    service._executing = True  # simulate a tick in progress

    service._arm_timer()

    # Yield control so any (buggy) cancellation would take effect.
    await asyncio.sleep(0)

    assert service._timer_task is original_task
    assert not original_task.cancelled()

    original_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await original_task


async def test_arm_timer_reschedules_when_not_executing(tmp_path):
    """Sanity check: when not executing, _arm_timer replaces the timer task."""
    service = CronService(store_path=tmp_path / "cron.json")
    service._running = True

    async def _long_sleep():
        await asyncio.sleep(3600)

    stale_task = asyncio.create_task(_long_sleep())
    service._timer_task = stale_task
    service._executing = False

    service._arm_timer()
    await asyncio.sleep(0)

    # No jobs scheduled -> _get_next_wake_ms returns None -> no new task armed,
    # but the stale task must have been cancelled (not left running).
    assert stale_task.cancelled()

    for task in {stale_task, service._timer_task}:
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
