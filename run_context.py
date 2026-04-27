from pico.metrics import run_real_context_experiment
result = run_real_context_experiment('gpt', 1)
for c in result['configs']:
    print(f"{c['id']:30s} full={c['avg_full_prompt_chars']:>5} raw={c['avg_raw_prompt_chars']:>5} 压缩率={c['avg_prompt_compression_ratio']:.2%}  正确率(full)={c['full_correct_rate']:.0%} 正确率(raw)={c['raw_correct_rate']:.0%}")