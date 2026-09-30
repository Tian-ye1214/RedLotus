#pragma once
#include <algorithm>
#include <cmath>
#include <functional>
#include <vector>
#include "GPTSoVITS/model/sovits.h"
#include "GPTSoVITS/Utils/LoudnessNormalizer.h"
struct WindowConfiguration {
  std::size_t first_tokens;
  std::size_t following_tokens;
  std::size_t history_tokens;
  std::size_t lookahead_tokens;
  std::size_t samples_per_token;
  std::size_t crossfade_samples;
};

// Bounded-window vocoder driven by the unchanged growing semantic sampler.
class StreamingVocoder {
  using Tensor = GPTSoVITS::Model::Tensor;
  GPTSoVITS::Model::SoVITSModel& model_;
  Tensor* phones_;
  Tensor* reference_;
  Tensor* speaker_;
  const WindowConfiguration configuration_;
  const std::function<void(const std::vector<float>&)> emit_;
  GPTSoVITS::LoudnessNormalizer normalizer_;
  std::size_t emitted_ = 0;
  std::vector<float> overlap_;

  void Decode(const std::vector<int64_t>& tokens, std::size_t end) {
    using namespace GPTSoVITS::Model;
    auto window_begin = emitted_ > configuration_.history_tokens ? emitted_ - configuration_.history_tokens : 0;
    const auto window_end = std::min(tokens.size(), end + configuration_.lookahead_tokens);
    // The upstream streaming recipe keeps at least ten semantic tokens at tails.
    if (window_end - window_begin < 10) window_begin = window_end > 10 ? window_end - 10 : 0;
    auto input = Tensor::CreateFromHost(const_cast<int64_t*>(tokens.data()) + window_begin,
        {1, 1, static_cast<int64_t>(window_end - window_begin)}, DataType::kInt64);
    auto audio = model_.GenerateTensor(input.get(), phones_, reference_, speaker_, 0.5f, 1.0f);
    if (!audio || audio->Type() != DataType::kFloat32 || !audio->IsCPU()
        || audio->ElementCount() != (window_end - window_begin) * configuration_.samples_per_token)
      throw std::runtime_error("Unexpected streaming vocoder result");
    auto* values = audio->Data<float>();
    double mean = 0;
    for (int64_t i = 0; i < audio->ElementCount(); ++i) mean += values[i];
    mean /= audio->ElementCount();
    for (int64_t i = 0; i < audio->ElementCount(); ++i) values[i] -= static_cast<float>(mean);
    const auto offset = (emitted_ - window_begin) * configuration_.samples_per_token;
    const auto frames = (end - emitted_) * configuration_.samples_per_token;
    std::vector<float> output(values + offset, values + offset + frames);
    const auto fade = std::min(frames, overlap_.size());
    for (std::size_t i = 0; i < fade; ++i) {
      const auto alpha = static_cast<float>(i + 1) / static_cast<float>(fade);
      output[i] = overlap_[i] * (1.0f - alpha) + output[i] * alpha;
    }
    const auto kept = std::min(configuration_.crossfade_samples,
        (window_end - end) * configuration_.samples_per_token);
    overlap_.assign(values + offset + frames, values + offset + frames + kept);
    normalizer_.NormalizeStreaming(output);
    emit_(output);
    emitted_ = end;
  }
public:
  StreamingVocoder(GPTSoVITS::Model::SoVITSModel& model, Tensor* phones, Tensor* reference,
      Tensor* speaker, WindowConfiguration configuration,
      std::function<void(const std::vector<float>&)> emit)
      : model_(model), phones_(phones), reference_(reference), speaker_(speaker),
        configuration_(configuration), emit_(std::move(emit)) {}

  void Accept(const std::vector<int64_t>& tokens, bool final) {
    while (emitted_ < tokens.size()) {
      const auto count = emitted_ ? configuration_.following_tokens : configuration_.first_tokens;
      if (!final && tokens.size() < emitted_ + count + configuration_.lookahead_tokens) return;
      Decode(tokens, std::min(tokens.size(), emitted_ + count));
    }
  }
};
