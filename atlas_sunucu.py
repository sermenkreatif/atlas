# -*- coding: utf-8 -*-
"""
ATLAS - Sermen Kreatif Teknolojileri
Bilgisayari sesle ve yaziyla kullanan masaustu asistani.

Calistir:  python atlas_sunucu.py
Ac      :  http://localhost:8140
"""

import os
import io
import sys
import re
import ssl
import json
import time
import queue
import base64
import asyncio
import threading
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

import atlas_araclar as ARAC
import atlas_yerel as YEREL

KOK = os.path.dirname(os.path.abspath(__file__))
AYAR_YOLU = os.path.join(KOK, "atlas_ayar.json")
SISTEM_YOLU = os.path.join(KOK, "atlas_sistem.txt")
ARAYUZ_YOLU = os.path.join(KOK, "atlas_web.html")

MODEL = "claude-sonnet-5"                    # ana model
ALT_MODEL = "claude-haiku-4-5-20251001"      # alt gorevler ve basit isler

# milyon token basina dolar: (giris, cikis, onbellek_yazma, onbellek_okuma)
FIYAT = {
    "claude-sonnet-5":            (2.00, 10.00, 2.50, 0.20),
    "claude-sonnet-4-6":          (3.00, 15.00, 3.75, 0.30),
    "claude-haiku-4-5-20251001":  (1.00,  5.00, 1.25, 0.10),
    "claude-opus-5":              (5.00, 25.00, 6.25, 0.50),
}
SES_MODELI = "tr-TR-EmelNeural"


# ================================================================= ayarlar

def ayar_yukle():
    if os.path.exists(AYAR_YOLU):
        with open(AYAR_YOLU, "r", encoding="utf-8") as f:
            return json.load(f)

    ev = os.path.expanduser("~")
    varsayilan = {
        "port": 8140,
        "isim": "Atlas",
        "izinli_klasorler": [
            os.path.join(ev, "Desktop"),
            os.path.join(ev, "Masaüstü"),
            os.path.join(ev, "Documents"),
            os.path.join(ev, "Belgeler"),
            os.path.join(ev, "Downloads"),
            os.path.join(ev, "İndirilenler"),
            os.path.join(KOK, "atlas_calisma"),
        ],
        "cop_klasoru": os.path.join(KOK, "atlas_cop"),
        "gecici_klasor": os.path.join(KOK, "atlas_gecici"),
        "model": MODEL,
        "alt_model": ALT_MODEL,
        "mod": "hibrit",
        "tema": "acik",
        "tarayici_gizli": False,
        "tam_erisim": False,
        "salt_okunur_surucular": ["C:"],
        "yazma_istisnalari": [
            KOK,
            os.path.join(ev, "Desktop"), os.path.join(ev, "Masaüstü"),
            os.path.join(ev, "Documents"), os.path.join(ev, "Belgeler"),
            os.path.join(ev, "Downloads"), os.path.join(ev, "İndirilenler"),
            os.path.join(ev, "Pictures"), os.path.join(ev, "Resimler"),
            os.path.join(ev, "Videos"), os.path.join(ev, "Music"),
            os.path.join(ev, "OneDrive"),
        ],
        "uygulamalar": {
            "excel": "excel.exe",
            "word": "winword.exe",
            "powerpoint": "powerpnt.exe",
            "outlook": "outlook.exe",
            "chrome": "chrome.exe",
            "edge": "msedge.exe",
            "not_defteri": "notepad.exe",
            "hesap_makinesi": "calc.exe",
            "gezgin": "explorer.exe",
            "boya": "mspaint.exe",
        },
    }
    varsayilan["izinli_klasorler"] = [y for y in varsayilan["izinli_klasorler"] if os.path.isdir(y)] + \
                                     [os.path.join(KOK, "atlas_calisma")]
    with open(AYAR_YOLU, "w", encoding="utf-8") as f:
        json.dump(varsayilan, f, ensure_ascii=False, indent=2)
    return varsayilan


AYAR = ayar_yukle()
MODEL = AYAR.get("model", MODEL)
ALT_MODEL = AYAR.get("alt_model", ALT_MODEL)
for _k in ("cop_klasoru", "gecici_klasor"):
    os.makedirs(AYAR[_k], exist_ok=True)
os.makedirs(os.path.join(KOK, "atlas_calisma"), exist_ok=True)

ANAHTAR = os.environ.get("ANTHROPIC_API_KEY", "")
DEEPGRAM = os.environ.get("DEEPGRAM_API_KEY", "")
_env = os.path.join(KOK, "atlas.env")
if os.path.exists(_env):
    for satir in open(_env, "r", encoding="utf-8"):
        if "=" in satir and not satir.strip().startswith("#"):
            a, d = satir.strip().split("=", 1)
            os.environ.setdefault(a.strip(), d.strip())
    ANAHTAR = os.environ.get("ANTHROPIC_API_KEY", ANAHTAR)
    DEEPGRAM = os.environ.get("DEEPGRAM_API_KEY", DEEPGRAM)


# ================================================================= durum

DURUM = {
    "durum": "hazir",          # hazir | dusunuyor | calisiyor | konusuyor
    "gunluk": [],              # islem gunlugu
    "sohbet": [],              # {rol, metin}
    "ses": None,               # calinacak mp3 adresi
    "panel": None,             # {baslik, icerik}
    "sistem": {"cpu": 0, "ram": 0, "disk": 0},
    "tema": AYAR.get("tema", "acik"),
    "muzik": None,
    "sahne": "yok",
    "sustur": 0,
    "sessiz": False,
    "maliyet": {"bugun": 0, "istek": 0, "toplam": 0, "onbellek": 0},
    "mod": AYAR.get("mod", "hibrit"),
    "istemci": 1,
    "surum": 0,
}
KILIT = threading.Lock()
ISTEMCILER = {}          # kimlik -> [ilk_gorulme, son_gorulme]
ISTEMCI_OMRU = 6.0       # saniye
KUYRUK = queue.Queue()
GECMIS = []                    # Claude mesaj gecmisi


def _degisti():
    DURUM["surum"] += 1


# ---------------------------------------------------------------- kalici gunluk

KAYIT_KLASORU = os.path.join(KOK, "atlas_gunluk")
KAYIT_KILIDI = threading.Lock()


def kayit_yolu():
    os.makedirs(KAYIT_KLASORU, exist_ok=True)
    return os.path.join(KAYIT_KLASORU, "atlas-%s.log" % time.strftime("%Y-%m-%d"))


