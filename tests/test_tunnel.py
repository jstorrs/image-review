import unittest

from image_review.tunnel import parse_ssh_host, parse_via


class TestParseVia(unittest.TestCase):
    def test_accepts(self):
        for ok in ["user@login.cluster", "login", "my-alias", "u@10.0.0.1"]:
            with self.subTest(ok=ok):
                self.assertEqual(parse_via(ok), ok)

    def test_rejects(self):
        for bad in [
            "-oProxyCommand=x",
            "",
            "a b",
            "a\nb",
            "a\tb",
            "a\x00b",
            "a\x1bb",
            "login$(cmd)",
            "login`cmd`",
            "a;b",
        ]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_via(bad)


class TestParseSshHost(unittest.TestCase):
    def test_accepts(self):
        for ok in ["node042", "node042.example.org", "10.0.0.1", "[::1]", "a_b-c"]:
            with self.subTest(ok=ok):
                self.assertEqual(parse_ssh_host(ok), ok)

    def test_rejects(self):
        for bad in ["", "-oProxyCommand=x", "a@b", "u@node", "a b", "a\nb", "a;b", "a/b", "a%b", "$(x)"]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_ssh_host(bad)


if __name__ == "__main__":
    unittest.main()
