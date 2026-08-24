import asyncio
import csv
import io
import json
import re
import uuid
from collections import deque
from pathlib import Path
from urllib.parse import urljoin, urlparse, urldefrag

import imagehash
import numpy as np
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image
from playwright.async_api import async_playwright
from pydantic import BaseModel, Field, HttpUrl

BASE = Path(__file__).resolve().parent
REFERENCE = Image.open(BASE / "reference" / "no-image.jpg").convert("RGB")
REF_PHASH = imagehash.phash(REFERENCE)
REF_DHASH = imagehash.dhash(REFERENCE)
REF_AHASH = imagehash.average_hash(REFERENCE)

app = FastAPI(title="Product Image Audit Pro")
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
JOBS: dict[str, dict] = {}

class ScanRequest(BaseModel):
    url: HttpUrl
    max_pages: int = Field(default=1000, ge=10, le=10000)
    threshold: int = Field(default=8, ge=2, le=16)
    concurrency: int = Field(default=4, ge=1, le=8)

def clean_text(value) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()

def canon(url: str) -> str:
    url = urldefrag(url.strip())[0]
    p = urlparse(url)
    scheme = p.scheme or "https"
    path = p.path or "/"
    return f"{scheme}://{p.netloc}{path}" + (f"?{p.query}" if p.query else "")

def same_domain(a: str, b: str) -> bool:
    return urlparse(a).netloc.lower().lstrip("www.") == urlparse(b).netloc.lower().lstrip("www.")

def is_skippable_path(url: str) -> bool:
    path = urlparse(url).path.lower()
    return any(token in path for token in ("/cart", "/checkout", "/login", "/logout", "/account", "/privacy", "/cookie", "/terms", "/wishlist", "/compare"))

def product_from_jsonld(soup: BeautifulSoup):
    for node in soup.find_all("script", type="application/ld+json"):
        raw = node.string or node.get_text()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict):
                continue
            typ = item.get("@type")
            if typ == "Product" or (isinstance(typ, list) and "Product" in typ):
                return clean_text(item.get("name"))
            graph = item.get("@graph")
            if isinstance(graph, list):
                for node2 in graph:
                    if isinstance(node2, dict):
                        typ2 = node2.get("@type")
                        if typ2 == "Product" or (isinstance(typ2, list) and "Product" in typ2):
                            return clean_text(node2.get("name"))
    return None

def detect_product(soup: BeautifulSoup, url: str):
    name = product_from_jsonld(soup)
    if name:
        return True, name
    product_type = soup.find(attrs={"itemtype": re.compile(r"Product", re.I)})
    if product_type:
        h1 = soup.find("h1")
        return True, clean_text(h1.get_text(" ", strip=True) if h1 else url)
    og_type = soup.find("meta", property="og:type")
    if og_type and (og_type.get("content") or "").lower() == "product":
        h1 = soup.find("h1")
        return True, clean_text(h1.get_text(" ", strip=True) if h1 else url)
    path = urlparse(url).path.lower()
    if re.search(r"/(product|prodotti|prodotto|shop)/[^/]+", path):
        h1 = soup.find("h1")
        if h1:
            return True, clean_text(h1.get_text(" ", strip=True))
    return False, ""

def image_urls(soup: BeautifulSoup, page_url: str) -> list[str]:
    urls, seen = [], set()
    def add(value):
        if not value:
            return
        value = value.strip()
        if value.startswith("data:") or value.lower().endswith(".svg"):
            return
        full = urljoin(page_url, value)
        if full not in seen:
            seen.add(full); urls.append(full)
    for img in soup.find_all("img"):
        for attr in ("src", "data-src", "data-lazy-src", "data-original", "data-fallback-src"):
            add(img.get(attr))
        srcset = img.get("srcset") or img.get("data-srcset")
        if srcset:
            for candidate in srcset.split(","):
                add(candidate.strip().split(" ")[0])
    for tag in (soup.find("meta", property="og:image"), soup.find("meta", attrs={"name":"twitter:image"})):
        if tag:
            add(tag.get("content"))
    return urls

def similarity(ref: Image.Image, candidate: Image.Image) -> float:
    a = np.asarray(ref.resize((96, 54)).convert("RGB"), dtype=np.float32)
    b = np.asarray(candidate.resize((96, 54)).convert("RGB"), dtype=np.float32)
    return max(0.0, 1.0 - float(np.mean(np.abs(a-b))) / 255.0)

async def download_image(request_context, url: str):
    try:
        response = await request_context.get(url, timeout=10000)
        if response.status != 200:
            return None
        body = await response.body()
        if len(body) < 500:
            return None
        return Image.open(io.BytesIO(body)).convert("RGB")
    except Exception:
        return None

