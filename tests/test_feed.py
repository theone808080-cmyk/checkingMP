import os
import unittest
import uuid
from datetime import datetime, timedelta, timezone

import psycopg

import db


class FeedValidationTests(unittest.TestCase):
    def test_rejects_untrusted_filter_cursor_and_limit_before_query(self):
        for kwargs in ({"period": "today' OR true--"}, {"platform": "ozon' OR true--"},
                       {"cursor": "not-a-cursor"}, {"limit": 0}, {"limit": True}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                db.feed_materials(None, **kwargs)

    def test_normalized_publication_fingerprint_only_ignores_unicode_and_whitespace_formatting(self):
        same = db.normalized_publication_fingerprint("  Новое\u00a0правило  ", "Проверьте\n\nнастройки\tпоставки")
        self.assertEqual(same, db.normalized_publication_fingerprint("Новое правило", "Проверьте настройки поставки"))
        self.assertNotEqual(same, db.normalized_publication_fingerprint("Новое правило", "Проверьте настройки возврата"))


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "TEST_DATABASE_URL is not configured")
class ExactDuplicatePostgresTests(unittest.TestCase):
    def setUp(self):
        self.schema = "exact_duplicate_" + uuid.uuid4().hex
        self.conn = psycopg.connect(os.environ["TEST_DATABASE_URL"], autocommit=True)
        self.conn.execute(f'CREATE SCHEMA {self.schema}')
        self.conn.execute(f'SET search_path = {self.schema}')
        db.init_schema(self.conn)
        db.sync_sources(self.conn)
        self.addCleanup(self.conn.close)
        self.addCleanup(self._drop_schema)

    def _drop_schema(self):
        with psycopg.connect(os.environ["TEST_DATABASE_URL"], autocommit=True) as cleanup:
            cleanup.execute(f'DROP SCHEMA {self.schema} CASCADE')

    def _source(self, code):
        return self.conn.execute("SELECT id FROM sources WHERE code=%s", (code,)).fetchone()[0]

    def _signal(self, material_id, *, summary="Площадка изменила правило поставки"):
        return {"material_ids": [material_id], "platform": "Ozon", "topic": "rules", "importance": 4,
                "summary": summary, "recommended_action": "Проверьте настройки", "verification": "needs_check"}

    def _translation(self, material_id):
        return {"material_id": material_id, "title_ru": "Новое правило", "text_ru": "Проверьте настройки поставки."}

    def test_exact_normalized_copy_links_to_existing_finding_and_keeps_both_sources(self):
        run_id = db.start_run(self.conn, "collector")
        first_created, first_id = db.add_material(self.conn, self._source("ozon_telegram"), run_id, {
            "url": "https://one.example/post", "title": "Новое правило", "text": "Проверьте настройки поставки.", "published_at": None,
        }, return_id=True)
        self.assertTrue(first_created)
        db.import_signals(self.conn, [self._signal(first_id)], translations=[self._translation(first_id)])
        second_created, second_id = db.add_material(self.conn, self._source("retail_ru_telegram"), run_id, {
            "url": "https://two.example/post", "title": "  Новое\u00a0правило  ", "text": "Проверьте\nнастройки   поставки.", "published_at": None,
        }, return_id=True)
        self.assertTrue(second_created)
        db.import_signals(self.conn, [self._signal(second_id, summary="Другая формулировка модели")],
                          translations=[self._translation(second_id)], reject_confirmed=True)
        event_rows = self.conn.execute("SELECT id FROM events ORDER BY id").fetchall()
        linked = self.conn.execute("SELECT material_id FROM event_materials WHERE event_id=%s ORDER BY material_id", (event_rows[0][0],)).fetchall()
        self.assertEqual(len(event_rows), 1)
        self.assertEqual(linked, [(first_id,), (second_id,)])
        source_urls = self.conn.execute("SELECT url FROM materials ORDER BY id").fetchall()
        self.assertEqual(source_urls, [("https://one.example/post",), ("https://two.example/post",)])

    def test_worker_exact_copy_keeps_existing_confirmed_finding_without_overwriting_it(self):
        run_id = db.start_run(self.conn, "collector")
        _, confirmed_id = db.add_material(self.conn, self._source("ozon_telegram"), run_id, {
            "url": "https://one.example/post", "title": "Новое правило",
            "text": "Проверьте настройки поставки.", "published_at": None,
        }, return_id=True)
        approved = {**self._signal(confirmed_id), "summary": "Подтверждённое правило поставки",
                    "recommended_action": "Сохраните подтверждение", "verification": "confirmed"}
        db.import_signals(self.conn, [approved], translations=[self._translation(confirmed_id)])
        _, copy_id = db.add_material(self.conn, self._source("retail_ru_telegram"), run_id, {
            "url": "https://two.example/post", "title": "Новое правило",
            "text": "Проверьте настройки поставки.", "published_at": None,
        }, return_id=True)
        worker_signal = {**self._signal(copy_id), "summary": "Другая автоматическая сводка",
                         "recommended_action": "Автоматическая рекомендация", "verification": "needs_check"}
        db.import_signals(self.conn, [worker_signal], translations=[self._translation(copy_id)], reject_confirmed=True)
        event_id, verification, summary, action = self.conn.execute(
            "SELECT id, verification, summary, recommended_action FROM events ORDER BY id").fetchone()
        linked = self.conn.execute("SELECT material_id FROM event_materials WHERE event_id=%s ORDER BY material_id", (event_id,)).fetchall()
        self.assertEqual((verification, summary, action),
                         ("confirmed", "Подтверждённое правило поставки", "Сохраните подтверждение"))
        self.assertEqual(linked, [(confirmed_id,), (copy_id,)])

    def test_similar_but_not_exact_publication_remains_a_separate_finding(self):
        run_id = db.start_run(self.conn, "collector")
        _, first_id = db.add_material(self.conn, self._source("ozon_telegram"), run_id, {
            "url": "https://one.example/post", "title": "Новое правило", "text": "Проверьте настройки поставки.", "published_at": None,
        }, return_id=True)
        _, second_id = db.add_material(self.conn, self._source("retail_ru_telegram"), run_id, {
            "url": "https://two.example/post", "title": "Новое правило", "text": "Проверьте настройки возврата.", "published_at": None,
        }, return_id=True)
        db.import_signals(self.conn, [self._signal(first_id)], translations=[self._translation(first_id)])
        db.import_signals(self.conn, [self._signal(second_id, summary="Изменилось правило возврата")], translations=[self._translation(second_id)])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM events").fetchone()[0], 2)

    def test_same_model_event_key_does_not_merge_different_publications(self):
        run_id = db.start_run(self.conn, "collector")
        _, first_id = db.add_material(self.conn, self._source("ozon_telegram"), run_id, {
            "url": "https://one.example/post", "title": "Новое правило поставки",
            "text": "Проверьте настройки поставки.", "published_at": None,
        }, return_id=True)
        _, second_id = db.add_material(self.conn, self._source("retail_ru_telegram"), run_id, {
            "url": "https://two.example/post", "title": "Новое правило возврата",
            "text": "Проверьте настройки возврата.", "published_at": None,
        }, return_id=True)
        db.import_signals(self.conn, [{**self._signal(first_id), "event_key": "model-reused-key"}],
                          translations=[self._translation(first_id)])
        db.import_signals(self.conn, [{**self._signal(second_id, summary="Площадка изменила правило возврата"),
                                       "event_key": "model-reused-key"}],
                          translations=[self._translation(second_id)])
        rows = self.conn.execute("""SELECT e.id, em.material_id FROM events e
                                   JOIN event_materials em ON em.event_id=e.id ORDER BY em.material_id""").fetchall()
        self.assertEqual([material_id for _, material_id in rows], [first_id, second_id])
        self.assertEqual(len({event_id for event_id, _ in rows}), 2)

    def test_manual_confirmed_exact_copy_promotes_existing_unconfirmed_finding(self):
        run_id = db.start_run(self.conn, "collector")
        _, initial_id = db.add_material(self.conn, self._source("ozon_telegram"), run_id, {
            "url": "https://one.example/post", "title": "Новое правило",
            "text": "Проверьте настройки поставки.", "published_at": None,
        }, return_id=True)
        db.import_signals(self.conn, [self._signal(initial_id)], translations=[self._translation(initial_id)])
        _, reviewed_id = db.add_material(self.conn, self._source("retail_ru_telegram"), run_id, {
            "url": "https://two.example/post", "title": "Новое правило",
            "text": "Проверьте настройки поставки.", "published_at": None,
        }, return_id=True)
        confirmed = {**self._signal(reviewed_id), "verification": "confirmed"}
        db.import_signals(self.conn, [confirmed], translations=[self._translation(reviewed_id)], reject_confirmed=False)
        event_id, verification, summary, action = self.conn.execute(
            "SELECT id, verification, summary, recommended_action FROM events ORDER BY id").fetchone()
        linked = self.conn.execute("SELECT material_id FROM event_materials WHERE event_id=%s ORDER BY material_id", (event_id,)).fetchall()
        self.assertEqual(verification, "confirmed")
        self.assertEqual((summary, action), ("Площадка изменила правило поставки", "Проверьте настройки"))
        self.assertEqual(linked, [(initial_id,), (reviewed_id,)])

    def test_manual_confirmed_exact_copy_with_changed_text_creates_separate_finding(self):
        run_id = db.start_run(self.conn, "collector")
        _, initial_id = db.add_material(self.conn, self._source("ozon_telegram"), run_id, {
            "url": "https://one.example/post", "title": "Новое правило",
            "text": "Проверьте настройки поставки.", "published_at": None,
        }, return_id=True)
        db.import_signals(self.conn, [self._signal(initial_id)], translations=[self._translation(initial_id)])
        _, reviewed_id = db.add_material(self.conn, self._source("retail_ru_telegram"), run_id, {
            "url": "https://two.example/post", "title": "Новое правило",
            "text": "Проверьте настройки поставки.", "published_at": None,
        }, return_id=True)
        confirmed = {**self._signal(reviewed_id, summary="Владелец подтвердил отдельное правило"),
                     "recommended_action": "Сохраните подтверждение", "verification": "confirmed"}
        db.import_signals(self.conn, [confirmed], translations=[self._translation(reviewed_id)], reject_confirmed=False)
        rows = self.conn.execute("""SELECT e.verification, e.summary, e.recommended_action, em.material_id
                                   FROM events e JOIN event_materials em ON em.event_id=e.id
                                   ORDER BY em.material_id""").fetchall()
        self.assertEqual(rows, [
            ("needs_check", "Площадка изменила правило поставки", "Проверьте настройки", initial_id),
            ("confirmed", "Владелец подтвердил отдельное правило", "Сохраните подтверждение", reviewed_id),
        ])

    def test_ambiguous_exact_fingerprint_never_chooses_between_existing_findings(self):
        run_id = db.start_run(self.conn, "collector")
        material_ids = []
        for index, source_code in enumerate(("ozon_telegram", "retail_ru_telegram", "oborot_telegram"), start=1):
            _, material_id = db.add_material(self.conn, self._source(source_code), run_id, {
                "url": f"https://{index}.example/post", "title": "Новое правило",
                "text": "Проверьте настройки поставки.", "published_at": None,
            }, return_id=True)
            material_ids.append(material_id)
        self.conn.execute("""UPDATE materials SET status='done', translation_status='ready',
                             title_ru='Новое правило', text_ru='Проверьте настройки поставки.'
                             WHERE id = ANY(%s)""", (material_ids[:2],))
        historical_events = []
        for index, material_id in enumerate(material_ids[:2], start=1):
            event_id = self.conn.execute(
                """INSERT INTO events
                   (event_key, platform, topic, importance, seller_impact, summary, recommended_action, verification)
                   VALUES (%s, 'Ozon', 'rules', 4, 'unclear', %s, 'Проверьте настройки', 'needs_check')
                   RETURNING id""",
                (f"historical-{index}", f"Историческая находка {index}"),
            ).fetchone()[0]
            historical_events.append(event_id)
            self.conn.execute("INSERT INTO event_materials (event_id, material_id) VALUES (%s, %s)",
                              (event_id, material_id))
        db.import_signals(self.conn, [self._signal(material_ids[2], summary="Третье сообщение")],
                          translations=[self._translation(material_ids[2])])
        rows = self.conn.execute("""SELECT em.material_id, em.event_id FROM event_materials em
                                   ORDER BY em.material_id""").fetchall()
        self.assertEqual([material_id for material_id, _ in rows], material_ids)
        self.assertEqual(len({event_id for _, event_id in rows}), 3)


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "TEST_DATABASE_URL is not configured")
class FeedPostgresTests(unittest.TestCase):
    def setUp(self):
        self.conn = psycopg.connect(os.environ["TEST_DATABASE_URL"], autocommit=True)
        self.addCleanup(self.conn.close)
        self.conn.execute("""CREATE TEMP TABLE sources (
            id SERIAL PRIMARY KEY, code TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
            kind TEXT NOT NULL, url TEXT NOT NULL, trust TEXT NOT NULL, scope TEXT NOT NULL,
            active BOOLEAN DEFAULT true, last_checked_at TIMESTAMPTZ, last_error TEXT,
            category TEXT, managed_by TEXT NOT NULL DEFAULT 'config')""")
        self.conn.execute("""CREATE TEMP TABLE materials (
            id SERIAL PRIMARY KEY, source_id INTEGER NOT NULL, url TEXT NOT NULL,
            title TEXT NOT NULL, text TEXT, published_at TIMESTAMPTZ,
            collected_at TIMESTAMPTZ NOT NULL, status TEXT NOT NULL,
            translation_status TEXT NOT NULL DEFAULT 'legacy', title_ru TEXT, text_ru TEXT)""")
        self.conn.execute("""CREATE TEMP TABLE feed_snapshots (
            id UUID PRIMARY KEY, period TEXT NOT NULL, platform TEXT NOT NULL,
            material_ids INTEGER[] NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
        db.sync_sources(self.conn)

    def add(self, source, title, published, collected, status="new"):
        self.conn.execute("""INSERT INTO materials(source_id, url, title, text, published_at, collected_at, status)
            SELECT id, %s, %s, %s, %s, %s, %s FROM sources WHERE code = %s""",
            ("https://example.org/" + title, title, "original " + title, published, collected, status, source))

    def test_categories_map_all_nine_sources_and_safe_statuses(self):
        rows = self.conn.execute("SELECT code, category FROM sources ORDER BY id").fetchall()
        self.assertEqual([category for _, category in rows],
                         ["ozon", "wildberries", "yandex", "chestny_znak", "chestny_znak",
                          "industry", "industry", "industry", "industry"])
        self.conn.execute("UPDATE sources SET last_error = 'secret error', last_checked_at = now() WHERE code = 'ozon_telegram'")
        statuses = db.feed_sources(self.conn)
        ozon = next(row for row in statuses if row["code"] == "ozon_telegram")
        self.assertEqual(ozon["status"], "error")
        self.assertNotIn("secret", str(statuses))
        self.assertIsNotNone(ozon["last_checked_at"])

    def test_filters_moscow_calendar_fallback_and_unprocessed(self):
        # Derive boundaries in Moscow independently of the server's local timezone.
        moscow = timezone(timedelta(hours=3))
        now = datetime.now(moscow)
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        self.add("ozon_telegram", "today", midnight, midnight, "new")
        self.add("wb_telegram", "yesterday", midnight - timedelta(microseconds=1), now, "done")
        self.add("rospotrebnadzor_federal", "fallback", None, midnight, "skipped")
        self.add("crpt_breaking", "week-first", midnight - timedelta(days=6), now)
        self.add("retail_ru_telegram", "too-old", midnight - timedelta(days=6, microseconds=1), now)
        self.add("yandex_market_telegram", "future", midnight + timedelta(days=1), now)
        today = db.feed_materials(self.conn, period="today")
        self.assertEqual({x["title"] for x in today["materials"]}, {"today", "fallback"})
        week = db.feed_materials(self.conn, period="week")
        self.assertEqual({x["title"] for x in week["materials"]}, {"today", "fallback", "yesterday", "week-first"})
        industry = db.feed_materials(self.conn, platform="industry")
        self.assertEqual({x["title"] for x in industry["materials"]}, {"fallback", "too-old"})
        self.assertEqual(db.feed_materials(self.conn, platform="ozon")["materials"][0]["title"], "today")
        for platform, title in (("wildberries", "yesterday"), ("chestny_znak", "week-first")):
            self.assertEqual(db.feed_materials(self.conn, platform=platform)["materials"][0]["title"], title)
        self.assertEqual(db.feed_materials(self.conn, platform="yandex")["total"], 1)

    def test_feed_returns_translation_fields_without_replacing_original(self):
        timestamp = datetime.now(timezone.utc)
        self.add("ozon_telegram", "The new rule", timestamp, timestamp, "done")
        self.conn.execute("""UPDATE materials SET translation_status='ready', title_ru='Новое правило',
            text_ru='Поставщик должен обновить маркировку.' WHERE title='The new rule'""")
        item = db.feed_materials(self.conn)["materials"][0]
        self.assertEqual(item["title"], "The new rule")
        self.assertEqual(item["text"], "original The new rule")
        self.assertEqual(item.get("translation_status"), "ready")
        self.assertEqual(item.get("title_ru"), "Новое правило")
        self.assertEqual(item.get("text_ru"), "Поставщик должен обновить маркировку.")

    def test_pending_translation_is_excluded_from_total_pages_and_cursor_until_ready(self):
        timestamp = datetime.now(timezone.utc)
        self.add("ozon_telegram", "ready-first", timestamp, timestamp)
        self.add("ozon_telegram", "ready-second", timestamp - timedelta(minutes=1), timestamp)
        self.add("ozon_telegram", "pending-newest", timestamp + timedelta(minutes=1), timestamp)
        self.conn.execute("UPDATE materials SET translation_status='ready' WHERE title LIKE 'ready-%'")
        self.conn.execute("UPDATE materials SET translation_status='pending' WHERE title='pending-newest'")

        first = db.feed_materials(self.conn, limit=1)
        self.assertEqual([item["title"] for item in first["materials"]], ["ready-first"])
        self.assertEqual(first["total"], 2)
        self.assertIsNotNone(first["next_cursor"])

        second = db.feed_materials(self.conn, cursor=first["next_cursor"], limit=1)
        self.assertEqual([item["title"] for item in second["materials"]], ["ready-second"])
        self.assertEqual(second["total"], 2)
        self.assertIsNone(second["next_cursor"])

        self.conn.execute("UPDATE materials SET translation_status='ready' WHERE title='pending-newest'")
        visible_after_ready = db.feed_materials(self.conn, limit=3)
        self.assertEqual([item["title"] for item in visible_after_ready["materials"]],
                         ["pending-newest", "ready-first", "ready-second"])
        self.assertEqual(visible_after_ready["total"], 3)

    def test_cursor_snapshot_rechecks_pending_translation_after_snapshot_creation(self):
        timestamp = datetime.now(timezone.utc)
        self.add("ozon_telegram", "visible-first", timestamp, timestamp)
        self.add("ozon_telegram", "visible-second", timestamp - timedelta(minutes=1), timestamp)
        first = db.feed_materials(self.conn, limit=1)
        self.assertEqual([item["title"] for item in first["materials"]], ["visible-first"])
        self.assertEqual(first["total"], 2)
        self.assertIsNotNone(first["next_cursor"])

        self.conn.execute("UPDATE materials SET translation_status='pending' WHERE title='visible-second'")
        second = db.feed_materials(self.conn, cursor=first["next_cursor"], limit=1)
        self.assertEqual(second["materials"], [])
        self.assertEqual(second["total"], 1)
        self.assertIsNone(second["next_cursor"])

    def test_stable_cursor_does_not_repeat_ties_or_newer_inserts(self):
        timestamp = datetime.now(timezone.utc) - timedelta(hours=1)
        for title in ("one", "two", "three"):
            self.add("ozon_telegram", title, timestamp, timestamp)
        first = db.feed_materials(self.conn, limit=2)
        self.assertEqual([x["title"] for x in first["materials"]], ["three", "two"])
        self.add("ozon_telegram", "later", timestamp + timedelta(minutes=1), timestamp)
        self.add("ozon_telegram", "late-old-publication", timestamp - timedelta(days=3), timestamp)
        second = db.feed_materials(self.conn, cursor=first["next_cursor"], limit=2)
        self.assertEqual([x["title"] for x in second["materials"]], ["one"])
        self.assertEqual(first["total"], 3)
        self.assertEqual(second["total"], 3)
        self.assertIsNone(second["next_cursor"])

    def test_first_page_and_total_are_read_in_one_database_statement(self):
        class CountingConnection:
            def __init__(self, conn):
                self.conn = conn
                self.calls = 0

            def execute(self, query, params):
                self.calls += 1
                return self.conn.execute(query, params)

        wrapped = CountingConnection(self.conn)
        result = db.feed_materials(wrapped)
        self.assertEqual(result["total"], 0)
        self.assertEqual(result["materials"], [])
        self.assertEqual(wrapped.calls, 1)

    def test_cursor_rejects_changed_filter_and_expired_snapshot(self):
        timestamp = datetime.now(timezone.utc)
        self.add('ozon_telegram', 'one', timestamp, timestamp)
        self.add('ozon_telegram', 'two', timestamp, timestamp)
        cursor = db.feed_materials(self.conn, limit=1)['next_cursor']
        with self.assertRaises(ValueError):
            db.feed_materials(self.conn, period='today', cursor=cursor)
        self.conn.execute("UPDATE feed_snapshots SET created_at = now() - INTERVAL '31 minutes'")
        with self.assertRaises(ValueError):
            db.feed_materials(self.conn, cursor=cursor)

    def test_old_schema_migrates_twice_and_backfills_categories(self):
        with psycopg.connect(os.environ["TEST_DATABASE_URL"], autocommit=True) as conn:
            conn.execute('BEGIN')
            try:
                conn.execute('CREATE SCHEMA feed_migration_test')
                conn.execute('SET LOCAL search_path = feed_migration_test')
                conn.execute("""CREATE TABLE sources (
                    id SERIAL PRIMARY KEY, code TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
                    kind TEXT NOT NULL, url TEXT NOT NULL, trust TEXT NOT NULL, scope TEXT NOT NULL,
                    active BOOLEAN DEFAULT true, last_checked_at TIMESTAMPTZ, last_error TEXT)""")
                conn.execute("INSERT INTO sources (code, name, kind, url, trust, scope) VALUES ('ozon_telegram', 'old', 'telegram', 'https://example.org', 'official', 'old')")
                conn.execute("""CREATE TABLE runs (
                    id SERIAL PRIMARY KEY, service TEXT NOT NULL, status TEXT NOT NULL,
                    started_at TIMESTAMPTZ NOT NULL DEFAULT now(), finished_at TIMESTAMPTZ,
                    items INTEGER NOT NULL DEFAULT 0, note TEXT)""")
                conn.execute("""CREATE TABLE materials (
                    id SERIAL PRIMARY KEY, source_id INTEGER NOT NULL REFERENCES sources(id),
                    run_id INTEGER REFERENCES runs(id), url TEXT NOT NULL, title TEXT NOT NULL,
                    text TEXT, published_at TIMESTAMPTZ, collected_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    fingerprint TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new','done','skipped','failed')))""")
                conn.execute("""INSERT INTO materials(source_id,url,title,text,fingerprint)
                    VALUES (1,'https://example.org/old','Old English headline','Old English excerpt','old-fingerprint')""")
                db.init_schema(conn)
                db.init_schema(conn)
                db.sync_sources(conn)
                self.assertEqual(conn.execute("SELECT category FROM sources WHERE code = 'ozon_telegram'").fetchone()[0], 'ozon')
                columns = conn.execute("""SELECT column_name FROM information_schema.columns
                    WHERE table_schema='feed_migration_test' AND table_name='materials'
                    AND column_name = ANY(%s) ORDER BY column_name""",
                    (['text_ru', 'title_ru', 'translation_status'],)).fetchall()
                self.assertEqual(columns, [('text_ru',), ('title_ru',), ('translation_status',)])
                self.assertEqual(conn.execute("""SELECT title,text,translation_status,title_ru,text_ru
                    FROM materials WHERE fingerprint='old-fingerprint'""").fetchone(),
                    ('Old English headline', 'Old English excerpt', 'legacy', None, None))
                run_id = db.start_run(conn, 'collector')
                self.assertTrue(db.add_material(conn, 1, run_id, {
                    'url': 'https://example.org/new', 'title': 'New English headline',
                    'text': 'New English excerpt', 'published_at': None,
                }))
                new_row = conn.execute("""SELECT id,title,text,translation_status,title_ru,text_ru
                    FROM materials WHERE url='https://example.org/new'""").fetchone()
                self.assertEqual(new_row[1:], ('New English headline', 'New English excerpt', 'pending', None, None))
                pending = {item['title']: item['translation_status'] for item in db.pending_materials(conn)}
                self.assertEqual(pending, {'Old English headline': 'legacy', 'New English headline': 'pending'})
                reviewed_signal = {
                    'material_ids': [new_row[0]], 'platform': 'Ozon', 'topic': 'rules',
                    'importance': 3, 'summary': 'Проверьте маркировку',
                    'recommended_action': 'Обновить карточку', 'verification': 'needs_check',
                }
                with conn.transaction():
                    with self.assertRaisesRegex(ValueError, 'translation'):
                        db.import_signals(conn, [reviewed_signal])
                with conn.transaction():
                    with self.assertRaisesRegex(ValueError, 'text'):
                        db.import_signals(conn, [reviewed_signal], translations=[{
                            'material_id': new_row[0], 'title_ru': 'Новый заголовок', 'text_ru': '',
                        }])
                self.assertEqual(conn.execute("SELECT status,translation_status FROM materials WHERE id=%s",
                                              (new_row[0],)).fetchone(), ('new', 'pending'))
                self.assertEqual(conn.execute("SELECT count(*) FROM events").fetchone()[0], 0)
                db.import_signals(conn, [reviewed_signal], translations=[{
                    'material_id': new_row[0], 'title_ru': 'Н' * 501,
                    'text_ru': 'Новый фрагмент на русском',
                }])
                saved = conn.execute("""SELECT title,text,translation_status,title_ru,text_ru,status
                    FROM materials WHERE id=%s""", (new_row[0],)).fetchone()
                self.assertEqual(saved, ('New English headline', 'New English excerpt', 'ready',
                                         'Н' * 501, 'Новый фрагмент на русском', 'done'))
                legacy = conn.execute("""SELECT title,text,translation_status,title_ru,text_ru
                    FROM materials WHERE fingerprint='old-fingerprint'""").fetchone()
                self.assertEqual(legacy, ('Old English headline', 'Old English excerpt', 'legacy', None, None))
            finally:
                conn.execute('ROLLBACK')

    def test_open_transaction_committed_after_first_page_is_not_in_snapshot(self):
        schema = 'feed_late_' + uuid.uuid4().hex
        with psycopg.connect(os.environ['TEST_DATABASE_URL'], autocommit=True) as reader, \
             psycopg.connect(os.environ['TEST_DATABASE_URL'], autocommit=False) as late:
            reader.execute(f'CREATE SCHEMA {schema}')
            try:
                reader.execute(f'SET search_path = {schema}')
                db.init_schema(reader)
                db.sync_sources(reader)
                late.execute(f'SET search_path = {schema}')
                source_id = reader.execute("SELECT id FROM sources WHERE code = 'ozon_telegram'").fetchone()[0]
                def insert(conn, title, published):
                    conn.execute("""INSERT INTO materials(source_id, url, title, text, published_at, fingerprint)
                        VALUES (%s, %s, %s, 'text', %s, %s)""",
                        (source_id, 'https://example.org/' + title, title, published, title))
                stamp = datetime.now(timezone.utc)
                insert(reader, 'first', stamp)
                insert(late, 'late', stamp - timedelta(days=1))
                insert(reader, 'second', stamp)
                first = db.feed_materials(reader, limit=1)
                late.commit()
                second = db.feed_materials(reader, cursor=first['next_cursor'], limit=20)
                self.assertEqual(first['total'], 2)
                self.assertEqual(second['total'], 2)
                self.assertEqual([m['title'] for m in second['materials']], ['first'])
            finally:
                late.rollback()
                reader.execute(f'DROP SCHEMA {schema} CASCADE')


if __name__ == "__main__":
    unittest.main()
