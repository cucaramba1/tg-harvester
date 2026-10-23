from harvester.autovideo import calibration_picks, cell_of
from tests.helpers import CHAT_A, CHAT_B, make_settings


def ask(db, chat, msg, score, text="x" * 50):
    pid = db.add_post(chat, msg, None, "2026-10-01T00:00:00+00:00", text, "video")
    db.update_post(pid, status="video_ask", video_state="ask", score=score)
    return pid


def test_cell_of_buckets_by_similarity_and_caption_length():
    assert cell_of(0.3, "") == (0, 0)
    assert cell_of(0.55, "x" * 199) == (1, 0)
    assert cell_of(0.70, "x" * 200) == (2, 1)
    assert cell_of(0.9, "x" * 500) == (3, 1)
    assert cell_of(None, "x") == (0, 0)


def test_calibration_spreads_over_cells_and_channels(tmp_path, db):
    s = make_settings(tmp_path, video_calib_n=4, video_calib_top=2)
    a_top = [ask(db, CHAT_A, i, 0.8) for i in range(1, 6)]        # пять «лучших» в одной ячейке (3, 0)
    a_low = ask(db, CHAT_A, 10, 0.4)                               # одна скучная, ячейка (0, 0)
    a_long = ask(db, CHAT_A, 11, 0.6, text="x" * 300)              # ячейка (1, 1)
    b = [ask(db, CHAT_B, 20 + i, 0.6) for i in range(3)]
    ids = [r["id"] for r in calibration_picks(s, db, 20)]
    assert len(ids) == 4 + 3                                       # А: ровно video_calib_n, Б: все три
    assert a_low in ids and a_long in ids                          # не 4 «лучших» подряд
    assert sum(i in a_top for i in ids) == 2
    assert [r["chat_id"] for r in calibration_picks(s, db, 20)][:2] == [CHAT_A, CHAT_B]  # каналы по кругу


def test_calibration_stops_when_channel_has_enough_answers(tmp_path, db):
    s = make_settings(tmp_path, video_calib_n=2, video_calib_top=2)
    done = [ask(db, CHAT_A, i, 0.8) for i in range(1, 3)]
    for pid in done:
        db.add_label(pid, True, source="video")
        db.update_post(pid, video_state="done", status="enriched")
    ask(db, CHAT_A, 9, 0.8)
    b = ask(db, CHAT_B, 20, 0.6)
    assert [r["id"] for r in calibration_picks(s, db, 10)] == [b]


def test_calibration_counts_unanswered_questions(tmp_path, db):
    s = make_settings(tmp_path, video_calib_n=2, video_calib_top=1)
    p = ask(db, CHAT_A, 1, 0.8)
    db.update_post(p, video_state="asked")  # уже отправлен, ответа ещё нет
    ask(db, CHAT_A, 2, 0.6)
    ask(db, CHAT_A, 3, 0.4)
    assert len(calibration_picks(s, db, 10)) == 1  # 2 нужно, 1 уже в пути


def test_calibration_respects_limit_and_explicit_channels(tmp_path, db):
    s = make_settings(tmp_path, video_calib_n=5, video_calib_channels=(CHAT_B,))
    for i in range(3):
        ask(db, CHAT_A, i, 0.8)
        ask(db, CHAT_B, 10 + i, 0.8)
    got = calibration_picks(s, db, 2)
    assert len(got) == 2 and {r["chat_id"] for r in got} == {CHAT_B}


# --- правило решения

def label(db, chat, msg, score, useful, text="x" * 50, source="video"):
    pid = db.add_post(chat, msg, None, "2026-10-01T00:00:00+00:00", text, "video")
    db.update_post(pid, score=score)
    db.add_label(pid, useful, source=source)
    return pid


def test_smoothed_share():
    from harvester.autovideo import smoothed
    assert smoothed(8, 8) == 0.9 and smoothed(0, 8) == 0.1 and smoothed(0, 0) == 0.5


def test_decide_yes_no_ask_by_cell(tmp_path, db):
    from harvester.autovideo import Rule
    s = make_settings(tmp_path, video_auto_min_n=8, video_auto_conf=0.9)
    for i in range(8):
        label(db, CHAT_A, i, 0.8, True)             # ячейка (3, 0): 8 из 8
        label(db, CHAT_A, 100 + i, 0.3, False)      # ячейка (0, 0): 0 из 8
        label(db, CHAT_A, 200 + i, 0.6, i < 4)      # ячейка (1, 0): 4 из 8
    rule = Rule.build(s, db)
    mk = lambda sc: {"chat_id": CHAT_A, "score": sc, "text": "x" * 50}
    assert rule.decide(mk(0.82))[0] == "yes"
    assert rule.decide(mk(0.30))[0] == "no"
    assert rule.decide(mk(0.60))[0] == "ask"


def test_decide_small_cell_falls_back_to_similarity_bucket_then_ask(tmp_path, db):
    from harvester.autovideo import Rule
    s = make_settings(tmp_path, video_auto_min_n=8, video_auto_conf=0.9)
    for i in range(5):
        label(db, CHAT_A, i, 0.8, True, text="x" * 50)          # (3, 0): 5
    for i in range(5):
        label(db, CHAT_A, 50 + i, 0.8, True, text="x" * 300)    # (3, 1): 5 -> вместе 10 из 10
    rule = Rule.build(s, db)
    verdict, why = rule.decide({"chat_id": CHAT_A, "score": 0.8, "text": "x" * 50})
    assert verdict == "yes" and "10" in why                      # ячейка маленькая, взяли корзину целиком
    assert rule.decide({"chat_id": CHAT_B, "score": 0.8, "text": "x"})[0] == "ask"  # канал без ответов


def test_auto_labels_do_not_train_the_rule(tmp_path, db):
    from harvester.autovideo import Rule
    s = make_settings(tmp_path, video_auto_min_n=2, video_auto_conf=0.7)
    for i in range(6):
        label(db, CHAT_A, i, 0.8, True, source="auto")
    assert Rule.build(s, db).decide({"chat_id": CHAT_A, "score": 0.8, "text": "x"})[0] == "ask"


def test_holdout_report_counts_precision_and_questions(tmp_path, db):
    from harvester.autovideo import holdout
    s = make_settings(tmp_path, video_auto_min_n=4, video_auto_conf=0.8)
    n = 0
    for i in range(30):
        n += 1
        label(db, CHAT_A, n, 0.8, True)
    for i in range(30):
        n += 1
        label(db, CHAT_A, n, 0.3, False)
    res = holdout(s, db, seed=1)
    a = res[CHAT_A]
    assert a["test"] == 12 and a["yes"] + a["no"] + a["ask"] == 12
    assert a["yes_wrong"] == 0 and a["no_wrong"] == 0 and a["ask"] <= 2


def test_report_text_mentions_channels_holdout_and_queue_forecast(tmp_path, db):
    from harvester.autovideo import report_text
    s = make_settings(tmp_path, video_auto_min_n=4, video_auto_conf=0.8)
    for i in range(20):
        label(db, CHAT_A, i, 0.8, True)
        label(db, CHAT_A, 100 + i, 0.3, False)
    ask(db, CHAT_A, 900, 0.82)
    ask(db, CHAT_A, 901, 0.31)
    ask(db, CHAT_B, 902, 0.5)
    text = report_text(s, db)
    assert "Канал А" in text and "Канал Б" in text
    assert "проверка на отложенных" in text and "очередь" in text.lower()
    assert "сам взял 1" in text and "сам отклонил 1" in text and "спросит 1" in text.replace("спросит: ", "спросит ")
