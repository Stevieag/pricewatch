import unittest
from decimal import Decimal

from pricewatch.config import Secret, load_settings, parse_env_file, validate_settings
from pricewatch.extract import extract
from pricewatch.fetcher import FetchError, check_url_allowed
from pricewatch.util import clean_url, extract_url, parse_frequency, parse_price, url_for_ai
from pricewatch.agent import heuristic_same_product


class TestPrice(unittest.TestCase):
    def test_parse(self):
        cases = {"£1,299.99": "1299.99", "1.299,99 €": "1299.99", "12": "12", "12,5": "12.5",
                 "$0.99": "0.99", "1,000": "1000", "2.000.000": "2000000", 19.5: "19.5"}
        for raw, want in cases.items():
            self.assertEqual(parse_price(raw), Decimal(want), raw)
        self.assertIsNone(parse_price("free"))
        self.assertIsNone(parse_price(None))


class TestFrequency(unittest.TestCase):
    def test_units(self):
        self.assertEqual(parse_frequency("15m"), 15)
        self.assertEqual(parse_frequency("2h"), 120)
        self.assertEqual(parse_frequency("1d"), 1440)
        self.assertEqual(parse_frequency("1w"), 10080)
        self.assertEqual(parse_frequency("1y"), 525600)
        self.assertEqual(parse_frequency("1m"), 5)          # clamped to minimum
        self.assertEqual(parse_frequency("5y"), 525600)     # clamped to a year
        with self.assertRaises(ValueError):
            parse_frequency("often")


class TestUrls(unittest.TestCase):
    def test_extract_from_share_text(self):
        self.assertEqual(extract_url("Look at this! https://shop.example/p/123?x=1."),
                         "https://shop.example/p/123?x=1")

    def test_clean_strips_tracking_and_secrets(self):
        u = clean_url("https://WWW.Shop.example/p/1?utm_source=x&id=9&session_id=abc&token=t&fbclid=z#frag")
        self.assertEqual(u, "https://www.shop.example/p/1?id=9")

    def test_rejects_credentials_and_schemes(self):
        for bad in ("https://user:pw@shop.example/", "file:///etc/passwd", "javascript:alert(1)"):
            with self.assertRaises(ValueError):
                clean_url(bad)

    def test_argos_link_cleaned(self):
        u = ("https://www.argos.co.uk/product/9841129?rec=TLP[]:quickViewComponent:P1:quickView:"
             "alternative:9841129:vKQhiN20GufYRhmJQpmu&_gl=1*1sjsme8*_up*MQ..*_ga*MTk3OTE5")
        self.assertEqual(clean_url(u), "https://www.argos.co.uk/product/9841129")

    def test_ai_view_has_no_query(self):
        self.assertEqual(url_for_ai("https://a.example/p?id=1&k=2"), "https://a.example/p")


class TestSSRF(unittest.TestCase):
    def test_blocks_local(self):
        for u in ("http://127.0.0.1/", "http://localhost:8080/", "http://10.0.0.1/", "http://169.254.169.254/"):
            with self.assertRaises(FetchError, msg=u):
                check_url_allowed(u)
        check_url_allowed("http://127.0.0.1/", allow_private=True)


