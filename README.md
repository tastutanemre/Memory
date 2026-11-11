# mem — İkinci Beyin Motoru

Sıfır bağımlılıklı (yalnız Python 3.11+ standart kütüphanesi), kurumsal EDR/DLP dostu mühendislik hafızası.
Markdown kasası (Obsidian uyumlu) + SQLite FTS5 indeksi + MCP sunucusu. **Devin Desktop** birincil hedef;
Antigravity, Claude Code ve Codex ile de çalışır.

## Tasarım ilkeleri

| İlke | Uygulama |
|---|---|
| Sıfır bağımlılık | `pip install` gerekmez: `python mem.py <komut>` (isteğe bağlı `pyproject.toml` ile `mem` komutu). |
| Sıfır ağ | `urllib/http/socket/ssl…` hiçbir yerde import edilmez (testle kilitli). Dış model/servis yok. |
| EDR dostu | Alt süreç yok, PowerShell/`EncodedCommand`/Base64 yok, başlangıç klasörü/zamanlanmış görev yok, gizli arka plan süreci yok. MCP kaydı doğrudan `python` + `mem.py` dizisidir. |
| Veri sızıntısı yok | Hafıza **beyaz liste** (`projects.json`) dışındaki çalışma alanlarında hiçbir şey okumaz/yazmaz (`unregistered_workspace`). Şirket repolarına dosya yazılmaz. |
| DLP varsayılan açık | `work` profili `block`, diğerleri `redact`. Sır hata mesajına/loga açık yazılmaz. |
| Çok ajanlı güvenlik | OS dosya kilidi (`msvcrt`/`fcntl`), atomik yazma (`fsync` + `os.replace`), `sha256` iyimser kilit, son 20 yedek. |

## Hızlı başlangıç (Windows / PowerShell, pip yok)

```powershell
cd C:\dev\repos\Memory
python mem.py init --profile work --vault D:\Hafiza\work     # kasa + profil (dlp: block)
python mem.py project add order-service --root C:\work\repos\OrderService --name OrderService
cd C:\work\repos\OrderService
python C:\dev\repos\Memory\mem.py bootstrap --task "cache"   # oturum bağlamı
python C:\dev\repos\Memory\mem.py decide --title "Redis cache kullan" --decision "Okumalar için Redis" --rationale "Hız"
python C:\dev\repos\Memory\mem.py rule "Her PR test içermeli" --reason "kalite"
python C:\dev\repos\Memory\mem.py search "önbellek kararları"
python C:\dev\repos\Memory\mem.py finish --summary "Cache eklendi" --thread "TTL belirle"
python C:\dev\repos\Memory\mem.py doctor
```

`--json` her komutta makine okunur çıktı verir. `MEM_HOME` ortam değişkeni profil kaydı ve state dizinini taşır
(varsayılan: `%LOCALAPPDATA%\mem-engine`, POSIX: `~/.local/share/mem-engine`). Profil seçimi: `--profile` > `MEM_PROFILE` > kayıtlı varsayılan.

Çıkış kodları: `0` tamam · `2` hata/kullanım · `3` DLP engeli · `4` sha256 çakışması · `5` kayıtsız çalışma alanı · `1` doctor hatası.

## Devin Desktop kurulumu

```powershell
python mem.py connect devin --profile work --dry-run   # önce ne yazılacağını gör
python mem.py connect devin --profile work
```

Yazılanlar (mevcut sunucular/metinler korunur, ilk değişiklikte `.bak` alınır):

- `%APPDATA%\devin\mcp_config.json` → `mcpServers.mem = { command: <python.exe>, args: [<...>\mem.py, "mcp", "--profile", "work"] }`
- `%APPDATA%\devin\AGENTS.md` → `<!-- mem-bridge:start --> … <!-- mem-bridge:end -->` kural bloğu

Kaldırma: `python mem.py connect devin --uninstall` (yalnız kendi anahtarı/bloğu silinir).
Diğer hedefler: `antigravity`, `claude`, `codex` (`--config-root <dizin>` ile gerçek ev dizinine dokunmadan deneme).
Skill: `skills/memory/SKILL.md` dosyasını `.devin/skills/memory/` veya `.agents/skills/memory/` altına kopyalayın.
Devin Desktop'ta MCP prompt'ları `/mcp__mem__start`, `/mcp__mem__decide`, `/mcp__mem__wrapup` olarak görünür.

