#pragma once
#include <algorithm>
#include <cstdint>
#include <istream>
#include <limits>
#include <optional>
#include <stdexcept>
#include <string>
#include <unordered_map>
#include <vector>
#include <nlohmann/json.hpp>

namespace RedLotus {
// Immutable pronunciation data. Phone strings are stored once; entries retain
// every pronunciation, including alternatives, in their original order.
class PronunciationDictionary {
  struct Range { std::size_t offset; std::size_t count; };
  struct Entry { std::string word; Range pronunciations; };
  std::vector<Entry> entries_;
  std::vector<Range> pronunciations_;
  std::vector<std::uint16_t> sequence_;
  std::vector<std::string> phones_;

  class Reader final : public nlohmann::json_sax<nlohmann::json> {
    PronunciationDictionary& dictionary_;
    std::unordered_map<std::string, std::uint16_t> phone_ids_;
    std::string word_;
    std::size_t pronunciation_start_ = 0;
    std::size_t phone_start_ = 0;
    int depth_ = 0;
    bool opened_ = false;
    bool closed_ = false;
    bool has_word_ = false;
  public:
    explicit Reader(PronunciationDictionary& dictionary) : dictionary_(dictionary) {}
    bool start_object(std::size_t) override {
      if (opened_) return false;
      opened_ = true;
      return true;
    }
    bool end_object() override {
      if (!opened_ || closed_ || depth_ || has_word_) return false;
      closed_ = true;
      return true;
    }
    bool key(string_t& word) override {
      if (!opened_ || closed_ || depth_ || has_word_) return false;
      word_ = std::move(word);
      has_word_ = true;
      pronunciation_start_ = dictionary_.pronunciations_.size();
      return true;
    }
    bool start_array(std::size_t) override {
      if (!has_word_ || depth_ >= 2) return false;
      if (++depth_ == 2) phone_start_ = dictionary_.sequence_.size();
      return true;
    }
    bool end_array() override {
      if (depth_ == 2) {
        dictionary_.pronunciations_.push_back(
            {phone_start_, dictionary_.sequence_.size() - phone_start_});
      } else if (depth_ == 1) {
        dictionary_.entries_.push_back({std::move(word_),
            {pronunciation_start_, dictionary_.pronunciations_.size() - pronunciation_start_}});
        has_word_ = false;
      } else return false;
      --depth_;
      return true;
    }
    bool string(string_t& phone) override {
      if (depth_ != 2) return false;
      auto found = phone_ids_.find(phone);
      if (found == phone_ids_.end()) {
        if (dictionary_.phones_.size() > std::numeric_limits<std::uint16_t>::max()) return false;
        const auto id = static_cast<std::uint16_t>(dictionary_.phones_.size());
        dictionary_.phones_.push_back(phone);
        found = phone_ids_.emplace(std::move(phone), id).first;
      }
      dictionary_.sequence_.push_back(found->second);
      return true;
    }
    bool null() override { return false; }
    bool boolean(bool) override { return false; }
    bool number_integer(number_integer_t) override { return false; }
    bool number_unsigned(number_unsigned_t) override { return false; }
    bool number_float(number_float_t, const string_t&) override { return false; }
    bool binary(binary_t&) override { return false; }
    bool parse_error(std::size_t, const std::string&, const nlohmann::detail::exception&) override {
      return false;
    }
  };

public:
  explicit PronunciationDictionary(std::istream& input) {
    {
      Reader reader(*this);
      if (!nlohmann::json::sax_parse(input, &reader))
        throw std::runtime_error("Invalid pronunciation dictionary");
    }
    std::stable_sort(entries_.begin(), entries_.end(),
        [](const Entry& left, const Entry& right) { return left.word < right.word; });
    // JSON objects retain the last duplicate key. Keep the same semantics.
    std::size_t kept = 0;
    for (std::size_t begin = 0; begin < entries_.size();) {
      std::size_t end = begin + 1;
      while (end < entries_.size() && entries_[end].word == entries_[begin].word) ++end;
      if (kept != end - 1) entries_[kept] = std::move(entries_[end - 1]);
      ++kept;
      begin = end;
    }
    entries_.resize(kept);
    entries_.shrink_to_fit();
    pronunciations_.shrink_to_fit();
    sequence_.shrink_to_fit();
    phones_.shrink_to_fit();
  }
  bool empty() const { return entries_.empty(); }
  std::size_t size() const { return entries_.size(); }
  std::optional<std::vector<std::string>> Lookup(const std::string& word,
                                                std::size_t variant) const {
    const auto entry = std::lower_bound(entries_.begin(), entries_.end(), word,
        [](const Entry& candidate, const std::string& key) { return candidate.word < key; });
    if (entry == entries_.end() || entry->word != word || variant >= entry->pronunciations.count)
      return {};
    const auto range = pronunciations_[entry->pronunciations.offset + variant];
    std::vector<std::string> result;
    result.reserve(range.count);
    for (std::size_t i = 0; i < range.count; ++i)
      result.push_back(phones_[sequence_[range.offset + i]]);
    return result;
  }
};
}
