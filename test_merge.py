import unittest

from seed_merge import merge3


class MergeTests(unittest.TestCase):
    def test_shortcuts(self):
        self.assertEqual(merge3(b"a\n", b"b\n", b"b\n").content, b"b\n")
        self.assertEqual(merge3(b"a\n", b"a\n", b"b\n").content, b"b\n")
        self.assertEqual(merge3(b"a\n", b"b\n", b"a\n").content, b"b\n")

    def test_non_overlapping_edits_merge(self):
        result = merge3(b"one\ntwo\nthree\n", b"ONE\ntwo\nthree\n", b"one\ntwo\nTHREE\n")
        self.assertEqual(result.content, b"ONE\ntwo\nTHREE\n")

    def test_touching_regions_conflict_but_a_gap_merges(self):
        adjacent = merge3(b"one\ntwo\nthree\n", b"ONE\ntwo\nthree\n", b"one\nTWO\nthree\n")
        self.assertEqual(adjacent.status, "conflict")
        insertion = merge3(b"one\ntwo\n", b"insert\none\ntwo\n", b"ONE\ntwo\n")
        self.assertEqual(insertion.status, "conflict")
        separated = merge3(b"one\ntwo\nthree\n", b"ONE\ntwo\nthree\n", b"one\ntwo\nTHREE\n")
        self.assertEqual(separated.content, b"ONE\ntwo\nTHREE\n")

    def test_overlap_and_identical_edits(self):
        self.assertEqual(merge3(b"a\n", b"ours\n", b"theirs\n").status, "conflict")
        same = merge3(b"a\n", b"changed\n", b"changed\n")
        self.assertEqual((same.status, same.content), ("clean", b"changed\n"))

    def test_same_position_inserts_conflict(self):
        result = merge3(b"base\n", b"ours\nbase\n", b"theirs\nbase\n")
        self.assertEqual(result.status, "conflict")
        self.assertGreaterEqual(result.conflicts, 1)

    def test_crlf_and_missing_final_newline_are_preserved(self):
        crlf = merge3(b"one\r\ngap\r\ntwo\r\n", b"ONE\r\ngap\r\ntwo\r\n", b"one\r\ngap\r\nTWO\r\n")
        self.assertEqual(crlf.content, b"ONE\r\ngap\r\nTWO\r\n")
        no_final = merge3(b"one\ngap\ntwo", b"ONE\ngap\ntwo", b"one\ngap\nTWO")
        self.assertEqual(no_final.content, b"ONE\ngap\nTWO")

    def test_binary_empty_and_delete_with_unrelated_edit(self):
        self.assertEqual(merge3(b"a\x00", b"a\x00", b"a\x00").status, "binary")
        self.assertEqual(merge3(b"", b"ours\n", b"theirs\n").status, "conflict")
        result = merge3(b"keep\ngap\ndelete\nend\n", b"keep\ngap\nend\n", b"KEEP\ngap\ndelete\nend\n")
        self.assertEqual(result.content, b"KEEP\ngap\nend\n")


if __name__ == "__main__":
    unittest.main()
