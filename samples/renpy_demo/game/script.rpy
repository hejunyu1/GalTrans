define aoi = Character("葵")

default player_name = "悠真"

label start:
    scene black

    aoi "ねえ、[player_name]。私の声が聞こえる？"
    "暗闇の中で、誰かの声がした。"

    menu:
        "返事をする":
            aoi happy "{color=#7f7}よかった。{/color}これで物語を始められるね。"

        "黙っている" if player_name != "":
            aoi "……まだ、目を覚ましたくないの？"

    $ internal_note = "この文字列は台詞として抽出しない"

    python:
        debug_message = "Python コード内も抽出しない"

    return
