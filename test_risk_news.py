import unittest
from datetime import datetime

from risk_news import Article, KST, already_seen, classify, event_id, group_articles, published_at, title_key


class RiskNewsTests(unittest.TestCase):
    def article(self, title, source="news.example", summary=""):
        return Article(title, f"https://{source}/story", source, datetime(2026, 10, 23, 16, tzinfo=KST), summary)

    def test_reports_from_multiple_outlets_are_one_allegation(self):
        reports = [
            self.article("세무사회, 비즈넵 운영사 대표 고발…세무대리 오인광고 의혹", "one.example"),
            self.article("환급플랫폼 비즈넵 대표 경찰에 고발", "two.example"),
        ]
        grouped = group_articles(reports)
        self.assertEqual(len(grouped), 1)
        self.assertEqual(grouped[0].stage, "고발·신고")
        self.assertEqual(len(grouped[0].evidence), 2)

    def test_bookkeeping_article_is_outside_refund_radar(self):
        article = self.article("세이브택스 기장 수수료 비교 안내")
        self.assertFalse(classify(article))

    def test_new_procedural_stage_gets_a_new_event(self):
        allegation = self.article("비즈넵 운영사 대표 고발")
        prosecution = self.article("비즈넵 운영사 대표 기소")
        self.assertTrue(classify(allegation))
        self.assertTrue(classify(prosecution))
        self.assertNotEqual(event_id(allegation), event_id(prosecution))

    def test_serper_relative_date(self):
        self.assertIsNotNone(published_at("2 days ago"))
        self.assertIsNotNone(published_at("3시간 전"))

    def test_other_outlet_headline_is_not_reposted(self):
        first = self.article("삼쩜삼, 환급 서비스 중심 앱 개편·월세지원센터 출시")
        second = self.article("삼쩜삼, 환급 중심으로 앱 개편…월세지원센터도 선보여", "two.example")
        self.assertTrue(classify(first))
        self.assertTrue(classify(second))
        seen = {event_id(first): {"posted_at": "2026-10-23T16:00:00+09:00", "company": first.company,
                                  "stage": first.stage, "published": first.published.date().isoformat(),
                                  "title_key": title_key(first)}}
        self.assertTrue(already_seen(second, seen))


if __name__ == "__main__":
    unittest.main()
