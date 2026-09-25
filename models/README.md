# Model deposu

Bu dizin ağırlık dosyalarını Git'e eklemez. `scripts/download_models.py` her modelin Hugging Face commit kimliğini çözer, dosyaları bu dizine indirir ve `model-lock.json` içine kaynak, commit, zaman ve boyut bilgisini yazar.

Aktif model kümesi:

- `faster-whisper-large-v3-turbo`: canlı Türkçe ASR.
- `qwen-mlx`: Apple Silicon üzerinde kanıta bağlı takip sorusu üretimi.
- `emotion-text`, `emotion-audio`, `emotion-video`: yalnız açık onaylı kişisel koçluk.
- `face-detector`: tek yüz kırpımı için YuNet.

```bash
.venv/bin/python scripts/download_models.py --profile mac
.venv/bin/python scripts/download_models.py --only whisper
```

Apple Silicon'da `--profile mac` Türkçe metin/ses ve yüz ifadesi modellerini, YuNet'i ve MLX uyumlu Qwen3-4B 4-bit modelini indirir. VLLM veya ikinci bir Qwen modeli tutulmaz. `requirements-mac.txt` ve `.env` içinde `QUESTION_BACKEND=local` kullanılır. Model çıktısı doğrulanamazsa açıkça kural motoruna dönülür.

`emotion-audio` checkpoint'i standart Transformers HuBERT sınıflandırıcısıyla aynı başlığa sahip değildir; projedeki `turkish_hubert.py` kendi yerel adaptörüdür. Eksik ağırlık veya tanımsız etiket varsa tahmin reddedilir. Çalışma anında uzaktan Python kodu veya ağırlık indirilmez. Native sınıflar ayrı tutulur; `Calm` etiketi `neutral` diye dönüştürülmez. Kaynak koşulları ve Türkçe görüşme verisiyle doğrulama tamamlanmadan sonuçlar üretim/işe alım ölçütü değildir.
