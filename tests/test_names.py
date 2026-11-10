"""Names compared as names: whole names in common, accents and scripts, Discogs' numbering."""
import pytest

from spindle.names import artist_key, artist_keys, artist_names, fold, is_various, same_artist
from spindle.tracklist import norm_title, titles_match


@pytest.mark.parametrize("a,b", [
    ("Kanye West", "Kanye West, JAŸ-Z, Pusha T, CyHi, Swizz Beatz & RZA"),
    ("Jay-Z", "Kanye West, JAŸ-Z, Pusha T, CyHi, Swizz Beatz & RZA"),
    ("A$AP Rocky", "ASAP Rocky"),
    ("Travi$ Scott", "Travis Scott (2)"),
    ("¥$", "¥$, Kanye West, Ty Dolla $ign"),
    ("Future (4), Metro Boomin", "Future & Metro Boomin"),
    ("Metro Boomin", "Future (4), Metro Boomin"),
    ("Tyler, The Creator", "Tyler, The Creator"),
    ("Drake Feat. Rihanna", "Rihanna"),
    ("MF Doom, Madlib, Madvillain & Medaphoar", "Madvillain"),
    ("Pendulum (3)", "Pendulum"),
    ("The Prodigy", "Prodigy"),
    ("Кино", "КИНО"),
    ("Beyoncé", "Beyonce"),
    ("坂本龍一", "坂本龍一"),
    ("Artist (feat. Guest)", "Guest"),
    ("Florence + The Machine", "Florence and the Machine"),
    ("Skrillex x Diplo", "Diplo"),
])
def test_one_whole_name_in_common_is_the_same_artist(a, b):
    assert same_artist(a, b) and same_artist(b, a)


@pytest.mark.parametrize("a,b", [
    ("Ye", "Yeat"),                 # all three are on one shelf
    ("Ye", "Kanye West"),           # strings cannot know; Discogs' aliases do
    ("Che", "Cher"),
    ("¥$", "Kanye West"),
    ("¥$", "Sade"),                 # "¥$" once folded to "s": everybody with an s
    ("Lil Nas X", "Lil Nas"),
    ("Earth, Wind & Fire", "Arcade Fire"),
    ("坂本龍一", "久石譲"),
    ("", "Kanye West"),
])
def test_a_name_inside_another_is_not_the_same_artist(a, b):
    assert not same_artist(a, b) and not same_artist(b, a)


def test_keys_and_credit_lines():
    assert artist_key("JAŸ-Z") == "jay z" and artist_key("Ty Dolla $ign") == "ty dolla sign"
    assert artist_key("Future (4)") == "future" and artist_key("The The") == "the"
    assert artist_key("¥$") == "¥$"                     # nothing to fold to: its own key
    assert artist_names("A, B & C feat. D") == ["A", "B", "C", "D"]
    assert artist_names("Future (4), Metro Boomin") == ["Future", "Metro Boomin"]
    assert "tyler the creator" in artist_keys("Tyler, The Creator")
    assert is_various("Various") and is_various("Various Artists") and not is_various("Variou")
    assert fold("Alivë") == "alive"


def test_titles_in_any_script_have_a_key():
    assert norm_title("Группа крови") == "группа крови"
    assert norm_title("Ἀχιλλεύς") and norm_title("千と千尋の神隠し") and norm_title("봄날")
    assert titles_match("千と千尋の神隠し", "千と千尋の神隠し")
    assert not titles_match("千と千尋の神隠し", "となりのトトロ")
    assert titles_match("On Tha Linë", "On Tha Line")           # accents fold
    assert titles_match("Место для шага вперёд", "Место Для Шага Вперед")   # ё is е
