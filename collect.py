"""Türkiye 5Y CDS ve USD/TRY günlük toplayıcı (investing.com geçmiş veri sayfaları).

Çıktılar (data/ klasörü):
  cds.csv / usdtry.csv              tarih,kapanis          -> yalnız bugünden ÖNCEKİ günler (kesin kapanış)
  gun_ici.csv / usdtry_gun_ici.csv  zaman_utc,tarih,deger  -> bugünün o anki değeri (kapanış değildir)
  spx_aylik.csv                     ay,kapanis             -> S&P 500 tamamlanmış ay sonu kapanışları (stooq, yedek Yahoo)
  log.csv        her denemenin kaydı (adres, HTTP kodu, okunan satır, son tarih/değer, sayfa özeti)
  raw/           son 20 denemenin ham sayfası (sıkıştırılmış)
"""
import csv, datetime as dt, gzip, hashlib, json, os, re, sys, time

VARLIKLAR = [
    # (kapanış dosyası, gün içi dosyası, geçerli aralık, ondalık, adresler)
    ("cds.csv", "gun_ici.csv", (30, 3000), 2, [
        "https://www.investing.com/rates-bonds/turkey-cds-5-year-usd-historical-data",
        "https://uk.investing.com/rates-bonds/turkey-cds-5-year-usd-historical-data",
        "https://tr.investing.com/rates-bonds/turkey-cds-5-year-usd-historical-data",
    ]),
    ("usdtry.csv", "usdtry_gun_ici.csv", (1, 1000), 4, [
        "https://www.investing.com/currencies/usd-try-historical-data",
        "https://uk.investing.com/currencies/usd-try-historical-data",
        "https://tr.investing.com/currencies/usd-try-historical-data",
    ]),
]
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9,tr;q=0.8",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}
DATA = "data"
TR = dt.timezone(dt.timedelta(hours=3))
AYLAR = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8,
         "sep": 9, "oct": 10, "nov": 11, "dec": 12,
         "oca": 1, "şub": 2, "sub": 2, "nis": 4, "haz": 6, "tem": 7, "ağu": 8, "agu": 8,
         "eyl": 9, "eki": 10, "kas": 11, "ara": 12}


def get(url):
    """Önce tarayıcı taklidi yapan curl_cffi, olmazsa requests."""
    try:
        from curl_cffi import requests as cr
        r = cr.get(url, headers=HEADERS, impersonate="chrome", timeout=30)
        return r.status_code, r.text
    except Exception as e:  # noqa
        print("curl_cffi olmadı:", e)
    import requests
    r = requests.get(url, headers=HEADERS, timeout=30)
    return r.status_code, r.text


def sayi(s):
    s = str(s).strip().replace(" ", "")
    if re.fullmatch(r"-?\d{1,3}(\.\d{3})*,\d+", s) or re.fullmatch(r"-?\d+,\d+", s):
        s = s.replace(".", "").replace(",", ".")      # Türkçe biçim
    else:
        s = s.replace(",", "")                        # İngilizce biçim
    return float(s)


def tarih(s):
    s = str(s).strip()
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return dt.date(int(m[1]), int(m[2]), int(m[3]))
    m = re.match(r"([A-Za-zÇĞİÖŞÜçğıöşü]{3})[a-zçğıöşü]* (\d{1,2}), (\d{4})", s)   # Nov 24, 2026
    if m:
        return dt.date(int(m[3]), AYLAR[m[1].lower()], int(m[2]))
    m = re.match(r"(\d{1,2})[ .]([A-Za-zÇĞİÖŞÜçğıöşü]{3})[a-zçğıöşü]*[ .](\d{4})", s)  # 24 Kas 2026
    if m:
        return dt.date(int(m[3]), AYLAR[m[2].lower()], int(m[1]))
    m = re.match(r"(\d{2})\.(\d{2})\.(\d{4})", s)
    if m:
        return dt.date(int(m[3]), int(m[2]), int(m[1]))
    raise ValueError(s)


def nextdata_satirlar(html):
    """Sayfadaki __NEXT_DATA__ JSON'unda tarih+kapanış içeren kayıtları bul."""
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        return []
    try:
        js = json.loads(m.group(1))
    except Exception:
        return []
    out = []

    def gez(o):
        if isinstance(o, dict):
            dk = next((k for k in ("rowDateTimestamp", "rowDate", "date") if k in o), None)
            vk = next((k for k in ("last_closeRaw", "last_close", "price", "close") if k in o), None)
            if dk and vk:
                try:
                    out.append((tarih(o[dk]), sayi(o[vk])))
                except Exception:
                    pass
            for v in o.values():
                gez(v)
        elif isinstance(o, list):
            for v in o:
                gez(v)
    gez(js)
    return out


def tablo_satirlar(html):
    """Yedek: HTML tablosundaki 'tarih | kapanış' satırları."""
    out = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S):
        hucre = [re.sub(r"<[^>]+>", "", c).strip() for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.S)]
        if len(hucre) >= 2:
            try:
                out.append((tarih(hucre[0]), sayi(hucre[1])))
            except Exception:
                pass
    return out


def oku_csv(yol, anahtar):
    if not os.path.exists(yol):
        return {}
    with open(yol, newline="", encoding="utf-8") as f:
        return {r[anahtar]: r for r in csv.DictReader(f)}


