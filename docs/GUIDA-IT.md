# capcut-mcp-kit — guida in italiano

Permette a un assistente AI (Claude Code o un altro client MCP) di costruire **progetti CapCut desktop**:
taglia e mette in fila le clip, aggiunge titoli, sottotitoli, musica, transizioni, effetti, animazioni,
keyframe e correzioni colore. Il risultato è un normale progetto CapCut che apri, rifinisci ed esporti.

Testato su macOS con CapCut 9.1.0 (versione internazionale).

## Installazione

Prerequisiti: CapCut desktop (aperto almeno una volta), [Homebrew](https://brew.sh), poi
`brew install node python ffmpeg`, e [Claude Code](https://claude.com/claude-code).

```bash
git clone https://github.com/simopa/capcut-mcp-kit.git
cd capcut-mcp-kit
./setup.sh                      # disponibile in tutti i progetti Claude Code
# oppure: ./setup.sh ~/MieiVideo  # solo in quella cartella
```

## Uso

1. Apri Claude Code (o Codex), approva il server `capcut` e chiedi il montaggio, ad esempio:
   *"Progetto verticale da `~/Desktop/intervista.mp4`, tieni dal secondo 5 al 40, titolo 'Puntata 3'
   in alto con dissolvenza, musica `~/Musica/base.mp3` al 30% con uscita in dissolvenza di 2 secondi,
   salvalo come 'Puntata 3'."*
2. **Riavvia CapCut**: la lista dei progetti si aggiorna solo all'avvio.

Il server Python parte da solo al primo comando e resta acceso (log in `vectcut-api/server.log`),
così Claude e Codex possono usarlo insieme. Per avviarlo a mano c'è `./start-server.sh`.

## Cosa sapere

- L'assistente non vede né sente il video: conosce durata e dimensioni, quindi taglia per tempi.
  Per tagli sul contenuto serve una trascrizione con i tempi (es. Whisper), oppure gli fai estrarre
  fotogrammi con ffmpeg da guardare.
- Posizioni da 0 a 1 partendo dall'alto a sinistra (0,5 / 0,5 = centro).
- Dimensione testo sulla scala di CapCut: circa 5 piccolo, 8 normale, 12–15 titolo.
- Transizioni, animazioni ed effetti hanno nomi esatti di CapCut: l'assistente li cerca con
  `capcut_list_types`.
- La transizione si mette sulla clip **precedente** (la collega alla successiva).
- Elementi sulla stessa traccia non possono sovrapporsi: per due testi insieme o un video sopra
  l'altro si usa un'altra traccia (`track_name`).
- Correzione colore: keyframe di saturazione, contrasto e luminosità; restano modificabili in CapCut.

## Limiti

- Sticker: serve l'ID di uno sticker della libreria CapCut e non c'è un catalogo; meglio un PNG come
  immagine.
- Non disponibili: filtri colore del pannello Filtri, ritocco viso, rimozione sfondo, stabilizzazione,
  sottotitoli automatici di CapCut.

## Trasloco su un nuovo Mac

Installa i prerequisiti, poi `git clone` + `./setup.sh` come sopra. Gli ambienti locali (`venv/`,
`node_modules/`, `dist/`) non sono nel repository: li rigenera `setup.sh`.

Elenco completo delle modifiche rispetto ai progetti originali: [NOTICE](../NOTICE).
