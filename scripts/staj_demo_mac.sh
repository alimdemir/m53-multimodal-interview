#!/usr/bin/env bash
# M53'ü Apple Silicon Mac'te uçtan uca çalıştırır:
#   1) Django + model sözleşmesi testleri
#   2) Türkçe sentetik test sesi (macOS `say`)
#   3) Gerçek ağırlıklarla çalışabilirlik kontrolü (Whisper, BERT, HuBERT, ViT, Qwen3-4B MLX)
#   4) Demo verisi + yerel sunucu (http://127.0.0.1:8000)
# Kullanım:  bash scripts/staj_demo_mac.sh
#            SADECE_KONTROL=1 bash scripts/staj_demo_mac.sh   (yalnız 2-3. adımlar, sunucu açılmaz)
set -euo pipefail
cd "$(dirname "$0")/.."
SADECE_KONTROL="${SADECE_KONTROL:-0}"
# Pencereyi ekran kenarlarından uzak tut (ekran görüntüsü kırpılırken kenar efektleri girmesin)
printf '\e[8;38;118t'; printf '\e[3;%st' "${PENCERE_KONUM:-160;160}"

PY=.venv/bin/python
LOG=docs/calisma_kayitlari
mkdir -p "$LOG" tmp
line() { printf '\n\033[1;36m▶ %s\033[0m\n' "$1"; }

if [ "$SADECE_KONTROL" != 1 ]; then
line "1/4 Testler (kural motoru, modelsiz sözleşme testleri dahil)"
QUESTION_BACKEND=rules ASR_ENABLED=0 EMOTION_ENABLED=0 "$PY" manage.py test -v 2 2>&1 | tee "$LOG/01_testler.log" | tail -n 14
fi

line "2/4 Türkçe test sesi"
VOICE=$(say -v '?' | awk '/tr_TR/ {print $1; exit}')
AUDIO_ARGS=()
if [ -n "${VOICE}" ]; then
  say -v "$VOICE" -o tmp/ai-smoke-tr.aiff "Merhaba. Son projemde Django ve Redis kullanarak mülakat sisteminin yanıt süresini sekiz yüz milisaniyeden iki yüz milisaniyeye indirdim."
  AUDIO_ARGS=(--audio tmp/ai-smoke-tr.aiff)
  echo "ses: $VOICE -> tmp/ai-smoke-tr.aiff"
else
  echo "Türkçe macOS sesi bulunamadı; ASR örneği atlanıyor."
fi

line "3/4 Gerçek modellerle check_ai (çalışabilirlik testi, doğruluk ölçmez)"
HF_HUB_OFFLINE=1 "$PY" manage.py check_ai "${AUDIO_ARGS[@]}" --report "$LOG/02_check_ai.json" 2>&1 | tee "$LOG/02_check_ai.log"
"$PY" - "$LOG/02_check_ai.json" <<'PY'
import json, sys
r = json.load(open(sys.argv[1]))
print(f"\ncihaz: {r['device']}")
for k, v in r["checks"].items():
    ok = v.get("ok", v.get("state") in ("ok", "uncertain"))
    extra = v.get("text") or v.get("provider") or ""
    ms = v.get("ms", v.get("latency_ms", ""))
    print(f"  {'✔' if ok else '✘'} {k:<26} {str(ms)+' ms' if ms != '' else '':>9}  {extra}")
q = r["checks"].get("questions", {}).get("questions", [])
if q:
    print("\nönerilen takip soruları:")
    for s in q:
        print("  •", s)
PY

[ "$SADECE_KONTROL" = 1 ] && exit 0

line "4/4 Demo verisi ve sunucu"
"$PY" manage.py migrate -v 0
"$PY" manage.py seed_demo
echo "Tarayıcı: http://127.0.0.1:8000  (durdurmak için Ctrl+C)"
HF_HUB_OFFLINE=1 "$PY" manage.py runserver 127.0.0.1:8000 --noreload
