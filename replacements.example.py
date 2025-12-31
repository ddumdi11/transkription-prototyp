# Beispiel-Datei für eigene Ersetzungsregeln
#
# Du kannst diese Datei als Vorlage verwenden, um eigene Ersetzungen zu definieren.
# Die Ersetzungen werden nach der Transkription angewendet.
#
# WICHTIG: Die Ersetzungen sind case-sensitive!

CUSTOM_REPLACEMENTS = {
    # Häufige Erkennungsfehler bei Eigennamen
    "Deklärgerät": "Diktiergerät",

    # AI/KI Tools
    "Cloud": "Claude",
    "Cloud Code": "Claude Code",
    "Cloud AI": "Claude AI",
    "GPT": "GPT",

    # Fachbegriffe
    "Transkribierung": "Transkription",

    # Weitere Beispiele:
    # "falscherkannt": "richtig",
    # "Wisp": "Whisper",
}

# Hinweis: Du kannst diese Datei nach replacements.py kopieren und in transcribe.py importieren:
# from replacements import CUSTOM_REPLACEMENTS
