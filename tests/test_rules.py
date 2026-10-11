from harvester.rules import ad_reason

M = ["erid", "#реклама", "розыгрыш"]


def test_ad_markers_case_insensitive():
    assert ad_reason("Реклама. ERID: 2Vtzq", M) == "реклама: erid"
    assert ad_reason("Большой РОЗЫГРЫШ айфонов", M) == "реклама: розыгрыш"


def test_plain_post_passes():
    assert ad_reason("Гайд по промптам для Claude", M) is None
    assert ad_reason("", M) is None
