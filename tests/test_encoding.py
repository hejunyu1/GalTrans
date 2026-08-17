from __future__ import annotations

import unittest

from galtrans.encoding import detect_and_decode


class EncodingTests(unittest.TestCase):
    def test_detects_utf8(self) -> None:
        decoded = detect_and_decode("こんにちは，世界".encode())
        self.assertIsNotNone(decoded)
        assert decoded is not None
        self.assertEqual(decoded.encoding, "utf-8")
        self.assertEqual(decoded.text, "こんにちは，世界")

    def test_detects_cp932(self) -> None:
        decoded = detect_and_decode("彼女は笑った。".encode("cp932"))
        self.assertIsNotNone(decoded)
        assert decoded is not None
        self.assertEqual(decoded.encoding, "cp932")
        self.assertEqual(decoded.text, "彼女は笑った。")

    def test_rejects_control_heavy_binary_data(self) -> None:
        self.assertIsNone(detect_and_decode(bytes(range(32))))


if __name__ == "__main__":
    unittest.main()

