// The native inference result must be owned PCM, independent of file codecs.
#include <stdexcept>
#include <vector>
#include "GPTSoVITS/AudioTools.h"

int main() {
  std::vector<float> input{0.25f, -0.5f, 0.125f};
  auto audio = GPTSoVITS::AudioTools::FromByte(input, 32000);
  input[0] = 1.0f;
  const auto header = audio->GetHeader();
  if (header.SampleRate != 32000 || header.Channels != 1 || header.Frames != 3 ||
      audio->ReadSamples() != std::vector<float>{0.25f, -0.5f, 0.125f})
    throw std::runtime_error("PCM ownership or metadata changed");
  auto empty = GPTSoVITS::AudioTools::FromByte({}, 32000);
  if (empty->GetHeader().Frames != 0 || !empty->ReadSamples().empty())
    throw std::runtime_error("An empty PCM result must remain empty");
  try {
    GPTSoVITS::AudioTools::FromByte({}, 0);
  } catch (const std::invalid_argument&) {
    return 0;
  }
  throw std::runtime_error("Invalid PCM sample rate accepted");
}
