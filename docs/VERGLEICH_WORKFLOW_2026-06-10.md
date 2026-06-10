# Workflow-Vergleich: Einzeltranskription vs. Audio-Join (10.06.2026)

**Material:** Aufnahmen #267–#270 (gleiche Quelldateien, gleiches Modell `gpt-4o-transcribe`).
**Neuer Weg:** Einzeltranskription + Sammeltranskript auf Textebene (`My_Cents_2026-06-10.md`).
**Alter Weg:** Audio-Join (64 kbps mono) → Transkription der Sammeldatei (`My_Cents_2026-06-10_alter-Weg.md`).
**Bezug:** `BUG-TRANSCRIBE-001.md`

## Ergebnis in einem Satz

Der alte Weg verlor rund **19 % des Inhalts** (≈ 1.450 von 7.300 Wörtern) in drei
großen Blöcken — alle exakt an den Split-Grenzen der zusammengefügten Datei. Die
Umstellung auf Einzeltranskription als Standard ist damit empirisch bestätigt.

## Quantitativ

| | Wörter | Bemerkung |
|---|---|---|
| Neuer Weg (Sammeltranskript) | 7.448 | davon ~120 Metadaten-Köpfe |
| Alter Weg | 5.875 | |
| **Differenz (netto)** | **≈ 1.450** | ≈ 19 % des gesprochenen Inhalts |

## Verlorene Inhalte im alten Weg

Die zusammengefügte Datei (~80 Min) wurde wegen des ~23-Min-Limits in 4 Teile
gesplittet. An **jeder** der drei Teilgrenzen riss das Transkript mitten im Satz ab
und setzte erst deutlich später wieder ein:

**Block A (in #268, ≈ 470 Wörter):** Drive-Automatik-Fortsetzung, Übergabe der
Transkripte ans Z-System, Übergabenotiz-Praxis, Projekt „Selbstregulierung statt
Selbstoptimierung", **Herbert der Hausmeister**, Idee „Anweisungen direkt in die
Sprachaufnahme einbetten", Projektanlage mit Claude/Cowork, Git/GitHub-Anbindung,
VSCode/IDE-Diskussion. → Inhaltlich der wertvollste Teil des Tages — genau die Art
„Seitenimpuls", deren Verlust im Bug-Report beschrieben ist (damals: ÖPNV-Idee in #264).

**Block B (in #269, ≈ 420 Wörter):** DM-Tüten-Schlaufen, Trainings-Überlegungen,
**Karpaltunnelsyndrom**, die 50-mal-Wechseln-Rechnung, komplette
Display-Helligkeits-Episode (adaptive Helligkeit, Maps/Reisholz, Dark Mode, Kamera-App).

**Block C (in #269, ≈ 400 Wörter):** Ohrstöpsel-Ladegerät-Fund, komplette
Jeans-Passage (Größen 54/56, W/L, Preisvergleich 10–30 € vs. 50–100 €, Gürtel,
Radfahrer), Einstieg der Popeye-Passage.

**Ende #270 (≈ 50 Wörter):** verschwitzt/runterfahren, „nächste Bahn in elf
Minuten", der interessante Stein samt Foto.

## Weitere Befunde alter Weg

- **Aufnahme-Grenzen verschliffen:** keine Markierung zwischen #267→#268→#269→#270;
  an der Grenze #269/#270 zusätzlich Sinnfehler: „Da hinten sitzt ein Weihnachtsmann."
  wurde zu „Vielleicht sitzt ein Weihnachtsmann auf."
- **Mehr Wortfehler**, teils bedeutungsverändernd: „LinkedIn-Post zu *verpasst*"
  (statt *verfasst*), „Muskel*karten*" (statt *Muskelkater*), „einge*sporen*" (statt
  *eingesprochen*), „Freikalt" (statt „Freikauf"), „das schwankt ja auch der Wagen"
  (statt „auf der Waage"). Plausible Mitursache: die 64-kbps-Mono-Rekompression vor
  der Transkription.
- Das im Audio getestete „Hahaha" (Lach-Erkennungs-Experiment aus #269/#270) fehlt
  im alten Weg an der Stelle in #270, im neuen Weg ist es vorhanden.

## Ehrlicher Nebenbefund: neuer Weg ist nicht verlustfrei

Auch der neue Weg hat eine kleine Lücke: **#269 ist >23 Min** und wurde daher
ebenfalls intern gesplittet. An einer Teilgrenze fehlen ≈ 45 Wörter (Übergang
„Zigarette fertig / Malzbier ausgetrunken / Jeans von meiner Mutter bekommen").
An einer anderen Teilgrenze gab es stattdessen eine kleine Doppelung
(„Chief Surgeon" / „Schief Searchen").

**Schlussfolgerung:** Verluste entstehen systematisch an *Split-Grenzen*. Der neue
Weg reduziert sie massiv (Grenzen liegen innerhalb einer Aufnahme statt quer über
Aufnahmen, kleinere Dateien, keine Rekompression), beseitigt sie aber nicht für
Einzelaufnahmen über ~23 Minuten.

## Empfehlungen (Folgeeinheiten)

1. **Überlapp-Splitting:** Teile mit z. B. 10–15 s Überlappung schneiden und die
   Nahtstelle textseitig deduplizieren — adressiert den Restverlust auch im neuen Weg.
   Alternativ/ergänzend: Schnitt an Stille-Grenzen (ffmpeg silencedetect).
2. **Praxis-Tipp bis dahin:** Einzelaufnahmen unter ~20 Minuten halten, dann wird
   gar nicht gesplittet.
3. Alter Weg nur noch für den dokumentierten Sonderfall — und dann möglichst ohne
   Rekompression (`--no-compress`).

---
*Erstellt: 2026-06-10, Analyse durch Claude (Cowork) auf Basis der beiden
Markdown-Ausgaben desselben Tagesmaterials.*
