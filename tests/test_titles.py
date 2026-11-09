from spindle.titles import TitleParts, clean_album, split_title


def t(title, artists=()):
    p = split_title(title, artists)
    return (p.base, list(p.featured), p.version)


def test_features_come_off_the_title_in_every_spelling():
    assert t("Demonz (feat. Brent Faiyaz) - Interlude") == \
        ("Demonz", ["Brent Faiyaz"], "Interlude")
    assert t("Demonz (feat. Brent Faiyaz) [Interlude]") == \
        ("Demonz", ["Brent Faiyaz"], "Interlude")
    assert t("SICKO MODE (feat. Drake, Swae Lee & Big Hawk)") == \
        ("SICKO MODE", ["Drake", "Swae Lee", "Big Hawk"], "")
    assert t("Demonz Feat. Brent Faiyaz") == ("Demonz", ["Brent Faiyaz"], "")
    assert t("Praise God [ft. Travis Scott]") == ("Praise God", ["Travis Scott"], "")
    assert t("Wake Up featuring Someone") == ("Wake Up", ["Someone"], "")
    assert t("Lose You To Love Me (with Selena Gomez)") == \
        ("Lose You To Love Me", ["Selena Gomez"], "")
    assert t("Stay (With Me)") == ("Stay (With Me)", [], "")   # a title's own Title-Case bracket
    assert t("Dance (With Tears In My Eyes)") == \
        ("Dance (With Tears In My Eyes)", [], "")


def test_versions_become_a_tag_and_identity_stays_in_the_title():
    assert t("Heroes (2017 Remaster)") == ("Heroes", [], "2017 Remaster")
    assert t("Heroes - 2017 Remaster") == ("Heroes", [], "2017 Remaster")
    assert t("Mo Bamba [Remix]") == ("Mo Bamba", [], "Remix")
    assert t("Bitch, Don't Kill My Vibe - Remix") == ("Bitch, Don't Kill My Vibe", [], "Remix")
    assert t("Intro (Skit)") == ("Intro", [], "Skit")
    assert t("All The Stars (with SZA) - From 'Black Panther'") == \
        ("All The Stars", ["SZA"], "From 'Black Panther'")
    assert t("Kill Bill - Pt. 2") == ("Kill Bill - Pt. 2", [], "")
    assert t("Runaway (Part 1)") == ("Runaway (Part 1)", [], "")
    assert t("(Sandy) Alex G") == ("(Sandy) Alex G", [], "")
    assert t("Let's Start Here.") == ("Let's Start Here.", [], "")
    assert t("The Ride-") == ("The Ride-", [], "")
    assert t("4:44") == ("4:44", [], "")
    assert t("M.A.A.D City (feat. MC Eiht)") == ("M.A.A.D City", ["MC Eiht"], "")
    assert t("Группа крови") == ("Группа крови", [], "")
    assert t("Something - Live at Wembley") == ("Something", [], "Live at Wembley")
    assert t("Black Skinhead - Explicit") == ("Black Skinhead", [], "Explicit")
    assert t("Song - Unknown Suffix Words") == ("Song - Unknown Suffix Words", [], "")
    assert t("Defeat") == ("Defeat", [], "")
    assert t("(feat. Nobody)") == ("(feat. Nobody)", [], "")      # nothing left: keep it whole


def test_the_catalogue_list_names_the_features_and_survives_commas():
    assert t("EARFQUAKE", ["Tyler, The Creator", "Playboi Carti"]) == \
        ("EARFQUAKE", ["Playboi Carti"], "")
    assert t("Demonz (feat. Brent Faiyaz) - Interlude", ["Juice WRLD", "Brent Faiyaz"]) == \
        ("Demonz", ["Brent Faiyaz"], "Interlude")
    assert t("Yonkers (feat. Tyler, The Creator)", ["Hodgy", "Tyler, The Creator"]) == \
        ("Yonkers", ["Tyler, The Creator"], "")
    assert t("Solo (feat. Juice WRLD)", ["Juice WRLD"]) == ("Solo", [], "")   # primary, no feature
    assert t("Praise God", ["Kanye West", "Travis Scott", "Baby Keem"]) == \
        ("Praise God", ["Travis Scott", "Baby Keem"], "")
    assert TitleParts("x", ("A", "B")).feat_line == "feat. A, B"
    assert TitleParts("x").feat_line == ""


def test_album_editions_come_off_the_album_line_only():
    assert clean_album("Death Race For Love (Bonus Track Version)") == "Death Race For Love"
    assert clean_album("Whole Lotta Red (Deluxe)") == "Whole Lotta Red"
    assert clean_album("Graduation (Deluxe Edition)") == "Graduation"
    assert clean_album("Thriller 25 Super Deluxe Edition") == "Thriller 25 Super Deluxe Edition"
    assert clean_album("Low - 2017 Remaster") == "Low"
    assert clean_album("Promise: Expanded Edition") == "Promise: Expanded Edition"
    assert clean_album("Love Deluxe") == "Love Deluxe"
    assert clean_album("2 Alivë (Geëk Pack)") == "2 Alivë (Geëk Pack)"
    assert clean_album("Madvillainy") == "Madvillainy"
    assert clean_album("Let's Start Here.") == "Let's Start Here."
    assert clean_album("Thriller (25th Anniversary Edition)") == "Thriller"
    assert clean_album("") == ""
