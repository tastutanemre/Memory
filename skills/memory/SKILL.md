---
name: memory
description: İkinci Beyin (`mem`) hafızası — oturum başında bağlam yükle (memory_bootstrap), mimari kararları ADR olarak kaydet (memory_decide), kullanıcı düzeltmelerini kalıcı kural yap (memory_rule), anlamlı iş bitince makbuz ve devir kartı yaz (memory_finish). Bir "neden böyle yaptık?" sorusunda veya geçmiş karar/kural gerektiğinde memory_search ile ara. Çalışma alanı hafıza beyaz listesinde değilse (status=unregistered_workspace) hafızayı hiç kullanma.
---

# Hafıza (`mem`) kullanım kılavuzu

`mem` MCP sunucusu bağlıysa (araçlar `memory_*` adıyla görünür; Devin Desktop'ta slash komutları `/mcp__mem__start`, `/mcp__mem__decide`, `/mcp__mem__wrapup`):

1. **Oturum başı:** `memory_bootstrap` çağır (`cwd` = aktif çalışma alanı yolu, zorunlu; mümkünse `task_hint` ver; isteğe bağlı `budget_chars` varsayılan `8000` — zorunlu kurallar asla kesilmez ve `Acik-Konular.md` + `Son-Oturum.md` taban payları nedeniyle çıktı `budget_chars` değerini ~2400+ karakter aşabilir). Dönen kuralları oturum boyunca uygula. **Sonraki her `memory_*` çağrısında da aynı `cwd`'yi gönder** (MCP sunucusu IDE'nin başlattığı dizinde çalışır, repo dizininde değil).
   - Yanıt `status: unregistered_workspace` ise bu repo beyaz listede değildir: **hafızaya hiçbir şey yazma, hafızadan bilgi aktarma.** Kullanıcı isterse `mem project add <slug> --root <repo>` ile kaydedebilir.
2. **Geçmişi sorgula:** karar/kural/"neden" sorularında `memory_search` (`query`, `cwd`, isteğe bağlı `limit`, `include_superseded`, `include_archived`; Türkçe çekimli sorgular desteklenir). Bir notu yamalamadan önce `memory_read` ile `sha256` al.
3. **Karar verilince:** `memory_decide` (`title`, `decision`, `cwd`; isteğe bağlı `context`, `alternatives`, `rationale`, `supersedes`, `sources`, `tags`, `shared=true`, `force=true`).
   - Projeler arası ortak kararlar için `shared=true` ver (`knowledge/kararlar/` altına yazar).
   - Yanıt `duplicate_warning` veya `advisor_warning` dönerse mevcut kararı ve uyarıyı kullanıcıya göster; eski kararın yerine geçiyorsa `supersedes` ver, kullanıcı açıkça onaylarsa `force=true` kullan, aksi halde yazma.
4. **Düzeltme alınca:** `memory_rule` (`rule`, `cwd`; isteğe bağlı `reason`, `scope='global'` veya proje slug'ı, `tags`, `supersedes`, `remove=true`). Kısa, tek satır ve gerekçeli yaz; eski bir kuralı güncellerken `supersedes`, kaldırırken `remove=true` kullan (eski kural arşive taşınır).
5. **İş bitince:** `memory_finish` (`summary`, `cwd`; isteğe bağlı `open_threads`, `close_threads`, `threads_mode='merge'|'replace'`, `refs`, `session_id`, `harness`). Önemsiz ayrıntıyı yazma. Çoklu ajan açık konu izolasyonu için her aracın `mem connect <harness>` (`--harness`) ile ayrı bir `harness` kimliğiyle başlatılması gerekir; sunucu `--harness` olmadan başlatıldıysa çağrıda ayrı `harness` argümanı ver (aksi durumda iki sunucu da `mcp` varsayılanını kullanır ve konu sahipliğini paylaşır).
   - Tekrarlı makbuz ve kart oluşumunu önlemek için her zaman kararlı bir `session_id` ver (`session_id` normalizasyonu önce tekrarlanan `<harness>-` öneklerini temizler, ardından kalan eki **24 karaktere** kırpar; bu nedenle aynı proje ve harness için farklı oturumlar ilk 24 karakter içinde ayrışmalıdır).
   - `threads_mode='merge'` (varsayılan) modunda `open_threads` yalnızca kendi aracının (`harness`) açık konularını günceller ve diğer ajanların açtığı maddeleri korur. `threads_mode='replace'` modunda ise `open_threads` listesi açıkça verildiğinde tüm açık konular (sahibine bakılmaksızın) yenisiyle değiştirilir ve listede olmayan tüm eski maddeler arşive kaldırılır. Başka bir ajanın tamamladığı maddeyi kapatmak için `close_threads` içine madde kimliğini (`t:<id>`) veya tam metnini ver.
6. **Düzenleme:** `memory_patch` (`path`, `expected_sha256`, `edits: [{"old": "...", "new": "..."}]`, `cwd`). Her `old` metni dosyada tam 1 kez geçmeli; `ConcurrencyConflictError` alırsan notu `memory_read` ile yeniden oku.

## Yasaklar

- Parola, token, API anahtarı, bağlantı dizesi, TCKN/IBAN gibi kişisel veri yazma. DLP kalkanı `work` profilinde isteği **reddeder** (`DLPBlockedError`), `personal` profilinde maskeler (`[REDACTED:...]`). Reddedilince hassas kısmı çıkarıp tekrar dene; kalkanı atlatmaya çalışma.
- Başka projenin notunu okuma/yazma; kasa dışına çıkan yol verme.
- Çalışma alanı git reposuna hafıza dosyası yazma.

## CLI karşılıkları

`python mem.py init|project|bootstrap|search|read|decide|rule|patch|finish|compact|sync|connect|profiles|advisor|doctor` (`--json` ile makine okunur çıktı; `compact --days 14` eski oturum makbuzlarını aylık özet notuna sıkıştırır).