def topla(kap_dosya, gi_dosya, aralik, ondalik, urls, simdi, bugun):
    satirlar, kullanilan = [], None
    for url in urls:
        try:
            kod, html = get(url)
        except Exception as e:
            kod, html = 0, ""
            print("hata", url, e)
        rows = nextdata_satirlar(html) or tablo_satirlar(html)
        rows = sorted({d: v for d, v in rows if aralik[0] < v < aralik[1]}.items())
        ozet = hashlib.sha256(html.encode("utf-8", "ignore")).hexdigest()[:16]
        son = rows[-1] if rows else (None, None)
        with open(os.path.join(DATA, "log.csv"), "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if f.tell() == 0:
                w.writerow(["zaman_utc", "adres", "http", "satir", "son_tarih", "son_deger", "sayfa_ozeti"])
            w.writerow([simdi.strftime("%Y-%m-%d %H:%M"), url, kod, len(rows), son[0], son[1], ozet])
        if html:
            with gzip.open(os.path.join(DATA, "raw", simdi.strftime("%Y%m%d_%H%M_") + url.split("//")[1][:2] + "_" + kap_dosya[:-4] + ".html.gz"), "wt", encoding="utf-8") as g:
                g.write(html)
        print(url, "HTTP", kod, "satır", len(rows), "son", son)
        if kod == 200 and rows:
            satirlar, kullanilan = rows, url
            break
        time.sleep(3)

    if not satirlar:
        print("VERİ ALINAMADI:", kap_dosya)
        return False

    # kesin kapanışlar: yalnız bugünden önceki günler
    kap = oku_csv(os.path.join(DATA, kap_dosya), "tarih")
    for d, v in satirlar:
        if d < bugun:
            kap[d.isoformat()] = {"tarih": d.isoformat(), "kapanis": f"{v:.{ondalik}f}"}
    with open(os.path.join(DATA, kap_dosya), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["tarih", "kapanis"])
        w.writeheader()
        for k in sorted(kap):
            w.writerow({"tarih": kap[k]["tarih"], "kapanis": kap[k]["kapanis"]})

    # bugünün gün içi değeri (kapanış değil)
    bugunku = [v for d, v in satirlar if d == bugun]
    if bugunku:
        yol = os.path.join(DATA, gi_dosya)
        yeni = not os.path.exists(yol)
        with open(yol, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if yeni:
                w.writerow(["zaman_utc", "tarih", "deger"])
            w.writerow([simdi.strftime("%Y-%m-%d %H:%M"), bugun.isoformat(), f"{bugunku[0]:.{ondalik}f}"])
    print("tamam:", kullanilan, "kapanış sayısı", len(kap))
    return True


def spx_aylik(simdi):
    """S&P 500 ay sonu kapanışları (yalnız tamamlanmış aylar)."""
    bu_ay = simdi.astimezone(TR).strftime("%Y-%m")
    aylar, kaynak, kod = {}, None, 0
    try:
        kod, txt = get("https://stooq.com/q/d/l/?s=%5Espx&i=m")
        for r in csv.DictReader(txt.splitlines()):
            if r.get("Date") and r.get("Close"):
                aylar[r["Date"][:7]] = float(r["Close"])
        if aylar: kaynak = "stooq"
    except Exception as e:
        print("stooq olmadı:", e)
    if not aylar:
        try:
            kod, txt = get("https://query1.finance.yahoo.com/v8/finance/chart/%5EGSPC?range=5y&interval=1mo")
            js = json.loads(txt)["chart"]["result"][0]
            for ts, c in zip(js["timestamp"], js["indicators"]["quote"][0]["close"]):
                if c: aylar[dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m")] = float(c)
            if aylar: kaynak = "yahoo"
        except Exception as e:
            print("yahoo olmadı:", e)
    son = max(aylar) if aylar else None
    with open(os.path.join(DATA, "log.csv"), "a", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow([simdi.strftime("%Y-%m-%d %H:%M"), "spx_aylik:" + str(kaynak), kod, len(aylar), son, aylar.get(son), ""])
    if not aylar:
        print("SPX VERİSİ ALINAMADI"); return False
    yol = os.path.join(DATA, "spx_aylik.csv")
    eski = oku_csv(yol, "ay")
    for a, v in aylar.items():
        if a < bu_ay:
            eski[a] = {"ay": a, "kapanis": f"{v:.2f}"}
    with open(yol, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["ay", "kapanis"]); w.writeheader()
        for k in sorted(eski)[-60:]:
            w.writerow(eski[k])
    print("spx tamam:", kaynak, len(eski))
    return True


def main():
    os.makedirs(os.path.join(DATA, "raw"), exist_ok=True)
    simdi = dt.datetime.now(dt.timezone.utc)
    bugun = simdi.astimezone(TR).date()
    sonuc = [topla(*v, simdi, bugun) for v in VARLIKLAR]
    spx_aylik(simdi)   # başarısızlığı iş akışını düşürmez
    # ham sayfa arşivini 30 dosyayla sınırla
    raws = sorted(os.listdir(os.path.join(DATA, "raw")))
    for x in raws[:-30]:
        os.remove(os.path.join(DATA, "raw", x))
    return 0 if all(sonuc) else 1


if __name__ == "__main__":
    sys.exit(main())
