# AGENTS.md — `mem` Otonom Kurulum ve Güncelleme Kılavuzu

Bu depo (`Memory`) sıfır harici bağımlılıklı yerel İkinci Beyin (`mem`) MCP motorudur. Kullanıcı senden *"Bu repoyu ortamıma kur"*, *"mem kurulumumu güncelle"* veya benzeri bir istekte bulunduğunda aşağıdaki deterministik adımları sırasıyla uygula.

## 1. Mevcut Kurulumu Kontrol Et (İkinci Klon Açma!)

Yeni bir repo klonu açmadan veya `setup` çalıştırmadan önce makinede zaten kurulu bir `mem` örneği olup olmadığını denetle:

- **Profil kaydı:**
  - Windows: `%LOCALAPPDATA%\mem-engine\profiles.json`
  - macOS / Linux: `~/.local/share/mem-engine/profiles.json`
  *(Ortamda `MEM_HOME` tanımlıysa `$MEM_HOME/profiles.json` dosyasına bak.)*
- **İstemci MCP ayarlarındaki mevcut `mem.py` yolu:**
  - Devin Desktop: `%APPDATA%\devin\mcp_config.json` (POSIX: `~/.config/devin/mcp_config.json`)
  - Claude Code: `~/.claude.json`
  - Codex: `~/.codex/config.toml`
  - Antigravity: `~/.gemini/config/mcp_config.json`

Bu dosyalarda `mcpServers.mem` (veya `[mcp_servers.mem]`) altında mevcut bir `mem.py` yolu varsa **ikinci bir klon açma**; doğrudan o mevcut `Memory` repo dizinine geç.

## 2. Güncelleme Akışı (Kurulu Ortam Varsa)

1. Mevcut `Memory` repo dizininde çalışma ağacının temiz olduğunu doğrula ve yalnızca fast-forward ile güncelle:
   ```bash
   git pull --ff-only origin main
   ```
2. Tek komutla kasa şablonlarını koruyarak (mevcut `Core.md`, `Kurallar.md`, `projects.json`, `dlp-patterns.txt` dosyalarına **dokunmadan**) kurulu istemcileri ve global `skills/memory/SKILL.md` dosyasını senkronize et:
   ```bash
   python mem.py setup
   ```
   *(Varsayılan profil dışında bir profili güncelliyorsan `--profile <ad>` ver. Başka bir profile bağlı istemciler otomatik algılamada korunur ve atlanır.)*
3. Sağlık kontrolünü çalıştır:
   ```bash
   python mem.py doctor
   ```
4. Kullanıcıya yapılan değişiklikleri raporla ve açık olan IDE / masaüstü istemcilerini (Windows'ta Claude Desktop dahil, sistem tepsisinden Quit ile) yeniden başlatmasını bildir.

## 3. İlk Kurulum Akışı (Kurulu Ortam Yoksa)

1. **Python 3.11+ doğrula:** `python --version` (POSIX'te `python3 --version`) ile `3.11` veya üstü olduğunu doğrula. Sanal ortam (`venv`) veya `pip install` gerekmez.
2. **Repo konumu:** Yerel diskte `Memory` klonu yoksa uygun bir araç dizinine klonla (ör. `~/tools/Memory` veya `/dev/tools/Memory`). Kasa dizinini **asla** `Memory` reposunun içine kurma.
3. **Profil seçimi:**
   - İş bilgisayarı / kurumsal ortamda: `--profile work` (`dlp: block`, hava boşluklu).
   - Kişisel ortamda: `--profile personal` (`dlp: redact`).
   - Hangisinin istendiği kullanıcının cümlesinden açık değilse tahmin etme; **tek kısa soruyla** `work` mü `personal` mı istediğini sor.
4. **Kurulum ve proje kaydı:** Aktif çalışma alanı / proje klasörlerini `--project <slug>=<yol>` bayrağıyla ekleyerek tek komutu çalıştır:
   ```bash
   python mem.py setup --profile <work|personal> --project <slug>=<yol>
   ```
   *(Özel kasa yolu istenirse `--vault <yol>` ekle; kurulu istemciler otomatik algılanıp bağlanır ve global `SKILL.md` kopyalanır.)*
5. **Doğrulama:**
   ```bash
   python mem.py doctor --profile <work|personal>
   ```
   Çıkış kodu `0` (`TAMAM`) olmalıdır. Ardından kullanıcıya açık istemcileri yeniden başlatmasını ve `Core.md` dosyasını kendi tercihlerine göre gözden geçirebileceğini bildir.

## 4. Güvenlik ve Bütünlük Kuralları

- Mevcut `Core.md`, `Kurallar.md`, `projects.json` veya notları asla silme ya da üzerine yazma; bozuk bir istemci JSON dosyası görürsen elle düzeltmeden önce kullanıcıya bildir.
- Parola, API anahtarı, token veya kişisel veriyi komut satırına ya da kasa dosyalarına yazma.
- `mem` çekirdeği (`src/mem/`) sıfır ağ ve sıfır üçüncü parti bağımlılıkla çalışır; ek paket kurma.
