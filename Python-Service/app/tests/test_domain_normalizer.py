"""Phase 1 tests — domain-concept identifier normalizer."""

from __future__ import annotations

from projects.domain.normalizer import ConceptNormalizer


class TestConceptNormalizer:
    def test_spec_corpus(self):
        normalizer = ConceptNormalizer()
        cases = {
            "PaymentService": "payment service",
            "payment_service": "payment service",
            "payment-service": "payment service",
            "payment.service": "payment service",
            "App\\Services\\PaymentService": "app services payment service",
            "App/Services/PaymentService": "app services payment service",
            "POST /api/orders": "post api orders",
            "GET /api/users/{id}": "get api users id",
            "DTOMapper": "dto mapper",
            "parseHTML": "parse html",
        }
        for raw, expected in cases.items():
            assert normalizer.normalize(raw) == expected, raw

    def test_acronym_run_stays_one_token(self):
        normalizer = ConceptNormalizer()
        assert normalizer.normalize("API") == "api"
        assert normalizer.normalize("DTOMapper") == "dto mapper"

    def test_file_extension_stripped(self):
        normalizer = ConceptNormalizer()
        assert normalizer.normalize("PaymentService.php") == "payment service"
        assert (
            normalizer.normalize("app/Http/Controllers/UserController.php")
            == "app http controllers user controller"
        )
        assert normalizer.normalize("checkout.blade.php") == "checkout"

    def test_punctuation_stripped(self):
        normalizer = ConceptNormalizer()
        assert normalizer.normalize("Order::create()") == "order create"
        assert normalizer.normalize("paymentService.checkout()") == (
            "payment service checkout"
        )

    def test_unicode_casefold(self):
        normalizer = ConceptNormalizer()
        # accented camelCase — boundary split before casefold, then casefold
        assert normalizer.normalize("CréationService") == "création service"
        assert normalizer.normalize("ÉlèveService") == "élève service"
        # non-ASCII script passes through casefold unharmed
        assert normalizer.normalize("علی") == "علی"

    def test_whitespace_collapsed_and_empty_safe(self):
        normalizer = ConceptNormalizer()
        assert normalizer.normalize("   ") == ""
        assert normalizer.normalize("") == ""
        assert normalizer.normalize(None) == ""  # type: ignore[arg-type]
        assert normalizer.normalize("  Payment   Service  ") == "payment service"

    def test_display_tokens(self):
        normalizer = ConceptNormalizer()
        assert normalizer.display_tokens("PaymentService") == ["payment", "service"]
        assert normalizer.display_tokens("POST /api/orders") == [
            "post",
            "api",
            "orders",
        ]
