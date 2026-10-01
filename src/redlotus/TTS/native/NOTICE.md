# Mambo CPU worker

RedLotus adapts GPT-SoVITS-cpp at commit
`384a2f995c48ef1903373ac8814569a8925779ba` (Apache-2.0).
Upstream: https://github.com/GPT-SoVITS-Devel/GPT-SoVITS-cpp
Its license is reproduced in `UPSTREAM-LICENSE.txt`.

`upstream.patch` records RedLotus changes: CPU-only loading, bounded feature
parsing, growing decoder caches, sampling, output ownership, shared allocator
and thread pool, producer-compatible BERT boundary tokens and feature alignment,
deterministic resource teardown, and owned PCM without file codecs or reference-audio
preparation. The source headers provide compact Chinese and English dictionary
storage and a bounded vocoder window with history, lookahead and waveform overlap.
`mambo_worker.cpp` provides protocol 2: bounded PCM byte blocks, an explicit
completion length, and a zero-PCM `skipped` reply for text with no phonemes.
The host drains each reply before reusing the worker after
cancellation, and resamples each request to 24 kHz with preserved filter state.
The executable loads the installed sherpa-onnx ONNX Runtime by absolute path;
no second runtime DLL, model weights, training environment, or audio is bundled.

The current worker supports Windows x64. Windows x64 wheels, source
distributions, and Windows frozen builds include the compiled executable and
these notices. Build it with `scripts/build_native_speech.py` from the pinned
source and ONNX Runtime 1.28.2 SDK. ONNX model weights are downloaded by the
application on first startup when absent; training and export tools are not runtime
dependencies. Other platforms retain the existing application distribution
without a Mambo worker.

`THIRD-PARTY-NOTICES.md` and `licenses/` record additional native runtime
notices. Model and voice resource terms are supplied with their own bundles;
these native notices do not grant redistribution rights for those resources.

This backend is under acceptance. Source, model, dependency and voice-resource
redistribution licenses must be reviewed before publishing an artifact; the
upstream code license does not grant redistribution rights for every voice.
