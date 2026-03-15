import pytest
from app.core.skip_list import SkipList


def test_insert_and_rank():
    sl = SkipList()
    sl.insert("alice", 100, 1.0)
    sl.insert("bob",   200, 2.0)
    sl.insert("carol", 150, 3.0)
    assert sl.get_rank("bob",   200, 2.0) == 1
    assert sl.get_rank("carol", 150, 3.0) == 2
    assert sl.get_rank("alice", 100, 1.0) == 3


def test_delete():
    sl = SkipList()
    sl.insert("alice", 100, 1.0)
    sl.insert("bob",   200, 2.0)
    assert sl.delete("bob", 200, 2.0)
    assert sl.get_rank("alice", 100, 1.0) == 1
    assert sl.size == 1


def test_top_k():
    sl = SkipList()
    for i, name in enumerate(["a", "b", "c", "d", "e"]):
        sl.insert(name, (5 - i) * 100, float(i))
    top3 = sl.get_top(3)
    assert [n.player_id for n in top3] == ["a", "b", "c"]


def test_tie_breaking():
    sl = SkipList()
    sl.insert("early", 500, 1.0)
    sl.insert("late",  500, 2.0)
    assert sl.get_rank("early", 500, 1.0) == 1
    assert sl.get_rank("late",  500, 2.0) == 2


def test_range():
    sl = SkipList()
    for i in range(10):
        sl.insert(f"p{i}", float(10 - i), float(i))
    r = sl.get_range(3, 5)
    assert len(r) == 3
