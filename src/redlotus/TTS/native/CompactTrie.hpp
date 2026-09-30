#ifndef CPPJIEBA_TRIE_HPP
#define CPPJIEBA_TRIE_HPP

#include <vector>
#include <queue>
#include "limonp/StdExtension.hpp"
#include "Unicode.hpp"

namespace cppjieba {

using namespace std;

const size_t MAX_WORD_LENGTH = 512;

struct DictUnit {
  Unicode word;
  double weight;
  string tag;
}; // struct DictUnit

// for debugging
// inline ostream & operator << (ostream& os, const DictUnit& unit) {
//   string s;
//   s << unit.word;
//   return os << StringFormat("%s %s %.3lf", s.c_str(), unit.tag.c_str(), unit.weight);
// }

struct Dag {
  RuneStr runestr;
  // [offset, nexts.first]
  limonp::LocalVector<pair<size_t, const DictUnit*> > nexts;
  const DictUnit * pInfo;
  double weight;
  size_t nextPos; // TODO
  Dag():runestr(), pInfo(NULL), weight(0.0), nextPos(0) {
  }
}; // struct Dag

typedef Rune TrieKey;

// Compact dictionary index. Dictionary ownership remains in DictTrie.
class Trie {
  std::vector<const DictUnit*> entries_;

  static bool Less(const Unicode& left, const Unicode& right) {
    return std::lexicographical_compare(left.begin(), left.end(), right.begin(), right.end());
  }

  static bool Equal(const Unicode& left, const Unicode& right) {
    return left.size() == right.size() && std::equal(left.begin(), left.end(), right.begin());
  }

  static int Compare(const Unicode& word, RuneStrArray::const_iterator begin,
                     RuneStrArray::const_iterator end) {
    auto current = word.begin();
    while (current != word.end() && begin != end) {
      if (*current != begin->rune) return *current < begin->rune ? -1 : 1;
      ++current;
      ++begin;
    }
    if (current != word.end()) return 1;
    return begin == end ? 0 : -1;
  }

 public:
  Trie(const vector<Unicode>& keys, const vector<const DictUnit*>& values)
      : entries_(values) {
    assert(keys.size() == values.size());
    for (size_t i = 0; i < keys.size(); ++i) assert(Equal(keys[i], values[i]->word));
    std::stable_sort(entries_.begin(), entries_.end(),
        [](const DictUnit* left, const DictUnit* right) { return Less(left->word, right->word); });
    size_t count = 0;
    for (const auto* value : entries_) {
      if (count && Equal(entries_[count - 1]->word, value->word))
        entries_[count - 1] = value;
      else entries_[count++] = value;
    }
    entries_.resize(count);
    entries_.shrink_to_fit();
  }

  const DictUnit* Find(RuneStrArray::const_iterator begin, RuneStrArray::const_iterator end) const {
    if (begin == end) return nullptr;
    const auto found = std::lower_bound(entries_.begin(), entries_.end(), begin,
        [end](const DictUnit* value, RuneStrArray::const_iterator key) {
          return Compare(value->word, key, end) < 0;
        });
    return found != entries_.end() && Compare((*found)->word, begin, end) == 0 ? *found : nullptr;
  }

  void Find(RuneStrArray::const_iterator begin, RuneStrArray::const_iterator end,
            vector<Dag>& result, size_t max_word_len = MAX_WORD_LENGTH) const {
    result.resize(end - begin);
    for (size_t i = 0; i < size_t(end - begin); ++i) {
      result[i].runestr = *(begin + i);
      auto first = entries_.begin(), last = entries_.end();
      for (size_t offset = 0; i + offset < size_t(end - begin) &&
           (offset == 0 || offset < max_word_len); ++offset) {
        const auto rune = (begin + i + offset)->rune;
        first = std::lower_bound(first, last, rune, [offset](const DictUnit* value, Rune letter) {
          return value->word.size() <= offset || value->word[offset] < letter;
        });
        last = std::upper_bound(first, last, rune, [offset](Rune letter, const DictUnit* value) {
          return value->word.size() > offset && letter < value->word[offset];
        });
        const DictUnit* terminal = first != last && (*first)->word.size() == offset + 1 ? *first : nullptr;
        if (offset == 0 || terminal)
          result[i].nexts.push_back(std::make_pair(i + offset, terminal));
        if (first == last) break;
      }
    }
  }

  void InsertNode(const Unicode& key, const DictUnit* value) {
    if (key.empty()) return;
    assert(value && Equal(key, value->word));
    const auto found = std::lower_bound(entries_.begin(), entries_.end(), key,
        [](const DictUnit* item, const Unicode& word) { return Less(item->word, word); });
    if (found != entries_.end() && Equal((*found)->word, key)) *found = value;
    else entries_.insert(found, value);
  }

  void DeleteNode(const Unicode& key, const DictUnit*) {
    const auto found = std::lower_bound(entries_.begin(), entries_.end(), key,
        [](const DictUnit* item, const Unicode& word) { return Less(item->word, word); });
    if (found != entries_.end() && Equal((*found)->word, key)) entries_.erase(found);
  }
};
} // namespace cppjieba

#endif // CPPJIEBA_TRIE_HPP
