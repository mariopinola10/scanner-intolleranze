#!/usr/bin/env python3
"""Genera le ricette IA della settimana e le aggiunge a ricette.json.

Lo usa la GitHub Action .github/workflows/ricette-settimanali.yml ogni lunedì,
ma si può lanciare anche a mano:
    GEMINI_API_KEY=... python3 strumenti/genera_ricette.py [--forza] [--quante 3]

- Se per la settimana corrente ci sono già ricette IA non fa nulla (salvo --forza).
- Ogni ingrediente viene controllato su fodmap.json: si tengono solo le ricette
  tutte nel verde; se ne mancano riprova fino a 3 volte.
"""
import argparse, datetime, json, os, re, sys, time, unicodedata, urllib.error, urllib.request

QUI = os.path.dirname(os.path.abspath(__file__))
RADICE = os.path.dirname(QUI)
MODELLI = ["gemini-3.8-flash", "gemini-3.6-flash", "gemini-3.5-flash-lite"]  # preferiti; gli altri li chiedo a Google
STAGIONI = ["inverno", "inverno", "primavera", "primavera", "primavera", "estate", "estate", "estate", "autunno", "autunno", "autunno", "inverno"]
MESI = ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio", "agosto", "settembre", "ottobre", "novembre", "dicembre"]
DI_STAGIONE = {
    "inverno": "arance, mandarini, clementine, kiwi, pompelmo, finocchi, cavolo nero, verza, broccoli (cime), spinaci, bietole, carote, porro (parte verde), radicchio, sedano rapa, pastinaca, patate, zucca",
    "primavera": "fragole, kiwi, zucchine, lattuga, rucola, spinaci novelli, bietole, carote, ravanelli, erba cipollina, cetrioli, fagiolini, patate novelle",
    "estate": "pomodori, zucchine, melanzane, peperoni (verdi; rossi in piccole dosi), cetrioli, fagiolini, melone cantalupo, ananas, fragole, mirtilli, lamponi, basilico",
    "autunno": "zucca (non butternut), carote, patate, spinaci, bietole, finocchi, radicchio, verza, cavolo nero, pastinaca, sedano rapa, kiwi, mandarini, castagne (piccole dosi)",
}
CATEGORIE = {"Verdura", "Frutta", "Cereali", "Proteine", "Latticini", "Legumi", "Frutta a guscio e semi", "Grassi e oli", "Condimenti"}
LIVN = {"basso": 0, "medio": 1, "alto": 2}


def norm(s):
    s = unicodedata.normalize("NFD", str(s or "").lower())
    return "".join(c for c in s if not unicodedata.combining(c)).strip()


def num(x):
    try:
        return None if x in ("", None) else float(x)
    except (TypeError, ValueError):
        return None


def oggi_roma():
    # ora italiana senza dipendere da tzdata: UTC+1, +2 tra l'ultima domenica di marzo e l'ultima di ottobre
    u = datetime.datetime.now(datetime.timezone.utc)
    def ultima_domenica(m):
        d = datetime.date(u.year, m, 31)
        return d - datetime.timedelta(days=(d.weekday() + 1) % 7)
    estiva = ultima_domenica(3) <= u.date() < ultima_domenica(10)
    return (u + datetime.timedelta(hours=2 if estiva else 1)).date()


def livello(a, g):
    """0 verde, 1 giallo, 2 rosso per g grammi di un alimento della tabella."""
    pz = a.get("porzioni") or {}
    v, y, r = num(pz.get("verde")), num(pz.get("giallo")), num(pz.get("rosso"))
    if g is not None and (v is not None or y is not None or r is not None):
        if r is not None and g >= r: return 2
        if v is not None and g <= v: return 0
        if y is None and r is None: return 0  # oltre la dose testata, ma senza soglie gialla/rossa
        return 1
    liv = [LIVN.get(x, 0) for x in (a.get("livelli") or {}).values()]
    return max(liv) if liv else 0


