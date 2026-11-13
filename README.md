# mem — Yerel İkinci Beyin (MCP) Motoru

Yapay zekâ kodlama ajanları ve masaüstü asistanları için **sıfır bağımlılıklı**, **tamamen çevrimdışı (yerel)** ve **Obsidian uyumlu** mühendislik hafızası motoru.

Projelerinizde alınan mimari kararları (ADR), kalıcı kuralları, açık konuları ve oturum devir özetlerini düz Markdown dosyaları olarak saklar; **Model Context Protocol (MCP)** üzerinden kullandığınız tüm yapay zekâ araçlarının aynı hafızayı paylaşmasını sağlar.

---

## Öne Çıkan Özellikler

- **Sıfır Harici Bağımlılık (`pip install` gerekmez):** Yalnızca Python 3.11+ standart kütüphanesi (`sqlite3`, `json`, `pathlib`) ile çalışır. Doğrudan `python mem.py` olarak çalıştırılabilir.
- **%100 Yerel ve Sıfır Ağ Erişimi:** Hiçbir ağ modülü (`http`, `urllib`, `socket`, `ssl`) içermez; dış API, telemetri veya bulut servisi kullanmaz (birim testleriyle kilitlidir).
- **Çoklu Ajan ve Masaüstü Desteği:** Tek komutla **Claude Code**, **Claude Desktop**, **ChatGPT / Codex**, **Google Antigravity** ve **Devin Desktop** ile bağlanır.
- **Obsidian Doğal Uyumu:** Notlar standart YAML frontmatter ve `[[WikiLink]]` bağlantılarıyla yazılır. Kasa doğrudan Obsidian'da açıldığında projeler kendi isimleriyle merkez düğüm olur, kararlar ve kurallar renk gruplarıyla otomatik görselleşir.
- **Türkçe Kök/Ek Duyarlı Hızlı Arama:** SQLite WAL + FTS5 BM25 tam metin araması; Türkçe çekim eklerini köklerken (`bağlantılarından` → `bağlant`) teknik terimleri (`PostgreSQL`, `OAuth2`, `mcp_config`) bozmaz. `[[WikiLink]]` komşuluk grafı ve karar/kural önceliğiyle sıralar.
- **Beyaz Liste ve DLP Kalkanı:** Yalnızca kaydettiğiniz proje klasörlerinde (`projects.json`) çalışır; kayıtsız dizinlerde hiçbir okuma/yazma yapmaz (`unregistered_workspace`). Dahili sır/anahtar/kişisel veri tarayıcısı (`redact` veya `block` modu) hassas verilerin kasaya sızmasını önler.
- **Çok Ajanlı Eşzamanlılık Güvenliği:** Aynı anda birden fazla ajan çalışırken işletim sistemi dosya kilidi (`msvcrt` / `fcntl`), atomik yazma (`fsync` + `os.replace`) ve `sha256` iyimser kilitleme ile veri ezilmesini engeller.

---

## Gereksinimler

- **Python 3.11 veya üzeri** (SQLite FTS5 desteği standart kurulumla birlikte gelir)
- Harici Python paketi gerekmez.

---

## Hızlı Başlangıç

### 1. Repoyu İndirin

```bash
git clone https://github.com/tastutanemre/Memory.git
cd Memory
```

### 2. Hafıza Kasasını (Vault) Oluşturun

**Windows (PowerShell):**
```powershell
python mem.py init --profile personal --vault C:\Notlar\Hafiza
```

**macOS / Linux:**
```bash
python3 mem.py init --profile personal --vault ~/Notes/MemoryVault
```

> Not: `--vault` vermezseniz kasa otomatik olarak kullanıcı veri dizininiz altında oluşturulur. İndeks veritabanı (`index.sqlite3`) ve kilit dosyaları her zaman kasa klasörünün **dışında** tutulur; böylece Obsidian veya bulut senkronizasyonuyla çakışmaz.

### 3. Projelerinizi Kaydedin

Hafızanın devreye girmesini istediğiniz yazılım projelerini beyaz listeye ekleyin:

```bash
python mem.py project add backend-api --root /yol/backend-api --name "Backend API"
```

### 4. Kullandığınız Yapay Zekâ Araçlarına Bağlayın

```bash
# Claude Code (CLI / IDE eklentisi)
python mem.py connect claude --profile personal

# Claude Desktop (Masaüstü sohbet uygulaması)
python mem.py connect claude-desktop --profile personal

# OpenAI Codex / ChatGPT Desktop (Codex modülü)
python mem.py connect codex --profile personal

# Google Antigravity (IDE & v2)
python mem.py connect antigravity --profile personal

# Devin Desktop
python mem.py connect devin --profile personal
```

Ne yazılacağını önceden görmek için `--dry-run`, bağlantıyı tamamen kaldırmak için `--uninstall` bayrağını kullanabilirsiniz (mevcut diğer MCP sunucularınıza ve ayarlarınıza dokunmaz; ilk değişiklikten önce `.bak` yedeği alır):

```bash
python mem.py connect claude --profile personal --dry-run
python mem.py connect claude --uninstall
```

