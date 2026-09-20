# Roadmap — Tommaso (ruolo E: Trust layer e UX)

Aggiornata 19 settembre, dopo il run completo di Giorgio sui 6 GB.

Questo file è tuo. È scritto in italiano perché serve a te per lavorare, non al
team. Tienilo aperto accanto al terminale.

---

## Chi fa cosa adesso

| Persona | Su cosa |
| --- | --- |
| Giorgio |  specializzare Ollama poi adattare secondo dominio |
| Manny | Installazione e configurazione di Ollama |
| Ezequiel | Collegare S4 e S7 al gateway |
|Zoe| fetch diretto features grafici
| **Tu** | **UX, chiarezza del prodotto, e ciò che l'operatore vede** |

Non toccare S4 né S7: ci sta lavorando Ezequiel. Se li modifichi anche tu,
finite con due versioni in conflitto e perdete un'ora a rimetterle insieme.

---

## La domanda che ti sei fatto: devo runnare la pipeline?

**No.** Per il tuo lavoro non serve.

La UI legge file JSON che **esistono già** in `artifacts/`. Giorgio li ha
generati facendo girare tutto sui 6 GB. Ti bastano:

```bash
python ui/server.py
```

e apri `http://localhost:8000/ui/`.

Quando Ezequiel finisce, `semantics.json` e `diagnosis.json` cambieranno
contenuto (arriveranno ruoli inferiti dal modello invece che da un if, e una
spiegazione in prosa). Ma **non cambierà la forma dei file**, quindi la tua UI
continuerà a funzionare. È esattamente il motivo per cui abbiamo i contratti.

Se vuoi comunque rigenerare tutto da zero in fretta:

```bash
python orchestrator.py --mode dev --start-ui
```

`--mode dev` gira su 2 run di simulazione invece che su tutto. Secondi, non ore.

---

## Tre problemi che ho trovato nel run di Giorgio

Non sono tuoi da risolvere, ma devi sapere che esistono perché due riguardano
cose che i giudici guarderanno.

### 1. S5 non viene mai eseguito

`orchestrator.py` lancia S1, S2, S3, S4, il manifest, S6 e S7. **S5 manca.**

Conseguenza: il data quality non gira mai nel percorso principale. Il verdetto
di fiducia non viene ricalcolato, e il gate "dato prima del processo" di S6 legge
un file che l'orchestratore non produce.

È il criterio 2 della rubrica, e adesso viene saltato in silenzio. Va detto a
Giorgio: serve una riga in più nella lista degli stage, fra S4 e il manifest.

### 2. Il baseline PCA viene rifatto a ogni esecuzione

`s6_drift.py` chiama `fit_reference(...)` a ogni run e non salva il risultato da
nessuna parte.

Perché conta, ed è anche la risposta alla tua domanda sui batch: in fabbrica i
dati arrivano a blocchi. Se ricalcoli il "normale" su ogni blocco nuovo, una
deriva lenta viene **assorbita dentro la definizione di normale** e non la vedi
più. È letteralmente il guasto che la challenge racconta: il sensore che deriva
per settimane senza far scattare allarmi.

Il baseline va calcolato una volta sui dati normali, salvato su disco, e
riutilizzato per tutti i batch successivi. Da segnalare a Giorgio o a Ezequiel.

### 3. Il repository è ingrassato a 900 MB

Sono finiti dentro git 20.985 file JSON in `artifacts/drift_events/` più uno zip
da 15 MB. Il `.gitignore` aveva `artifacts/*.json`, che copre solo il primo
livello e non le sottocartelle.

Non è urgente, ma ogni clone del team adesso scarica quasi un giga. Si sistema
così (da concordare con chi ha generato i file):

```bash
printf 'artifacts/**/*.json\nartifacts/**/*.jsonl\n*.zip\n' >> .gitignore
git rm -r --cached artifacts/drift_events online_retail_II.csv.zip
git commit -m "Stop tracking generated artifacts and the sample archive"
```

---

## La tua roadmap

In ordine. I primi due sono richiesti dalla challenge, il resto sono bonus.

| # | Cosa | Tempo | Perché |
| --- | --- | --- | --- |
| 1 | Vista operatore: diagnosi in italiano comprensibile | 1,5 h | Deliverable 4, oggi non soddisfatto |
| 2 | Toggle per la vista tecnica | 20 min | Tiene le prove per i giudici senza seppellire l'operatore |
| 3 | Chatbox "perché?" | 1,5 h | Bonus, ed è economico perché il log è già strutturato |
| 4 | Grafico del drift per evento | 1 h | Bonus, ed è l'immagine che vende la storia sul palco |