def voci_per_ia(alimenti):
    righe = []
    for a in alimenti:
        if a.get("categoria") not in CATEGORIE or a.get("marca") or "var." in a["nome"] or len(a["nome"]) >= 48 or a.get("conferma") == "da verificare":
            continue
        pz, liv = a.get("porzioni") or {}, [(g, x) for g, x in (a.get("livelli") or {}).items() if x != "basso"]
        if num(pz.get("verde")) is not None:
            righe.append(f"{a['nome']} | verde fino a {num(pz['verde']):g} {pz.get('unita', 'g')}")
        elif liv:
            righe.append(f"{a['nome']} | " + ", ".join(f"{g} {x}" for g, x in liv))
        else:
            righe.append(f"{a['nome']} | libero")
    return "\n".join(righe[:450])


def prompt(giorno, stagione, gia, voci, quante):
    return f"""Sei un cuoco italiano esperto di dieta low FODMAP (Monash University). Proponi {quante} ricette nuove per la settimana del {giorno.day} {MESI[giorno.month - 1]}, stagione {stagione}, per una persona con dieta senza glutine, senza lattosio e low FODMAP.
Usa ingredienti di stagione in Italia: {DI_STAGIONE[stagione]}.
Niente aglio e cipolla (usa olio aromatizzato all'aglio, erba cipollina, parte verde del porro), niente frumento, orzo, segale, niente latticini con lattosio (parmigiano stagionato va bene), niente funghi, cavolfiore, asparagi, legumi secchi, mele, pere, anguria, miele.
Varia le portate (colazione, primo, secondo, piatto unico) e non ripetere queste ricette: {"; ".join(gia)}.
Le dosi sono per UNA persona e ogni ingrediente deve restare entro la soglia verde indicata qui sotto. Per collegare un ingrediente alla tabella metti in "voce" il nome ESATTO di una di queste righe (prima del simbolo |); per sale, pepe, olio d'oliva, aceto, erbe e spezie lascia "voce" vuota.
{voci}

Rispondi SOLO con un array JSON di {quante} oggetti:
{{"titolo": testo, "portata": "Colazione"|"Primo"|"Secondo"|"Contorno"|"Piatto unico"|"Dolce", "tempo": minuti, "ingredienti": [{{"nome": nome in cucina, "q": numero per una persona, "u": "g"|"ml"|"q.b.", "voce": nome esatto in tabella o "", "gc": grammi da cotto se la soglia è sul cotto (riso, pasta, quinoa, polenta) altrimenti null, "nota": breve o ""}}], "passi": [frasi brevi], "nota": un consiglio o ""}}"""


def modelli_disponibili(chiave):
    """Modelli "flash" che questa chiave può usare, dal più recente (Google ritira spesso i vecchi)."""
    try:
        req = urllib.request.Request("https://generativelanguage.googleapis.com/v1beta/models?pageSize=200", headers={"x-goog-api-key": chiave})
        with urllib.request.urlopen(req, timeout=30) as r:
            elenco = json.load(r).get("models", [])
    except Exception as e:
        print(f"  elenco modelli non disponibile ({e})")
        return []
    nomi = [m["name"].split("/")[-1] for m in elenco if "generateContent" in m.get("supportedGenerationMethods", [])]
    nomi = [n for n in nomi if "flash" in n and not re.search(r"image|tts|audio|live|embed|thinking|exp", n)]
    versione = lambda n: [float(x) for x in re.findall(r"\d+(?:\.\d+)?", n)[:1]] or [0]
    return sorted(nomi, key=lambda n: (versione(n), "lite" not in n, "preview" not in n), reverse=True)


