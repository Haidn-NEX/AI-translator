import os
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import ai_engine
from ai_engine import LocalAIEngine
from hardware_profile import detect_hardware_profile

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SAMPLE_20_SENTENCES = [
    "Good morning everyone, thank you for joining our fourth quarter product planning meeting today.",
    "Before we dive into the engineering roadmap, let's quickly review the key metrics from last month.",
    "Our active user retention increased by twelve percent after we fixed the onboarding latency issue.",
    "However, the enterprise customers reported that the export feature still times out on large reports.",
    "The QA team needs at least two more weeks to finish regression testing on the new billing module.",
    "If we push the release date from November fifteenth to November twenty-ninth, we can avoid weekend hotfixes.",
    "Meanwhile, marketing proposed increasing our digital campaign budget by fifteen percent for the holiday season.",
    "We should also make sure the documentation is updated before the customer support training on Friday.",
    "Let's check if the backend database migration requires any scheduled downtime for existing tenants.",
    "According to the infrastructure team, the rolling upgrade can run with zero downtime during off-peak hours.",
    "We still have three critical security patches that must be merged into the main branch by tomorrow evening.",
    "Can someone from the mobile team confirm whether the iOS build has been submitted to App Store Review?",
    "The Android release is ready, but we are waiting for final sign-off from the legal and compliance department.",
    "Our cloud hosting costs went up slightly in October because we kept two staging clusters running in parallel.",
    "Once the migration is complete, we will decommission the legacy instances and save around four thousand dollars a month.",
    "Let's prioritize stability over adding new experimental features in the final sprint of the year.",
    "Does anyone have concerns about shifting two backend engineers to help finish the API integration this week?",
    "Please send your updated sprint capacity estimates to the project channel before five o'clock today.",
    "So to summarize the main decision, do you agree with delaying the public launch by two weeks?",
    "Thank you all for the clear updates, let's reconvene on Thursday morning to check the blocker list.",
]

SUMMARY_SYSTEM_PROMPT = (
    "You are the note-taker for a simultaneous interpreter. You receive NEW transcript lines (any language)\n"
    "from a live meeting/video and your PREVIOUS NOTES. Update the notes for a Vietnamese listener.\n"
    'Return ONLY a JSON object: {"topic": string, "new_points": [string], "question_to_listener": string}\n'
    "RULES:\n"
    "1. All values in natural Vietnamese (100% Quốc ngữ). NEVER output Chinese/Cyrillic characters.\n"
    '2. "topic": <= 12 words, the subject being discussed NOW. Keep the previous topic if unchanged.\n'
    '3. "new_points": 0-3 items, each <= 20 words, ONLY information from NEW LINES that is not already in\n'
    "   PREVIOUS NOTES. Keep numbers, dates, names, decisions and commitments exact.\n"
    '4. "question_to_listener": if a speaker asks the listener/audience a question or requests an action,\n'
    '   summarize it in <= 20 words; otherwise "".\n'
    "5. Ignore greetings, fillers and repetitions. NEVER invent facts that are not in NEW LINES."
)

SUMMARY_8_LINES = [
    "Good morning everyone, thank you for joining our Q4 product launch review.",
    "The QA team needs two more weeks to complete regression testing on the billing service.",
    "Because of the QA bottleneck, we propose moving the release deadline back by two weeks to November 29.",
    "Marketing also suggested increasing the Q4 campaign budget by 15% to capture year-end demand.",
    "Infrastructure confirmed that the database migration will run with zero downtime.",
    "We will shut down the old staging cluster next week to save $4,000 per month.",
    "Three critical security patches must be merged into main by tomorrow evening.",
    "Do you agree with delaying the release schedule by two weeks so QA can finish testing?",
]


def build_sample_summary_user_msg() -> str:
    numbered = "\n".join(f"{idx + 1}. {line}" for idx, line in enumerate(SUMMARY_8_LINES))
    return (
        "Glossary / proper nouns: QA, Q4, SLA\n"
        "PREVIOUS NOTES:\n"
        "topic: (chưa có)\n"
        "points:\n"
        "NEW LINES:\n"
        f"{numbered}"
    )


def query_vram_mb() -> str:
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=3,
        ).strip()
        return out.splitlines()[0].strip() if out else "N/A"
    except Exception:
        return "N/A"


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] + (s[c] - s[f]) * (k - f)


