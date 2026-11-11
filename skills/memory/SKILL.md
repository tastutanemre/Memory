---
name: memory
description: İkinci Beyin (`mem`) hafızası — oturum başında bağlam yükle (memory_bootstrap), mimari kararları ADR olarak kaydet (memory_decide), kullanıcı düzeltmelerini kalıcı kural yap (memory_rule), anlamlı iş bitince makbuz ve devir kartı yaz (memory_finish). Bir "neden böyle yaptık?" sorusunda veya geçmiş karar/kural gerektiğinde memory_search ile ara. Çalışma alanı hafıza beyaz listesinde değilse (status=unregistered_workspace) hafızayı hiç kullanma.
---

# Hafıza (`mem`) kullanım kılavuzu

`mem` MCP sunucusu bağlıysa (araçlar `memory_*` adıyla görünür; Devin Desktop'ta slash komutları `/mcp__mem__start`, `/mcp__mem__decide`, `/mcp__mem__wrapup`):

1. **Oturum başı:** `memory_bootstrap` çağır (`cwd` = aktif çalışma alanı yolu, zorunlu; mümkünse `task_hint` ver). Dönen kuralları oturum boyunca uygula. **Sonraki her `memory_*` çağrısında da aynı `cwd`'yi gönder** (MCP sunucusu IDE'nin başlattığı dizinde çalışır, repo dizininde değil).
   - Yanıt `status: unregistered_workspace` ise bu repo beyaz listede değildir: **hafızaya hiçbir şey yazma, hafızadan bilgi aktarma.** Kullanıcı isterse `mem project add <slug> --root <repo>` ile kaydedebilir.
2. **Geçmişi sorgula:** karar/kural/"neden" sorularında `memory_search` (Türkçe çekimli sorgular desteklenir). Bir notu yamalamadan önce `memory_read` ile `sha256` al.
3. **Karar verilince:** `memory_decide` (title, decision, context, alternatives, rationale). `duplicate_warning` dönerse mevcut ADR'yi göster; eskisinin yerine geçiyorsa `supersedes` ver, aksi halde yazma.
4. **Düzeltme alınca:** `memory_rule` (kısa, tek satır, gerekçeyle).
5. **İş bitince:** `memory_finish` (summary, open_threads, refs). Önemsiz ayrıntıyı yazma.
6. **Düzenleme:** `memory_patch` yalnız `expected_sha256` ile; `ConcurrencyConflictError` alırsan notu yeniden oku.

## Yasaklar

- Parola, token, API anahtarı, bağlantı dizesi, TCKN/IBAN gibi kişisel veri yazma. DLP kalkanı `work` profilinde isteği **reddeder** (`DLPBlockedError`), `personal` profilinde maskeler (`[REDACTED:...]`). Reddedilince hassas kısmı çıkarıp tekrar dene; kalkanı atlatmaya çalışma.
- Başka projenin notunu okuma/yazma; kasa dışına çıkan yol verme.
- Şirket git reposuna hafıza dosyası yazma.

## CLI karşılıkları

`python mem.py bootstrap|search|decide|rule|finish|patch|doctor` (`--json` ile makine okunur çıktı).