async def read_sitemap(context, base_url: str) -> list[str]:
    candidates = [urljoin(base_url, "/sitemap.xml"), urljoin(base_url, "/sitemap_index.xml")]
    urls, queue, visited = set(), deque(candidates), set()
    while queue and len(visited) < 25:
        url = queue.popleft()
        if url in visited:
            continue
        visited.add(url)
        try:
            r = await context.get(url, timeout=12000)
            if r.status != 200:
                continue
            xml = await r.text()
        except Exception:
            continue
        soup = BeautifulSoup(xml, "xml")
        for loc in soup.find_all("loc"):
            value = clean_text(loc.get_text())
            if not value or not same_domain(base_url, value):
                continue
            if value.lower().endswith(".xml"):
                queue.append(value)
            else:
                urls.add(canon(value))
    return list(urls)

async def analyze_image_urls(context, urls, threshold):
    for img_url in urls:
        image = await download_image(context, img_url)
        if image is None:
            continue
        ph = REF_PHASH - imagehash.phash(image)
        dh = REF_DHASH - imagehash.dhash(image)
        ah = REF_AHASH - imagehash.average_hash(image)
        px = similarity(REFERENCE, image)
        match = (ph <= threshold and dh <= threshold + 4 and ah <= threshold + 4) or (px >= 0.985 and ph <= threshold + 8)
        if match:
            return {"image_url": img_url, "phash_distance": int(ph), "dhash_distance": int(dh), "ahash_distance": int(ah), "pixel_similarity": round(px, 4)}
    return None

async def crawl(job_id: str, req: ScanRequest):
    start_url = canon(str(req.url))
    job = JOBS[job_id]
    job["status"] = "running"
    job["message"] = "Preparazione browser…"
    queue, visited, discovered, results = deque(), set(), set(), {}
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(user_agent="Mozilla/5.0 (compatible; ProductImageAuditPro/1.0)", viewport={"width":1440,"height":1000})
        sitemap_urls = await read_sitemap(ctx.request, start_url)
        for url in sitemap_urls:
            if not is_skippable_path(url):
                queue.append(url); discovered.add(url)
        queue.appendleft(start_url) if queue else queue.append(start_url)
        discovered.add(start_url)
        pages = [await ctx.new_page() for _ in range(req.concurrency)]
        async def analyze_page(page, url):
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(500)
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await page.wait_for_timeout(350)
                html = await page.content()
            except Exception:
                return None, []
            soup = BeautifulSoup(html, "html.parser")
            is_product, name = detect_product(soup, url)
            links = []
            for a in soup.find_all("a", href=True):
                nxt = canon(urljoin(url, a["href"]))
                if same_domain(start_url, nxt) and nxt not in visited and not is_skippable_path(nxt):
                    links.append(nxt)
            product = {"name":name,"url":url,"images":image_urls(soup,url)} if is_product else None
            return product, links
        while queue and len(visited) < req.max_pages:
            batch=[]
            while queue and len(batch)<len(pages) and len(visited)+len(batch)<req.max_pages:
                candidate=queue.popleft()
                if candidate in visited or not same_domain(start_url,candidate):
                    continue
                visited.add(candidate); batch.append(candidate)
            if not batch:
                break
            data=await asyncio.gather(*[analyze_page(pages[i], batch[i]) for i in range(len(batch))], return_exceptions=True)
            for item in data:
                if isinstance(item, Exception) or not item:
                    continue
                product, links=item
                for link in links:
                    if len(discovered)<req.max_pages*4 and link not in discovered:
                        discovered.add(link); queue.append(link)
                if not product:
                    continue
                match=await analyze_image_urls(ctx.request, product["images"], req.threshold)
                if match:
                    results[product["url"]]={"name":product["name"],"url":product["url"],**match}
            job["scanned"]=len(visited); job["discovered"]=len(discovered); job["matches"]=len(results)
            job["message"]=f"Analisi: {len(visited)} pagine · {len(results)} prodotti senza foto"
        for page in pages:
            await page.close()
        await browser.close()
    job["status"]="done"; job["scanned"]=len(visited); job["matches"]=len(results); job["results"]=list(results.values()); job["message"]=f"Completata: {len(results)} prodotti trovati"

@app.get("/")
async def home():
    return FileResponse(BASE / "static" / "index.html")

@app.post("/api/jobs")
async def create_job(req: ScanRequest):
    job_id=uuid.uuid4().hex
    JOBS[job_id]={"id":job_id,"status":"queued","message":"In coda…","scanned":0,"discovered":0,"matches":0,"results":[]}
    asyncio.create_task(crawl(job_id, req))
    return {"job_id":job_id}

@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str):
    job=JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job non trovato")
    return job

@app.get("/api/jobs/{job_id}/csv")
async def job_csv(job_id: str):
    job=JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job non trovato")
    buffer=io.StringIO(); writer=csv.writer(buffer); writer.writerow(["Nome prodotto","Link","Immagine rilevata"])
    for row in job.get("results",[]):
        writer.writerow([row["name"],row["url"],row["image_url"]])
    return StreamingResponse(iter([buffer.getvalue().encode("utf-8-sig")]), media_type="text/csv; charset=utf-8", headers={"Content-Disposition":'attachment; filename="prodotti-senza-foto.csv"'})