**Se il tempo stringe, fermati dopo il 2.** I primi due trasformano una demo di
macchinario in una demo di prodotto.

---

## I prompt per Claude Code

Uno per sessione. `/clear` fra uno e l'altro. Fatti dare il piano prima del
codice: correggere un piano costa un minuto, correggere il codice ne costa
quaranta.

### Passo 1 — la vista operatore

> Leggi CONTRACTS.md e INTEGRATION.md. Possiedo ui/. Le schermate mostrano
> col_009, epistemic_status ed evidence ids. La challenge richiede "a
> step-by-step explanation suitable for an operator with no data science
> background", quindi quel pubblico non è servito.
>
> Ristruttura la schermata di diagnosi in due livelli.
>
> VISTA OPERATORE, mostrata di default:
> - cosa non va, una frase, senza gergo e senza col_NNN
> - quanto il sistema è sicuro, a parole e non con un numero
> - quali sensori sono coinvolti, chiamati con il ruolo inferito
> - cosa il sistema NON sa, dalla lista uncertainties
> - Accetta / Contesta / Ribalta, invariati
>
> VISTA TECNICA, chiusa dietro un toggle:
> - tutto quello che c'è adesso, inclusi col ids, evidenze, statistiche e il
>   prompt generato
>
> Leggi dagli stessi artefatti. Non cambiare nessun formato. Dove diagnosis.json
> non ha ancora un campo in prosa, costruisci la frase da fault_class, dal primo
> sensore in ranked_signals e dal suo ruolo in semantics.json.
>
> Attenzione: Ezequiel sta collegando S4 e S7 al gateway, quindi semantics.json e
> diagnosis.json cambieranno contenuto ma non forma. Scrivi il codice in modo che
> funzioni con entrambi: con la prosa quando c'è, con la frase costruita quando
> manca.
>
> Pianifica prima il layout e lo stato in meno di dieci righe, poi fermati.

### Passo 2 — il toggle

> Nella schermata di diagnosi, sposta tutti gli elementi tecnici dietro un solo
> controllo "Mostra evidenza tecnica", chiuso all'avvio. Deve ricordare la
> scelta dell'utente fra un reload e l'altro.
>
> Dentro ci va anche il prompt generato, letto da debug_prompt_generated in
> semantics.json. Non è materiale da operatore, ma è la nostra prova che il
> prompt è generato dalle statistiche e non scritto a mano, quindi non va tolta.
>
> Pianifica prima, poi fermati.

### Passo 3 — la chatbox

> Leggi CONTRACTS.md e trust/decision_log.py. Aggiungi una casella domande alla
> UI operatore.
>
> Vincolo non negoziabile: non deve diventare una seconda via d'uscita per i
> dati. La domanda passa da call_model() in trust/gateway.py come ogni altra
> chiamata, e il payload contiene SOLO voci recuperate da decision_log.jsonl più
> gli oggetti evidence che citano. Mai gli artefatti interi, mai niente da data/.
>
> Flusso: l'operatore chiede → recupera le voci di log pertinenti → manda quelle
> voci e le loro evidenze come payload → la risposta deve citare gli entry_id
> usati → registra lo scambio come una nuova voce del decision log.
>
> Così la chatbox non è una funzionalità nuova ma un lettore in linguaggio
> naturale sopra la traccia di audit, e ogni risposta resta tracciabile.
>
> Pianifica prima, poi fermati.

### Passo 4 — il grafico

> Leggi contracts/drift_events.schema.json e un file da artifacts/drift_events/.
> Aggiungi un grafico alla schermata di diagnosi: la deviazione nel tempo per un
> evento selezionato, con il momento del flag segnato, e una linea per ogni
> sensore in ranked_signals colorata secondo la sua quota di contributo.
>
> HTML autocontenuto e SVG inline, nessuna libreria esterna e nessun CDN. Etichetta
> gli assi in unità che un operatore capisce: tempo trascorso, non indice campione.
>
> Lo scopo di questo grafico è una sola immagine che mostri un valore che resta
> dentro il suo range normale mentre se ne allontana come tendenza. È la storia
> di tutta la challenge in una figura.
>
> Pianifica prima, poi fermati.

---

## Prima di ogni commit

```bash
make check
```

Se fallisce, incolla l'errore a Claude Code e basta. Si spiega da solo.