def eski_kayitlari_temizle(gun=14):
    try:
        sinir = time.time() - gun * 86400
        for ad in os.listdir(KAYIT_KLASORU):
            yol = os.path.join(KAYIT_KLASORU, ad)
            if ad.endswith(".log") and os.path.getmtime(yol) < sinir:
                os.remove(yol)
    except Exception:
        pass


def dosyaya_yaz(tur, metin):
    """Her satiri atlas_gunluk/atlas-YYYY-AA-GG.log dosyasina ekler."""
    try:
        satir = "%s  %-10s %s\n" % (time.strftime("%H:%M:%S"), tur,
                                     str(metin).replace("\n", " | ")[:600])
        with KAYIT_KILIDI:
            with open(kayit_yolu(), "a", encoding="utf-8") as f:
                f.write(satir)
    except Exception:
        pass


def istemci_gor(kimlik):
    """Aktif arayuzleri sayar; sesi yalnizca ilkine verir."""
    simdi = time.time()
    if kimlik:
        if kimlik in ISTEMCILER:
            ISTEMCILER[kimlik][1] = simdi
        else:
            ISTEMCILER[kimlik] = [simdi, simdi]
    for k in [k for k, t in ISTEMCILER.items() if simdi - t[1] > ISTEMCI_OMRU]:
        del ISTEMCILER[k]
    sayi = max(1, len(ISTEMCILER))
    if DURUM.get("istemci") != sayi:
        with KILIT:
            DURUM["istemci"] = sayi
            _degisti()
        if sayi > 1:
            gunluk_yaz("arayuz", "%d arayuz acik - ses sadece ilkinde calar" % sayi)
    sahip = min(ISTEMCILER, key=lambda k: ISTEMCILER[k][0]) if ISTEMCILER else None
    return kimlik == sahip or sahip is None


def gunluk_yaz(tur, metin):
    with KILIT:
        DURUM["gunluk"].append({"t": time.strftime("%H:%M:%S"), "tur": tur, "metin": metin})
        DURUM["gunluk"] = DURUM["gunluk"][-120:]
        _degisti()
    dosyaya_yaz(tur, metin)
    print("[%s] %s" % (tur, metin))


def sohbete_ekle(rol, metin):
    with KILIT:
        if DURUM["sohbet"] and DURUM["sohbet"][-1]["rol"] == rol \
                and DURUM["sohbet"][-1]["metin"].strip() == str(metin).strip():
            return
        DURUM["sohbet"].append({"rol": rol, "metin": metin})
        DURUM["sohbet"] = DURUM["sohbet"][-60:]
        _degisti()
    dosyaya_yaz("SEN" if rol == "kullanici" else "ATLAS", metin)


def durumu_ayarla(d):
    with KILIT:
        DURUM["durum"] = d
        _degisti()


def durum_yaz(anahtar, deger):
    with KILIT:
        DURUM[anahtar] = deger
        _degisti()


ARAC.ayarla(AYAR, gunluk_yaz, durum_yaz)
YEREL.bagla(ARAC)


# ================================================================= Claude

def sistem_sabit():
    """Her istekte ayni kalan bolum - onbellege alinir."""
    metin = ""
    if os.path.exists(SISTEM_YOLU):
        metin = open(SISTEM_YOLU, "r", encoding="utf-8").read()
    try:
        beceriler = ARAC.beceri_adlari()
    except Exception:
        beceriler = []
    if beceriler:
        metin += ("\n\nÖĞRENDİĞİN BECERİLER (beceri_calistir ile tek adımda çağır, "
                  "yeniden yazma):\n" + "\n".join("- %s: %s" % b for b in beceriler))
    else:
        metin += ("\n\nHenüz öğrenilmiş becerin yok. Toplu bir iş istendiğinde "
                  "önce beceri_yaz ile öğren, sonra beceri_calistir ile uygula.")
    metin += "\n\nKullanıcı klasörü: %s\nİzinli klasörler: %s" % (
        os.path.expanduser("~"), ", ".join(AYAR.get("izinli_klasorler", [])))
    return metin


def sistem_degisken():
    gunler = ["Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar"]
    simdi = time.localtime()
    return "ŞU AN: %s %s, saat %s." % (
        time.strftime("%d.%m.%Y", simdi), gunler[simdi.tm_wday], time.strftime("%H:%M", simdi))


def sistem_bloklari():
    """Sabit bolum onbellege isaretlenir; degisken bolum sonda, onbelleksiz."""
    return [
        {"type": "text", "text": sistem_sabit(),
         "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": sistem_degisken()},
    ]


def sistem_metni():
    metin = ""
    if os.path.exists(SISTEM_YOLU):
        metin = open(SISTEM_YOLU, "r", encoding="utf-8").read()
    gunler = ["Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar"]
    simdi = time.localtime()
    try:
        beceriler = ARAC.beceri_adlari()
    except Exception:
        beceriler = []
    if beceriler:
        metin += ("\n\nÖĞRENDİĞİN BECERİLER (beceri_calistir ile tek adımda çağır, "
                  "yeniden yazma):\n" + "\n".join("- %s: %s" % b for b in beceriler))
    else:
        metin += ("\n\nHenüz öğrenilmiş becerin yok. Toplu bir iş istendiğinde "
                  "önce beceri_yaz ile öğren, sonra beceri_calistir ile uygula.")
    metin += ("\n\nŞU AN: %s, saat %s. Kullanıcı: %s. "
              "İzinli klasörler: %s") % (
        time.strftime("%d.%m.%Y", simdi) + " " + gunler[simdi.tm_wday],
        time.strftime("%H:%M", simdi),
        os.path.expanduser("~"),
        ", ".join(AYAR["izinli_klasorler"]),
    )
    return metin


def claude(mesajlar, araclar=None, sistem=None, en_fazla=4000, model=None):
    model = model or MODEL
    govde = {
        "model": model,
        "max_tokens": en_fazla,
        "system": sistem if sistem is not None else sistem_bloklari(),
        "messages": mesajlar,
    }
    if araclar:
        govde["tools"] = araclar
    istek = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(govde).encode("utf-8"),
        headers={
            "content-type": "application/json",
            "x-api-key": ANAHTAR,
            "anthropic-version": "2023-06-01",
        },
    )
    with urllib.request.urlopen(istek, timeout=180) as y:
        yanit = json.loads(y.read().decode("utf-8"))
    k = yanit.get("usage", {})
    dosyaya_yaz("api", "%s | giris %s, cikis %s, onbellek yazma %s, okuma %s" % (
        model, k.get("input_tokens", 0), k.get("output_tokens", 0),
        k.get("cache_creation_input_tokens", 0), k.get("cache_read_input_tokens", 0)))
    maliyet_ekle(model, k)
    return yanit


