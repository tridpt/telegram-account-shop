import unittest

from vietqr import vietqr_payload, vietqr_png


class VietQRTests(unittest.TestCase):
    def test_matches_sepay_virtual_account_qr_payload(self):
        payload = vietqr_payload("970422", "VQRQAMHLG6528", 10_000, "NAPABCDEF123456")
        self.assertEqual(
            payload,
            "00020101021238570010A000000727012700069704220113VQRQAMHLG6528"
            "0208QRIBFTTA53037045405100005802VN62190815NAPABCDEF123456630470A4",
        )
        self.assertTrue(
            vietqr_png("970422", "VQRQAMHLG6528", 10_000, "NAPABCDEF123456")
            .startswith(b"\x89PNG\r\n\x1a\n")
        )

    def test_rejects_invalid_qr_fields(self):
        for args in (
            ("MBBank", "VQRQAMHLG6528", 10_000, "NAPABCDEF123456"),
            ("970422", "bad/account", 10_000, "NAPABCDEF123456"),
            ("970422", "VQRQAMHLG6528", 0, "NAPABCDEF123456"),
            ("970422", "VQRQAMHLG6528", 10_000, "bad reference"),
        ):
            with self.subTest(args=args), self.assertRaises(ValueError):
                vietqr_payload(*args)


if __name__ == "__main__":
    unittest.main()
