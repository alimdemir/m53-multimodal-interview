"""Small local Hugging Face Qwen adapter; isolated from biometric analysis."""
import os
import platform
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from .hf_runtime import model_available, model_path


class LocalQuestionModel:
    def __init__(self):
        self.lock = threading.Lock()
        self.model = None
        self.tokenizer = None
        self.failures = 0
        self.cooldown_until = 0.0
        # MLX streams are thread-local. Keeping both runtimes on one worker also
        # prevents concurrent CPU/GPU generations from exhausting the host.
        self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="local-question")

    @staticmethod
    def model_key():
        if platform.system() == "Darwin" and platform.machine() == "arm64" and model_available("qwen-mlx"):
            return "qwen-mlx"
        if model_available("qwen-cpu"):
            return "qwen-cpu"
        return ""

    @property
    def provider(self):
        return "hf-qwen-mlx" if self.model_key() == "qwen-mlx" else "hf-qwen-transformers"

    def generate(self, messages):
        if not self.model_key():
            raise FileNotFoundError("Yerel soru modeli indirilmemiş.")
        if time.monotonic() < self.cooldown_until:
            raise RuntimeError("Soru modeli kısa süreli dinlenmede.")
        if not self.lock.acquire(blocking=False):
            raise RuntimeError("Soru modeli meşgul.")
        try:
            future = self.worker.submit(self._run, messages)
        except BaseException:
            self.lock.release()
            raise
        # A caller timeout must not admit another GPU job while this one runs.
        return future.result(timeout=int(os.getenv("QUESTION_TIMEOUT_SECONDS", "40")))

    def _run(self, messages):
        try:
            result = self._generate_mlx(messages) if self.model_key() == "qwen-mlx" else self._generate_transformers(messages)
            self.failures = 0
            return result
        except Exception:
            self.failures += 1
            if self.failures >= 3:
                self.cooldown_until = time.monotonic() + 60
                self.failures = 0
            raise
        finally:
            self.lock.release()

    def _generate_mlx(self, messages):
        from mlx_lm import load, stream_generate
        from mlx_lm.sample_utils import make_sampler

        if self.model is None:
            self.model, self.tokenizer = load(str(model_path("qwen-mlx")), tokenizer_config={"trust_remote_code": False})
        prompt = self.tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True, enable_thinking=False)
        if len(self.tokenizer.encode(prompt)) > 4096:
            raise ValueError("Soru bağlamı çok uzun.")
        started = time.monotonic()
        parts = []
        stream = stream_generate(self.model, self.tokenizer, prompt=prompt, max_tokens=320, sampler=make_sampler(temp=0))
        try:
            for response in stream:
                parts.append(response.text)
                if time.monotonic() - started > 25:
                    raise TimeoutError("Soru üretim süresi aşıldı.")
        finally:
            stream.close()
        return "".join(parts)

    def _generate_transformers(self, messages):
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer

        torch.set_num_threads(int(os.getenv("QUESTION_CPU_THREADS", "4")))
        if self.model is None:
            path = str(model_path("qwen-cpu"))
            self.tokenizer = AutoTokenizer.from_pretrained(path, local_files_only=True, trust_remote_code=False)
            self.model = AutoModelForCausalLM.from_pretrained(
                path,
                local_files_only=True,
                trust_remote_code=False,
                dtype="auto",
            ).eval()
            self.model.generation_config.do_sample = False
            self.model.generation_config.temperature = None
            self.model.generation_config.top_p = None
            self.model.generation_config.top_k = None
        prompt = self.tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
        inputs = self.tokenizer(prompt, return_tensors="pt")
        if inputs["input_ids"].shape[-1] > 4096:
            raise ValueError("Soru bağlamı çok uzun.")
        with torch.inference_mode():
            output = self.model.generate(
                **inputs,
                max_new_tokens=int(os.getenv("QUESTION_MAX_NEW_TOKENS", "240")),
                do_sample=False,
                use_cache=True,
                pad_token_id=self.tokenizer.eos_token_id,
            )
        generated = output[0, inputs["input_ids"].shape[-1]:]
        return self.tokenizer.decode(generated, skip_special_tokens=True)


local_question_model = LocalQuestionModel()