## Kasa yapısı

```text
.mem-vault.json  projects.json  dlp-patterns.txt  Core.md  Kurallar.md
projects/<slug>/{Genel-Bakis,Kurallar,Acik-Konular,Son-Oturum}.md  projects/<slug>/kararlar/YYYY-MM-DD-*.md
knowledge/  receipts/YYYY-MM/<hash16>.md  archive/YYYY-MM.md
```

İndeks (`index.sqlite3`, kilit, yedekler) **kasanın dışında** `state/<profil>/` altındadır. Notlar gerçek YAML frontmatter taşır
(eski JSON frontmatter okunur, yazarken YAML'a çevrilir).

## Modüller (`src/mem/`)

`config` profil/beyaz liste · `frontmatter` YAML alt kümesi · `dlp` sır kalkanı · `lock` kilit + atomik yazma + yol güvenliği ·
`turkish` kök/ek + teknik terim koruması · `store` SQLite WAL+FTS5 + `(mtime_ns,size)` artımlı sync · `search` BM25 + WikiLink grafı + `SemanticReranker` kancası ·
`vault` iş akışları · `mcp_server` JSON-RPC 2.0 stdio · `harness` bağlayıcılar · `migrate` `import-v3` · `cli`.

### MCP araçları

`memory_bootstrap`, `memory_search`, `memory_read`, `memory_decide`, `memory_rule`, `memory_patch`, `memory_finish` (+ `start`/`decide`/`wrapup` prompt'ları).
Hatalar JSON-RPC'yi çökertmez: `isError: true` + eyleme dönük metin (`DLPBlockedError`, `ConcurrencyConflictError`…).
`stdout` yalnız protokol içindir; tanı çıktıları `stderr`'e gider. Varsayılan çerçeveleme satır-sonu ayrımlı JSON; ilk mesaj `Content-Length:` ile başlarsa LSP çerçevelemesine otomatik geçilir.

## DLP

Kategoriler: `api_token`, `private_key`, `jwt_bearer`, `connection_string`, `generic_secret_assignment`, `pii_tr` (IBAN, algoritma doğrulamalı TCKN), `custom_pattern`.
Şirkete özel desenler için kasa kökündeki `dlp-patterns.txt` (düz metin veya `regex:` önekli sınırlı regex).
`block`: yazma reddedilir, hata yalnız kategori ve maskeli önizleme içerir. `redact`: `[REDACTED:<kategori>]`.

## Arama

Her kelime için hem katlanmış tam biçim hem kök indekslenir (`bağlantılarından` → `bağlant`); CamelCase/rakam/`_`/`.`/`-` içeren ve İngilizce teknik terimler
(`AuthHub`, `claude`, `mcp_config`) asla köklenmez. Varsayılan olarak `untrusted`, `superseded` ve `archive/` notlar elenir; proje kapsamı yalnız o projeyi + paylaşılan notları getirir.
Karar/kural +%15, aktif proje +%25, 1-hop `[[WikiLink]]` komşuluğu bonus alır.

## Testler

```powershell
python -m unittest discover -s tests -v
```

Tüm testler `tempfile` + `MEM_HOME`/`--config-root` izolasyonunda çalışır; gerçek `~/.claude`, `~/.codex`, `~/.gemini`, `%APPDATA%\devin` dosyalarına dokunmaz.

## Bilinen sınırlar

- Kök bulucu ünlü uyumunu ASCII-katlanmış uzayda yaklaşık uygular (tutarlılık için agresif); nadir yanlış eşleşmeler mümkündür, tam eşleşme her zaman daha yüksek puan alır.
- Aynı süreçte `VaultLock` yeniden girilemez; kilit yalnız yazma işlemlerinde alınır.
- `archive/YYYY-MM.md` tüm projelerin kartlarını `<!-- proje: slug -->` etiketiyle taşır; arşiv varsayılan aramada elenir.
- MCP sunucusu IDE'nin başlattığı dizinde çalışabilir; ajanlar her araç çağrısında `cwd` göndermelidir. `cwd` atlanırsa sunucu önce kendi (kayıtlıysa) dizinini, sonra son başarılı `memory_bootstrap` çalışma alanını kullanır; ikisi de yoksa açık hata (`isError`) döner.