def main():
    profile = detect_hardware_profile()
    engine = LocalAIEngine(profile)
    engine.set_custom_glossary("QA, Q4, SLA")

    # Đảm bảo Ollama đang chạy trước khi benchmark
    engine.warmup_ollama()
    if not engine._can_use_ollama_now():
        print("LỖI: Ollama không khả dụng để chạy benchmark.")
        sys.exit(1)

    user_msg = build_sample_summary_user_msg()
    ctx_results = {}

    for ctx in (512, 1024, 2048):
        ai_engine.OLLAMA_NUM_CTX = ctx
        engine.reset_context()
        # Warmup ở đúng num_ctx để nạp cố định KV cache trước khi đo
        engine._probe_ollama_inference()
        _, _ = engine.translate_to_vietnamese("Warmup sentence before measuring latency.")
        engine.reset_context()

        latencies = []
        for sent in SAMPLE_20_SENTENCES:
            vi, ms = engine.translate_to_vietnamese(sent)
            latencies.append(ms)

        vram_used = query_vram_mb()
        p50 = percentile(latencies, 50)
        p95 = percentile(latencies, 95)

        sum_prompt_ms = None
        sum_total_ms = None
        sum_ok = None
        sum_topic = ""
        if ctx in (1024, 2048):
            data, meta = engine.generate_summary_json(SUMMARY_SYSTEM_PROMPT, user_msg)
            sum_prompt_ms = meta.get("prompt_ms", 0.0)
            sum_total_ms = meta.get("total_ms", 0.0)
            sum_ok = meta.get("ok", False)
            if isinstance(data, dict):
                sum_topic = data.get("topic", "")

        ctx_results[ctx] = {
            "p50": p50,
            "p95": p95,
            "vram": vram_used,
            "sum_prompt_ms": sum_prompt_ms,
            "sum_total_ms": sum_total_ms,
            "sum_ok": sum_ok,
            "sum_topic": sum_topic,
        }

    # Chọn num_ctx=1024 để test preempt (hoặc theo kết quả)
    test_ctx = 1024
    ai_engine.OLLAMA_NUM_CTX = test_ctx
    engine.reset_context()
    engine._probe_ollama_inference()
    _, _ = engine.translate_to_vietnamese("Warmup before preempt test.")

    baseline_p50 = ctx_results[test_ctx]["p50"]
    preempt_trials = []
    for trial_idx in range(5):
        sum_holder = {}

        def _run_summary():
            data, meta = engine.generate_summary_json(SUMMARY_SYSTEM_PROMPT, user_msg)
            sum_holder["data"] = data
            sum_holder["meta"] = meta

        th = threading.Thread(target=_run_summary, daemon=True)
        t_start = time.perf_counter()
        th.start()
        time.sleep(0.30)  # Sau 300ms gọi dịch một câu ở thread chính
        test_sent = SAMPLE_20_SENTENCES[trial_idx % len(SAMPLE_20_SENTENCES)]
        vi_out, trans_ms = engine.translate_to_vietnamese(test_sent)
        th.join(timeout=5.0)
        meta = sum_holder.get("meta", {})
        preempt_trials.append(
            {
                "trial": trial_idx + 1,
                "trans_ms": trans_ms,
                "delta_ms": trans_ms - baseline_p50,
                "preempted": meta.get("preempted", False),
                "sum_total_ms": meta.get("total_ms", 0.0),
                "vi_ok": bool(vi_out and not vi_out.startswith("[Lỗi")),
            }
        )
        time.sleep(0.25)

    p512_p95 = ctx_results[512]["p95"]
    print("\n### 1. Bảng Benchmark `num_ctx` (20 câu dịch + 8 câu tóm tắt)\n")
    print(
        "| `num_ctx` | Dịch p50 (`ms`) | Dịch p95 (`ms`) | Chênh p95 vs 512 | VRAM (`nvidia-smi`) | Summary `prompt_ms` | Summary `total_ms` | Summary JSON `ok` |"
    )
    print("| :--- | ---: | ---: | ---: | ---: | ---: | ---: | :--- |")
    for ctx in (512, 1024, 2048):
        r = ctx_results[ctx]
        diff_pct = ((r["p95"] - p512_p95) / p512_p95 * 100.0) if p512_p95 > 0 else 0.0
        diff_str = "baseline" if ctx == 512 else f"{diff_pct:+.1f}%"
        p_ms = f"{r['sum_prompt_ms']:.0f}" if r["sum_prompt_ms"] is not None else "—"
        t_ms = f"{r['sum_total_ms']:.0f}" if r["sum_total_ms"] is not None else "—"
        ok_str = str(r["sum_ok"]) if r["sum_ok"] is not None else "—"
        print(
            f"| `{ctx}` | {r['p50']:.0f} | {r['p95']:.0f} | {diff_str} | {r['vram']} | {p_ms} | {t_ms} | {ok_str} |"
        )

    print(f"\n### 2. Kết quả Test Preempt ở `num_ctx = {test_ctx}` (Dịch gọi sau 300ms khi đang chạy Tóm tắt)\n")
    print(f"- **Baseline dịch p50 (`num_ctx={test_ctx}`):** `{baseline_p50:.0f} ms`")
    print("\n| Lượt | `trans_ms` khi preempt (`ms`) | Độ trễ tăng thêm vs p50 (`ms`) | Summary `preempted` | Summary thoát sau (`ms`) | Bản dịch hợp lệ |")
    print("| :---: | ---: | ---: | :---: | ---: | :---: |")
    deltas = []
    trans_list = []
    for tr in preempt_trials:
        deltas.append(tr["delta_ms"])
        trans_list.append(tr["trans_ms"])
        print(
            f"| #{tr['trial']} | {tr['trans_ms']:.0f} | {tr['delta_ms']:+.0f} | `{tr['preempted']}` | {tr['sum_total_ms']:.0f} | `{tr['vi_ok']}` |"
        )

    preempt_p50 = percentile(trans_list, 50)
    preempt_p95 = percentile(trans_list, 95)
    delta_p50 = percentile(deltas, 50)
    delta_p95 = percentile(deltas, 95)
    print(
        f"\n- **Tổng hợp Preempt:** `trans_ms` p50 = `{preempt_p50:.0f} ms`, p95 = `{preempt_p95:.0f} ms` "
        f"| **Độ trễ dịch tăng thêm:** p50 = `{delta_p50:+.0f} ms`, **p95 = `{delta_p95:+.0f} ms`**"
    )


if __name__ == "__main__":
    main()

