# Product Image Audit Pro

Versione online deployabile del tool per trovare prodotti che usano l'immagine
segnaposto `app/reference/no-image.jpg`.

## Architettura

- FastAPI backend
- Playwright + Chromium per siti JavaScript
- sitemap.xml / sitemap_index.xml quando disponibili
- crawler dello stesso dominio
- riconoscimento prodotto via JSON-LD, microdata, OpenGraph e fallback URL
- confronto immagine con pHash / dHash / aHash + similarity pixel-level
- progress polling
- export CSV

## Deploy rapido

### Render

1. Crea un nuovo Web Service collegando questo progetto.
2. Seleziona Docker.
3. Il `Dockerfile` è già pronto.
4. Deploy.

Il file `render.yaml` contiene anche una configurazione base.

### Docker locale

```bash
docker build -t product-image-audit .
docker run --rm -p 8000:8000 product-image-audit
```

Apri `http://localhost:8000`.

## Produzione

Per uso continuativo conviene aggiungere una coda persistente (Redis/Celery o RQ),
storage dei risultati, autenticazione e rate limiting. La versione inclusa usa una
memoria in-process per semplicità: i job vengono persi se il container viene riavviato.

## Importante

Usare il tool solo su siti che si è autorizzati ad analizzare. Non aggira CAPTCHA,
login o altre misure di sicurezza.
