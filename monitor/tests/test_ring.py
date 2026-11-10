import numpy as np

from printmon.ring import RingBuffer


def test_tail_before_and_after_wrap():
    r = RingBuffer(5, ("a", "b"))
    for i in range(3):
        r.append(i, [i, 10 * i])
    t, v = r.tail(10)
    assert t.tolist() == [0, 1, 2]
    for i in range(3, 12):
        r.append(i, [i, 10 * i])
    t, v = r.tail(5)
    assert t.tolist() == [7, 8, 9, 10, 11]
    assert v[:, r.col("b")].tolist() == [70, 80, 90, 100, 110]
    assert r.total == 12 and len(r) == 5


def test_window_selects_by_time_across_wrap():
    r = RingBuffer(8, ("v",))
    for i in range(20):
        r.append(i * 100, [i])
    t, _ = r.window(1450)
    assert t.tolist() == [1500, 1600, 1700, 1800, 1900]
    t, _ = r.window(0)  # older than retained: everything retained
    assert len(t) == 8
    t, _ = r.window(10_000)
    assert len(t) == 0


def test_window_on_empty_and_exact_capacity():
    r = RingBuffer(4, ("v",))
    assert len(r.window(0)[0]) == 0
    for i in range(4):
        r.append(i, [i])
    t, v = r.window(2)
    assert t.tolist() == [2, 3]
    assert np.allclose(v[:, 0], [2, 3])


def test_between_selects_a_range_across_wrap():
    r = RingBuffer(8, ("v",))
    for i in range(20):
        r.append(i * 100, [i])
    t, v = r.between(1300, 1600)
    assert t.tolist() == [1300, 1400, 1500, 1600] and v[:, 0].tolist() == [13, 14, 15, 16]
    assert r.between(0, 1100)[0].tolist() == []  # evicted history is simply absent
    assert r.first_t() == 1200