### 5. Kurulumu Doğrulayın

```bash
python mem.py doctor --profile personal
```

---

## Desteklenen Araçlar ve Yapılandırma Dosyaları

`mem.py connect <hedef>` komutu herhangi bir gizli kabuk betiği veya arka plan servisi kurmaz; yalnızca ilgili aracın standart MCP ve kural dosyasına şeffaf bir kayıt ekler:

| Hedef (`harness`) | MCP Yapılandırması | Global Kural Bloğu | Not |
|---|---|---|---|
| `claude` | `~/.claude.json` | `~/.claude/CLAUDE.md` | Claude Code CLI ve IDE eklentisi |
| `claude-desktop` | `claude_desktop_config.json` *(AppData / MSIX / macOS Application Support)* | — | Masaüstü sohbet için `--vault-scope` ile bağlanır |
| `codex` | `~/.codex/config.toml` | `~/.codex/AGENTS.md` | Codex CLI ve ChatGPT/Codex Desktop |
| `antigravity` | `~/.gemini/config/mcp_config.json` | `~/.gemini/config/GEMINI.md` | Antigravity IDE ve Antigravity 2.0 |
| `devin` | `%APPDATA%\devin\mcp_config.json` *(POSIX: `~/.config/devin/mcp_config.json`)* | `.../devin/AGENTS.md` | Devin Desktop |

İsteğe bağlı olarak `skills/memory/SKILL.md` dosyasını kullandığınız aracın `skills/memory/SKILL.md` dizinine kopyalayarak ajan becerisi (skill) olarak da tanıtabilirsiniz.

---

## MCP Araçları ve Prompt'lar

Bağlantı kurulduğunda yapay zekâ ajanınız şu MCP araçlarını doğrudan kullanır:

| MCP Aracı | Görevi |
|---|---|
| `memory_bootstrap` | Oturum başında çağrılır; `Core.md`, genel/proje kuralları, açık konular ve son oturum devir kartını getirir. |
| `memory_search` | Türkçe kök/ek duyarlı FTS5 BM25 + `[[WikiLink]]` komşuluk grafı ile hafızada arama yapar. |
| `memory_read` | Belirtilen notu ve iyimser kilitleme için güncel `sha256` özetini okur. |
| `memory_decide` | Mimari/ürün kararlarını standart ADR formatında kaydeder; mükerrer kararları uyarır, `supersedes` ile eski kararları geçersiz kılar. |
| `memory_rule` | Kullanıcı düzeltmelerinden çıkan kalıcı kuralları (`global` veya proje kapsamlı) ekler. |
| `memory_patch` | Mevcut bir notu `expected_sha256` doğrulamasıyla güvenli biçimde günceller. |
| `memory_finish` | Oturum sonunda iş makbuzu oluşturur, `Son-Oturum.md` devir kartını ve `Acik-Konular.md` listesini günceller. |

**Hazır MCP Prompt'ları:** `start` (oturumu bağlamla başlat), `decide` (kararı ADR olarak kaydet), `wrapup` (oturum devir özeti yaz).

---

## Komut Satırı (CLI) Kullanımı

MCP dışında doğrudan terminalden de tüm işlemler yapılabilir (`--json` bayrağı makine okunur çıktı üretir):

```bash
python mem.py bootstrap --task "kimlik doğrulama"
python mem.py decide --title "Önbellek katmanı" --decision "Okuma yoğun uçlarda Redis kullanılacak" --rationale "Düşük gecikme"
python mem.py rule "Her veritabanı şema değişikliği geri alınabilir migrasyon içermeli" --reason "Canlı güvenlik"
python mem.py search "önbellek kararları"
python mem.py read projects/backend-api/Backend-API.md
python mem.py finish --summary "OAuth2 akışı tamamlandı" --thread "Yenileme token rotasyonu eklenecek"
python mem.py sync
python mem.py profiles
python mem.py doctor
```

**Çıkış Kodları:** `0` başarılı · `1` doctor hatası · `2` kullanım/doğrulama hatası · `3` DLP engeli · `4` `sha256` çakışması · `5` kayıtsız çalışma alanı.

---

## Profiller ve Hassas Veri Kalkanı (DLP)

Aynı bilgisayarda birden fazla bağımsız kasa tutabilirsiniz (ör. `--profile personal` ve `--profile work`).

- **`redact` modu (`personal` ve genel profillerde varsayılan):** API anahtarı, özel anahtar (`PEM`/`PGP`), JWT, veritabanı bağlantı dizgesi (`URI` / `ADO.NET`), `.env` parola atamaları, IBAN ve TCKN yakalandığında otomatik olarak `[REDACTED:<kategori>]` ile maskelenir.
- **`block` modu (`work` profilinde varsayılan veya `--dlp block` ile):** Hassas veri tespit edildiğinde yazma işlemi tamamen reddedilir; hata mesajında gizli değerin kendisi asla açık yazılmaz.
- **Özel Kurallar (`dlp-patterns.txt`):** Kasa kökündeki `dlp-patterns.txt` dosyasına kendi alan adlarınızı, iç IP bloklarınızı veya `regex:` önekli desenlerinizi ekleyebilirsiniz.

