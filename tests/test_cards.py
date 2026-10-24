

def test_unverified_facts_and_sentence_trim():
    from harvester.cards import _trim_sentence, unverified
    post = "Google выпустил SynthID Detector, проверка бесплатная. Точность 95%."
    draft = ("SynthID Detector от Google: точность 95%, а цена 20$ в месяц, подробнее https://x.example.\n"
             "1. Загрузить файл\n```text\nкоманда\n```\nСвязанные: [[Гайд за 15 минут]]")
    assert unverified(draft, post) == ["20", "https://x.example"]
    t = _trim_sentence("Первое предложение. " * 40, 300)
    assert len(t) <= 300 and t.endswith("(полностью в заметке)") and "предложение. …" in t
