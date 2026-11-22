import tempfile
import unittest
from pathlib import Path

from researchtool.gapr import matrix as mx
from researchtool.gapr import schema

FIX = Path(__file__).parent / "fixtures" / "gapr_ltsf_2022"


def load():
    return mx.load_matrix(FIX / "matrix.csv")


class TestLtsfBacktestMatrix(unittest.TestCase):
    """复现 docs/backtest-ltsf-2022.md 里手工得出的候选。"""

    def test_r1_columns(self):
        a = mx.analyze(load())
        self.assertEqual([c.attrs[0] for c in a.r1], ["S", "V", "Z"])

    def test_r1_partial_coverage_listed(self):
        a = mx.analyze(load())
        partial = {c.attrs[0]: c.partial for c in a.r1}
        self.assertEqual(partial["S"], ["N-BEATS / N-HiTS", "Autoformer / FEDformer"])
        self.assertEqual(partial["Z"], ["DeepAR", "N-BEATS / N-HiTS"])
        self.assertEqual(set(partial["V"]), {"TFT", "Informer / Pyraformer",
                                             "Autoformer / FEDformer", "Non-stationary Transformer"})

    def test_supported_columns(self):
        self.assertEqual(mx.analyze(load()).supported, ["L", "D", "P", "H"])

    def test_r2_pairs(self):
        a = mx.analyze(load())
        self.assertEqual([c.attrs for c in a.r2], [("L", "D"), ("L", "P"), ("D", "P"), ("P", "H")])

    def test_r2_covered_pairs(self):
        a = mx.analyze(load())
        self.assertEqual(a.covered_pairs[("L", "H")], ["DLinear"])
        self.assertEqual(a.covered_pairs[("D", "H")], ["RevIN", "Non-stationary Transformer"])

    def test_r2_partial_for_L_D(self):
        a = mx.analyze(load())
        ld = next(c for c in a.r2 if c.attrs == ("L", "D"))
        self.assertEqual(ld.partial, ["DLinear(L✓ D△)"])

    def test_no_minimal_triples(self):
        self.assertEqual(mx.analyze(load()).r2p, [])

    def test_partial_as_cover_would_hide_S_V_Z(self):
        """锁住规则：如果把 △ 算作覆盖，回测里命中的 #1、#3、#4 都会被漏掉。"""
        a = mx.analyze(load(), partial_counts_as_cover=True)
        r1 = [c.attrs[0] for c in a.r1]
        for col in ("S", "V", "Z"):
            self.assertNotIn(col, r1)

    def test_incompatible_pair_skipped(self):
        a = mx.analyze(load(), {frozenset(("P", "H"))})
        self.assertNotIn(("P", "H"), [c.attrs for c in a.r2])
        self.assertIn(("P", "H"), a.skipped_incompatible)


class TestTriples(unittest.TestCase):
    def test_minimal_triple_found(self):
        # A、B、C 两两都有方法同时 ✓，但没有一行三者同时 ✓
        m = mx.Matrix(
            attrs=["A", "B", "C"], methods=["m1", "m2", "m3"],
            cells={("m1", "A"): "Y", ("m1", "B"): "Y", ("m1", "C"): "N",
                   ("m2", "A"): "N", ("m2", "B"): "Y", ("m2", "C"): "Y",
                   ("m3", "A"): "Y", ("m3", "B"): "P", ("m3", "C"): "Y"})
        a = mx.analyze(m)
        self.assertEqual(a.r2, [])
        self.assertEqual([c.attrs for c in a.r2p], [("A", "B", "C")])
        self.assertEqual(a.r2p[0].partial, ["m3(A✓ B△ C✓)"])
        # △ 当覆盖时 m3 填掉了这个三元组
        self.assertEqual(mx.analyze(m, partial_counts_as_cover=True).r2p, [])


class TestParsing(unittest.TestCase):
    def _write(self, text):
        d = Path(tempfile.mkdtemp())
        p = d / "matrix.csv"
        p.write_text(text, encoding="utf-8")
        return p

    def test_ascii_aliases(self):
        m = mx.load_matrix(self._write("method,A,B\nm1,Y,P\nm2,n,x\n"))
        self.assertEqual(m.get("m1", "A"), "Y")
        self.assertEqual(m.get("m1", "B"), "P")
        self.assertEqual(m.get("m2", "B"), "N")

    def test_invalid_and_empty_cells_rejected(self):
        with self.assertRaises(ValueError) as e:
            mx.load_matrix(self._write("method,A,B\nm1,?,\n"))
        self.assertIn("无法识别", str(e.exception))
        self.assertIn("格子为空", str(e.exception))

    def test_bom_csv(self):
        p = self._write("")
        schema.write_rows(p, ["method", "A"], [{"method": "m1", "A": "✓"}])
        self.assertEqual(mx.load_matrix(p).get("m1", "A"), "Y")


class TestDangling(unittest.TestCase):
    def test_r3(self):
        defects = [{"id": "d1", "node_id": "n1", "defect": "x"},
                   {"id": "d2", "node_id": "n1", "defect": "y"}]
        edges = [{"parent_id": "n1", "defect_id": "d1", "child_id": "n2"}]
        self.assertEqual([c.ref for c in mx.dangling_defects(defects, edges)], ["d2"])


if __name__ == "__main__":
    unittest.main()