def chiama_gemini(chiave, testo, modello=None):
    """Prova i modelli in ordine; su 429/500/503 (quota o sovraccarico) aspetta e riprova lo stesso modello."""
    errori = []
    corpo = json.dumps({"contents": [{"role": "user", "parts": [{"text": testo}]}],
                        "generationConfig": {"temperature": 0.7, "responseMimeType": "application/json"}}).encode()
    modelli = list(dict.fromkeys([modello or MODELLI[0], *MODELLI, *modelli_disponibili(chiave)]))
    print("  modelli da provare: " + ", ".join(modelli[:8]))
    for m in modelli[:8]:
        for attesa in (0, 20, 60):
            if attesa:
                print(f"  {m} occupato, riprovo tra {attesa} s")
                time.sleep(attesa)
            req = urllib.request.Request(f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent", data=corpo,
                                         headers={"Content-Type": "application/json", "x-goog-api-key": chiave})
            try:
                with urllib.request.urlopen(req, timeout=180) as r:
                    j = json.load(r)
                t = "".join(p.get("text", "") for p in (j.get("candidates") or [{}])[0].get("content", {}).get("parts", []))
                if t:
                    print(f"  risposta da {m}")
                    return t, m
                errori.append(f"{m}: risposta vuota")
                break
            except urllib.error.HTTPError as e:
                try:
                    msg = json.loads(e.read().decode("utf-8", "replace"))["error"]["message"]
                except Exception:
                    msg = str(e)
                errori.append(f"{m}: {e.code} {msg[:160]}")
                if e.code in (401, 403) or (e.code == 400 and "api key" in msg.lower()):
                    raise RuntimeError("Chiave Gemini non valida: controlla il secret GEMINI_API_KEY. " + msg[:160])
                if e.code not in (429, 500, 503):
                    break  # modello non disponibile o richiesta rifiutata: passo al successivo
            except Exception as e:  # rete, timeout
                errori.append(f"{m}: {e}")
    raise RuntimeError("Nessun modello Gemini ha risposto:\n  " + "\n  ".join(errori))


def leggi_ricette(testo, tab):
    m = re.search(r"```(?:json)?\s*([\s\S]*?)```", testo)
    s = m.group(1) if m else testo[testo.find("["): testo.rfind("]") + 1]
    a = json.loads(s)
    if isinstance(a, dict):
        a = a.get("ricette") or [a]
    lista = lambda v: v if isinstance(v, list) else ([v] if v else [])
    out = []
    for x in a:
        if not isinstance(x, dict) or not x.get("titolo"):
            continue
        ingr = []
        for i in lista(x.get("ingredienti")):
            if not isinstance(i, dict) or not i.get("nome"):
                continue
            d = {"nome": str(i["nome"]), "q": num(i.get("q")) if num(i.get("q")) is not None else i.get("q", ""), "u": i.get("u") or "g"}
            if d["q"] is not None and isinstance(d["q"], float) and d["q"].is_integer():
                d["q"] = int(d["q"])
            v = tab.get(norm(i.get("voce")))
            if v:
                d["voce"] = v["nome"]
            if num(i.get("gc")):
                d["gc"] = int(num(i["gc"]))
            if i.get("nota"):
                d["nota"] = str(i["nota"])
            ingr.append(d)
        out.append({"titolo": str(x["titolo"]).strip(), "portata": str(x.get("portata") or ""), "tempo": int(num(x.get("tempo")) or 0) or None,
                    "ingredienti": ingr, "passi": [str(p) for p in lista(x.get("passi"))], **({"nota": str(x["nota"])} if x.get("nota") else {})})
    return out


def tutta_verde(r, tab):
    for i in r["ingredienti"]:
        a = tab.get(norm(i.get("voce")))
        if a and livello(a, num(i.get("gc")) or num(i.get("q"))) > 0:
            return False, i["nome"]
    return True, ""