# ---------------------------------------------------------------- maliyet

MALIYET_YOLU = os.path.join(KOK, "atlas_maliyet.json")


def maliyet_oku():
    if os.path.exists(MALIYET_YOLU):
        try:
            return json.load(open(MALIYET_YOLU, "r", encoding="utf-8"))
        except Exception:
            pass
    return {"toplam_dolar": 0.0, "gunler": {}}


def maliyet_ekle(model, kullanim):
    giris, cikis, yazma, okuma = FIYAT.get(model, FIYAT[MODEL])
    d = (kullanim.get("input_tokens", 0) * giris
         + kullanim.get("output_tokens", 0) * cikis
         + kullanim.get("cache_creation_input_tokens", 0) * yazma
         + kullanim.get("cache_read_input_tokens", 0) * okuma) / 1_000_000.0
    bugun = time.strftime("%Y-%m-%d")
    kayit = maliyet_oku()
    gun = kayit["gunler"].setdefault(bugun, {"dolar": 0.0, "istek": 0, "token": 0})
    gun["dolar"] += d
    gun["istek"] += 1
    gun["token"] += (kullanim.get("input_tokens", 0) + kullanim.get("output_tokens", 0)
                     + kullanim.get("cache_read_input_tokens", 0)
                     + kullanim.get("cache_creation_input_tokens", 0))
    kayit["toplam_dolar"] = kayit.get("toplam_dolar", 0.0) + d
    for eski in sorted(kayit["gunler"])[:-30]:
        del kayit["gunler"][eski]
    try:
        with open(MALIYET_YOLU, "w", encoding="utf-8") as f:
            json.dump(kayit, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
    with KILIT:
        DURUM["maliyet"] = {
            "bugun": round(gun["dolar"], 4),
            "istek": gun["istek"],
            "toplam": round(kayit["toplam_dolar"], 4),
            "onbellek": kullanim.get("cache_read_input_tokens", 0),
        }
        _degisti()


ARACLAR = ARAC.TANIMLAR + [
    {"type": "web_search_20250305", "name": "web_search"},
    {"name": "sahne_ayarla",
     "description": ("Ekranin atmosferini degistirir: hava durumu anlatirken butun ekran o havaya "
                     "burunur ve ortam sesi gelir. Hava durumu sorularinda MUTLAKA kullan. "
                     "Sahne kendiliginden kapanir, kapatmana gerek yok. "
                     "Sahneler: yagmur, kar, gunesli, bulutlu, firtina, sis, gece, yok."),
     "input_schema": {"type": "object", "properties": {
         "sahne": {"type": "string", "description": "yagmur / kar / gunesli / bulutlu / firtina / sis / gece / yok"},
         "saniye": {"type": "integer", "description": "Ekranda kalma suresi (varsayilan 25)"}},
         "required": ["sahne"]}},

    {"name": "alt_gorev",
     "description": ("Bagimsiz bir alt gorevi ayri bir ajana devreder. Uzun, cok adimli veya "
                     "birbirinden bagimsiz isleri parcalayip paralel dusunmek icin kullan. "
                     "Alt ajan ayni bilgisayar araclarina sahiptir, sonucu ozet olarak doner."),
     "input_schema": {"type": "object", "properties": {
         "gorev": {"type": "string", "description": "Alt ajana verilecek net, tek basina anlasilir gorev"}},
         "required": ["gorev"]}},
]
# son aracin uzerine onbellek isareti: arac tanimlari her istekte ayni,
# onbellekten okununca girdi maliyetinin onda birine dusuyor
ARACLAR[-1]["cache_control"] = {"type": "ephemeral"}


def _icerik_metni(blok_listesi):
    return "\n".join(b.get("text", "") for b in blok_listesi if b.get("type") == "text").strip()


def alt_gorev(gorev, derinlik=1):
    if derinlik > 2:
        return "Alt gorev derinligi asildi."
    gunluk_yaz("ajan", "alt gorev: " + gorev[:90])
    mesajlar = [{"role": "user", "content": gorev}]
    sistem = sistem_metni() + ("\n\nSen bir ALT AJANSIN. Sadece verilen gorevi yap ve sonunda "
                               "sonucu kisa, net bir ozetle bildir. Kullaniciyla konusmuyorsun.")
    araclar = [a for a in ARACLAR if a.get("name") not in ("alt_gorev", "sahne_ayarla")]
    for _ in range(12):
        yanit = claude(mesajlar, araclar, sistem, model=ALT_MODEL)
        mesajlar.append({"role": "assistant", "content": yanit["content"]})
        if yanit.get("stop_reason") != "tool_use":
            return _icerik_metni(yanit["content"]) or "Alt gorev bitti."
        sonuclar = []
        for blok in yanit["content"]:
            if blok.get("type") == "tool_use":
                cikti = ARAC.calistir(blok["name"], blok.get("input", {}))
                sonuclar.append({"type": "tool_result", "tool_use_id": blok["id"],
                                 "content": _sonuc_icerigi(cikti)})
        mesajlar.append({"role": "user", "content": sonuclar})
    return "Alt gorev adim sinirina takildi."


def _sonuc_icerigi(cikti):
    """Arac ciktisini Claude'un anlayacagi bicime cevirir (gorsel dahil)."""
    if isinstance(cikti, dict) and "gorsel_b64" in cikti:
        return [{"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                             "data": cikti["gorsel_b64"]}}]
    if isinstance(cikti, dict):
        return json.dumps(cikti, ensure_ascii=False)[:6000]
    return str(cikti)[:6000]


# ================================================================= ses

def seslendir(metin):
    """Edge TTS ile mp3 uretir, dosya adini doner."""
    try:
        import edge_tts
    except ImportError:
        gunluk_yaz("ses", "edge-tts kurulu degil (pip install edge-tts)")
        return None
    temiz = re.sub(r"\[[A-ZÇĞİÖŞÜ_]+(:[^\]]*)?\]", "", metin)
    temiz = re.sub(r"[*_`#>]|^\s*[-•]\s*", " ", temiz, flags=re.M)
    temiz = re.sub(r"https?://\S+", "bağlantı", temiz)
    temiz = re.sub(r"\s+", " ", temiz).strip()
    if not temiz:
        return None
    ad = "atlas_%d.mp3" % int(time.time() * 1000)
    yol = os.path.join(AYAR["gecici_klasor"], ad)

    async def _uret():
        await edge_tts.Communicate(temiz, SES_MODELI).save(yol)

    try:
        asyncio.run(_uret())
    except Exception as e:
        gunluk_yaz("ses", "seslendirme hatasi: %s" % e)
        return None
    return ad


def yaziya_cevir(ses_baytlari, tur="audio/webm"):
    if not DEEPGRAM:
        return ""
    url = "https://api.deepgram.com/v1/listen?model=nova-2&language=tr&smart_format=true&punctuate=true"
    istek = urllib.request.Request(url, data=ses_baytlari, headers={
        "Authorization": "Token " + DEEPGRAM, "Content-Type": tur})
    try:
        with urllib.request.urlopen(istek, timeout=60) as y:
            veri = json.loads(y.read().decode("utf-8"))
        return veri["results"]["channels"][0]["alternatives"][0]["transcript"].strip()
    except Exception as e:
        gunluk_yaz("ses", "STT hatasi: %s" % e)
        return ""


# ================================================================= sus / sessiz

SON_MESAJ = {"metin": "", "an": 0.0}


SUS_KELIMELERI = {
    "sus", "sus!", "sussana", "sus artik", "dur", "dur!", "durdur", "kes", "sesi kes",
    "sesini kes", "yeter", "yeter artik", "tamam yeter", "sustur", "sessiz", "bekle",
    "konusma", "konuşma", "yeter dur", "stop",
}
SESSIZ_AC = {"sessiz ol", "sessiz mod", "artik konusma", "artık konuşma",
             "sesini kapat", "sesli cevap verme", "sessize al"}
SESSIZ_KAPAT = {"konus", "konuş", "sesli konus", "sesli konuş", "sesini ac",
                "sesini aç", "sessiz modu kapat", "konusmaya devam et"}


def _sadeles(metin):
    return " ".join(str(metin).lower().replace("ı", "i").split()).strip(" .!,?")


def yerel_komut(metin):
    """Modele gitmeden burada karsilanacak komutlar. Karsilandiysa True doner."""
    k = _sadeles(metin)

    if k in {_sadeles(x) for x in SUS_KELIMELERI}:
        with KILIT:
            DURUM["ses"] = None
            DURUM["sustur"] += 1
            _degisti()
        if DURUM.get("muzik"):
            try:
                ARAC.muzik_dur()
            except Exception:
                pass
        durumu_ayarla("hazir")
        gunluk_yaz("ses", "susturuldu")
        return True

    if k in {_sadeles(x) for x in SESSIZ_AC}:
        with KILIT:
            DURUM["sessiz"] = True
            DURUM["ses"] = None
            DURUM["sustur"] += 1
            _degisti()
        durumu_ayarla("hazir")
        sohbete_ekle("atlas", "Sessiz moddayım, yazarak cevap veriyorum.")
        gunluk_yaz("ses", "sessiz mod acildi")
        return True

    if k in {_sadeles(x) for x in SESSIZ_KAPAT}:
        with KILIT:
            DURUM["sessiz"] = False
            _degisti()
        sohbete_ekle("atlas", "Tamam, tekrar sesli konuşuyorum.")
        gunluk_yaz("ses", "sessiz mod kapatildi")
        return True

    return False


# ================================================================= ajan dongusu

def _tool_result_var(mesaj):
    icerik = mesaj.get("content")
    if isinstance(icerik, list):
        return any(isinstance(b, dict) and b.get("type") == "tool_result" for b in icerik)
    return False


def _tool_use_var(mesaj):
    icerik = mesaj.get("content")
    if isinstance(icerik, list):
        return any((b.get("type") if isinstance(b, dict) else getattr(b, "type", None)) == "tool_use"
                   for b in icerik)
    return False


def gecmisi_kirp(gecmis, en_fazla=40):
    """Gecmisi kisaltir ama arac cagrisini sonucundan ayirmaz.
    Ayrilirsa API 'tool_use ids without tool_result' diye 400 doner."""
    if len(gecmis) <= en_fazla:
        return gecmis
    kes = len(gecmis) - en_fazla
    while kes < len(gecmis):
        m = gecmis[kes]
        if m.get("role") == "user" and not _tool_result_var(m):
            break
        kes += 1
    return gecmis[kes:] if kes < len(gecmis) else gecmis[-2:]


def gecmisi_sadelestir(gecmis, koru=8):
    """Eski arac ciktilarini kisaltir, gorselleri metne cevirir.
    Ayni icerik her istekte tekrar gonderildigi icin maliyetin buyuk kismi buradan geliyor."""
    if len(gecmis) <= koru:
        return gecmis
    for mesaj in gecmis[:-koru]:
        icerik = mesaj.get("content")
        if not isinstance(icerik, list):
            continue
        for blok in icerik:
            if not isinstance(blok, dict):
                continue
            if blok.get("type") == "tool_result":
                ic = blok.get("content")
                if isinstance(ic, list):
                    blok["content"] = "[görsel çıkarıldı]"
                elif isinstance(ic, str) and len(ic) > 400:
                    blok["content"] = ic[:400] + "\n... (eski çıktı kısaltıldı)"
    return gecmis


def gecmisi_toparla(gecmis):
    """Sonda cevapsiz kalmis arac cagrisi varsa atar (hata sonrasi olusur)."""
    while gecmis and gecmis[-1].get("role") == "assistant" and _tool_use_var(gecmis[-1]):
        gecmis.pop()
    return gecmis


def tekrarlari_ele(metin):
    """Bir cevabin icinde ayni cumle iki kez geciyorsa ikincisini atar."""
    parcalar = re.split(r"(?<=[.!?])\s+", str(metin).strip())
    gorulen, kalan = set(), []
    for p in parcalar:
        anahtar = re.sub(r"[^\wçğıöşü]+", "", p.lower())
        if len(anahtar) > 12 and anahtar in gorulen:
            continue
        gorulen.add(anahtar)
        kalan.append(p)
    return " ".join(kalan)


def son_atlas_cevabi():
    for m in reversed(DURUM["sohbet"]):
        if m["rol"] == "atlas":
            return m["metin"]
    return ""


def _panel_ayikla(metin):
    """[PANEL]baslik\nicerik[/PANEL] etiketini ayirir."""
    m = re.search(r"\[PANEL\](.*?)\[/PANEL\]", metin, re.S)
    if not m:
        return metin, None
    govde = m.group(1).strip()
    satirlar = govde.split("\n", 1)
    panel = {"baslik": satirlar[0].strip(),
             "icerik": satirlar[1].strip() if len(satirlar) > 1 else ""}
    return (metin[: m.start()] + metin[m.end():]).strip(), panel


SAHNE_SAYACI = {"t": None}


def sahne_ayarla(ad, saniye=None):
    """Ekran atmosferini ayarlar. saniye verilirse o sure sonunda kendiliginden kapanir."""
    ad = (ad or "yok").strip().lower()
    with KILIT:
        DURUM["sahne"] = ad
        _degisti()
    if SAHNE_SAYACI["t"]:
        try:
            SAHNE_SAYACI["t"].cancel()
        except Exception:
            pass
        SAHNE_SAYACI["t"] = None
    if ad != "yok" and saniye:
        def kapat():
            with KILIT:
                if DURUM.get("sahne") == ad:
                    DURUM["sahne"] = "yok"
                    _degisti()
        SAHNE_SAYACI["t"] = threading.Timer(float(saniye), kapat)
        SAHNE_SAYACI["t"].daemon = True
        SAHNE_SAYACI["t"].start()
    return ad


def konusmayi_kes(sebep=""):
    """Yeni girdi gelince calan sesi aninda durdurur."""
    with KILIT:
        DURUM["ses"] = None
        DURUM["sustur"] += 1
        _degisti()
    if sebep:
        gunluk_yaz("ses", "konusma kesildi (%s)" % sebep)


def eski_sesleri_sil(saklanacak=6):
    """Gecici klasordeki eski mp3 dosyalarini temizler."""
    try:
        klasor = AYAR["gecici_klasor"]
        dosyalar = sorted((os.path.join(klasor, a) for a in os.listdir(klasor)
                           if a.startswith("atlas_") and a.endswith(".mp3")),
                          key=os.path.getmtime, reverse=True)
        for eski in dosyalar[saklanacak:]:
            os.remove(eski)
    except Exception:
        pass


def yerel_cevapla(metin):
    """Yerel katmanin cevabini seslendirip sohbete yazar."""
    sohbete_ekle("atlas", metin)
    gunluk_yaz("yerel", "ücretsiz karşılandı")
    if DURUM.get("sessiz"):
        durumu_ayarla("hazir")
        return
    durumu_ayarla("konusuyor")
    ad = seslendir(metin)
    with KILIT:
        DURUM["ses"] = ("/ses/" + ad) if ad else None
        _degisti()
    durumu_ayarla("hazir")


def isle(kullanici_metni):
    global GECMIS
    konusmayi_kes("yeni mesaj")
    sohbete_ekle("kullanici", kullanici_metni)

    mod = AYAR.get("mod", "hibrit")
    if mod in ("hibrit", "yerel"):
        yerel = YEREL.coz(kullanici_metni)
        if yerel:
            yerel_cevapla(yerel)
            return
        if mod == "yerel":
            yerel_cevapla("Bunu ücretsiz modda çözemedim. API modunu açarsan yapabilirim.")
            return

    durumu_ayarla("dusunuyor")
    gecmisi_toparla(GECMIS)
    gecmisi_sadelestir(GECMIS)
    GECMIS.append({"role": "user", "content": kullanici_metni})
    GECMIS = gecmisi_kirp(GECMIS)

    cevap = ""
    try:
        for _ in range(12):
            yanit = claude(GECMIS, ARACLAR)
            GECMIS.append({"role": "assistant", "content": yanit["content"]})

            if yanit.get("stop_reason") != "tool_use":
                cevap = _icerik_metni(yanit["content"])
                break

            durumu_ayarla("calisiyor")
            ara_metin = _icerik_metni(yanit["content"])
            if ara_metin:
                gunluk_yaz("ara", ara_metin[:110])

            sonuclar = []
            for blok in yanit["content"]:
                if blok.get("type") != "tool_use":
                    continue
                ad, girdi = blok["name"], blok.get("input", {})
                gunluk_yaz("arac", "%s %s" % (ad, json.dumps(girdi, ensure_ascii=False)[:120]))
                if ad == "alt_gorev":
                    cikti = alt_gorev(girdi.get("gorev", ""))
                elif ad == "sahne_ayarla":
                    sec = sahne_ayarla(girdi.get("sahne"), girdi.get("saniye", 25))
                    cikti = "Sahne ayarlandi: %s" % sec
                else:
                    cikti = ARAC.calistir(ad, girdi)
                sonuclar.append({"type": "tool_result", "tool_use_id": blok["id"],
                                 "content": _sonuc_icerigi(cikti)})
            GECMIS.append({"role": "user", "content": sonuclar})
        else:
            cevap = "Bu iş için adım sınırına takıldım. Görevi biraz daha küçük parçaya böler misin?"
    except urllib.error.HTTPError as e:
        ham = e.read().decode("utf-8", "ignore")
        try:
            ayrinti = json.loads(ham).get("error", {}).get("message", ham)
        except Exception:
            ayrinti = ham
        d = ayrinti.lower()
        if "credit balance" in d or "insufficient" in d:
            ipucu = ("Anthropic hesabında API kredisi kalmamış. console.anthropic.com "
                     "→ Plans & Billing'den kredi yükle. Claude.ai aboneliği API'yi kapsamıyor.")
        elif "rate limit" in d:
            ipucu = "Dakikalık istek sınırına takıldık, biraz bekle."
        elif "too long" in d or "context" in d:
            ipucu = "Sohbet çok uzadı. Temizle ile sıfırla."
        else:
            ipucu = ""
        ipucu = ipucu or {
            400: "İstek geçersiz. Sohbeti Temizle ile sıfırlamayı dene.",
            401: "API anahtarı geçersiz. atlas.env dosyasındaki ANTHROPIC_API_KEY'i kontrol et.",
            403: "Bu anahtarın yetkisi yok.",
            429: "Kullanım sınırına takıldık, biraz bekle.",
            500: "Anthropic tarafında geçici arıza.",
            529: "Sunucular yoğun, birazdan tekrar dene.",
        }.get(e.code, "")
        gunluk_yaz("hata", "API %s: %s" % (e.code, ayrinti[:200]))
        gecmisi_toparla(GECMIS)
        cevap = "API hatası (%s). %s\n%s" % (e.code, ipucu, ayrinti[:200])
    except urllib.error.URLError as e:
        gunluk_yaz("hata", "baglanti: %s" % e)
        cevap = "İnternete ulaşamadım: %s" % e
    except Exception as e:
        import traceback
        gunluk_yaz("hata", traceback.format_exc()[-300:])
        gecmisi_toparla(GECMIS)
        cevap = "Bir hata oldu: %s" % e

    cevap, panel = _panel_ayikla(cevap)
    if panel:
        with KILIT:
            DURUM["panel"] = panel
            _degisti()
        if not cevap.strip():
            # tum cevap panele yazilmissa sohbet bos kalmasin
            cevap = "%s panelde." % panel["baslik"]
    if not cevap.strip():
        cevap = "Tamamlandı."
    cevap = tekrarlari_ele(cevap)
    ayni = cevap.strip() and cevap.strip() == son_atlas_cevabi().strip()
    if cevap:
        sohbete_ekle("atlas", cevap)
        if ayni:
            gunluk_yaz("tekrar", "ayni cevap tekrar uretildi, seslendirilmedi")
            durumu_ayarla("hazir")
            return
        if DURUM.get("sessiz"):
            durumu_ayarla("hazir")
            return
        durumu_ayarla("konusuyor")
        eski_sesleri_sil()
        ad = seslendir(cevap)
        with KILIT:
            DURUM["ses"] = ("/ses/" + ad) if ad else None
            _degisti()
    durumu_ayarla("hazir")


def hatirlatma_bekcisi():
    """Zamani gelen hatirlatmalari sesli soyler ve panele yazar."""
    import datetime
    GUN_ADI = ["Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar"]
    while True:
        try:
            liste = ARAC.hatirlatmalari_oku()
            simdi = datetime.datetime.now()
            bugun = simdi.strftime("%Y-%m-%d")
            saat = simdi.strftime("%H:%M")
            degisti = False
            for k in liste:
                if k.get("saat") != saat:
                    if k.get("soylendi") and k.get("tekrar") != "bir_kez":
                        k["soylendi"] = False
                        degisti = True
                    continue
                if k.get("soylendi"):
                    continue
                tekrar = k.get("tekrar", "bir_kez")
                uygun = (
                    (tekrar == "bir_kez" and k.get("tarih") == bugun) or
                    tekrar == "gunluk" or
                    (tekrar == "hafta_ici" and simdi.weekday() < 5) or
                    (tekrar == "haftalik" and simdi.weekday() == int(k.get("gun") or 0))
                )
                if not uygun:
                    continue
                metin = "Hatırlatma: " + k["metin"]
                gunluk_yaz("hatirlatma", "calisti #%s %s" % (k.get("no"), k["metin"][:50]))
                sohbete_ekle("atlas", metin)
                with KILIT:
                    DURUM["panel"] = {"baslik": "Hatırlatma",
                                      "icerik": "%s  %s\n\n%s" % (GUN_ADI[simdi.weekday()], saat, k["metin"])}
                    _degisti()
                if not DURUM.get("sessiz"):
                    durumu_ayarla("konusuyor")
                    ad = seslendir(metin)
                else:
                    ad = None
                with KILIT:
                    DURUM["ses"] = ("/ses/" + ad) if ad else None
                    _degisti()
                durumu_ayarla("hazir")
                k["soylendi"] = True
                degisti = True
            if tekrar_temizle(liste):
                degisti = True
            if degisti:
                ARAC.hatirlatmalari_yaz(liste)
        except Exception as e:
            gunluk_yaz("hata", "hatirlatma: %s" % e)
        time.sleep(20)


def tekrar_temizle(liste):
    """Gecmis tek seferlik hatirlatmalari listeden atar."""
    import datetime
    bugun = datetime.datetime.now().strftime("%Y-%m-%d")
    once = len(liste)
    liste[:] = [k for k in liste
                if not (k.get("tekrar", "bir_kez") == "bir_kez" and k.get("soylendi")
                        and (k.get("tarih") or bugun) <= bugun)]
    return len(liste) != once


def sistem_olc():
    """CPU/RAM/disk yuzdelerini arayuz icin gunceller."""
    try:
        import psutil
    except ImportError:
        return
    while True:
        try:
            with KILIT:
                DURUM["sistem"] = {
                    "cpu": round(psutil.cpu_percent(interval=None)),
                    "ram": round(psutil.virtual_memory().percent),
                    "disk": round(psutil.disk_usage(os.path.splitdrive(KOK)[0] or "/").percent),
                }
                _degisti()
        except Exception:
            pass
        time.sleep(3)


def acilis_selami():
    """Sunucu hazir olunca bir kez selam verir."""
    time.sleep(2.5)
    try:
        metin = "Merhaba, ben Atlas. Sana nasıl yardımcı olabilirim?"
        sohbete_ekle("atlas", metin)
        if DURUM.get("sessiz"):
            return
        durumu_ayarla("konusuyor")
        ad = seslendir(metin)
        with KILIT:
            DURUM["ses"] = ("/ses/" + ad) if ad else None
            _degisti()
        durumu_ayarla("hazir")
    except Exception as e:
        gunluk_yaz("hata", "selam: %s" % e)


def isci():
    while True:
        metin = KUYRUK.get()
        try:
            isle(metin)
        except Exception as e:
            gunluk_yaz("hata", str(e))
            durumu_ayarla("hazir")


# ================================================================= guncelleme

DEPO = "https://raw.githubusercontent.com/sermenkreatif/atlas/main/"
GUNCELLENECEK = [
    "atlas_sunucu.py", "atlas_araclar.py", "atlas_web.html",
    "atlas_sistem.txt", "ATLAS.pyw", "atlas_logo.png",
    "atlas_logo.ico", "surum.txt",
]


def yerel_surum():
    yol = os.path.join(KOK, "surum.txt")
    if os.path.exists(yol):
        return open(yol, "r", encoding="utf-8").read().strip()
    return "?"


def _indir(ad):
    istek = urllib.request.Request(DEPO + ad, headers={"Cache-Control": "no-cache"})
    with urllib.request.urlopen(istek, timeout=45) as y:
        return y.read()


def uzak_surum():
    try:
        return _indir("surum.txt").decode("utf-8").strip()
    except Exception as e:
        return "hata: %s" % e


def guncelle():
    """Depodan dosyalari ceker, eskilerini yedekler. (ad, durum) listesi doner."""
    yedek = os.path.join(KOK, "atlas_yedek", time.strftime("%Y%m%d_%H%M%S"))
    os.makedirs(yedek, exist_ok=True)
    sonuc = []
    for ad in GUNCELLENECEK:
        try:
            veri = _indir(ad)
        except Exception as e:
            sonuc.append((ad, "atlandi (%s)" % str(e)[:40]))
            continue
        hedef = os.path.join(KOK, ad)
        if os.path.exists(hedef):
            if open(hedef, "rb").read() == veri:
                sonuc.append((ad, "zaten guncel"))
                continue
            shutil_kopya(hedef, os.path.join(yedek, ad))
        with open(hedef, "wb") as f:
            f.write(veri)
        sonuc.append((ad, "guncellendi (%d bayt)" % len(veri)))
    gunluk_yaz("guncelleme", "; ".join("%s: %s" % x for x in sonuc))
    return sonuc, yedek


def shutil_kopya(kaynak, hedef):
    import shutil
    os.makedirs(os.path.dirname(hedef), exist_ok=True)
    shutil.copy2(kaynak, hedef)


def yeniden_baslat():
    """ATLAS'i kapatip yeniden acar."""
    import subprocess
    kabuk = os.path.join(KOK, "ATLAS.pyw")
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.exists(pyw):
        pyw = sys.executable
    if os.path.exists(kabuk):
        subprocess.Popen([pyw, kabuk], cwd=KOK)
    threading.Timer(1.0, lambda: os._exit(0)).start()


# ================================================================= HTTP

class Sunucu(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _gonder(self, kod, tur, govde):
        if isinstance(govde, str):
            govde = govde.encode("utf-8")
        self.send_response(kod)
        self.send_header("Content-Type", tur)
        self.send_header("Content-Length", str(len(govde)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(govde)

    def _json(self, veri, kod=200):
        self._gonder(kod, "application/json; charset=utf-8",
                     json.dumps(veri, ensure_ascii=False))

    def do_GET(self):
        yol = self.path.split("?")[0]
        if yol in ("/", "/index.html"):
            if not os.path.exists(ARAYUZ_YOLU):
                return self._gonder(404, "text/plain; charset=utf-8", "atlas_web.html bulunamadı.")
            with open(ARAYUZ_YOLU, "rb") as f:
                return self._gonder(200, "text/html; charset=utf-8", f.read())
        if yol == "/durum":
            kimlik = self.headers.get("X-Atlas-Istemci", "")
            sahip = istemci_gor(kimlik)
            with KILIT:
                veri = dict(DURUM)
            if not sahip:
                veri["ses"] = None          # ikinci pencere ayni sesi calmasin
            return self._json(veri)
        if yol == "/surum":
            return self._json({"yerel": yerel_surum(), "uzak": uzak_surum()})
        if yol == "/ayar":
            return self._json({
                "kok": KOK,
                "port": AYAR.get("port", 8140),
                "model": MODEL,
                "anahtar": bool(ANAHTAR),
                "deepgram": bool(DEEPGRAM),
                "izinli": AYAR.get("izinli_klasorler", []),
                "tam_erisim": AYAR.get("tam_erisim", False),
                "uygulamalar": sorted(AYAR.get("uygulamalar", {}).keys()),
            })
        if yol == "/favicon.ico":
            dosya = os.path.join(KOK, "atlas_logo.ico")
            if os.path.exists(dosya):
                with open(dosya, "rb") as f:
                    return self._gonder(200, "image/x-icon", f.read())
            return self._gonder(404, "text/plain", "ikon yok")
        if yol == "/logo.png":
            dosya = os.path.join(KOK, "atlas_logo.png")
            if os.path.exists(dosya):
                with open(dosya, "rb") as f:
                    return self._gonder(200, "image/png", f.read())
            return self._gonder(404, "text/plain", "logo yok")
        if yol.startswith("/ses/"):
            dosya = os.path.join(AYAR["gecici_klasor"], os.path.basename(yol))
            with KILIT:
                if DURUM.get("ses") == yol:
                    DURUM["ses"] = None     # indirildi, ikinci kez dagitilmasin
                    _degisti()
            if os.path.exists(dosya):
                with open(dosya, "rb") as f:
                    return self._gonder(200, "audio/mpeg", f.read())
            return self._gonder(404, "text/plain", "yok")
        return self._gonder(404, "text/plain", "yok")

    def do_POST(self):
        yol = self.path.split("?")[0]
        uzunluk = int(self.headers.get("Content-Length", 0))
        govde = self.rfile.read(uzunluk) if uzunluk else b""

        if yol == "/mesaj":
            veri = json.loads(govde.decode("utf-8") or "{}")
            metin = (veri.get("metin") or "").strip()
            if metin:
                simdi = time.time()
                if (metin == SON_MESAJ["metin"] and simdi - SON_MESAJ["an"] < 2.0):
                    gunluk_yaz("arayuz", "ayni mesaj iki kez geldi, ikincisi yok sayildi")
                    return self._json({"ok": True, "yinelenen": True})
                SON_MESAJ["metin"], SON_MESAJ["an"] = metin, simdi
                konusmayi_kes("yeni mesaj")
                if yerel_komut(metin):
                    return self._json({"ok": True, "yerel": True})
                KUYRUK.put(metin)
            return self._json({"ok": True})

        if yol == "/ses":
            tur = self.headers.get("X-Ses-Turu", "audio/webm")
            konusmayi_kes("sesli girdi")
            metin = yaziya_cevir(govde, tur)
            if metin and not yerel_komut(metin):
                KUYRUK.put(metin)
            return self._json({"metin": metin})

        if yol == "/sus":
            with KILIT:
                DURUM["ses"] = None
                DURUM["sustur"] += 1
                _degisti()
            if DURUM.get("muzik"):
                try:
                    ARAC.muzik_dur()
                except Exception:
                    pass
            durumu_ayarla("hazir")
            return self._json({"ok": True})

        if yol == "/gunluk_ac":
            try:
                os.makedirs(KAYIT_KLASORU, exist_ok=True)
                os.startfile(KAYIT_KLASORU)      # noqa - Windows
            except AttributeError:
                pass
            except Exception as e:
                return self._json({"ok": False, "mesaj": str(e)})
            return self._json({"ok": True, "klasor": KAYIT_KLASORU})

        if yol == "/tema":
            veri = json.loads(govde.decode("utf-8") or "{}")
            yeni = veri.get("tema")
            if yeni not in ("acik", "koyu"):
                yeni = "koyu" if AYAR.get("tema", "acik") == "acik" else "acik"
            AYAR["tema"] = yeni
            with open(AYAR_YOLU, "w", encoding="utf-8") as f:
                json.dump(AYAR, f, ensure_ascii=False, indent=2)
            with KILIT:
                DURUM["tema"] = yeni
                _degisti()
            return self._json({"ok": True, "tema": yeni})

        if yol == "/mod":
            veri = json.loads(govde.decode("utf-8") or "{}")
            yeni = veri.get("mod")
            if yeni not in ("hibrit", "yerel", "api"):
                sira = ["hibrit", "yerel", "api"]
                yeni = sira[(sira.index(AYAR.get("mod", "hibrit")) + 1) % 3]
            AYAR["mod"] = yeni
            with open(AYAR_YOLU, "w", encoding="utf-8") as f:
                json.dump(AYAR, f, ensure_ascii=False, indent=2)
            with KILIT:
                DURUM["mod"] = yeni
                _degisti()
            gunluk_yaz("ayar", "mod: " + yeni)
            return self._json({"ok": True, "mod": yeni})

        if yol == "/sessiz":
            veri = json.loads(govde.decode("utf-8") or "{}")
            with KILIT:
                DURUM["sessiz"] = bool(veri.get("ac", not DURUM.get("sessiz")))
                if DURUM["sessiz"]:
                    DURUM["ses"] = None
                    DURUM["sustur"] += 1
                _degisti()
            return self._json({"ok": True, "sessiz": DURUM["sessiz"]})

        if yol == "/panel_kapat":
            with KILIT:
                DURUM["panel"] = None
                _degisti()
            return self._json({"ok": True})

        if yol == "/ses_alindi":
            with KILIT:
                DURUM["ses"] = None
                _degisti()
            return self._json({"ok": True})

        if yol == "/guncelle":
            veri = json.loads(govde.decode("utf-8") or "{}")
            sonuc, yedek = guncelle()
            metin = "\n".join("%-18s %s" % (a, d) for a, d in sonuc)
            metin += "\n\nEski dosyalar: " + yedek
            degisti = any("guncellendi" in d for _, d in sonuc)
            metin += "\n\n" + ("Yeniden baslatiliyor..." if (degisti and veri.get("yeniden", True))
                                 else "Degisiklik yok.")
            with KILIT:
                DURUM["panel"] = {"baslik": "Guncelleme", "icerik": metin}
                _degisti()
            self._json({"ok": True, "degisti": degisti})
            if degisti and veri.get("yeniden", True):
                threading.Timer(2.5, yeniden_baslat).start()
            return

        if yol == "/izin_ekle":
            veri = json.loads(govde.decode("utf-8") or "{}")
            hedef = (veri.get("yol") or "").strip().strip('"').replace("\\", "/")
            if not hedef:
                return self._json({"ok": False, "mesaj": "Klasör yolu boş."})
            hedef = os.path.abspath(os.path.expanduser(hedef))
            if not os.path.isdir(hedef):
                return self._json({"ok": False, "mesaj": "Böyle bir klasör yok: " + hedef})
            liste = AYAR.setdefault("izinli_klasorler", [])
            if any(os.path.abspath(k) == hedef for k in liste):
                return self._json({"ok": True, "mesaj": "Zaten izinli: " + hedef})
            liste.append(hedef)
            with open(AYAR_YOLU, "w", encoding="utf-8") as f:
                json.dump(AYAR, f, ensure_ascii=False, indent=2)
            gunluk_yaz("ayar", "izin verildi: " + hedef)
            return self._json({"ok": True, "mesaj": "İzin verildi: " + hedef})

        if yol == "/izin_sil":
            veri = json.loads(govde.decode("utf-8") or "{}")
            hedef = (veri.get("yol") or "").strip()
            liste = AYAR.get("izinli_klasorler", [])
            kalan = [k for k in liste if k != hedef]
            if len(kalan) == len(liste):
                return self._json({"ok": False, "mesaj": "Listede yok."})
            AYAR["izinli_klasorler"] = kalan
            with open(AYAR_YOLU, "w", encoding="utf-8") as f:
                json.dump(AYAR, f, ensure_ascii=False, indent=2)
            gunluk_yaz("ayar", "izin kaldirildi: " + hedef)
            return self._json({"ok": True, "mesaj": "Kaldırıldı: " + hedef})

        if yol == "/kapat":
            self._json({"ok": True})
            threading.Timer(0.4, lambda: os._exit(0)).start()
            return

        if yol == "/sifirla":
            global GECMIS
            GECMIS = []
            with KILIT:
                DURUM["sohbet"] = []
                DURUM["panel"] = None
                _degisti()
            return self._json({"ok": True})

        return self._json({"hata": "bilinmeyen"}, 404)


def gunluge_baslik():
    eski_kayitlari_temizle()
    dosyaya_yaz("baslangic", "ATLAS %s | model %s | mod %s | klasor %s" % (
        (open(os.path.join(KOK, "surum.txt"), encoding="utf-8").read().strip()
         if os.path.exists(os.path.join(KOK, "surum.txt")) else "?"),
        MODEL, AYAR.get("mod", "hibrit"), KOK))


def baslat_arka():
    """Sunucuyu arka planda baslatir; masaustu kabugu (ATLAS.pyw) bunu cagirir."""
    gunluge_baslik()
    threading.Thread(target=isci, daemon=True).start()
    threading.Thread(target=sistem_olc, daemon=True).start()
    threading.Thread(target=hatirlatma_bekcisi, daemon=True).start()
    threading.Thread(target=acilis_selami, daemon=True).start()
    port = AYAR.get("port", 8140)
    sunucu = ThreadingHTTPServer(("127.0.0.1", port), Sunucu)
    threading.Thread(target=sunucu.serve_forever, daemon=True).start()
    return port


def main():
    if not ANAHTAR:
        print("UYARI: ANTHROPIC_API_KEY yok. atlas.env dosyasına ekle.")
    if not DEEPGRAM:
        print("UYARI: DEEPGRAM_API_KEY yok — sesli komut çalışmaz, yazı ile çalışır.")
    gunluge_baslik()
    threading.Thread(target=isci, daemon=True).start()
    threading.Thread(target=sistem_olc, daemon=True).start()
    threading.Thread(target=hatirlatma_bekcisi, daemon=True).start()
    threading.Thread(target=acilis_selami, daemon=True).start()
    port = AYAR.get("port", 8140)
    print("ATLAS hazır  →  http://localhost:%d" % port)
    ThreadingHTTPServer(("127.0.0.1", port), Sunucu).serve_forever()


if __name__ == "__main__":
    main()
