// Isolated CPU inference. The protocol carries text and PCM only in memory.
#include <windows.h>
#include <fcntl.h>
#include <io.h>
#include <onnxruntime_cxx_api.h>
#include <cmath>
#include <cstdio>
#include <filesystem>
#include <iostream>
#include <stdexcept>
#include "GPTSoVITS/InferencePipeline.h"
#include "GPTSoVITS/GPTSoVITSCpp.h"
#include "nlohmann/json.hpp"

class MamboWorker {
  using Json = nlohmann::json;
  static constexpr uint32_t kProtocol = 2;
  static constexpr uint32_t kMaxTextBytes = 480;
  static constexpr uint32_t kSampleRate = 32000;
  static constexpr uint32_t kMaxFrames = 55 * kSampleRate;

  static void Write(const Json& metadata, const std::vector<float>* pcm = nullptr) {
    const auto encoded = metadata.dump();
    const uint32_t size = static_cast<uint32_t>(encoded.size());
    if (std::fwrite(&size, sizeof(size), 1, stdout) != 1 ||
        std::fwrite(encoded.data(), 1, size, stdout) != size ||
        (pcm && std::fwrite(pcm->data(), sizeof(float), pcm->size(), stdout) != pcm->size()) ||
        std::fflush(stdout) != 0)
      throw std::runtime_error("Speech response pipe closed");
  }

  static std::string ReadText() {
    uint32_t size = 0;
    if (std::fread(&size, sizeof(size), 1, stdin) != 1 || size == 0) return {};
    if (size > kMaxTextBytes) throw std::invalid_argument("Speech text exceeds segment limit");
    std::string text(size, '\0');
    if (std::fread(text.data(), 1, size, stdin) != size)
      throw std::runtime_error("Truncated speech request");
    // JSON validates UTF-8 without treating the text as executable input.
    Json(text).dump();
    if (text.find('\0') != std::string::npos) throw std::invalid_argument("Invalid speech text");
    return text;
  }

  static std::string RuntimePath() {
    wchar_t path[32768];
    const auto length = GetModuleFileNameW(GetModuleHandleW(L"onnxruntime.dll"), path, 32768);
    if (!length || length >= 32768) throw std::runtime_error("Cannot identify ONNX Runtime");
    const auto utf8 = std::filesystem::path(path).u8string();
    return std::string(utf8.begin(), utf8.end());
  }

  static std::wstring Environment(const wchar_t* name) {
    wchar_t value[32768];
    const auto length = GetEnvironmentVariableW(name, value, 32768);
    if (!length || length >= 32768) throw std::runtime_error("Missing native runtime location");
    return value;
  }

  static void InitializeRuntime() {
    if (!SetDefaultDllDirectories(LOAD_LIBRARY_SEARCH_SYSTEM32 | LOAD_LIBRARY_SEARCH_USER_DIRS))
      throw std::runtime_error("Cannot restrict native library lookup");
    const auto runtime = Environment(L"REDLOTUS_MAMBO_ORT");
    auto module = LoadLibraryExW(runtime.c_str(), nullptr,
        LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR | LOAD_LIBRARY_SEARCH_SYSTEM32);
    if (!module) throw std::runtime_error("Cannot load the application's ONNX Runtime");
    const auto get_base = reinterpret_cast<const OrtApiBase*(ORT_API_CALL*)()>(GetProcAddress(module, "OrtGetApiBase"));
    if (!get_base || !get_base()->GetApi(ORT_API_VERSION))
      throw std::runtime_error("Incompatible ONNX Runtime API");
    Ort::InitApi(get_base()->GetApi(ORT_API_VERSION));
    if (!SetCurrentDirectoryW(Environment(L"REDLOTUS_MAMBO_ROOT").c_str()))
      throw std::runtime_error("Cannot open the model package directory");
  }

 public:
  static void Error(const std::exception& error) {
    Write({{"status", "error"}, {"message", std::string(error.what()).substr(0, 4096)}});
  }

  void Run(int threads) {
    if (threads < 1 || threads > 32) throw std::invalid_argument("Invalid inference thread count");
    InitializeRuntime();
    GPTSoVITS::SetGlobalResourcesPath("frontend");
    auto config = GPTSoVITS::PipelineConfig::Edge(".");
    config.resources_path = "frontend";
    config.thread_num = threads;
    config.verbose = false;
    config.backend = GPTSoVITS::Model::BackendType::kONNX;
    GPTSoVITS::InferencePipeline pipeline(config);
    if (!pipeline.ImportSpeaker("mambo.gsppkg", "voice"))
      throw std::runtime_error("Dedicated voice feature import failed");
    // Release unused parser/load heap caches, not live weights or working-set pages.
    HEAP_OPTIMIZE_RESOURCES_INFORMATION heap_info{HEAP_OPTIMIZE_RESOURCES_CURRENT_VERSION, 0};
    HeapSetInformation(nullptr, HeapOptimizeResources, &heap_info, sizeof(heap_info));
    Write({{"status", "ready"}, {"protocol", kProtocol}, {"sample_rate", kSampleRate},
           {"runtime", RuntimePath()}, {"runtime_version", OrtGetApiBase()->GetVersionString()}});
    const GPTSoVITS::Model::SampleConfig sampling{5, 1.0f, 1.0f};
    for (;;) {
      const auto text = ReadText();
      if (text.empty()) break;
      try {
        std::size_t frames = 0;
        auto audio = pipeline.Infer("voice", text, "auto", sampling, 0.5f, 1.0f, nullptr, {},
            [&](const std::vector<float>& samples) {
              if (samples.empty() || samples.size() > 2 * kSampleRate || frames + samples.size() > kMaxFrames)
                throw std::runtime_error("Speech chunk or duration exceeds its limit");
              for (const auto sample : samples)
                if (!std::isfinite(sample)) throw std::runtime_error("Non-finite speech sample");
              Write({{"status", "data"}, {"size", samples.size() * sizeof(float)}}, &samples);
              frames += samples.size();
            });
        if (!audio || !frames) throw std::runtime_error("No speech samples generated");
        Write({{"status", "done"}, {"size", frames * sizeof(float)}});
      } catch (const std::exception& error) {
        Error(error);
      }
    }
  }
};

int main(int argc, char** argv) {
  _setmode(_fileno(stdin), _O_BINARY);
  _setmode(_fileno(stdout), _O_BINARY);
  std::cout.rdbuf(std::cerr.rdbuf());
  try {
    if (argc != 2) throw std::invalid_argument("Inference thread count required");
    MamboWorker().Run(std::stoi(argv[1]));
    return 0;
  } catch (const std::exception& error) {
    try { MamboWorker::Error(error); } catch (...) {}
    return 1;
  }
}
