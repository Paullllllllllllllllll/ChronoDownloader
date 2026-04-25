"""Quota and deferred-queue CLI commands.

Implements the ``--quota-status`` display and ``--cleanup-deferred``
maintenance command.
"""
from __future__ import annotations

from datetime import datetime, timezone

from main.state.background import get_background_scheduler
from main.state.deferred import get_deferred_queue
from main.state.quota import get_quota_manager


def show_quota_status() -> None:
    """Display quota and deferred queue status."""
    print("\n" + "=" * 60)
    print("QUOTA & DEFERRED QUEUE STATUS")
    print("=" * 60)

    quota_manager = get_quota_manager()
    quota_providers = quota_manager.get_quota_limited_providers()

    if quota_providers:
        print("\n[QUOTA STATUS]")
        for provider_key in quota_providers:
            status = quota_manager.get_quota_status(provider_key)
            remaining = status["remaining"]
            daily_limit = status["daily_limit"]
            used = status["downloads_used"]

            if status["is_exhausted"]:
                reset_secs = status["seconds_until_reset"]
                hours = reset_secs / 3600
                print(
                    f"  * {provider_key}: {used}/{daily_limit} used "
                    f"(EXHAUSTED - resets in {hours:.1f}h)"
                )
            else:
                print(
                    f"  * {provider_key}: {used}/{daily_limit} used "
                    f"({remaining} remaining)"
                )
    else:
        print("\n[QUOTA STATUS] No quota-limited providers configured.")

    queue = get_deferred_queue()
    counts = queue.count_by_status()
    pending = counts.get("pending", 0) + counts.get("retrying", 0)
    completed = counts.get("completed", 0)
    failed = counts.get("failed", 0)

    print("\n[DEFERRED QUEUE]")
    print(f"  * Pending: {pending}")
    print(f"  * Completed: {completed}")
    print(f"  * Failed: {failed}")

    if pending > 0:
        next_ready = queue.get_next_ready_time()
        if next_ready:
            now = datetime.now(timezone.utc)
            delta = (next_ready - now).total_seconds()
            if delta > 0:
                hours = delta / 3600
                print(f"  * Next retry in: {hours:.1f} hours")
            else:
                print("  * Ready for retry NOW")

        print("\n  Pending items:")
        for item in queue.get_pending()[:10]:
            title_display = item.title[:50] if len(item.title) > 50 else item.title
            print(f"    - {title_display} ({item.provider_name})")
        if pending > 10:
            print(f"    ... and {pending - 10} more")

    scheduler = get_background_scheduler()
    print("\n[BACKGROUND SCHEDULER]")
    if scheduler.is_running():
        stats = scheduler.get_stats()
        print("  * Status: RUNNING")
        print(f"  * Checks: {stats.get('checks', 0)}")
        print(f"  * Retries attempted: {stats.get('retries_attempted', 0)}")
        print(f"  * Retries succeeded: {stats.get('retries_succeeded', 0)}")
    else:
        print("  * Status: STOPPED")

    print("\n" + "=" * 60 + "\n")


def cleanup_deferred_queue() -> None:
    """Remove completed items from the deferred queue."""
    queue = get_deferred_queue()

    counts_before = queue.count_by_status()
    completed_before = counts_before.get("completed", 0)

    removed = queue.clear_completed()

    print(f"Cleaned up {removed} completed item(s) from deferred queue.")

    counts_after = queue.count_by_status()
    pending = counts_after.get("pending", 0) + counts_after.get("retrying", 0)
    failed = counts_after.get("failed", 0)
    print(f"Remaining: {pending} pending, {failed} failed")