---

## Opsiyonel: Gemini Decisions Danışmanı (yalnız `personal`)

Varsayılan olarak `mem` **sıfır ağ** ile çalışır. İsterseniz, **yalnızca `work` olmayan ve `dlp_mode != block` olan** profillerde
(ör. `personal`) Google'ın Gemini Decisions API'sini (`gemini-decision-1`, `generativelanguage.googleapis.com/v1beta/decisions`)
kullanan iki opsiyonel yan yetenek açabilirsiniz. Ağ kodu yalnızca `src/mem_advisor/` paketindedir; `src/mem/` çekirdeği ağ modülü içermez.

- **Anlamsal yeniden sıralama:** FTS5 adayları (ve hiç kelime örtüşmesi yokken en güncel 24 not) anlam açısından puanlanır;
  ör. `önbellek ne zaman bayatlar` sorgusu `Redis TTL 300 saniye` notunu bulur. Anahtar yoksa veya hata olursa yerel sonuçlar aynen döner.
- **Karar danışmanı (`decide`):** Yeni karar taslağı, aktif önceki kararlarla çelişiyor/mükerrer ya da henüz kesinleşmemişse
  `advisor_warning` döner ve not yazılmaz (`supersedes` verin veya `force=true` ile zorlayın).

```bash
python mem.py init --profile personal --advisor gemini        # veya: python mem.py advisor enable --profile personal
printf '%s' "$GEMINI_API_KEY" | python mem.py advisor set-key --stdin   # anahtar argv'ye yazılmaz
python mem.py advisor status                                  # anahtarın kendisi asla gösterilmez
python mem.py search --advisor gemini "önbellek ne zaman bayatlar"
python mem.py connect antigravity --profile personal --advisor gemini    # MCP'ye --advisor gemini ekler
```

Güvenlik sınırları:

- `work` profilinde veya `block` DLP kasalarında `--advisor gemini` açıkça **reddedilir**; `MEM_ADVISOR=gemini` ortam değişkeni de bu kasalarda yok sayılır.
- Dışarı çıkan her metin önce `dlp.scan` ile taranır; bulgu içeren sorgu/not/karar **asla gönderilmez**. Yalnızca başlık, kısa pasaj ve karar alanları gider.
- Yalnızca `https://generativelanguage.googleapis.com/v1beta/…` adresine izin verilir (yönlendirme izlenmez). Zaman aşımı/hata durumunda sessizce yerel davranışa dönülür.
- API anahtarı sırayla `MEM_GEMINI_API_KEY` → `GEMINI_API_KEY` → `GOOGLE_API_KEY` → kasa **dışındaki** `MEM_HOME/advisor.json` kaynağından okunur; loglanmaz.
- Yanıtlar `state_dir/advisor_cache.sqlite3` içinde (7 gün, en fazla 2000 kayıt; yalnızca özet anahtar + puan) önbelleğe alınır.
- `MEM_ADVISOR_DEBUG=1` hata ayıklama için gizli veri içermeyen tek satır `stderr` çıktısı verir.

---

## Kasa Yapısı ve Obsidian Görünümü

```text
<vault>/
├── .mem-vault.json
├── .obsidian/graph.json       # Otomatik renk grupları ve gürültü filtresi
├── projects.json              # İzinli proje kök dizinleri (beyaz liste)
├── dlp-patterns.txt           # Özel DLP desenleri
├── Core.md                    # Çalışma biçimi ve kalıcı tercihler
├── Kurallar.md                # Genel kurallar
├── projects/
│   └── <slug>/
│       ├── <ProjeAdı>.md      # Proje merkezi (Obsidian grafik yıldızı)
│       ├── Kurallar.md        # Projeye özel kurallar
│       ├── Acik-Konular.md    # Açık işler
│       ├── Son-Oturum.md      # Son oturum devir kartları
│       └── kararlar/          # YYYY-MM-DD-baslik.md (ADR kayıtları)
├── knowledge/                 # Projeler arası ortak bilgi ve kararlar
├── receipts/                  # Oturum makbuzları (grafikte varsayılan gizli)
└── archive/                   # Otomatik arşivlenen eski oturum kartları
```

Kasayı **Obsidian** ile doğrudan klasör olarak açtığınızda (`.obsidian/graph.json` hazır gelir):
- Merkez düğümler (`Core` / `Kurallar`), proje merkezleri, mimari kararlar (`kararlar/`), proje kuralları ve ortak bilgi notları farklı renklerle ayrılır.
- Ham oturum makbuzları (`receipts/`) ve arşiv (`archive/`) varsayılan filtreyle gizlenerek grafik temiz tutulur.

---

## Testleri Çalıştırma

Tüm birim, eşzamanlılık ve güvenlik değişmezi testleri geçici dizin izolasyonunda (`tempfile`) çalışır; gerçek kullanıcı yapılandırmalarınıza dokunmaz:

```bash
python -m unittest discover -s tests -v
```

---

## Lisans

[MIT License](./LICENSE)
