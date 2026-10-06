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

## Trascrizione, pause e sottotitoli

- La trascrizione gira sul Mac con Whisper (su M1 circa 9 minuti per 52 minuti di parlato), niente
  viene caricato online. Viene salvata accanto al video: `video.mp4.transcript.json` (dati con i tempi
  di ogni parola) e `video.mp4.transcript.txt` (testo leggibile). Ogni video si trascrive una volta sola.
- Con la trascrizione l'assistente può tagliare in base a cosa dici, togliere le pause in un colpo
  solo e aggiungere sottotitoli che seguono i tagli.
- CapCut può aprire da solo solo i file nella cartella Filmati (`~/Movies`): lì i video vengono usati
  dove sono; gli altri entrano nel progetto come copia istantanea del Mac, senza occupare spazio in più.

## Movimenti di camera

`capcut_add_camera_move` aggiunge movimenti di camera professionali, "virtuali" (fatti con keyframe
ed effetti), su un tratto di tempo: avvicinamento netto e ritorno (punch_in), zoom morbido avanti e
indietro, avvicinamento lento, colpo su una parola (punch), panoramiche, inclinazioni, effetto
camera a mano, frustate, e gli effetti di CapCut (tremolio, fisheye, messa a fuoco, flash, glitch).
Funzionano anche attraverso i tagli e tornano sempre all'inquadratura di partenza. Togliendo le
pause, `punch_in_zoom: 1.12` alterna un'inquadratura più stretta su un pezzo sì e uno no, per
nascondere i salti. I movimenti si sommano ai keyframe già presenti; per rifare un movimento sullo
stesso tratto si usa `mode: "replace"`. `easing` sceglie l'andamento: morbido (smooth), lineare,
scattante (snappy) o drammatico (dramatic).

## Progetti esistenti

Si può aggiungere a un progetto fatto in CapCut: chiedi per esempio "apri il progetto *Intervista*
e aggiungi un titolo nei primi 3 secondi". `capcut_list_projects` mostra i progetti,
`capcut_open_project` lo apre. Le aggiunte vanno su tracce nuove sopra quelle esistenti: le clip
che ci sono già non vengono mai modificate, spostate o tolte. Al salvataggio il kit riscrive la
timeline originale identica più le aggiunte, in tutte le copie che CapCut tiene, dopo aver fatto un
backup dell'intera cartella del progetto in `~/Movies/CapCut MCP Backups`. **Chiudi CapCut prima di
salvare** (se il kit non riesce a capire se è aperto, non salva); se nel frattempo hai modificato il
progetto in CapCut, va riaperto. Se il salvataggio si interrompe a metà, alla chiamata successiva su
quel draft (o al riavvio del kit) viene completato o annullato: il progetto, i suoi metadati e i
media restano tutti nella versione vecchia o tutti in quella nuova, mai a metà. Il kit tocca solo
ciò che quel salvataggio aveva creato: se nel frattempo il progetto è cambiato (per esempio l'hai
modificato in CapCut), lo lascia com'è e ti dice dove trovare la versione precedente. Se per
annullare deve toccare la cartella dei progetti di CapCut, aspetta che CapCut sia chiuso. `capcut_get_timeline`
mostra i salvataggi rimasti a metà e cosa fare.

Se hai modificato in CapCut un progetto creato dal kit, il kit non lo sovrascrive più ri-salvando il
vecchio draft: lo rifiuta e chiede di aprirlo con `capcut_open_project` (oppure `overwrite: true`,
e la tua versione finisce comunque nei backup).

## Cosa sapere

- L'assistente non vede né sente il video: conosce durata e dimensioni, quindi taglia per tempi.
  Per tagli sul contenuto serve una trascrizione con i tempi (es. Whisper), oppure gli fai estrarre
  fotogrammi con ffmpeg da guardare.
- Posizioni da 0 a 1 partendo dall'alto a sinistra (0,5 / 0,5 = centro).
- Dimensione testo sulla scala di CapCut: circa 5 piccolo, 8 normale, 12–15 titolo.
- Transizioni, animazioni ed effetti hanno nomi esatti di CapCut: l'assistente li cerca con
  `capcut_list_types`.
- La transizione si mette sulla clip **precedente** (la collega alla successiva).
- Elementi sulla stessa traccia non possono sovrapporsi: un elemento che si sovrapporrebbe va da solo
  sulla prima traccia libera dello stesso tipo (`text_main_2`, `video_main_2`…, sopra la prima).
- Musica di sottofondo: `capcut_add_background_music` la stende sotto tutto il montaggio (ripetendola
  se è più corta), con dissolvenze, e la abbassa mentre parli se le indichi il video della voce
  (serve la trascrizione).
- Correzione colore: keyframe di saturazione, contrasto e luminosità; restano modificabili in CapCut.

## Limiti

- Sticker: serve l'ID di uno sticker della libreria CapCut e non c'è un catalogo; meglio un PNG come
  immagine.
- Non disponibili: filtri colore del pannello Filtri, ritocco viso, rimozione sfondo, stabilizzazione,
  sottotitoli automatici di CapCut.

## Trasloco su un nuovo Mac

Installa i prerequisiti, poi `git clone` + `./setup.sh` come sopra. Gli ambienti locali (`venv`, che
punta all'ambiente attivo in `venvs/`, `node_modules/`, `dist/`) non sono nel repository: li rigenera
`setup.sh`, con le stesse versioni dei pacchetti (file `requirements*.lock.txt` e
`package-lock.json`). Si può rilanciare quando si vuole:
prepara tutto a parte e sostituisce l'installazione solo se funziona. I draft in corso stanno in
`~/Library/Application Support/capcut-mcp-kit`: copiala se vuoi ritrovarli sul nuovo Mac.

Elenco completo delle modifiche rispetto ai progetti originali: [NOTICE](../NOTICE).
