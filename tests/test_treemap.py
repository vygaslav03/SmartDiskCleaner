import random
import unittest

from app.core.treemap import squarify


class SquarifyTests(unittest.TestCase):
    def test_empty_and_zero(self):
        self.assertEqual(squarify([], 0, 0, 100, 100), [])
        self.assertEqual(squarify([("a", 0), ("b", -5)], 0, 0, 100, 100), [])
        self.assertEqual(squarify([("a", 10)], 0, 0, 0, 100), [])

    def test_single_fills_all(self):
        (r,) = squarify([("a", 42)], 10, 20, 300, 200)
        self.assertEqual((r.key, r.x, r.y), ("a", 10, 20))
        self.assertAlmostEqual(r.w, 300)
        self.assertAlmostEqual(r.h, 200)

    def test_skips_non_positive(self):
        keys = {r.key for r in squarify([("a", 5), ("b", 0), ("c", 3)], 0, 0, 10, 10)}
        self.assertEqual(keys, {"a", "c"})

    def test_areas_proportional_and_cover(self):
        rnd = random.Random(7)
        items = [(i, rnd.randint(1, 1000)) for i in range(60)]
        W, H = 800.0, 450.0
        rects = squarify(items, 0, 0, W, H)
        total = sum(v for _, v in items)
        self.assertEqual(len(rects), len(items))
        self.assertAlmostEqual(sum(r.area for r in rects), W * H, delta=1e-6 * W * H)
        weights = dict(items)
        for r in rects:
            self.assertAlmostEqual(r.area, W * H * weights[r.key] / total, delta=1e-6 * W * H)
            self.assertGreaterEqual(r.x, -1e-9)
            self.assertGreaterEqual(r.y, -1e-9)
            self.assertLessEqual(r.x + r.w, W + 1e-6)
            self.assertLessEqual(r.y + r.h, H + 1e-6)

    def test_no_overlap(self):
        rects = squarify([(i, v) for i, v in enumerate([50, 30, 10, 6, 3, 1])], 0, 0, 200, 100)
        for i, a in enumerate(rects):
            for b in rects[i + 1:]:
                ox = min(a.x + a.w, b.x + b.w) - max(a.x, b.x)
                oy = min(a.y + a.h, b.y + b.h) - max(a.y, b.y)
                self.assertFalse(ox > 1e-6 and oy > 1e-6, (a, b))

    def test_aspect_reasonable(self):
        rects = squarify([(i, 1) for i in range(16)], 0, 0, 400, 400)
        for r in rects:
            self.assertLess(max(r.w / r.h, r.h / r.w), 2.01)

    def test_sorted_desc_and_contains(self):
        rects = squarify([("s", 1), ("b", 9)], 0, 0, 100, 100)
        self.assertEqual(rects[0].key, "b")
        self.assertTrue(rects[0].contains(rects[0].x + 1, rects[0].y + 1))
        self.assertFalse(rects[0].contains(rects[0].x + rects[0].w, rects[0].y))


if __name__ == "__main__":
    unittest.main()
