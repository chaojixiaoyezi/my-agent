from __future__ import annotations

from queue import Queue

from agent_py_agent.cli.chat_parts.chat_prompt_queue import pop_all_matching


def test_pop_all_matching_keeps_fifo_and_queue_join_accounting() -> None:
    jobs: Queue[int] = Queue()
    for item in (1, 2, 3, 4):
        jobs.put(item)

    assert pop_all_matching(jobs, lambda item: item % 2 == 0) == (2, 4)
    assert jobs.unfinished_tasks == 2
    assert jobs.get_nowait() == 1
    jobs.task_done()
    assert jobs.get_nowait() == 3
    jobs.task_done()
    assert jobs.unfinished_tasks == 0


def test_pop_all_matching_no_match_does_not_mutate_queue() -> None:
    jobs: Queue[str] = Queue()
    jobs.put("keep")

    assert pop_all_matching(jobs, lambda _item: False) == ()
    assert jobs.qsize() == 1
    assert jobs.unfinished_tasks == 1