def unisci(percorso_ricette, nuove, quante, forza=False):
    """Aggiunge le ricette nuove a ricette.json (letto adesso, quindi aggiornato). Ritorna quante ne ha aggiunte."""
    R = json.load(open(percorso_ricette, encoding="utf-8"))
    titoli = {norm(r["titolo"]) for r in R["ricette"]}
    aggiunte = 0
    for r in nuove:
        k = r["settimana"]
        gia = [x for x in R["ricette"] if x.get("settimana") == k and x.get("fonte") == "IA"]
        if (len(gia) >= quante and not forza) or norm(r["titolo"]) in titoli:
            continue
        n = len([x for x in R["ricette"] if x.get("settimana") == k]) + 1
        while any(x.get("id") == f"ia-{k.lower()}-{n}" for x in R["ricette"]):
            n += 1
        R["ricette"].append({"id": f"ia-{k.lower()}-{n}", **r})
        titoli.add(norm(r["titolo"]))
        aggiunte += 1
    if aggiunte:
        R["versione"] = int(R.get("versione", 0)) + 1
        R["aggiornato"] = oggi_roma().isoformat()
        with open(percorso_ricette, "w", encoding="utf-8") as f:
            json.dump(R, f, ensure_ascii=False, indent=1)
            f.write("\n")
    return aggiunte


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--forza", action="store_true", help="genera anche se la settimana ha già ricette IA")
    ap.add_argument("--quante", type=int, default=3)
    ap.add_argument("--ricette", default=os.path.join(RADICE, "ricette.json"))
    ap.add_argument("--tabella", default=os.path.join(RADICE, "fodmap.json"))
    ap.add_argument("--uscita", help="salva le ricette generate in questo file invece di modificare ricette.json")
    ap.add_argument("--unisci", help="non chiama Gemini: aggiunge a ricette.json le ricette salvate in questo file")
    args = ap.parse_args()

    if args.unisci:
        if not os.path.exists(args.unisci):
            print("Nessuna ricetta da aggiungere.")
            return
        nuove = json.load(open(args.unisci, encoding="utf-8"))
        n = unisci(args.ricette, nuove, args.quante, args.forza)
        print(f"Aggiunte {n} ricette a ricette.json" if n else "Ricette già presenti: niente da aggiungere.")
        return

    chiave = os.environ.get("GEMINI_API_KEY", "").strip()
    if not chiave:
        sys.exit("Manca GEMINI_API_KEY (nella GitHub Action: Settings > Secrets and variables > Actions).")

    oggi = oggi_roma()
    lun = oggi - datetime.timedelta(days=oggi.weekday())
    anno, sett, _ = lun.isocalendar()
    chiave_sett = f"{anno}-W{sett:02d}"
    stagione = STAGIONI[lun.month - 1]

    R = json.load(open(args.ricette, encoding="utf-8"))
    alimenti = json.load(open(args.tabella, encoding="utf-8"))["alimenti"]
    tab = {norm(a["nome"]): a for a in alimenti}
    gia_sett = [r for r in R["ricette"] if r.get("settimana") == chiave_sett and r.get("fonte") == "IA"]
    if gia_sett and not args.forza:
        print(f"Settimana {chiave_sett}: ci sono già {len(gia_sett)} ricette IA, niente da fare.")
        return
    titoli = {norm(r["titolo"]) for r in R["ricette"]}
    gia = [r["titolo"] for r in R["ricette"] if stagione in r.get("stagioni", [])]
    voci = voci_per_ia(alimenti)

    buone = []
    for tentativo in range(3):
        manca = args.quante - len(buone)
        if manca <= 0:
            break
        testo, modello = chiama_gemini(chiave, prompt(lun, stagione, gia + [b["titolo"] for b in buone], voci, manca), os.environ.get("GEMINI_MODEL"))
        for r in leggi_ricette(testo, tab):
            ok, perche = tutta_verde(r, tab)
            if not ok:
                print(f"  scartata '{r['titolo']}': {perche} fuori dal verde")
            elif norm(r["titolo"]) in titoli or len(r["ingredienti"]) < 2 or not r["passi"]:
                print(f"  scartata '{r['titolo']}': doppione o incompleta")
            else:
                titoli.add(norm(r["titolo"]))
                buone.append({**r, "stagioni": [stagione], "fonte": "IA", "settimana": chiave_sett, "creata": oggi.isoformat(), "modello": modello})
    if not buone:
        sys.exit("Nessuna ricetta valida generata.")
    buone = buone[: args.quante]
    print(f"Settimana {chiave_sett} ({stagione}): " + "; ".join(b["titolo"] for b in buone))

    if args.uscita:
        with open(args.uscita, "w", encoding="utf-8") as f:
            json.dump(buone, f, ensure_ascii=False, indent=1)
        print(f"Salvate in {args.uscita}: le aggiungo a ricette.json nel passo successivo.")
    else:
        print(f"Aggiunte {unisci(args.ricette, buone, args.quante, args.forza)} ricette a ricette.json")


if __name__ == "__main__":
    main()