class TestExtract(unittest.TestCase):
    def test_jsonld(self):
        html = """<html><head><title>x</title><script type="application/ld+json">
        {"@context":"https://schema.org","@graph":[{"@type":"Product","name":"Acme Kettle 1.7L",
        "offers":{"@type":"Offer","price":"39.99","priceCurrency":"GBP",
        "availability":"https://schema.org/InStock"}}]}</script></head></html>"""
        ex = extract(html)
        self.assertEqual((ex.title, ex.price, ex.currency, ex.method, ex.in_stock),
                         ("Acme Kettle 1.7L", Decimal("39.99"), "GBP", "jsonld", True))

    def test_offer_list_takes_lowest(self):
        html = """<script type="application/ld+json">{"@type":"Product","name":"T",
        "offers":[{"price":20,"priceCurrency":"EUR"},{"price":"18.50","priceCurrency":"EUR"}]}</script>"""
        self.assertEqual(extract(html).price, Decimal("18.50"))

    def test_microdata(self):
        html = '<div itemscope><h1 itemprop="name">Lamp</h1><span itemprop="price" content="12.00"></span>' \
               '<meta itemprop="priceCurrency" content="USD"></div>'
        ex = extract(html)
        self.assertEqual((ex.price, ex.currency, ex.method), (Decimal("12.00"), "USD", "microdata"))

    def test_meta(self):
        html = '<meta property="og:title" content="Chair"><meta property="product:price:amount" content="99">' \
               '<meta property="product:price:currency" content="EUR">'
        ex = extract(html)
        self.assertEqual((ex.title, ex.price, ex.method), ("Chair", Decimal("99"), "meta"))

    def test_heuristic(self):
        ex = extract("<title>Mug</title><body><p>Now only £7.49!</p><script>var p='£1'</script></body>")
        self.assertEqual((ex.price, ex.currency, ex.method), (Decimal("7.49"), "GBP", "heuristic"))

    def test_visible_price_element_beats_noise(self):
        # shaped like Argos: no structured data, price only in visible markup
        html = """<html><head><title>iPad mini | Argos</title>
        <meta property="og:title" content="Apple iPad mini 128GB - Starlight"></head><body>
        <div class="banner">Spend £50 and get free delivery</div>
        <h1>Apple iPad mini 128GB - Starlight</h1>
        <span class="was-price">Was £749.00</span>
        <p>Pay from £28.05 per month with Klarna</p>
        <li data-test="product-price-primary"><h2>£699.00</h2></li>
        <p>Argos Care +£15.49per month</p></body></html>"""
        ex = extract(html)
        self.assertEqual((ex.price, ex.currency, ex.method), (Decimal("699.00"), "GBP", "price-element"))
        self.assertEqual(ex.title, "Apple iPad mini 128GB - Starlight")

    def test_heading_price_when_no_hints(self):
        html = "<p>from £28.05 per month</p><h1>Kettle</h1><h2>£39.99</h2>"
        self.assertEqual(extract(html).price, Decimal("39.99"))

    def test_skips_monthly_amounts(self):
        self.assertEqual(extract("<p>£12.50 per month or £299 today</p>").price, Decimal("299"))

    def test_none(self):
        self.assertIsNone(extract("<p>hello</p>").price)


class TestSecrets(unittest.TestCase):
    def test_secret_never_renders(self):
        s = Secret("sk-very-secret-value")
        for rendered in (str(s), repr(s), f"{s}", "%s" % s, str([s]), str({"k": s})):
            self.assertNotIn("very-secret", rendered)
        self.assertEqual(s.reveal(), "sk-very-secret-value")
        import pickle
        with self.assertRaises(TypeError):
            pickle.dumps(s)

    def test_public_summary_has_no_secret(self):
        # deliberately FAKE values: this test proves secrets never render
        s = load_settings({"PW_ACCESS_TOKEN": "x" * 40, "PW_LLM_API_KEY": "sk-abcdef123456",  # gitleaks:allow
                           "PW_FAIR_PROXIES": "http://u:p@proxy:8080"})
        dumped = str(s.public_summary()) + repr(s)
        for leak in ("x" * 40, "sk-abcdef123456", "u:p@proxy"):
            self.assertNotIn(leak, dumped)

    def test_refuses_open_network_without_token(self):
        s = load_settings({"PW_HOST": "0.0.0.0"})
        self.assertTrue(validate_settings(s))
        s = load_settings({"PW_HOST": "0.0.0.0", "PW_ACCESS_TOKEN": "t" * 32})
        self.assertFalse(validate_settings(s))

    def test_env_parser(self, ):
        import tempfile, pathlib
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "x.env"
            p.write_text('# c\nexport PW_PORT=9000\nPW_LLM_MODEL="llama3.1"  \nPW_HOST=127.0.0.1 # comment\n')
            self.assertEqual(parse_env_file(p), {"PW_PORT": "9000", "PW_LLM_MODEL": "llama3.1",
                                                 "PW_HOST": "127.0.0.1"})


class TestMatching(unittest.TestCase):
    def test_heuristic(self):
        self.assertTrue(heuristic_same_product("Sony WH-1000XM5 Wireless Headphones Black",
                                               "Sony WH-1000XM5 Noise Cancelling Wireless Headphones - Black"))
        self.assertFalse(heuristic_same_product("Sony WH-1000XM5 Headphones", "Sony WH-1000XM4 Headphones"))
        self.assertFalse(heuristic_same_product("Kettle", "Toaster 4 slice"))


if __name__ == "__main__":
    unittest.main()
