"""The run's size budget: run and work limits, stop and skip."""

from __future__ import annotations

import threading

from chronodownloader._budget import GB, MB, SizeBudget
from chronodownloader.settings import DownloadLimits
from chronodownloader.spec import SizeLimits


def test_flags_override_the_settings_and_zero_means_no_limit() -> None:
    budget = SizeBudget.of(
        SizeLimits(total_images_gb=2, work_pdfs_gb=0, on_exceed="skip"),
        DownloadLimits(),
    )

    assert budget.total == {"image": 2 * GB, "pdf": 50 * GB, "metadata": 1 * GB}
    assert budget.per_work == {"image": 5 * GB, "pdf": None, "metadata": 250 * MB}
    assert budget.on_exceed == "skip"


def test_a_work_limit_refuses_the_work_but_not_the_others() -> None:
    budget = SizeBudget({"image": None}, {"image": 100}, "stop")
    first, second = budget.for_work("E1"), budget.for_work("E2")

    first.charge("image", 100)

    assert not first.allows("image")
    assert second.allows("image")
    assert not budget.stopped


def test_a_run_limit_stops_the_run_under_stop_only() -> None:
    for policy, stopped in (("stop", True), ("skip", False)):
        budget = SizeBudget({"pdf": 10}, {"pdf": None}, policy)
        work = budget.for_work("E1")
        assert work.allows("pdf")

        work.charge("pdf", 10)

        assert not budget.for_work("E2").allows("pdf")
        assert budget.stopped is stopped
        assert budget.used("pdf") == 10
        assert budget.used("pdf", "E1") == 10


def test_concurrent_charges_are_all_counted() -> None:
    budget = SizeBudget({"image": None}, {"image": None})

    def charge() -> None:
        for _ in range(1000):
            budget.for_work("E1").charge("image", 1)

    threads = [threading.Thread(target=charge) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert budget.used("image") == 4000
